"""FastAPI server for the chat agent with Alfred v2 bridge support.

This is a separate sidecar from the legacy ``server.py``. It preserves
the existing chat routes while adding two v2 ingestion paths:

* ``POST /v2/events`` for live bot fanout.
* Blob archive polling for replay/catch-up from the canonical v2 paths.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .config import BridgeConfig, ChatConfig
from .context import assemble_context, format_user_payload
from .llm import chat_completion, chat_completion_stream

app = FastAPI(title="IAS Chat Agent (v2)")

_config: ChatConfig | None = None
_bridge_tasks: list[asyncio.Task] = []
_event_queue: asyncio.Queue["QueuedEnvelope"] | None = None
_event_worker_task: asyncio.Task | None = None
_seen_event_ids: set[str] = set()
_seen_event_order: deque[str] = deque()
_last_conversation_refs: dict[str, str] = {}

STATIC_DIR = Path(__file__).parent / "static"
DEBUG = os.getenv("ALFRED_BRIDGE_DEBUG", "").lower() in {"1", "true", "yes", "on"}
MAX_SEEN_EVENTS = int(os.getenv("ALFRED_BRIDGE_SEEN_EVENTS", "10000"))
EVENT_QUEUE_SIZE = int(os.getenv("ALFRED_BRIDGE_QUEUE_SIZE", "1000"))
DEFAULT_EVENT_KINDS = {
    "channel.message.created",
    "meeting.chat.created",
}
UNSAFE_PATH_CHARS = re.compile(r"[^a-zA-Z0-9\-_.]")


class ChatRequest(BaseModel):
    message: str
    stream: bool = False


class ChatResponse(BaseModel):
    reply: str


@dataclass(frozen=True, slots=True)
class StorageTarget:
    base_url: str
    sas_query: str
    source: str

    def list_url(self, prefix: str, marker: str = "") -> str:
        query = {
            "restype": "container",
            "comp": "list",
            "maxresults": "5000",
            "prefix": prefix,
        }
        if marker:
            query["marker"] = marker
        return self._with_query(query)

    def blob_url(self, name: str) -> str:
        quoted_name = urllib.parse.quote(name, safe="/")
        url = f"{self.base_url}/{quoted_name}"
        if self.sas_query:
            return f"{url}?{self.sas_query}"
        return url

    def _with_query(self, query: dict[str, str]) -> str:
        parts = [urllib.parse.urlencode(query)]
        if self.sas_query:
            parts.append(self.sas_query)
        return f"{self.base_url}?{'&'.join(parts)}"


@dataclass(frozen=True, slots=True)
class QueuedEnvelope:
    envelope: dict[str, Any]
    source: str
    blob_name: str | None = None


@dataclass(frozen=True, slots=True)
class IncomingEvent:
    event_id: str
    event_type: str
    text: str
    sender: str
    from_bot: bool
    conversation_reference_id: str | None
    team_id: str | None
    channel_id: str | None
    channel_thread_id: str | None
    meeting_id: str | None
    source: str
    blob_name: str | None = None


def configure(config: ChatConfig) -> None:
    global _config
    _config = config


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/health")
async def health():
    bridges = getattr(_config, "bridges", None) or []
    return {
        "ok": _config is not None,
        "bridge_count": len(bridges),
        "queue_depth": _event_queue.qsize() if _event_queue else 0,
        "debug": DEBUG,
    }


@app.post("/chat")
async def chat(request: ChatRequest):
    assert _config is not None, "Server not configured - call configure() first"

    t0 = time.time()

    ctx = assemble_context(request.message, _config)
    t_context = time.time() - t0

    messages = _build_messages(request.message, ctx)
    _log_llm_input(messages)
    _log_context_trace(request.message, ctx)

    t_ready = time.time() - t0
    sub_timings = ctx.pop("_timings", {})
    timings = {"context_ms": int(t_context * 1000), "ready_ms": int(t_ready * 1000), **sub_timings}

    if request.stream:
        return StreamingResponse(
            _stream_and_log(messages, request.message, timings),
            media_type="text/event-stream",
        )

    t_llm_start = time.time()
    reply = chat_completion(messages, config=_config)
    t_llm = time.time() - t_llm_start
    _append_transcript(request.message, reply)
    return {
        "reply": reply,
        "timings": {**timings, "llm_ms": int(t_llm * 1000), "total_ms": int((time.time() - t0) * 1000)},
    }


@app.post("/v2/events")
async def ingest_v2_event(envelope: dict[str, Any] = Body(...)):
    """Accept live alfred-v2 fanout from the Teams bot.

    The route queues work and returns quickly so bot fanout retries are not
    tied to the LLM response time.
    """
    if _event_queue is None:
        raise HTTPException(status_code=503, detail="Bridge event queue is not initialized")
    if not isinstance(envelope, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON event envelope object")
    try:
        _event_queue.put_nowait(QueuedEnvelope(envelope=envelope, source="live"))
    except asyncio.QueueFull as exc:
        raise HTTPException(status_code=429, detail="Bridge event queue is full") from exc
    return {"ok": True, "action": "queued"}


async def _stream_and_log(messages: list[dict], user_message: str, timings: dict):
    """Stream SSE chunks and log the full reply when done."""
    yield f"data: {json.dumps({'meta': timings})}\n\n"

    t_first = None
    t_start = time.time()
    full_reply = []
    for chunk in chat_completion_stream(messages, config=_config):
        if t_first is None:
            t_first = time.time() - t_start
            yield f"data: {json.dumps({'meta': {'first_token_ms': int(t_first * 1000)}})}\n\n"
        full_reply.append(chunk)
        yield f"data: {json.dumps({'content': chunk})}\n\n"

    t_total = time.time() - t_start
    yield f"data: {json.dumps({'meta': {'stream_ms': int(t_total * 1000)}})}\n\n"
    yield "data: [DONE]\n\n"
    _append_transcript(user_message, "".join(full_reply))


def _build_messages(user_message: str, ctx: dict[str, Any]) -> list[dict[str, str]]:
    assert _config is not None
    system_prompt = ""
    if _config.respond_prompt_path.exists():
        system_prompt = _config.respond_prompt_path.read_text().strip()

    user_payload = format_user_payload(ctx)
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_payload})
    return messages


def _log_llm_input(messages: list[dict]) -> None:
    assert _config is not None
    _config.logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = _config.logs_dir / f"llm_input_{ts}.json"
    with open(path, "w") as f:
        json.dump(messages, f, indent=2)


def _log_context_trace(user_message: str, ctx: dict) -> None:
    """Log what data was pulled for this turn."""
    assert _config is not None
    _config.logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    path = _config.logs_dir / f"context_trace_{ts}.json"

    trace = {
        "timestamp": datetime.now().isoformat(),
        "user_message": user_message,
        "timings": ctx.get("_timings", {}),
        "behavior_notes_chars": len(ctx.get("behavior_notes", "")),
        "transcript_chars": len(ctx.get("transcript", "")),
        "abouts_chars": len(ctx.get("abouts_context", "")),
    }

    if ctx.get("block_results"):
        trace["blocks"] = []
        for block in ctx["block_results"]:
            block_trace = {
                "name": block["name"],
                "description": block["description"],
                "chunk_count": len(block["chunks"]),
                "chunks": [],
            }
            for chunk in block["chunks"]:
                block_trace["chunks"].append({
                    "collection": chunk["collection"],
                    "distance": round(chunk["distance"], 4),
                    "source": chunk["metadata"].get("source_name", ""),
                    "date": chunk["metadata"].get("source_date") or "undated",
                    "kind": chunk["metadata"].get("kind", ""),
                    "preview": chunk["document"][:200],
                })
            trace["blocks"].append(block_trace)
    elif ctx.get("collections_context"):
        trace["collections_context_chars"] = len(ctx["collections_context"])

    with open(path, "w") as f:
        json.dump(trace, f, indent=2)


def _append_transcript(user_message: str, reply: str) -> None:
    assert _config is not None
    _config.logs_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(_config.transcript_path, "a") as f:
        f.write(f"[{ts}] USER: {user_message}\n")
        f.write(f"[{ts}] AGENT: {reply}\n\n")


def _resolve_log_path() -> str:
    for candidate in (
        "/tmp/alfred-bridge-v2.log",
        os.path.join(os.path.expanduser("~"), "alfred-bridge-v2.log"),
        os.path.abspath("alfred-bridge-v2.log"),
    ):
        try:
            with open(candidate, "a", encoding="utf-8"):
                pass
            return candidate
        except Exception:
            continue
    return os.path.abspath("alfred-bridge-v2.log")


LOG_PATH = _resolve_log_path()
print(f"[bridge-log] writing to {LOG_PATH}", flush=True)


def _write_to_file(line: str) -> None:
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def _log(prefix: str, msg: str) -> None:
    line = f"{prefix} {msg}"
    print(line, flush=True)
    _write_to_file(line)


def _dlog(prefix: str, msg: str) -> None:
    if DEBUG:
        _log(prefix, msg)


def _bridge_value(bridge: Any, name: str) -> Any:
    if isinstance(bridge, dict):
        return bridge.get(name)
    return getattr(bridge, name, None)


def _as_nonempty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(part).strip() for part in value if str(part).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _first_nonblank(*values: Any) -> str | None:
    for value in values:
        text = _as_nonempty_str(value)
        if text:
            return text
    return None


def _sanitize_path_segment(raw: str | None) -> str:
    if not raw or not raw.strip():
        return "_"
    replaced = UNSAFE_PATH_CHARS.sub("_", raw.strip())
    return replaced[:200] if len(replaced) > 200 else replaced


def _split_url_base_and_query(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlsplit(url)
    base = urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))
    return base, parts.query


def _join_container_url(account_url: str, container: str) -> str:
    base, query = _split_url_base_and_query(account_url)
    joined = f"{base.rstrip('/')}/{urllib.parse.quote(container.strip('/'), safe='')}"
    if query:
        return f"{joined}?{query}"
    return joined


def _infer_account_url_from_blob_base(blob_base: str) -> str | None:
    parts = urllib.parse.urlsplit(blob_base)
    path_segments = [segment for segment in parts.path.split("/") if segment]
    if not parts.scheme or not parts.netloc or not path_segments:
        return None
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", parts.query))


def _resolve_storage_target(bridge: BridgeConfig) -> StorageTarget:
    bucket = _first_nonblank(
        _bridge_value(bridge, "storage_bucket"),
        os.getenv("ALFRED_STORAGE_BUCKET"),
        os.getenv("STORAGE_BUCKET"),
    )
    container = _first_nonblank(
        _bridge_value(bridge, "storage_container"),
        os.getenv("ALFRED_STORAGE_CONTAINER"),
        os.getenv("STORAGE_CONTAINER"),
    )
    account_url = _first_nonblank(
        _bridge_value(bridge, "storage_account_url"),
        os.getenv("ALFRED_STORAGE_ACCOUNT_URL"),
        os.getenv("STORAGE_ACCOUNT_URL"),
    )
    legacy_blob_base = _first_nonblank(_bridge_value(bridge, "blob_base"))

    if bucket and bucket.startswith(("http://", "https://")):
        base, query = _split_url_base_and_query(bucket)
        return StorageTarget(base_url=base, sas_query=query, source="storage_bucket_url")

    selected_container = container or bucket
    if selected_container and account_url:
        base, query = _split_url_base_and_query(_join_container_url(account_url, selected_container))
        return StorageTarget(base_url=base, sas_query=query, source="storage_container")

    if selected_container and legacy_blob_base:
        inferred_account_url = _infer_account_url_from_blob_base(legacy_blob_base)
        if inferred_account_url:
            base, query = _split_url_base_and_query(_join_container_url(inferred_account_url, selected_container))
            return StorageTarget(base_url=base, sas_query=query, source="storage_container_from_legacy_account")

    if legacy_blob_base:
        base, query = _split_url_base_and_query(legacy_blob_base)
        return StorageTarget(base_url=base, sas_query=query, source="legacy_blob_base")

    raise ValueError(
        "Bridge storage is required: set storage_bucket + storage_account_url, "
        "storage_container + storage_account_url, a full storage_bucket URL, or legacy blob_base"
    )


def _bridge_event_kinds(bridge: BridgeConfig) -> set[str] | None:
    configured = _first_nonblank(
        _bridge_value(bridge, "event_kinds"),
        _bridge_value(bridge, "event_types"),
    )
    if not configured:
        return set(DEFAULT_EVENT_KINDS)
    if configured == "*":
        return None
    return set(_as_string_list(configured))


def _bridge_meeting_ids(bridge: BridgeConfig) -> list[str]:
    values = _as_string_list(_bridge_value(bridge, "meeting_ids"))
    single = _as_nonempty_str(_bridge_value(bridge, "meeting_id"))
    if single:
        values.append(single)
    return values


def _bridge_prefixes(bridge: BridgeConfig) -> list[str]:
    team = _as_nonempty_str(_bridge_value(bridge, "team"))
    channel = _as_nonempty_str(_bridge_value(bridge, "channel"))
    prefixes: list[str] = []

    if team and channel:
        safe_team = _sanitize_path_segment(team)
        safe_channel = _sanitize_path_segment(channel)
        prefixes.append(f"teams/{safe_team}/channels/{safe_channel}/messages/")
        prefixes.append(f"channels/{safe_team}/{safe_channel}/chat.message/")

    event_kinds = _bridge_event_kinds(bridge)
    include_live_transcript = event_kinds is None or "meeting.transcript.final" in event_kinds
    for meeting_id in _bridge_meeting_ids(bridge):
        safe_meeting = _sanitize_path_segment(meeting_id)
        prefixes.append(f"meetings/{safe_meeting}/messages/")
        if include_live_transcript:
            prefixes.append(f"meetings/{safe_meeting}/live_transcript/")

    prefixes.extend(_as_string_list(_bridge_value(bridge, "extra_prefixes")))
    deduped: list[str] = []
    for prefix in prefixes:
        if prefix not in deduped:
            deduped.append(prefix)
    return deduped


def _list_blobs(storage: StorageTarget, prefix: str) -> list[str]:
    names: list[str] = []
    marker = ""
    pages = 0
    while True:
        url = storage.list_url(prefix, marker)
        xml_bytes = urllib.request.urlopen(url, timeout=15).read()
        root = ET.fromstring(xml_bytes)
        for blob in root.iter("Blob"):
            name = blob.findtext("Name")
            if name:
                names.append(name)
        marker = (root.findtext("NextMarker") or "").strip()
        pages += 1
        if not marker:
            break
        if pages > 100:
            _log("[bridge]", f"WARNING listing aborted after 100 pages prefix={prefix!r}")
            break
    return sorted(names)


def _download_envelope(storage: StorageTarget, name: str) -> dict[str, Any] | None:
    try:
        body = urllib.request.urlopen(storage.blob_url(name), timeout=15).read().decode("utf-8", errors="replace")
    except Exception as exc:
        _log("[bridge]", f"download FAILED name={name} err={type(exc).__name__}: {exc}")
        return None

    if "---ENVELOPE---" in body:
        body = body.split("---ENVELOPE---", 1)[1].strip()

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        _log("[bridge]", f"json parse FAILED name={name} err={exc}")
        return None

    if not isinstance(parsed, dict):
        _log("[bridge]", f"json parse ignored non-object name={name}")
        return None
    return parsed


def _extract_sender(payload: dict[str, Any]) -> str:
    sender_obj = payload.get("sender") or payload.get("speaker") or {}
    if isinstance(sender_obj, dict):
        nested = _first_nonblank(sender_obj.get("display_name"), sender_obj.get("id"))
        if nested:
            return nested
    return _first_nonblank(payload.get("sender_display_name"), payload.get("speaker_display_name"), "?") or "?"


def _event_type_from_v1(envelope: dict[str, Any], payload: dict[str, Any]) -> str:
    raw_type = _as_nonempty_str(envelope.get("event_type")) or ""
    if raw_type != "chat.message":
        return raw_type
    inner = _as_nonempty_str(payload.get("event_type"))
    if inner == "chat_updated":
        return "channel.message.updated"
    if inner == "chat_deleted":
        return "channel.message.deleted"
    return "channel.message.created"


def _normalize_envelope(envelope: dict[str, Any], source: str, blob_name: str | None = None) -> IncomingEvent | None:
    payload_raw = envelope.get("payload") or {}
    if not isinstance(payload_raw, dict):
        payload_raw = {}
    payload: dict[str, Any] = payload_raw

    schema = _as_nonempty_str(envelope.get("schema_version"))
    is_v1 = schema == "alfred-events-v1" or _as_nonempty_str(envelope.get("event_type")) == "chat.message"

    if is_v1:
        event_type = _event_type_from_v1(envelope, payload)
        team_id = _first_nonblank(envelope.get("team_id"), payload.get("team_id"))
        channel_id = _first_nonblank(envelope.get("channel_id"), payload.get("channel_id"))
        channel_thread_id = _first_nonblank(envelope.get("channel_thread_id"), payload.get("channel_thread_id"))
        meeting_id = _first_nonblank(envelope.get("meeting_id"), payload.get("meeting_id"))
    else:
        event_type = _as_nonempty_str(envelope.get("event_type")) or ""
        channel_ref = envelope.get("channel_ref") if isinstance(envelope.get("channel_ref"), dict) else {}
        meeting_ref = envelope.get("meeting_ref") if isinstance(envelope.get("meeting_ref"), dict) else {}
        channel_link = meeting_ref.get("channel_link") if isinstance(meeting_ref.get("channel_link"), dict) else {}

        team_id = _first_nonblank(channel_ref.get("team_id"), channel_link.get("team_id"), payload.get("team_id"))
        channel_id = _first_nonblank(channel_ref.get("channel_id"), channel_link.get("channel_id"), payload.get("channel_id"))
        channel_thread_id = _first_nonblank(
            channel_ref.get("thread_id"),
            channel_link.get("thread_id"),
            payload.get("channel_thread_id"),
            channel_id,
        )
        meeting_id = _first_nonblank(
            meeting_ref.get("meeting_id"),
            meeting_ref.get("meeting_chat_thread_id"),
            payload.get("meeting_id"),
            payload.get("meeting_chat_thread_id"),
        )

    event_id = _first_nonblank(envelope.get("event_id"), blob_name)
    if not event_id:
        _log("[bridge]", f"ignored event without event_id source={source} blob={blob_name}")
        return None

    text = _first_nonblank(payload.get("text"), payload.get("body"), "")
    conversation_reference_id = _first_nonblank(
        envelope.get("conversation_reference_id"),
        payload.get("conversation_reference_id"),
    )
    from_bot = bool(payload.get("from_bot"))

    return IncomingEvent(
        event_id=event_id,
        event_type=event_type,
        text=text or "",
        sender=_extract_sender(payload),
        from_bot=from_bot,
        conversation_reference_id=conversation_reference_id,
        team_id=team_id,
        channel_id=channel_id,
        channel_thread_id=channel_thread_id,
        meeting_id=meeting_id,
        source=source,
        blob_name=blob_name,
    )


def _remember_event_id(event_id: str) -> bool:
    if event_id in _seen_event_ids:
        return False
    _seen_event_ids.add(event_id)
    _seen_event_order.append(event_id)
    while len(_seen_event_order) > MAX_SEEN_EVENTS:
        old = _seen_event_order.popleft()
        _seen_event_ids.discard(old)
    return True


def _ref_key(event: IncomingEvent, bridge: BridgeConfig) -> str:
    if event.meeting_id:
        return f"meeting:{event.meeting_id}"
    if event.team_id and event.channel_id:
        return f"channel:{event.team_id}:{event.channel_id}"
    return f"bridge:{id(bridge)}"


def _event_matches_bridge(event: IncomingEvent, bridge: BridgeConfig) -> bool:
    event_kinds = _bridge_event_kinds(bridge)
    if event_kinds is not None and event.event_type not in event_kinds:
        return False

    bridge_team = _as_nonempty_str(_bridge_value(bridge, "team"))
    bridge_channel = _as_nonempty_str(_bridge_value(bridge, "channel"))
    if event.team_id or event.channel_id:
        if bridge_team and event.team_id != bridge_team:
            return False
        if bridge_channel and event.channel_id != bridge_channel:
            return False
        return bool(bridge_team or bridge_channel)

    meeting_ids = set(_bridge_meeting_ids(bridge))
    if event.meeting_id and meeting_ids:
        return event.meeting_id in meeting_ids

    return False


def _selected_bridges(event: IncomingEvent) -> list[BridgeConfig]:
    bridges = getattr(_config, "bridges", None) or []
    selected: list[BridgeConfig] = []
    for bridge in bridges:
        if not bool(_bridge_value(bridge, "enabled")):
            continue
        if _bridge_value(bridge, "type") != "teams":
            continue
        if _event_matches_bridge(event, bridge):
            selected.append(bridge)
    return selected


def _looks_like_system_message(text: str) -> bool:
    return "<URIObject" in text or "scopeId" in text


def _send_to_teams(bot_url: str, text: str, ref: str) -> tuple[bool, int, str]:
    payload = json.dumps({"conversation_reference_id": ref, "text": text}).encode()
    req = urllib.request.Request(
        f"{bot_url.rstrip('/')}/api/send-chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body_head = resp.read().decode("utf-8", errors="replace")[:200]
            return (200 <= resp.status < 300, resp.status, body_head)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:200]
        return (False, exc.code, body)
    except Exception as exc:
        return (False, 0, f"{type(exc).__name__}: {exc}")


async def _handle_incoming_event(event: IncomingEvent, bridge: BridgeConfig) -> None:
    assert _config is not None
    ref_key = _ref_key(event, bridge)
    if event.conversation_reference_id:
        _last_conversation_refs[ref_key] = event.conversation_reference_id
    conversation_reference_id = event.conversation_reference_id or _last_conversation_refs.get(ref_key)
    if not conversation_reference_id:
        _log("[bridge]", f"cannot send: no conversation_reference_id event_id={event.event_id} type={event.event_type}")
        return

    preview = event.text.replace("\n", " ")[:120]
    _log("[bridge]", f"IN  [{event.sender}] {event.event_type} {preview!r}")

    try:
        t_llm0 = time.time()
        ctx = await asyncio.to_thread(assemble_context, event.text, _config)
        messages = _build_messages(event.text, ctx)
        reply = await asyncio.to_thread(chat_completion, messages, config=_config)
        t_llm = int((time.time() - t_llm0) * 1000)
        _append_transcript(event.text, reply)
        _log("[bridge]", f"OUT [{len(reply)} chars, llm={t_llm}ms] preview={reply[:100]!r}")
    except Exception as exc:
        _log("[bridge]", f"AGENT FAILED event_id={event.event_id}: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    bot_url = _as_nonempty_str(_bridge_value(bridge, "bot_url"))
    if not bot_url:
        _log("[bridge]", f"SEND FAILED event_id={event.event_id}: bridge.bot_url is required")
        return

    t_send0 = time.time()
    ok, status, body_head = await asyncio.to_thread(_send_to_teams, bot_url, reply, conversation_reference_id)
    t_send = int((time.time() - t_send0) * 1000)
    if ok:
        _log("[bridge]", f"SENT status={status} send_ms={t_send} body={body_head!r}")
    else:
        _log("[bridge]", f"SEND FAILED status={status} send_ms={t_send} body={body_head!r}")


async def _dispatch_envelope(queued: QueuedEnvelope) -> None:
    if _config is None:
        _log("[bridge]", "ignored event because server is not configured")
        return

    event = _normalize_envelope(queued.envelope, queued.source, queued.blob_name)
    if event is None:
        return
    if not _remember_event_id(event.event_id):
        _dlog("[bridge]", f"skip duplicate event_id={event.event_id}")
        return
    if event.from_bot:
        _dlog("[bridge]", f"skip from_bot=true event_id={event.event_id}")
        return
    if _looks_like_system_message(event.text):
        _dlog("[bridge]", f"skip system_message event_id={event.event_id}")
        return
    if not event.text.strip():
        _dlog("[bridge]", f"skip empty_text event_id={event.event_id}")
        return

    bridges = _selected_bridges(event)
    if not bridges:
        _dlog(
            "[bridge]",
            f"skip no matching bridge event_id={event.event_id} type={event.event_type} "
            f"team={event.team_id!r} channel={event.channel_id!r} meeting={event.meeting_id!r}",
        )
        return

    for bridge in bridges:
        await _handle_incoming_event(event, bridge)


async def _event_worker() -> None:
    assert _event_queue is not None
    _log("[bridge]", f"event worker started queue_size={EVENT_QUEUE_SIZE}")
    while True:
        queued = await _event_queue.get()
        try:
            await _dispatch_envelope(queued)
        except Exception as exc:
            _log("[bridge]", f"event worker error: {type(exc).__name__}: {exc}")
            traceback.print_exc()
        finally:
            _event_queue.task_done()


async def _teams_bridge_loop(bridge: BridgeConfig, storage: StorageTarget) -> None:
    prefixes = _bridge_prefixes(bridge)
    if not prefixes:
        raise ValueError(
            "Teams bridge needs team+channel, meeting_id/meeting_ids, or extra_prefixes to poll blob storage"
        )

    poll_seconds = float(_bridge_value(bridge, "poll") or 5)
    seen_names: set[str] = set()
    prefix_totals: dict[str, int] = {}

    _log("[bridge]", f"poller starting storage={storage.base_url} source={storage.source} poll={poll_seconds:g}s")
    for prefix in prefixes:
        _log("[bridge]", f"  prefix={prefix!r}")

    try:
        for prefix in prefixes:
            initial = await asyncio.to_thread(_list_blobs, storage, prefix)
            seen_names.update(initial)
            prefix_totals[prefix] = len(initial)
        _log("[bridge]", f"initial snapshot total_seen={len(seen_names)} details={prefix_totals}")
    except Exception as exc:
        _log("[bridge]", f"initial snapshot FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    poll_n = 0
    while True:
        poll_n += 1
        cycle_start = time.time()
        try:
            current_names: list[str] = []
            prefix_totals = {}
            for prefix in prefixes:
                names = await asyncio.to_thread(_list_blobs, storage, prefix)
                current_names.extend(names)
                prefix_totals[prefix] = len(names)

            new_names = sorted(name for name in current_names if name not in seen_names)
            if new_names:
                _log("[bridge]", f"poll #{poll_n}: {len(new_names)} new blob(s) details={prefix_totals}")
            else:
                elapsed_ms = int((time.time() - cycle_start) * 1000)
                _dlog("[bridge]", f"poll #{poll_n}: nothing new elapsed={elapsed_ms}ms details={prefix_totals}")

            for name in new_names:
                seen_names.add(name)
                envelope = await asyncio.to_thread(_download_envelope, storage, name)
                if not envelope:
                    continue
                if _event_queue is None:
                    _log("[bridge]", f"drop blob event queue missing name={name}")
                    continue
                await _event_queue.put(QueuedEnvelope(envelope=envelope, source="blob", blob_name=name))

        except Exception as exc:
            _log("[bridge]", f"poll #{poll_n} FAILED: {type(exc).__name__}: {exc}")
            traceback.print_exc()

        await asyncio.sleep(poll_seconds)


async def _startup_probe(storage_targets: list[StorageTarget]) -> None:
    _log("[probe]", "============ bridge-v2 probe start ============")
    try:
        bridges = getattr(_config, "bridges", None) or []
        _log("[probe]", f"_config present: {_config is not None}, bridges_count: {len(bridges)}")
        for i, bridge in enumerate(bridges):
            _log(
                "[probe]",
                f"bridge[{i}] type={_bridge_value(bridge, 'type')!r} enabled={_bridge_value(bridge, 'enabled')} "
                f"team={_bridge_value(bridge, 'team')!r} channel={_bridge_value(bridge, 'channel')!r} "
                f"poll={_bridge_value(bridge, 'poll')!r} event_kinds={_bridge_event_kinds(bridge)}",
            )
    except Exception as exc:
        _log("[probe]", f"config inspect FAILED: {type(exc).__name__}: {exc}")
        traceback.print_exc()

    hosts = {
        "alfred-disney-bot.eastus.cloudapp.azure.com",
        *(urllib.parse.urlsplit(target.base_url).netloc for target in storage_targets),
    }
    for host in sorted(h for h in hosts if h):
        try:
            ip = await asyncio.to_thread(socket.gethostbyname, host)
            _log("[probe]", f"dns {host} -> {ip}")
        except Exception as exc:
            _log("[probe]", f"dns {host} FAILED: {type(exc).__name__}: {exc}")

    for target in storage_targets:
        try:
            def _list_one() -> tuple[int, str]:
                with urllib.request.urlopen(target.list_url("", ""), timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8", errors="replace")[:200]

            status, body = await asyncio.to_thread(_list_one)
            _log("[probe]", f"blob list {target.base_url}: {status} body={body!r}")
        except Exception as exc:
            _log("[probe]", f"blob list {target.base_url} FAILED: {type(exc).__name__}: {exc}")

    _log("[probe]", "============ bridge-v2 probe end ============")


def _enabled_teams_bridges() -> list[BridgeConfig]:
    bridges = getattr(_config, "bridges", None) or []
    return [
        bridge for bridge in bridges
        if bool(_bridge_value(bridge, "enabled")) and _bridge_value(bridge, "type") == "teams"
    ]


@app.on_event("startup")
async def _start_bridges():
    global _event_queue, _event_worker_task
    _event_queue = asyncio.Queue(maxsize=EVENT_QUEUE_SIZE)
    _event_worker_task = asyncio.create_task(_event_worker())

    if not _config:
        _log("[bridge]", "no config loaded - call configure(config) before app startup")
        await _startup_probe([])
        return

    bridges = _enabled_teams_bridges()
    if not bridges:
        _log("[bridge]", "no enabled Teams bridges configured")
        await _startup_probe([])
        return

    storage_targets: list[StorageTarget] = []
    for bridge in bridges:
        storage = _resolve_storage_target(bridge)
        storage_targets.append(storage)

    await _startup_probe(storage_targets)

    for bridge, storage in zip(bridges, storage_targets, strict=True):
        task = asyncio.create_task(_teams_bridge_loop(bridge, storage))

        def _on_done(t: asyncio.Task, channel: str | None = _as_nonempty_str(_bridge_value(bridge, "channel"))) -> None:
            if t.cancelled():
                _log("[bridge]", f"task cancelled channel={channel!r}")
                return
            exc = t.exception()
            if exc is not None:
                _log("[bridge]", f"task CRASHED channel={channel!r} err={type(exc).__name__}: {exc}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)
            else:
                _log("[bridge]", f"task exited cleanly channel={channel!r}")

        task.add_done_callback(_on_done)
        _bridge_tasks.append(task)
        _log("[bridge]", f"registered v2 bridge channel={_bridge_value(bridge, 'channel')!r}")
