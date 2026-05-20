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
    "TranscriptFile",
    "TranscriptFileSearchResult",
    "TranscriptResult",
    "build_alfred_tools",
    "fetch_meeting_transcript_impl",
    "fetch_transcript_by_filename_impl",
    "find_meeting_by_chat_thread_id_impl",
    "list_meetings_impl",
    "list_meeting_transcript_files_impl",
    "request_transcript_backfill_impl",
    "resolve_meeting_by_date_impl",
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


def _recency_sort_key(entry: MeetingListEntry) -> str:
    """Sort key: actual_start > scheduled_start > empty (oldest).

    The sink already sorts newest-first via
    ``ORDER BY COALESCE(last_event_utc, scheduled_start_utc, created_at_utc) DESC``.
    Sorting again here is defense-in-depth so the tool's contract holds
    even against a misbehaving / out-of-order sink response.
    """
    return entry.actual_start_utc or entry.scheduled_start_utc or ""


async def list_meetings_impl(
    context: AlfredAgentContext,
    limit: int = 25,
    since: str | None = None,
    until: str | None = None,
) -> MeetingListResult:
    """List meetings the sink knows about via ``GET /v2/meetings``.

    Newest-first ordering. Used when a user asks "what meetings do you
    have?" or as a discovery step before calling
    ``fetch_meeting_transcript``. Pass ``since`` / ``until`` (ISO 8601
    UTC strings) to restrict the result to a date range.
    """
    sink = _sink_url()
    params: dict[str, Any] = {"limit": int(limit)}
    if since:
        params["since"] = since
    if until:
        params["until"] = until
    arguments = {"limit": limit, "since": since, "until": until}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{sink}/v2/meetings", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_meetings failed: %s", exc)
        result = MeetingListResult(ok=False, reason=f"http_error: {exc!r}")
        context.record("list_meetings", arguments, result.model_dump(), ok=False, error=str(exc))
        return result

    meetings = [_meeting_entry_from_v2(m) for m in (data.get("meetings") or [])]
    meetings.sort(key=_recency_sort_key, reverse=True)
    result = MeetingListResult(ok=True, count=len(meetings), meetings=meetings)
    context.record("list_meetings", arguments, result.model_dump(), ok=True)
    return result


# ---------------------------------------------------------------------------
# Natural-language date-phrase parsing for resolve_meeting_by_date.
# Kept intentionally narrow: today / yesterday / this week / last week /
# this month / last month / ISO date / ISO date range. No dateparser dep.
# ---------------------------------------------------------------------------

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATE_RANGE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s*(?:to|-|\.\.|,)\s*(\d{4}-\d{2}-\d{2})$"
)


def _utc_day_start(d: datetime) -> str:
    """ISO 8601 Z-suffixed midnight UTC for the date portion of ``d``."""
    midnight = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return midnight.isoformat().replace("+00:00", "Z")


def _add_days(d: datetime, n: int) -> datetime:
    from datetime import timedelta
    return d + timedelta(days=n)


def parse_date_phrase(phrase: str, now: datetime | None = None) -> tuple[str, str] | None:
    """Translate a natural-language date phrase into ``(since, until)`` ISO
    UTC strings (half-open: since <= ts < until).

    Returns ``None`` if the phrase isn't recognized — the caller should
    surface that as a clean error rather than silently matching everything.

    Recognized phrases (case-insensitive):
      * ``today``, ``yesterday``
      * ``this week``, ``last week``  (Monday-anchored)
      * ``this month``, ``last month``
      * ``YYYY-MM-DD``                 (single day)
      * ``YYYY-MM-DD to YYYY-MM-DD``   (inclusive range)
    """
    if not phrase or not phrase.strip():
        return None
    p = phrase.strip().lower()
    base = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = datetime(base.year, base.month, base.day, tzinfo=timezone.utc)

    if p == "today":
        return _utc_day_start(today), _utc_day_start(_add_days(today, 1))
    if p == "yesterday":
        return _utc_day_start(_add_days(today, -1)), _utc_day_start(today)

    if p in ("this week", "current week"):
        monday = _add_days(today, -today.weekday())
        return _utc_day_start(monday), _utc_day_start(_add_days(monday, 7))
    if p in ("last week", "previous week"):
        monday = _add_days(today, -today.weekday() - 7)
        return _utc_day_start(monday), _utc_day_start(_add_days(monday, 7))

    if p in ("this month", "current month"):
        first = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
        next_month = datetime(
            today.year + (1 if today.month == 12 else 0),
            1 if today.month == 12 else today.month + 1,
            1,
            tzinfo=timezone.utc,
        )
        return _utc_day_start(first), _utc_day_start(next_month)
    if p in ("last month", "previous month"):
        last_month_year = today.year - 1 if today.month == 1 else today.year
        last_month = 12 if today.month == 1 else today.month - 1
        first_last = datetime(last_month_year, last_month, 1, tzinfo=timezone.utc)
        first_this = datetime(today.year, today.month, 1, tzinfo=timezone.utc)
        return _utc_day_start(first_last), _utc_day_start(first_this)

    range_match = _ISO_DATE_RANGE_RE.match(p)
    if range_match:
        try:
            start = datetime.fromisoformat(range_match.group(1)).replace(tzinfo=timezone.utc)
            end = datetime.fromisoformat(range_match.group(2)).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return _utc_day_start(start), _utc_day_start(_add_days(end, 1))

    if _ISO_DATE_RE.match(p):
        try:
            day = datetime.fromisoformat(p).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
        return _utc_day_start(day), _utc_day_start(_add_days(day, 1))

    return None


async def resolve_meeting_by_date_impl(
    context: AlfredAgentContext,
    date_phrase: str,
    limit: int = 25,
) -> MeetingListResult:
    """Translate a natural-language date phrase into a meeting list.

    Wraps ``parse_date_phrase`` + ``list_meetings_impl``. The agent calls
    this when a user references meetings by time ("yesterday's standup",
    "meetings from last week").
    """
    arguments = {"date_phrase": date_phrase, "limit": limit}
    parsed = parse_date_phrase(date_phrase)
    if parsed is None:
        result = MeetingListResult(
            ok=False,
            reason=(
                "unrecognized_date_phrase: try 'today', 'yesterday', "
                "'this week', 'last week', 'this month', 'last month', "
                "YYYY-MM-DD, or YYYY-MM-DD to YYYY-MM-DD"
            ),
        )
        context.record(
            "resolve_meeting_by_date", arguments, result.model_dump(),
            ok=False, error="unrecognized_date_phrase",
        )
        return result
    since, until = parsed
    inner = await list_meetings_impl(context, limit=limit, since=since, until=until)
    # Re-record under this tool's name so the audit trail attributes the
    # call correctly (list_meetings already recorded internally).
    context.record(
        "resolve_meeting_by_date",
        {**arguments, "resolved_since": since, "resolved_until": until},
        inner.model_dump(),
        ok=inner.ok,
        error=inner.reason if not inner.ok else None,
    )
    return inner


async def find_meeting_by_chat_thread_id_impl(
    context: AlfredAgentContext,
    chat_thread_id: str,
) -> MeetingResolveResult:
    """Reverse-lookup: chat_thread_id (``19:xxx@thread.v2``) → canonical
    meeting via ``GET /v2/resolve?kind=meeting&chat_thread_id=...``.

    Returns 0 or 1 ``MeetingListEntry``. Use when an upstream code path
    gives you the chat thread id but you need the canonical
    ``meeting_id`` (e.g. to pass to ``fetch_meeting_transcript``).
    """
    sink = _sink_url()
    cleaned = (chat_thread_id or "").strip()
    arguments = {"chat_thread_id": cleaned}
    if not cleaned:
        result = MeetingResolveResult(
            ok=False, query="", reason="empty_chat_thread_id",
        )
        context.record(
            "find_meeting_by_chat_thread_id", arguments,
            result.model_dump(), ok=False, error="empty_chat_thread_id",
        )
        return result

    params = {"kind": "meeting", "chat_thread_id": cleaned}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{sink}/v2/resolve", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("find_meeting_by_chat_thread_id failed: %s", exc)
        result = MeetingResolveResult(
            ok=False, query=cleaned, reason=f"http_error: {exc!r}",
        )
        context.record(
            "find_meeting_by_chat_thread_id", arguments,
            result.model_dump(), ok=False, error=str(exc),
        )
        return result

    matches = [_meeting_entry_from_v2(m) for m in (data.get("matches") or [])]
    result = MeetingResolveResult(ok=True, query=cleaned, matches=matches)
    context.record(
        "find_meeting_by_chat_thread_id", arguments,
        result.model_dump(), ok=True,
    )
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


class TranscriptFile(BaseModel):
    """One transcript file present in blob storage."""

    meeting_id: str
    filename: str
    blob_path: str
    blob_url: str
    size_bytes: int | None = None
    last_modified_utc: str | None = None


class TranscriptFileSearchResult(BaseModel):
    """List/search result for transcript files in blob storage."""

    ok: bool
    count: int = 0
    files: list[TranscriptFile] = Field(default_factory=list)
    query: str | None = None
    reason: str | None = None


async def list_meeting_transcript_files_impl(
    context: AlfredAgentContext,
    query: str | None = None,
    limit: int = 25,
) -> TranscriptFileSearchResult:
    """List every ``meetings/{meeting_id}/transcripts/*.{vtt,txt}`` file in blob storage.

    Optionally filter by substring match on the filename (case-
    insensitive). Pure blob listing — no sink dependency. Useful when
    ``resolve_meeting_by_name`` returns no match (the meeting's
    ``subject`` field is empty) but the transcript file itself carries
    a recognisable name like ``Supermemory Meeting.vtt`` (Microsoft's
    default download name).
    """
    archive = _archive_url()
    arguments = {"query": query, "limit": limit}
    # List ALL blobs under meetings/ (paged). Filter to */transcripts/*.
    # The archive uses anonymous read so no auth required.
    files: list[TranscriptFile] = []
    needle = (query or "").strip().lower() or None
    try:
        marker = ""
        pages = 0
        async with httpx.AsyncClient(timeout=15.0) as client:
            while True:
                params = {
                    "restype": "container",
                    "comp": "list",
                    "prefix": "meetings/",
                    "maxresults": "5000",
                }
                if marker:
                    params["marker"] = marker
                resp = await client.get(archive, params=params)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for blob in root.iter("Blob"):
                    name = blob.findtext("Name") or ""
                    # We want meetings/{meeting_id}/transcripts/{file}
                    parts = name.split("/")
                    if (
                        len(parts) < 4
                        or parts[0] != "meetings"
                        or parts[2] != "transcripts"
                    ):
                        continue
                    filename = parts[3]
                    if not (filename.endswith(".vtt") or filename.endswith(".txt")):
                        continue
                    if needle and needle not in filename.lower():
                        continue
                    props = blob.find("Properties")
                    size_str = (props.findtext("Content-Length") or "0") if props is not None else "0"
                    last_mod = (props.findtext("Last-Modified") or "") if props is not None else ""
                    files.append(
                        TranscriptFile(
                            meeting_id=parts[1],
                            filename=filename,
                            blob_path=name,
                            blob_url=f"{archive}/{name}",
                            size_bytes=int(size_str) if size_str.isdigit() else None,
                            last_modified_utc=last_mod or None,
                        )
                    )
                    if len(files) >= limit:
                        break
                if len(files) >= limit:
                    break
                marker = (root.findtext("NextMarker") or "").strip()
                pages += 1
                if not marker or pages > 20:
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("list_meeting_transcript_files failed: %s", exc)
        result = TranscriptFileSearchResult(
            ok=False, query=query, reason=f"http_error: {exc!r}",
        )
        context.record(
            "list_meeting_transcript_files", arguments,
            result.model_dump(), ok=False, error=str(exc),
        )
        return result

    # Sort by most recent first when timestamps are present.
    files.sort(key=lambda f: f.last_modified_utc or "", reverse=True)
    result = TranscriptFileSearchResult(
        ok=True, count=len(files), files=files, query=query,
    )
    context.record(
        "list_meeting_transcript_files", arguments,
        result.model_dump(), ok=True,
    )
    return result


async def fetch_transcript_by_filename_impl(
    context: AlfredAgentContext,
    filename_substring: str,
) -> TranscriptResult:
    """Fetch a transcript by filename substring match.

    Searches blob storage for the first
    ``meetings/{meeting_id}/transcripts/*.{vtt,txt}`` whose filename
    contains the given substring (case-insensitive), then downloads
    and returns its plaintext. Use when the user names the meeting by
    its TRANSCRIPT FILENAME (Microsoft's default download is
    ``<meeting subject> Recording.vtt`` — so saying "fetch the
    Supermemory transcript" should match a file like
    ``Supermemory Meeting.vtt`` even when the sink's ``subject``
    field is unset.
    """
    arguments = {"filename_substring": filename_substring}
    query = (filename_substring or "").strip()
    if not query:
        result = TranscriptResult(ok=False, reason="empty_query")
        context.record(
            "fetch_transcript_by_filename", arguments,
            result.model_dump(), ok=False, error="empty_query",
        )
        return result

    search = await list_meeting_transcript_files_impl(context, query=query, limit=10)
    if not search.ok or not search.files:
        result = TranscriptResult(
            ok=False,
            reason=f"no_match_for: {query}",
        )
        context.record(
            "fetch_transcript_by_filename", arguments,
            result.model_dump(), ok=False, error="no_match",
        )
        return result

    # Prefer .vtt → .txt → first. Within ties take most-recent (already
    # sorted desc by last_modified).
    vtts = [f for f in search.files if f.filename.lower().endswith(".vtt")]
    txts = [f for f in search.files if f.filename.lower().endswith(".txt")]
    pick = (vtts or txts or search.files)[0]

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(pick.blob_url)
            resp.raise_for_status()
            body = resp.text
    except Exception as exc:  # noqa: BLE001
        logger.warning("fetch_transcript_by_filename download failed: %s", exc)
        result = TranscriptResult(
            ok=False,
            meeting_id=pick.meeting_id,
            blob_url=pick.blob_url,
            reason=f"download_failed: {exc!r}",
        )
        context.record(
            "fetch_transcript_by_filename", arguments,
            result.model_dump(), ok=False, error=str(exc),
        )
        return result

    # If it's a VTT, render to clean speaker-per-line plaintext.
    if pick.filename.lower().endswith(".vtt") or body.lstrip().startswith("WEBVTT"):
        rendered = _vtt_to_plaintext_local(body)
    else:
        rendered = body

    result = TranscriptResult(
        ok=True,
        transcript=rendered,
        meeting_id=pick.meeting_id,
        blob_url=pick.blob_url,
        bytes=len(rendered),
    )
    context.record(
        "fetch_transcript_by_filename",
        arguments,
        {
            "meeting_id": pick.meeting_id,
            "blob_url": pick.blob_url,
            "bytes": len(rendered),
            "filename": pick.filename,
        },
        ok=True,
    )
    return result


def _vtt_to_plaintext_local(vtt: str) -> str:
    """Mirror of the sink's _vtt_to_plaintext. Inlined here so the
    agent's blob-direct fetch path doesn't depend on a sink endpoint."""
    out: list[str] = []
    pending_start: str | None = None
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal pending_start, body_lines
        if not body_lines:
            return
        body = "\n".join(body_lines).strip()
        body_lines = []
        if not body:
            pending_start = None
            return
        prefix = f"[{pending_start}] " if pending_start else ""
        speaker = ""
        text = body
        open_idx = body.find("<v ")
        if open_idx >= 0:
            close_idx = body.find(">", open_idx)
            end_idx = body.find("</v>")
            if close_idx > open_idx and end_idx > close_idx:
                speaker = body[open_idx + 3 : close_idx].strip()
                text = body[close_idx + 1 : end_idx].strip()
        if speaker:
            out.append(f"{prefix}{speaker}: {text}")
        else:
            out.append(f"{prefix}{text}")
        pending_start = None

    for raw_line in vtt.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            flush()
            continue
        arrow = line.find(" --> ")
        if arrow > 0 and len(line) > arrow + 5:
            start = line[:arrow]
            dot = start.find(".")
            pending_start = start[:dot] if dot > 0 else start
            continue
        if line.startswith("WEBVTT") or line.startswith("NOTE"):
            continue
        body_lines.append(line)
    flush()
    return "\n".join(out) + "\n"


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
        since: str | None = None,
        until: str | None = None,
    ) -> MeetingListResult:
        """List meetings the sink knows about, newest first (subject, organizer, times).

        Useful for "what meetings did we have today?" style questions and
        as a discovery step before ``fetch_meeting_transcript``.

        Args:
            limit: Maximum number of meetings to return.
            since: Optional ISO 8601 UTC timestamp ("2026-05-19T00:00:00Z").
                Meetings whose actual_start_utc (or scheduled_start_utc)
                is < since are excluded.
            until: Optional ISO 8601 UTC timestamp. Meetings whose start
                is >= until are excluded. Pair with ``since`` for a range.
                Prefer ``resolve_meeting_by_date`` for natural-language
                phrases like "yesterday" / "last week".
        """
        return await list_meetings_impl(ctx.context, limit=limit, since=since, until=until)

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
    async def resolve_meeting_by_date(
        ctx: RunContextWrapper[AlfredAgentContext],
        date_phrase: str,
        limit: int = 25,
    ) -> MeetingListResult:
        """Resolve a natural-language date phrase → meetings in that range.

        Use this when a user references meetings by time ("yesterday's
        standup", "what meetings did we have last week?", "show me
        2026-05-15 meetings"). Returns the standard meeting list
        (subject / organizer / times); pick the right one and pass its
        ``meeting_id`` to ``fetch_meeting_transcript`` if the user
        wants the transcript.

        Args:
            date_phrase: One of: ``today``, ``yesterday``, ``this week``,
                ``last week``, ``this month``, ``last month``, an ISO
                date ``YYYY-MM-DD``, or an ISO date range
                ``YYYY-MM-DD to YYYY-MM-DD``. Unrecognized phrases
                return ``ok: false`` with a hint — re-ask the user.
            limit: Maximum number of meetings to return.
        """
        return await resolve_meeting_by_date_impl(ctx.context, date_phrase=date_phrase, limit=limit)

    @function_tool
    async def find_meeting_by_chat_thread_id(
        ctx: RunContextWrapper[AlfredAgentContext],
        chat_thread_id: str,
    ) -> MeetingResolveResult:
        """Reverse-lookup: chat_thread_id → canonical meeting.

        ``chat_thread_id`` is the ``19:xxx@thread.v2`` form that surfaces
        in conversation references and Graph notifications. This tool
        bridges to the canonical ``meeting_id`` you need for
        ``fetch_meeting_transcript`` and ``request_transcript_backfill``.

        Returns 0 or 1 match. Useful primarily for historical data where
        the bot wrote events under the chat thread id before
        canonicalization landed; for new events the canonical id is
        already populated.

        Args:
            chat_thread_id: The ``19:meeting_xxx@thread.v2`` chat thread id.
        """
        return await find_meeting_by_chat_thread_id_impl(
            ctx.context, chat_thread_id=chat_thread_id,
        )

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

    @function_tool
    async def list_meeting_transcript_files(
        ctx: RunContextWrapper[AlfredAgentContext],
        query: str | None = None,
        limit: int = 25,
    ) -> TranscriptFileSearchResult:
        """List transcript files in blob storage, optionally filtered by name.

        Returns every ``meetings/{meeting_id}/transcripts/*.{vtt,txt}``
        file. Use to find a transcript when the user names the meeting
        by something close to the FILENAME rather than the canonical
        subject (e.g. Microsoft's default ``"<subject> Recording.vtt"``).

        Args:
            query: Optional case-insensitive substring filter on the
                filename. Empty means "list everything (capped at
                ``limit``)".
            limit: Max entries to return.
        """
        return await list_meeting_transcript_files_impl(
            ctx.context, query=query, limit=limit,
        )

    @function_tool
    async def fetch_transcript_by_filename(
        ctx: RunContextWrapper[AlfredAgentContext],
        filename_substring: str,
    ) -> TranscriptResult:
        """Fetch a transcript by filename substring match.

        Searches blob storage for the first
        ``meetings/{meeting_id}/transcripts/*.vtt`` (preferred) or
        ``*.txt`` whose filename contains ``filename_substring``
        (case-insensitive), downloads it, and returns the cleaned
        speaker-per-line plaintext. Use this when ``resolve_meeting_by_name``
        returns 0 matches but the transcript file itself has a
        recognizable name (Microsoft's default download is
        ``"<subject> Recording.vtt"``).

        Args:
            filename_substring: e.g. ``"Supermemory"`` or
                ``"Sprint Planning"`` — substring of the filename, not
                the meeting subject.
        """
        return await fetch_transcript_by_filename_impl(
            ctx.context, filename_substring=filename_substring,
        )

    return (
        send_to_meeting_chat,
        fetch_meeting_transcript,
        list_meetings,
        resolve_meeting_by_name,
        resolve_meeting_by_date,
        find_meeting_by_chat_thread_id,
        request_transcript_backfill,
        list_meeting_transcript_files,
        fetch_transcript_by_filename,
    )
