"""Webhook output route."""

from __future__ import annotations

from typing import Any

import httpx

from legionmeet_platform.routes.base import RouteDispatchResult


class WebhookRoute:
    """Send analysis/checklist payloads to external HTTP endpoints."""

    route_type = "webhook"

    def __init__(
        self,
        route_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.route_id = route_id
        self.url = url
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds

    async def dispatch(self, payload: dict[str, Any]) -> RouteDispatchResult:
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.url,
                    headers=self.headers,
                    json=payload,
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
