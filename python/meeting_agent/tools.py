"""
Agent tools.

The Alfred agent now has real hands. The ``send_to_meeting_chat`` tool is
the one and only path from agent-decision to Teams chat: it inspects the
active session, respects the mute flag, posts to the C# bot's
``/api/send-chat`` endpoint (which then calls
``CloudAdapter.ContinueConversationAsync``), records an outbound intent for
echo suppression, and appends an Alfred-sourced ``MeetingEvent`` into the
ledger so the agent sees its own utterance on the next tick.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from agents import RunContextWrapper, function_tool
from pydantic import BaseModel, Field

from .models import MeetingEvent
from .session import InterviewSessionManager

__all__ = [
    "AlfredAgentContext",
    "SendResult",
    "build_alfred_tools",
    "send_to_meeting_chat_impl",
]


logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class AlfredAgentContext(BaseModel):
    """
    Per-run context threaded through the agent.

    Holds the bits a tool needs in order to actually *do* something: which
    session to mutate, where the C# bot's send endpoint lives, and the
    session-scoped records list so tool invocations can be audited on the
    resulting ``AnalysisItem``.
    """

    model_config = {"arbitrary_types_allowed": True}

    session_manager: InterviewSessionManager
    send_chat_url: str | None = None
    tool_records: list = Field(default_factory=list)

    @property
    def conversation_reference_id(self) -> str | None:
        if self.session_manager.session is None:
            return None
        return self.session_manager.session.conversation_reference_id

    def record(
        self,
        tool_name: str,
        arguments: dict,
        result: dict,
        ok: bool,
        error: str | None = None,
    ) -> None:
        from .models import ToolCallRecord

        self.tool_records.append(
            ToolCallRecord(
                id=f"tc_{uuid.uuid4().hex[:10]}",
                tool_name=tool_name,
                arguments=arguments,
                result=result,
                ok=ok,
                error=error,
            )
        )


class SendResult(BaseModel):
    """What the send tool returns to the LLM."""

    ok: bool
    reason: str | None = None
    posted_at: str | None = None
    message_id: str | None = None


def _resolve_send_chat_url(explicit: str | None) -> str | None:
    if explicit and explicit.strip():
        return explicit.strip()
    env_url = (os.environ.get("BOT_SEND_CHAT_URL") or "").strip()
    return env_url or None


async def send_to_meeting_chat_impl(
    context: AlfredAgentContext,
    text: str,
    kind: Literal["statement", "question"] = "statement",
    reply_to_message_id: str | None = None,
) -> SendResult:
    """Pure async implementation of the send tool — no SDK wrapping.

    Directly testable; wired into the Agents SDK by ``build_alfred_tools``.
    """
    arguments = {
        "text": text,
        "kind": kind,
        "reply_to_message_id": reply_to_message_id,
    }

    body = (text or "").strip()
    if not body:
        result = SendResult(ok=False, reason="empty_text")
        context.record(
            "send_to_meeting_chat", arguments, result.model_dump(), ok=False, error="empty_text"
        )
        return result

    session = context.session_manager.session
    if session is None:
        result = SendResult(ok=False, reason="no_active_session")
        context.record(
            "send_to_meeting_chat",
            arguments,
            result.model_dump(),
            ok=False,
            error="no_active_session",
        )
        return result

    if session.alfred_muted:
        logger.info(
            "send_to_meeting_chat blocked: Alfred is muted for session %s",
            session.session_id,
        )
        result = SendResult(ok=False, reason="muted")
        context.record(
            "send_to_meeting_chat", arguments, result.model_dump(), ok=False, error="muted"
        )
        return result

    if kind == "question" and not body.rstrip().endswith("?"):
        body = f"{body.rstrip()}?"

    conversation_reference_id = session.conversation_reference_id
    if not conversation_reference_id:
        result = SendResult(ok=False, reason="no_conversation_reference")
        context.record(
            "send_to_meeting_chat",
            arguments,
            result.model_dump(),
            ok=False,
            error="no_conversation_reference",
        )
        return result

    # Record intent BEFORE posting so a late-arriving echo is suppressed.
    context.session_manager.record_outbound_chat_intent(body, reply_to_message_id)

    url = _resolve_send_chat_url(context.send_chat_url)
    posted_at = _utc_now()
    message_id = f"alfred_{uuid.uuid4().hex[:10]}"

    if url:
        payload = {
            "conversation_reference_id": conversation_reference_id,
            "action": "SEND" if kind == "statement" else "ASK",
            "text": body,
            "reply_to_message_id": reply_to_message_id,
            "rationale": None,
            "session_id": session.session_id,
        }
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.post(url, json=payload)
            if response.status_code >= 400:
                err = f"HTTP {response.status_code}: {response.text[:160]}"
                logger.warning("send_to_meeting_chat failed: %s", err)
                result = SendResult(ok=False, reason=err)
                context.record(
                    "send_to_meeting_chat",
                    arguments,
                    result.model_dump(),
                    ok=False,
                    error=err,
                )
                return result
        except Exception as exc:  # noqa: BLE001 - tool must never raise to the LLM
            err = f"transport: {exc!s}"
            logger.warning("send_to_meeting_chat transport error: %s", err)
            result = SendResult(ok=False, reason=err)
            context.record(
                "send_to_meeting_chat",
                arguments,
                result.model_dump(),
                ok=False,
                error=err,
            )
            return result
    else:
        logger.info(
            "send_to_meeting_chat: no send_chat_url configured — dry-run (session=%s)",
            session.session_id,
        )

    # Append Alfred's own utterance into the ledger so the next tick sees it.
    try:
        context.session_manager._append_meeting_event(
            MeetingEvent(
                event_id=f"alfred:{message_id}",
                kind="chat",
                timestamp_utc=posted_at,
                source="alfred",
                text=body,
                role="bot",
                display_name="Alfred",
                message_id=message_id,
                reply_to_message_id=reply_to_message_id,
                from_bot=True,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to append Alfred utterance to ledger: %s", exc)

    result = SendResult(ok=True, posted_at=posted_at, message_id=message_id)
    context.record("send_to_meeting_chat", arguments, result.model_dump(), ok=True)
    return result


def build_alfred_tools() -> tuple[Any, ...]:
    """Build the tools tuple wired against ``AlfredAgentContext`` for the SDK."""

    @function_tool
    async def send_to_meeting_chat(
        ctx: RunContextWrapper[AlfredAgentContext],
        text: str,
        kind: Literal["statement", "question"] = "statement",
        reply_to_message_id: str | None = None,
    ) -> SendResult:
        """Post a message into the Teams meeting chat the agent is currently in.

        Use sparingly — silence is the default. Only call when you have
        concrete value to add (a decision, a missing link, a clarifying
        question blocking progress). Never call to recap or narrate.

        Args:
            text: Body to post in the meeting chat. One or two sentences.
            kind: "statement" for a normal remark, "question" for a clarifying question.
                  Question-kind posts are auto-suffixed with '?' if missing.
            reply_to_message_id: Optional message id to thread under.
        """
        return await send_to_meeting_chat_impl(
            context=ctx.context,
            text=text,
            kind=kind,
            reply_to_message_id=reply_to_message_id,
        )

    return (send_to_meeting_chat,)
