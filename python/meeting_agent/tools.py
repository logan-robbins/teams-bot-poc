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
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal
from xml.etree import ElementTree as ET

import httpx
from agents import RunContextWrapper, function_tool
from pydantic import BaseModel, Field

from .models import MeetingEvent
from .session import InterviewSessionManager

__all__ = [
    "AlfredAgentContext",
    "SendResult",
    "TranscriptResult",
    "build_alfred_tools",
    "fetch_meeting_transcript_impl",
    "send_to_meeting_chat_impl",
]


# Public-read Azure Blob container that the C# bot mirrors every Alfred
# event + post-meeting transcript into. Override at deploy time with
# BLOB_ARCHIVE_URL if the storage account changes.
_BLOB_ARCHIVE_URL_DEFAULT = "https://stalfreddisney.blob.core.windows.net/alfred-events"

_BLOB_PATH_UNSAFE = re.compile(r"[^a-zA-Z0-9\-_.]")


def _parse_iso_utc(timestamp: str | None) -> datetime | None:
    if not timestamp:
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _is_directly_addressed(text: str | None, mention_strings: list[str]) -> bool:
    if not text or not mention_strings:
        return False
    haystack = text.lower()
    return any(token.lower() in haystack for token in mention_strings if token)


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
    # E4: explicit proactivity policy carried per-run from the product spec.
    cooldown_seconds: float = Field(
        default=0.0,
        description="Min seconds between consecutive Alfred posts (E4). Bypassed when directly addressed.",
    )
    directly_addressed_bypass: bool = Field(
        default=True,
        description="If True, a human directly addressing Alfred bypasses the cooldown.",
    )
    mention_strings: list[str] = Field(
        default_factory=lambda: ["alfred"],
        description="Substrings that count as 'directly addressed' for cooldown bypass.",
    )
    trigger_text: str | None = Field(
        default=None,
        description="Text of the event that triggered this analysis tick (used to detect direct address).",
    )

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


class TranscriptResult(BaseModel):
    """What the fetch-transcript tool returns to the LLM.

    On success, ``transcript`` is Microsoft's official Record-and-Transcribe
    output rendered as speaker-per-line plaintext (the same content the
    C# bot wrote to ``_official-transcript.txt`` in the blob archive).
    On failure, ``reason`` is a short error code the LLM can surface.
    """

    ok: bool
    transcript: str | None = None
    meeting_thread_id_sanitized: str | None = None
    blob_url: str | None = None
    last_modified: str | None = None
    bytes: int | None = None
    reason: str | None = None


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

    # E4: cooldown enforcement. Skip when the trigger event was a direct
    # address ("Alfred, …" / "@alfred") and `directly_addressed_bypass` is on.
    bypass_cooldown = (
        context.directly_addressed_bypass
        and _is_directly_addressed(context.trigger_text, context.mention_strings)
    )
    cooldown = max(float(context.cooldown_seconds or 0.0), 0.0)
    if cooldown > 0 and not bypass_cooldown and session.outbound_chat_intents:
        last_intent = session.outbound_chat_intents[-1]
        last_ts = _parse_iso_utc(last_intent.timestamp_utc)
        now_ts = datetime.now(timezone.utc)
        if last_ts is not None:
            gap = (now_ts - last_ts).total_seconds()
            if gap < cooldown:
                logger.info(
                    "send_to_meeting_chat blocked by cooldown: gap=%.1fs < %.1fs (session=%s)",
                    gap,
                    cooldown,
                    session.session_id,
                )
                result = SendResult(ok=False, reason="cooldown_active")
                context.record(
                    "send_to_meeting_chat",
                    arguments,
                    result.model_dump(),
                    ok=False,
                    error="cooldown_active",
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


def _archive_url() -> str:
    raw = (os.environ.get("BLOB_ARCHIVE_URL") or _BLOB_ARCHIVE_URL_DEFAULT).strip()
    return raw.rstrip("/")


def _sanitize_blob_segment(raw: str) -> str:
    """Mirror of ``BlobEventArchive.SanitizePathSegment`` in the C# bot.

    The bot replaces every non-alphanumeric path char with ``_`` when it
    writes blobs. This Python helper must produce the same string from a
    given Teams id so the agent can build the right list/get URLs.
    """
    return _BLOB_PATH_UNSAFE.sub("_", raw)


def _resolve_channel_id(
    session_channel_id: str | None,
    session_channel_thread_id: str | None,
    session_chat_thread_id: str | None,
    explicit: str | None,
) -> str | None:
    """Pick the most specific channel id available for the transcript lookup.

    Order: explicit arg > ``channel_thread_id`` (channel meetings stamp
    the parent here) > ``channel_id`` (raw channel install) > the
    session's own ``chat_thread_id`` (which, for a channel install, IS
    the channel thread id). Strips a ``;messageid=...`` suffix when
    present so the prefix matches the blob's sanitized channel folder.
    """
    candidate = (
        (explicit or "").strip()
        or (session_channel_thread_id or "").strip()
        or (session_channel_id or "").strip()
        or (session_chat_thread_id or "").strip()
    )
    if not candidate:
        return None
    semi = candidate.find(";")
    if semi >= 0:
        candidate = candidate[:semi]
    return candidate


async def fetch_meeting_transcript_impl(
    context: AlfredAgentContext,
    channel_id: str | None = None,
) -> TranscriptResult:
    """Fetch the most recent meeting's official transcript from the blob archive.

    The C# bot writes one ``_official-transcript.txt`` per meeting at
    ``meetings/{sanitizedChatThreadId}/_official-transcript.txt`` in the
    public-read alfred-events container. For channel meetings, the
    sanitized chat thread id starts with the sanitized channel id (with
    ``_messageid_xxx`` appended), so a prefix list scoped to the current
    channel finds every meeting that happened in that channel.

    The newest such transcript wins. The transcript body is returned to
    the LLM so it can read it and answer the user's question — no
    summarization happens inside this tool.
    """
    session = context.session_manager.session
    resolved_channel = _resolve_channel_id(
        session_channel_id=getattr(session, "channel_id", None) if session is not None else None,
        session_channel_thread_id=getattr(session, "channel_thread_id", None) if session is not None else None,
        session_chat_thread_id=getattr(session, "chat_thread_id", None) if session is not None else None,
        explicit=channel_id,
    )
    if not resolved_channel:
        return TranscriptResult(ok=False, reason="no_channel_context")

    archive = _archive_url()
    list_prefix = f"meetings/{_sanitize_blob_segment(resolved_channel)}"
    list_url = (
        f"{archive}?restype=container&comp=list&prefix={list_prefix}&maxresults=500"
    )

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            list_response = await client.get(list_url)
            list_response.raise_for_status()
            xml_text = list_response.text
    except Exception as e:
        logger.warning("fetch_meeting_transcript list failed: %s", e)
        return TranscriptResult(ok=False, reason=f"list_failed: {e!r}")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.warning("fetch_meeting_transcript list parse failed: %s", e)
        return TranscriptResult(ok=False, reason=f"parse_failed: {e!r}")

    candidates: list[tuple[str, str]] = []
    for blob in root.iter("Blob"):
        name = blob.findtext("Name") or ""
        if not name.endswith("/_official-transcript.txt"):
            continue
        last_modified = blob.findtext("Properties/Last-Modified") or ""
        candidates.append((last_modified, name))

    if not candidates:
        return TranscriptResult(ok=False, reason="no_transcript_found")

    candidates.sort(reverse=True)
    last_modified, blob_name = candidates[0]
    blob_url = f"{archive}/{blob_name}"

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            get_response = await client.get(blob_url)
            get_response.raise_for_status()
            transcript_text = get_response.text
    except Exception as e:
        logger.warning("fetch_meeting_transcript download failed: %s", e)
        return TranscriptResult(ok=False, reason=f"download_failed: {e!r}")

    # blob_name = meetings/{sanitized}/_official-transcript.txt
    parts = blob_name.split("/")
    meeting_thread_id_sanitized = parts[1] if len(parts) >= 3 else None

    context.record(
        "fetch_meeting_transcript",
        {"channel_id": resolved_channel},
        {
            "blob_url": blob_url,
            "last_modified": last_modified,
            "bytes": len(transcript_text),
            "meeting_thread_id_sanitized": meeting_thread_id_sanitized,
        },
        ok=True,
    )

    return TranscriptResult(
        ok=True,
        transcript=transcript_text,
        meeting_thread_id_sanitized=meeting_thread_id_sanitized,
        blob_url=blob_url,
        last_modified=last_modified,
        bytes=len(transcript_text),
    )


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

    @function_tool
    async def fetch_meeting_transcript(
        ctx: RunContextWrapper[AlfredAgentContext],
        channel_id: str | None = None,
    ) -> TranscriptResult:
        """Fetch the most recent meeting's full official transcript so you can READ it and answer questions about what was said.

        Call this any time a user explicitly asks you to look up, recap,
        summarize, or answer a question about a past meeting in this
        channel ("alfred, get the transcript", "what did the team
        decide?", "summarize the last meeting", etc.). The returned
        ``transcript`` is Microsoft's official Record-and-Transcribe
        output as speaker-per-line plaintext — read it, then answer the
        user via ``send_to_meeting_chat`` (concise; quote sparingly).

        Do NOT call this on a normal silence-default tick. Only call
        when a user is directly addressing you with a question or
        recap request.

        Args:
            channel_id: Optional Teams channel id (``19:...@thread.tacv2``).
                If omitted, uses the current channel context resolved
                from this session.
        """
        return await fetch_meeting_transcript_impl(
            context=ctx.context,
            channel_id=channel_id,
        )

    return (send_to_meeting_chat, fetch_meeting_transcript)
