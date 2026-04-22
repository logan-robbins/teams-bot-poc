"""
Teams chat send-intent output route.

Fires only on analysis payloads where `analysis_item.alfred_action.action`
is SEND or ASK. Posts the send-intent envelope to the C# bot's
/api/send-chat endpoint, which then calls adapter.ContinueConversationAsync
against the captured ConversationReference.

Rate-limited client-side via an asyncio semaphore sized from the route's
max_rps setting (default 4 rps).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from legionmeet_platform.routes.base import RouteDispatchResult


class TeamsChatRoute:
    """Post Alfred SEND/ASK intents to the bot's proactive-messaging endpoint."""

    route_type = "teams_chat"

    def __init__(
        self,
        route_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 5.0,
        max_rps: float = 4.0,
    ) -> None:
        self.route_id = route_id
        self.url = url
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds
        self._min_interval = 1.0 / max_rps if max_rps > 0 else 0.0
        self._lock = asyncio.Lock()
        self._last_dispatch_monotonic: float = 0.0

    @staticmethod
    def _extract_send_intent(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Return a send-intent body when the payload has a SEND/ASK action."""
        if payload.get("event_type") != "analysis":
            return None

        item = payload.get("analysis_item") or {}
        action = (item.get("alfred_action") or {}) if isinstance(item, dict) else {}
        action_kind = str(action.get("action") or "").upper()
        if action_kind not in ("SEND", "ASK"):
            return None

        chat_text = action.get("chat_text") or ""
        if not str(chat_text).strip():
            return None

        conversation_ref_id = None
        session_context = payload.get("session_context") or {}
        if isinstance(session_context, dict):
            conversation_ref_id = session_context.get("conversation_reference_id")

        return {
            "conversation_reference_id": conversation_ref_id,
            "action": action_kind,
            "text": chat_text,
            "mentions": action.get("mentions") or [],
            "reply_to_message_id": action.get("reply_to_message_id"),
            "rationale": action.get("rationale"),
            "session_id": payload.get("session_id"),
            "product_id": payload.get("product_id"),
            "instance_id": payload.get("instance_id"),
        }

    async def _throttle(self) -> None:
        if self._min_interval <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            wait = self._min_interval - (now - self._last_dispatch_monotonic)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_dispatch_monotonic = time.monotonic()

    async def dispatch(self, payload: dict[str, Any]) -> RouteDispatchResult:
        intent = self._extract_send_intent(payload)
        if intent is None:
            # Non-send payloads are a no-op for this route (success).
            return RouteDispatchResult(
                route_id=self.route_id,
                route_type=self.route_type,
                ok=True,
                detail="skipped (not a SEND/ASK intent)",
            )

        if not intent.get("conversation_reference_id"):
            return RouteDispatchResult(
                route_id=self.route_id,
                route_type=self.route_type,
                ok=False,
                detail="no conversation_reference_id on session (bot hasn't seen chat yet)",
            )

        await self._throttle()

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.url,
                    headers=self.headers,
                    json=intent,
                )
            if response.status_code >= 400:
                return RouteDispatchResult(
                    route_id=self.route_id,
                    route_type=self.route_type,
                    ok=False,
                    detail=f"HTTP {response.status_code}: {response.text[:160]}",
                )
            return RouteDispatchResult(
                route_id=self.route_id,
                route_type=self.route_type,
                ok=True,
            )
        except Exception as exc:  # noqa: BLE001 - dispatch must never throw
            return RouteDispatchResult(
                route_id=self.route_id,
                route_type=self.route_type,
                ok=False,
                detail=str(exc),
            )
