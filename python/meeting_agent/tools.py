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
    "MeetingListEntry",
    "MeetingListResult",
    "MeetingResolveResult",
    "RequestBackfillResult",
    "SendResult",
    "TranscriptResult",
    "build_alfred_tools",
    "fetch_meeting_transcript_impl",
    "list_meetings_impl",
    "request_transcript_backfill_impl",
    "resolve_meeting_by_name_impl",
    "send_to_meeting_chat_impl",
]


# Public-read Azure Blob container that the C# bot mirrors every Alfred
# event + post-meeting transcript into. Override at deploy time with
# BLOB_ARCHIVE_URL if the storage account changes.
_BLOB_ARCHIVE_URL_DEFAULT = "https://stalfreddisney.blob.core.windows.net/alfred-events"

# Sink base URL used by the v2 query tools below. Falls back to the
# in-cluster URL the C# bot already targets so this works the same way
# Alfred reads its own ledger.
_SINK_URL_DEFAULT = "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io"

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
    C# bot wrote to ``meetings/{meeting_id}/transcripts/official.txt`` in
    the blob archive). On failure, ``reason`` is a short error code.
    """

    ok: bool
    transcript: str | None = None
    meeting_id: str | None = None
    subject: str | None = None
    blob_url: str | None = None
    last_modified: str | None = None
    bytes: int | None = None
    reason: str | None = None


class MeetingListEntry(BaseModel):
    """One meeting in a ``list_meetings`` / ``resolve_meeting_by_name`` result."""

    meeting_id: str
    subject: str | None = None
    organizer_display_name: str | None = None
    scheduled_start_utc: str | None = None
    scheduled_end_utc: str | None = None
    actual_start_utc: str | None = None
    actual_end_utc: str | None = None
    channel_team_id: str | None = None
    channel_team_display_name: str | None = None
    channel_id: str | None = None
    channel_display_name: str | None = None


class MeetingListResult(BaseModel):
    """What the ``list_meetings`` tool returns."""

    ok: bool
    count: int = 0
    meetings: list[MeetingListEntry] = Field(default_factory=list)
    reason: str | None = None


class MeetingResolveResult(BaseModel):
    """What the ``resolve_meeting_by_name`` tool returns."""

    ok: bool
    query: str = ""
    matches: list[MeetingListEntry] = Field(default_factory=list)
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


def _sink_url() -> str:
    raw = (os.environ.get("SINK_URL") or _SINK_URL_DEFAULT).strip()
    return raw.rstrip("/")


def _sanitize_blob_segment(raw: str) -> str:
    """Mirror of ``BlobEventArchive.SanitizePathSegment`` in the C# bot.

    The bot replaces every non-alphanumeric path char with ``_`` when it
    writes blobs. This Python helper must produce the same string from a
    given Teams id so the agent can build the right list/get URLs.
    """
    return _BLOB_PATH_UNSAFE.sub("_", raw)


def _meeting_entry_from_v2(row: dict[str, Any]) -> MeetingListEntry:
    channel_link = row.get("channel_link") or {}
    return MeetingListEntry(
        meeting_id=row.get("meeting_id") or "",
        subject=row.get("subject"),
        organizer_display_name=(row.get("organizer") or {}).get("display_name")
            if isinstance(row.get("organizer"), dict)
            else row.get("organizer_display_name"),
        scheduled_start_utc=row.get("scheduled_start_utc"),
        scheduled_end_utc=row.get("scheduled_end_utc"),
        actual_start_utc=row.get("actual_start_utc"),
        actual_end_utc=row.get("actual_end_utc"),
        channel_team_id=(channel_link or {}).get("team_id"),
        channel_team_display_name=(channel_link or {}).get("team_display_name"),
        channel_id=(channel_link or {}).get("channel_id"),
        channel_display_name=(channel_link or {}).get("channel_display_name"),
    )


async def list_meetings_impl(
    context: AlfredAgentContext,
    limit: int = 25,
) -> MeetingListResult:
    """List meetings the sink knows about via ``GET /v2/meetings``.

    Used when a user asks "what meetings do you have?" or as a discovery
    step before calling ``fetch_meeting_transcript``.
    """
    sink = _sink_url()
    url = f"{sink}/v2/meetings?limit={int(limit)}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_meetings failed: %s", exc)
        result = MeetingListResult(ok=False, reason=f"http_error: {exc!r}")
        context.record("list_meetings", {"limit": limit}, result.model_dump(), ok=False, error=str(exc))
        return result

    meetings = [_meeting_entry_from_v2(m) for m in (data.get("meetings") or [])]
    result = MeetingListResult(ok=True, count=len(meetings), meetings=meetings)
    context.record("list_meetings", {"limit": limit}, result.model_dump(), ok=True)
    return result


async def resolve_meeting_by_name_impl(
    context: AlfredAgentContext,
    subject: str,
    limit: int = 10,
) -> MeetingResolveResult:
    """Resolve a meeting subject substring → canonical ``meeting_id`` matches.

    Calls ``GET /v2/resolve?kind=meeting&subject=...``.
    """
    sink = _sink_url()
    query = (subject or "").strip()
    url = f"{sink}/v2/resolve?kind=meeting&subject={httpx.QueryParams({'q': query}).get('q')}&limit={int(limit)}"
    # httpx QueryParams above is just to URL-encode; rebuild cleanly:
    params = {"kind": "meeting", "subject": query, "limit": int(limit)}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{sink}/v2/resolve", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("resolve_meeting_by_name failed: %s", exc)
        result = MeetingResolveResult(ok=False, query=query, reason=f"http_error: {exc!r}")
        context.record(
            "resolve_meeting_by_name", {"subject": query}, result.model_dump(),
            ok=False, error=str(exc),
        )
        return result

    matches = [_meeting_entry_from_v2(m) for m in (data.get("matches") or [])]
    result = MeetingResolveResult(ok=True, query=query, matches=matches)
    context.record("resolve_meeting_by_name", {"subject": query}, result.model_dump(), ok=True)
    return result


class RequestBackfillResult(BaseModel):
    """What the request_transcript_backfill tool returns."""

    ok: bool
    meeting_id: str | None = None
    registered_key: str | None = None
    note: str | None = None
    reason: str | None = None


def _bot_base_from_send_chat(send_chat_url: str | None) -> str | None:
    """Derive the bot's base URL from the configured send-chat URL.

    BOT_SEND_CHAT_URL is conventionally ``<bot-base>/api/send-chat``;
    strip the ``/api/send-chat`` suffix to get the base. Returns None
    if we can't figure it out — caller should surface as a clean error.
    """
    if not send_chat_url:
        return None
    cleaned = send_chat_url.strip().rstrip("/")
    if cleaned.endswith("/api/send-chat"):
        return cleaned[: -len("/api/send-chat")]
    # Tolerate operators who supply just the base URL.
    if "/api/" not in cleaned:
        return cleaned
    return None


async def request_transcript_backfill_impl(
    context: AlfredAgentContext,
    meeting_id: str | None = None,
    organizer_oid: str | None = None,
) -> RequestBackfillResult:
    """Ask the bot to backfill an official Microsoft transcript.

    Wraps ``POST {BOT}/api/debug/fetch-transcript`` (see
    DebugController.ManualFetchTranscript). The bot then polls Graph's
    ``installedToOnlineMeetings/getAllTranscripts`` for ~30 minutes;
    when the transcript materializes the bot emits
    ``meeting.transcript.official`` and writes the plaintext to
    ``meetings/{meeting_id}/transcripts/official.txt``. After ~2-3
    minutes the agent can call ``fetch_meeting_transcript`` again to
    read it.

    Args:
        meeting_id: Canonical Graph onlineMeeting id. Defaults to the
            most recent meeting if omitted.
        organizer_oid: AAD object id of the meeting organizer. Required
            by the bot's transcript fetcher. If omitted, derived from
            the sink's V2Meeting record when available.
    """
    sink = _sink_url()
    arguments = {"meeting_id": meeting_id, "organizer_oid": organizer_oid}

    resolved_meeting_id = (meeting_id or "").strip() or None
    resolved_organizer_oid = (organizer_oid or "").strip() or None

    # Pull the most recent meeting if no meeting_id was given.
    if resolved_meeting_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{sink}/v2/meetings", params={"limit": 1})
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            result = RequestBackfillResult(ok=False, reason=f"list_failed: {exc!r}")
            context.record("request_transcript_backfill", arguments,
                           result.model_dump(), ok=False, error=str(exc))
            return result
        meetings = data.get("meetings") or []
        if not meetings:
            result = RequestBackfillResult(ok=False, reason="no_meetings_known")
            context.record("request_transcript_backfill", arguments,
                           result.model_dump(), ok=False, error="no_meetings_known")
            return result
        resolved_meeting_id = meetings[0].get("meeting_id")
        if not resolved_organizer_oid:
            org_obj = meetings[0].get("organizer") or {}
            if isinstance(org_obj, dict):
                resolved_organizer_oid = org_obj.get("aad_id")

    # If organizer still unknown, look it up from the meeting record.
    if not resolved_organizer_oid and resolved_meeting_id:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{sink}/v2/meetings/{resolved_meeting_id}")
                resp.raise_for_status()
                data = resp.json()
            org_obj = data.get("organizer") or {}
            if isinstance(org_obj, dict):
                resolved_organizer_oid = org_obj.get("aad_id")
        except Exception:
            pass

    if not resolved_meeting_id or not resolved_organizer_oid:
        result = RequestBackfillResult(
            ok=False,
            meeting_id=resolved_meeting_id,
            reason="need_meeting_id_and_organizer_oid",
        )
        context.record("request_transcript_backfill", arguments,
                       result.model_dump(), ok=False,
                       error="need_meeting_id_and_organizer_oid")
        return result

    bot_base = _bot_base_from_send_chat(_resolve_send_chat_url(context.send_chat_url))
    if not bot_base:
        result = RequestBackfillResult(
            ok=False,
            meeting_id=resolved_meeting_id,
            reason="no_bot_url",
        )
        context.record("request_transcript_backfill", arguments,
                       result.model_dump(), ok=False, error="no_bot_url")
        return result

    payload = {"meeting_id": resolved_meeting_id, "organizer_oid": resolved_organizer_oid}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{bot_base}/api/debug/fetch-transcript",
                json=payload,
            )
        if resp.status_code >= 400:
            err = f"HTTP {resp.status_code}: {resp.text[:200]}"
            result = RequestBackfillResult(
                ok=False, meeting_id=resolved_meeting_id, reason=err,
            )
            context.record("request_transcript_backfill", arguments,
                           result.model_dump(), ok=False, error=err)
            return result
        body = resp.json()
        result = RequestBackfillResult(
            ok=True,
            meeting_id=resolved_meeting_id,
            registered_key=body.get("registered_key"),
            note=body.get("note"),
        )
        context.record("request_transcript_backfill", arguments,
                       result.model_dump(), ok=True)
        return result
    except Exception as exc:  # noqa: BLE001
        err = f"transport: {exc!s}"
        result = RequestBackfillResult(
            ok=False, meeting_id=resolved_meeting_id, reason=err,
        )
        context.record("request_transcript_backfill", arguments,
                       result.model_dump(), ok=False, error=err)
        return result


async def fetch_meeting_transcript_impl(
    context: AlfredAgentContext,
    meeting_id: str | None = None,
) -> TranscriptResult:
    """Fetch a meeting's official transcript by canonical Graph meeting_id.

    Calls ``GET /v2/meetings/{meeting_id}/transcript`` which proxies the
    transcript text from the blob archive
    (``meetings/{meeting_id}/transcripts/official.txt``). If no
    ``meeting_id`` is provided, falls back to the most recent meeting in
    the sink's meetings registry (``GET /v2/meetings?limit=1``) so the
    agent can still answer "summarize the last meeting" without being
    told a meeting_id.

    The transcript body is returned to the LLM so it can READ it and
    answer the user's question — no summarization happens here.
    """
    sink = _sink_url()
    resolved_meeting_id = (meeting_id or "").strip() or None
    subject: str | None = None

    if resolved_meeting_id is None:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{sink}/v2/meetings", params={"limit": 1})
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_meeting_transcript list failed: %s", exc)
            return TranscriptResult(ok=False, reason=f"list_failed: {exc!r}")
        meetings = data.get("meetings") or []
        if not meetings:
            return TranscriptResult(ok=False, reason="no_meetings_known")
        first = meetings[0]
        resolved_meeting_id = first.get("meeting_id")
        subject = first.get("subject")

    if not resolved_meeting_id:
        return TranscriptResult(ok=False, reason="no_meeting_id")

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{sink}/v2/meetings/{resolved_meeting_id}/transcript")
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("fetch_meeting_transcript fetch failed: %s", exc)
        if exc.response.status_code == 404:
            return TranscriptResult(ok=False, meeting_id=resolved_meeting_id, reason="meeting_not_found")
        return TranscriptResult(ok=False, meeting_id=resolved_meeting_id, reason=f"http_error: {exc!r}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_meeting_transcript fetch failed: %s", exc)
        return TranscriptResult(ok=False, meeting_id=resolved_meeting_id, reason=f"http_error: {exc!r}")

    if not payload.get("available"):
        result = TranscriptResult(
            ok=False,
            meeting_id=resolved_meeting_id,
            subject=subject,
            blob_url=payload.get("official_transcript_txt_url"),
            reason="no_transcript_found",
        )
        context.record(
            "fetch_meeting_transcript",
            {"meeting_id": resolved_meeting_id},
            result.model_dump(),
            ok=False,
            error="no_transcript_found",
        )
        return result

    transcript_text = payload.get("text") or ""
    blob_url = payload.get("official_transcript_txt_url")
    result = TranscriptResult(
        ok=True,
        transcript=transcript_text,
        meeting_id=resolved_meeting_id,
        subject=subject,
        blob_url=blob_url,
        bytes=len(transcript_text),
    )
    context.record(
        "fetch_meeting_transcript",
        {"meeting_id": resolved_meeting_id},
        {
            "blob_url": blob_url,
            "bytes": len(transcript_text),
            "subject": subject,
        },
        ok=True,
    )
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

    @function_tool
    async def fetch_meeting_transcript(
        ctx: RunContextWrapper[AlfredAgentContext],
        meeting_id: str | None = None,
    ) -> TranscriptResult:
        """Fetch a meeting's official transcript so you can READ it and answer questions about what was said.

        Call this any time a user explicitly asks you to look up, recap,
        summarize, or answer a question about a past meeting ("alfred,
        get the transcript", "what did the team decide?", "summarize the
        last meeting", etc.). The returned ``transcript`` is Microsoft's
        official Record-and-Transcribe output as speaker-per-line
        plaintext — read it, then answer the user via
        ``send_to_meeting_chat`` (concise; quote sparingly).

        Do NOT call this on a normal silence-default tick. Only call
        when a user is directly addressing you with a question or
        recap request.

        Args:
            meeting_id: Optional canonical Graph onlineMeeting id. If
                omitted, fetches the most recent meeting. Use
                ``resolve_meeting_by_name`` first when the user names a
                meeting by its subject, then pass the canonical id here.
        """
        return await fetch_meeting_transcript_impl(
            context=ctx.context,
            meeting_id=meeting_id,
        )

    @function_tool
    async def list_meetings(
        ctx: RunContextWrapper[AlfredAgentContext],
        limit: int = 25,
    ) -> MeetingListResult:
        """List meetings the sink knows about (subject, organizer, times).

        Useful for "what meetings did we have today?" style questions and
        as a discovery step before ``fetch_meeting_transcript``.
        """
        return await list_meetings_impl(ctx.context, limit=limit)

    @function_tool
    async def resolve_meeting_by_name(
        ctx: RunContextWrapper[AlfredAgentContext],
        subject: str,
        limit: int = 10,
    ) -> MeetingResolveResult:
        """Resolve a meeting subject (substring) → canonical ``meeting_id`` matches.

        Use this when a user names a meeting by its subject. The result
        contains zero or more ``MeetingListEntry`` rows; pick the most
        likely match and pass its ``meeting_id`` to
        ``fetch_meeting_transcript``.

        Args:
            subject: Plain-text subject substring (case-insensitive).
            limit: Maximum number of matches to return.
        """
        return await resolve_meeting_by_name_impl(ctx.context, subject=subject, limit=limit)

    @function_tool
    async def request_transcript_backfill(
        ctx: RunContextWrapper[AlfredAgentContext],
        meeting_id: str | None = None,
        organizer_oid: str | None = None,
    ) -> RequestBackfillResult:
        """Ask the bot to backfill an official Microsoft transcript.

        Call this when ``fetch_meeting_transcript`` returns
        ``ok: false`` with ``reason: no_transcript_found`` AND a user
        is actively asking about that meeting. The bot will start
        polling Graph for the transcript (~30 min); the agent should
        tell the user to retry in 2-3 minutes.

        Idempotent — calling multiple times for the same meeting_id is
        cheap (bot dedups on the meeting_id key). Most "+Apps" meetings
        already have a fetcher running from the time of the first chat
        event, so this tool is for retry / catch-up cases.

        Args:
            meeting_id: Canonical Graph onlineMeeting id. Defaults to
                the most recent meeting if omitted.
            organizer_oid: AAD object id of the meeting organizer.
                Required by the bot's fetcher. If omitted, derived
                from the sink's V2Meeting record when available.
        """
        return await request_transcript_backfill_impl(
            context=ctx.context,
            meeting_id=meeting_id,
            organizer_oid=organizer_oid,
        )

    return (
        send_to_meeting_chat,
        fetch_meeting_transcript,
        list_meetings,
        resolve_meeting_by_name,
        request_transcript_backfill,
    )
