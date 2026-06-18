"""Intent Alignment demo consumer for Alfred v2 events.

This is a deliberately small, deployable variant of the reference sink. It
keeps the v2 event contract but changes the core job:

    hear something -> search known sources + persisted memories -> emit an
    intent-alignment readout -> persist useful memories

The index is dependency-free for now. It is a lexical scorer over immutable
sample source documents plus JSONL memories persisted under ``INTENT_DATA_DIR``.
That keeps the container simple while leaving one canonical seam for a later
Chroma or embeddings-backed implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field


DATA_DIR = Path(os.environ.get("INTENT_DATA_DIR", "/tmp/alfred-intent")).expanduser()
SPEECH_REFLECT_SECONDS = float(os.environ.get("INTENT_SPEECH_REFLECT_SECONDS", "12"))
CHAT_REFLECT_SECONDS = float(os.environ.get("INTENT_CHAT_REFLECT_SECONDS", "2"))
MAX_REFLECT_BATCH = int(os.environ.get("INTENT_MAX_REFLECT_BATCH", "12"))
SEND_CHAT_URL = (os.environ.get("INTENT_SEND_CHAT_URL") or os.environ.get("BOT_SEND_CHAT_URL") or "").strip()
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]*", re.IGNORECASE)
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "i",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "that",
    "the",
    "this",
    "to",
    "we",
    "with",
}


class SourceDocument(BaseModel):
    id: str
    source: str
    title: str
    summary: str
    text: str
    tags: list[str] = Field(default_factory=list)


class MemoryRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"mem_{uuid.uuid4().hex[:12]}")
    created_at_utc: str = Field(default_factory=lambda: utc_now())
    meeting_id: str | None = None
    thread_id: str | None = None
    speaker: str | None = None
    text: str
    reason: str
    source_event_id: str | None = None
    tags: list[str] = Field(default_factory=list)


class SearchHit(BaseModel):
    id: str
    kind: Literal["source", "memory"]
    source: str
    title: str
    score: float
    overlap_terms: list[str]
    summary: str
    text: str


class IntentSignal(BaseModel):
    kind: Literal[
        "decision",
        "action_item",
        "risk",
        "open_question",
        "contradiction",
        "confirmation",
        "memory_candidate",
    ]
    confidence: float
    evidence: str
    source_ids: list[str] = Field(default_factory=list)


class IntentAnalysis(BaseModel):
    analysis_id: str = Field(default_factory=lambda: f"ia_{uuid.uuid4().hex[:12]}")
    event_id: str | None = None
    event_type: str | None = None
    created_at_utc: str = Field(default_factory=lambda: utc_now())
    meeting_id: str | None = None
    thread_id: str | None = None
    speaker: str | None = None
    text: str
    alignment_state: Literal["aligned", "possible_misalignment", "needs_context", "no_signal"]
    rationale: str
    signals: list[IntentSignal] = Field(default_factory=list)
    hits: list[SearchHit] = Field(default_factory=list)
    persisted_memory: MemoryRecord | None = None
    observation_count: int = 1
    importance: Literal["low", "medium", "high"] = "low"
    next_action: Literal["keep_listening", "persist_memory", "retrieve_context", "respond"] = "keep_listening"
    search_queries: list[str] = Field(default_factory=list)
    response_text: str | None = None
    chat_posted: bool = False
    chat_post_error: str | None = None


class Observation(BaseModel):
    observation_id: str = Field(default_factory=lambda: f"obs_{uuid.uuid4().hex[:12]}")
    received_at_utc: str = Field(default_factory=lambda: utc_now())
    event_id: str | None = None
    event_type: str
    modality: Literal["speech", "chat"]
    text: str
    speaker: str | None = None
    meeting_id: str | None = None
    thread_id: str | None = None
    conversation_reference_id: str | None = None
    direct_address: bool = False
    from_bot: bool = False


class AnalyzeRequest(BaseModel):
    text: str
    speaker: str | None = None
    meeting_id: str | None = None
    thread_id: str | None = None
    event_id: str | None = None
    event_type: str | None = "manual.analysis"
    persist_memory: bool | None = None


class ManualMemoryRequest(BaseModel):
    text: str
    reason: str = "manual"
    speaker: str | None = None
    meeting_id: str | None = None
    thread_id: str | None = None
    source_event_id: str | None = None
    tags: list[str] = Field(default_factory=list)


@dataclass(slots=True)
class IndexedItem:
    id: str
    kind: Literal["source", "memory"]
    source: str
    title: str
    summary: str
    text: str
    tokens: Counter[str] = field(default_factory=Counter)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tokens(text: str) -> list[str]:
    return [
        t.lower()
        for t in TOKEN_RE.findall(text or "")
        if len(t) > 1 and t.lower() not in STOPWORDS
    ]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSONL in {path}:{line_no}: {exc}") from exc
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _append_jsonl(path: Path, payload: BaseModel | dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    row = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


SAMPLE_SOURCES = [
    SourceDocument(
        id="platform-contract-v2",
        source="alfred-platform",
        title="Only alfred-v2 envelopes are supported",
        summary="Downstream consumers should key on the v2 event contract and avoid v1 shims.",
        tags=["contract", "events", "v2"],
        text=(
            "The Teams bot emits alfred-v2 envelopes for channel and meeting events. "
            "The canonical meeting_id is the Teams meeting chat thread id. Consumers "
            "should read event_type, channel_ref, meeting_ref, conversation_reference_id, "
            "and payload. New consumers should not add v1 fallback branches."
        ),
    ),
    SourceDocument(
        id="persistence-postgres",
        source="alfred-ops",
        title="Durable ledger uses PostgreSQL",
        summary="PostgreSQL is the approved durable store; sqlite on Azure Files is not acceptable.",
        tags=["persistence", "postgres", "storage"],
        text=(
            "The reference Python sink persists to PostgreSQL through ALFRED_DB_URL. "
            "SQLite over Azure Files caused database locked failures because SMB does "
            "not provide the required advisory locks. Durable container deployments "
            "should use Postgres or another real service, not sqlite on SMB."
        ),
    ),
    SourceDocument(
        id="sidecar-routing",
        source="client-integration",
        title="Client-owned Alfreds register a consumer route",
        summary="Adding Alfred to a meeting captures it; routing to a client service requires a registered sink.",
        tags=["routing", "client", "consumer"],
        text=(
            "A client-owned Alfred should receive live POST delivery by registering a "
            "consumer URL or client route. Adding the Teams app to a private meeting "
            "does not by itself route events to Michael's service. Live push uses the "
            "configured sink_url; blob pull uses the canonical archive paths."
        ),
    ),
    SourceDocument(
        id="live-stt-finals",
        source="real-time-awareness",
        title="Live speech uses meeting.transcript.final",
        summary="Real-time awareness comes from Azure Speech finalized utterance segments, not post-meeting official transcripts.",
        tags=["speech", "realtime", "stt"],
        text=(
            "During a live meeting the bot emits meeting.transcript.final when "
            "Azure Speech ConversationTranscriber finalizes an utterance segment. Intent Alignment should "
            "reflect over these finalized live segments while people are talking. "
            "Microsoft's meeting.transcript.official arrives after the meeting "
            "ends and is not the mid-conversation awareness path."
        ),
    ),
    SourceDocument(
        id="intent-alignment-goal",
        source="intent-demo",
        title="Intent Alignment detection goal",
        summary="Detect when live discussion confirms, contradicts, or weakens known intent.",
        tags=["intent", "alignment", "memory"],
        text=(
            "Intent Alignment is not only note taking. It compares new statements "
            "against known goals, prior decisions, project constraints, and memories. "
            "When someone says something that agrees with, contradicts, or changes "
            "known intent, the agent should surface the relationship and remember "
            "the useful update."
        ),
    ),
]


class IntentStore:
    def __init__(self, data_dir: Path | str = DATA_DIR) -> None:
        self.data_dir = Path(data_dir).expanduser()
        self.memories_path = self.data_dir / "memories.jsonl"
        self.events_path = self.data_dir / "events.jsonl"
        self.analyses_path = self.data_dir / "analyses.jsonl"
        self._lock = threading.Lock()
        self.sources = list(SAMPLE_SOURCES)
        self.memories: list[MemoryRecord] = []
        self.analyses: list[IntentAnalysis] = []
        self._index: list[IndexedItem] = []
        self.load()

    def load(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.memories = [MemoryRecord(**row) for row in _read_jsonl(self.memories_path)]
        self.analyses = [IntentAnalysis(**row) for row in _read_jsonl(self.analyses_path)]
        self._rebuild_index()

    def _rebuild_index(self) -> None:
        items: list[IndexedItem] = []
        for doc in self.sources:
            blob = f"{doc.title}\n{doc.summary}\n{doc.text}\n{' '.join(doc.tags)}"
            items.append(
                IndexedItem(
                    id=doc.id,
                    kind="source",
                    source=doc.source,
                    title=doc.title,
                    summary=doc.summary,
                    text=doc.text,
                    tokens=Counter(_tokens(blob)),
                )
            )
        for memory in self.memories:
            blob = f"{memory.reason}\n{memory.text}\n{' '.join(memory.tags)}"
            title = f"Memory from {memory.speaker or 'unknown'}"
            items.append(
                IndexedItem(
                    id=memory.id,
                    kind="memory",
                    source="memory",
                    title=title,
                    summary=memory.reason,
                    text=memory.text,
                    tokens=Counter(_tokens(blob)),
                )
            )
        self._index = items

    def source_overview(self) -> dict[str, Any]:
        buckets: dict[str, dict[str, Any]] = {}
        for item in self._index:
            bucket = buckets.setdefault(
                item.source,
                {"source": item.source, "documents": 0, "top_terms": Counter()},
            )
            bucket["documents"] += 1
            bucket["top_terms"].update(item.tokens)
        return {
            "sources": [
                {
                    "source": value["source"],
                    "documents": value["documents"],
                    "top_terms": [term for term, _count in value["top_terms"].most_common(12)],
                }
                for value in sorted(buckets.values(), key=lambda row: row["source"])
            ],
            "document_count": len(self.sources),
            "memory_count": len(self.memories),
        }

    def search(self, query: str, limit: int = 5) -> list[SearchHit]:
        query_tokens = Counter(_tokens(query))
        if not query_tokens:
            return []
        hits: list[SearchHit] = []
        for item in self._index:
            overlap = query_tokens.keys() & item.tokens.keys()
            if not overlap:
                continue
            weighted = sum(query_tokens[t] * item.tokens[t] for t in overlap)
            coverage = len(overlap) / max(len(query_tokens), 1)
            score = round(weighted + coverage, 4)
            hits.append(
                SearchHit(
                    id=item.id,
                    kind=item.kind,
                    source=item.source,
                    title=item.title,
                    score=score,
                    overlap_terms=sorted(overlap),
                    summary=item.summary,
                    text=item.text,
                )
            )
        hits.sort(key=lambda hit: (hit.score, hit.kind == "source"), reverse=True)
        return hits[: max(1, min(limit, 20))]

    def append_event(self, envelope: dict[str, Any]) -> None:
        with self._lock:
            _append_jsonl(
                self.events_path,
                {
                    "received_at_utc": utc_now(),
                    "envelope": envelope,
                },
            )

    def append_memory(self, memory: MemoryRecord) -> MemoryRecord:
        with self._lock:
            self.memories.append(memory)
            _append_jsonl(self.memories_path, memory)
            self._rebuild_index()
        return memory

    def append_analysis(self, analysis: IntentAnalysis) -> IntentAnalysis:
        with self._lock:
            self.analyses.append(analysis)
            _append_jsonl(self.analyses_path, analysis)
        return analysis


def _first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _is_direct_address(text: str | None) -> bool:
    return bool(text and "alfred" in text.lower())


def _normalize_event(envelope: dict[str, Any]) -> list[Observation]:
    event_type = _first_text(envelope.get("event_type")) or ""
    event_id = _first_text(envelope.get("event_id"))
    conversation_reference_id = _first_text(envelope.get("conversation_reference_id"))
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    channel_ref = envelope.get("channel_ref") if isinstance(envelope.get("channel_ref"), dict) else {}
    meeting_ref = envelope.get("meeting_ref") if isinstance(envelope.get("meeting_ref"), dict) else {}

    meeting_id = _first_text(
        meeting_ref.get("meeting_id"),
        meeting_ref.get("meeting_chat_thread_id"),
        payload.get("meeting_id"),
    )
    thread_id = _first_text(
        channel_ref.get("thread_id"),
        channel_ref.get("channel_id"),
        meeting_ref.get("meeting_chat_thread_id"),
        meeting_ref.get("meeting_id"),
    )

    if event_type in {"meeting.chat.created", "channel.message.created"}:
        sender = payload.get("sender") if isinstance(payload.get("sender"), dict) else {}
        text = _first_text(payload.get("text"), payload.get("body"), "") or ""
        return [
            Observation(
                event_id=event_id,
                event_type=event_type,
                modality="chat",
                text=text,
                speaker=_first_text(sender.get("display_name"), sender.get("aad_id")),
                meeting_id=meeting_id,
                thread_id=thread_id,
                conversation_reference_id=conversation_reference_id,
                direct_address=_is_direct_address(text),
                from_bot=bool(payload.get("from_bot")),
            )
        ]

    if event_type == "meeting.transcript.final":
        speaker = payload.get("speaker") if isinstance(payload.get("speaker"), dict) else {}
        text = _first_text(payload.get("text"), "") or ""
        return [
            Observation(
                event_id=event_id,
                event_type=event_type,
                modality="speech",
                text=text,
                speaker=_first_text(speaker.get("display_name"), speaker.get("id"), speaker.get("aad_id")),
                meeting_id=meeting_id,
                thread_id=thread_id,
                conversation_reference_id=conversation_reference_id,
                direct_address=_is_direct_address(text),
                from_bot=False,
            )
        ]

    return []


DECISION_PATTERNS = (
    "we decided",
    "we agreed",
    "decision",
    "let's use",
    "lets use",
    "go with",
    "we will",
)
ACTION_PATTERNS = ("i will", "i'll", "i can", "i got", "i’ll", "owner", "by friday", "by eod")
RISK_PATTERNS = ("risk", "worried", "concern", "blocked", "blocker", "could break", "slip", "delay")
QUESTION_PATTERNS = ("?", "how do we", "what about", "who owns", "do we know", "should we")
CONTRADICTION_PATTERNS = (
    "actually",
    "instead",
    "not postgres",
    "not v2",
    "back to v1",
    "sqlite",
    "skip",
    "don't need",
    "do not need",
)
CONFIRMATION_PATTERNS = ("agreed", "+1", "sounds good", "yes", "confirmed", "that works")


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in patterns)


def _looks_contradictory(text: str) -> bool:
    lower = text.lower()
    if any(phrase in lower for phrase in ("avoid sqlite", "no sqlite", "not sqlite", "don't use sqlite", "do not use sqlite")):
        return False
    return _contains_any(text, CONTRADICTION_PATTERNS)


def _signal_from_hits(kind: str, confidence: float, evidence: str, hits: list[SearchHit]) -> IntentSignal:
    return IntentSignal(
        kind=kind,  # type: ignore[arg-type]
        confidence=confidence,
        evidence=evidence,
        source_ids=[hit.id for hit in hits[:3]],
    )


def _should_persist_memory(text: str, signals: list[IntentSignal], explicit: bool | None) -> tuple[bool, str]:
    if explicit is not None:
        return explicit, "explicit_request" if explicit else "explicit_skip"
    if _contains_any(text, DECISION_PATTERNS):
        return True, "decision-like statement"
    if _contains_any(text, ACTION_PATTERNS):
        return True, "action-like statement"
    if any(signal.kind in {"contradiction", "confirmation"} for signal in signals):
        return True, "alignment-changing statement"
    if "remember" in text.lower():
        return True, "remember instruction"
    return False, "not memory-worthy yet"


def analyze_intent(
    store: IntentStore,
    *,
    text: str,
    speaker: str | None = None,
    meeting_id: str | None = None,
    thread_id: str | None = None,
    event_id: str | None = None,
    event_type: str | None = None,
    persist_memory: bool | None = None,
    record: bool = True,
) -> IntentAnalysis:
    body = (text or "").strip()
    if not body:
        raise ValueError("text is required")

    hits = store.search(body, limit=6)
    signals: list[IntentSignal] = []

    if _contains_any(body, DECISION_PATTERNS):
        signals.append(_signal_from_hits("decision", 0.78, "Statement looks like a decision or proposed direction.", hits))
    if _contains_any(body, ACTION_PATTERNS):
        signals.append(_signal_from_hits("action_item", 0.72, "Statement looks like an ownership or follow-up commitment.", hits))
    if _contains_any(body, RISK_PATTERNS):
        signals.append(_signal_from_hits("risk", 0.74, "Statement raises a concern, blocker, or delivery risk.", hits))
    if _contains_any(body, QUESTION_PATTERNS):
        signals.append(_signal_from_hits("open_question", 0.65, "Statement asks for missing alignment context.", hits))
    if _looks_contradictory(body) and hits:
        signals.append(_signal_from_hits("contradiction", 0.82, "Statement may conflict with indexed source or memory context.", hits))
    if _contains_any(body, CONFIRMATION_PATTERNS) and hits:
        signals.append(_signal_from_hits("confirmation", 0.7, "Statement appears to confirm indexed context.", hits))

    should_remember, memory_reason = _should_persist_memory(body, signals, persist_memory)
    if should_remember:
        signals.append(
            IntentSignal(
                kind="memory_candidate",
                confidence=0.68,
                evidence=f"Persisting memory because: {memory_reason}.",
                source_ids=[hit.id for hit in hits[:3]],
            )
        )

    if any(signal.kind == "contradiction" for signal in signals):
        alignment_state: Literal["aligned", "possible_misalignment", "needs_context", "no_signal"] = "possible_misalignment"
        rationale = "New statement may conflict with known intent; review the top hits before acting."
    elif signals and hits:
        alignment_state = "aligned"
        rationale = "New statement has matching source or memory context and no contradiction signal."
    elif signals:
        alignment_state = "needs_context"
        rationale = "Intent signal detected, but the index has weak supporting context."
    else:
        alignment_state = "no_signal"
        rationale = "No strong intent-alignment signal detected."

    memory: MemoryRecord | None = None
    if should_remember:
        memory = store.append_memory(
            MemoryRecord(
                meeting_id=meeting_id,
                thread_id=thread_id,
                speaker=speaker,
                text=body,
                reason=memory_reason,
                source_event_id=event_id,
                tags=sorted({signal.kind for signal in signals}),
            )
        )

    analysis = IntentAnalysis(
        event_id=event_id,
        event_type=event_type,
        meeting_id=meeting_id,
        thread_id=thread_id,
        speaker=speaker,
        text=body,
        alignment_state=alignment_state,
        rationale=rationale,
        signals=signals,
        hits=hits,
        persisted_memory=memory,
    )
    if record:
        return store.append_analysis(analysis)
    return analysis


def _conversation_key(observation: Observation) -> str:
    if observation.meeting_id:
        return f"meeting:{observation.meeting_id}"
    if observation.thread_id:
        return f"thread:{observation.thread_id}"
    return "global"


def _format_observation_text(observations: list[Observation]) -> str:
    lines: list[str] = []
    for obs in observations:
        who = obs.speaker or "unknown"
        lines.append(f"{who}: {obs.text.strip()}")
    return "\n".join(lines)


def _search_queries_for_text(text: str) -> list[str]:
    tokens = [t for t in _tokens(text) if len(t) > 2]
    unique: list[str] = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    if not unique:
        return []
    queries = [" ".join(unique[:8])]
    if len(unique) > 8:
        queries.append(" ".join(unique[8:16]))
    return queries


def _importance_for(signals: list[IntentSignal], direct_address: bool) -> Literal["low", "medium", "high"]:
    kinds = {signal.kind for signal in signals}
    if direct_address or "contradiction" in kinds or "risk" in kinds:
        return "high"
    if kinds & {"decision", "action_item", "open_question", "confirmation", "memory_candidate"}:
        return "medium"
    return "low"


def _response_for(analysis: IntentAnalysis, direct_address: bool) -> str | None:
    top_hit = analysis.hits[0] if analysis.hits else None
    if any(signal.kind == "contradiction" for signal in analysis.signals) and top_hit is not None:
        return (
            f"Quick check: I have prior context saying {top_hit.summary} "
            "Are we intentionally changing that direction?"
        )
    if direct_address and top_hit is not None:
        return f"I found relevant context: {top_hit.summary}"
    if direct_address:
        return "I heard you, but I do not have enough indexed context to answer confidently yet."
    return None


async def _post_chat_response(
    *,
    send_chat_url: str,
    conversation_reference_id: str | None,
    text: str | None,
) -> tuple[bool, str | None]:
    if not text:
        return False, None
    if not send_chat_url:
        return False, "INTENT_SEND_CHAT_URL is not configured"
    if not conversation_reference_id:
        return False, "conversation_reference_id missing"
    payload = {
        "conversation_reference_id": conversation_reference_id,
        "text": text,
        "action": "SEND",
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(send_chat_url, json=payload)
        if response.status_code >= 400:
            return False, f"HTTP {response.status_code}: {response.text[:160]}"
    except Exception as exc:  # noqa: BLE001 - chat send failures are recorded on the analysis.
        return False, f"transport: {exc!s}"
    return True, None


async def reflect_observations(
    store: IntentStore,
    observations: list[Observation],
    *,
    send_chat_url: str = "",
    persist_memory: bool | None = None,
) -> IntentAnalysis | None:
    useful = [obs for obs in observations if not obs.from_bot and obs.text.strip()]
    if not useful:
        return None

    combined_text = _format_observation_text(useful)
    direct_address = any(obs.direct_address for obs in useful)
    latest = useful[-1]
    analysis = analyze_intent(
        store,
        text=combined_text,
        speaker=latest.speaker if len(useful) == 1 else None,
        meeting_id=latest.meeting_id,
        thread_id=latest.thread_id,
        event_id=latest.event_id,
        event_type=latest.event_type if len(useful) == 1 else "reflection.batch",
        persist_memory=persist_memory,
        record=False,
    )
    analysis.observation_count = len(useful)
    analysis.search_queries = _search_queries_for_text(combined_text)
    analysis.importance = _importance_for(analysis.signals, direct_address)
    analysis.response_text = _response_for(analysis, direct_address)

    if analysis.response_text:
        analysis.next_action = "respond"
    elif analysis.hits and analysis.importance in {"medium", "high"}:
        analysis.next_action = "retrieve_context"
    elif analysis.persisted_memory is not None:
        analysis.next_action = "persist_memory"
    else:
        analysis.next_action = "keep_listening"

    if analysis.next_action == "respond":
        conversation_reference_id = next(
            (obs.conversation_reference_id for obs in reversed(useful) if obs.conversation_reference_id),
            None,
        )
        posted, error = await _post_chat_response(
            send_chat_url=send_chat_url,
            conversation_reference_id=conversation_reference_id,
            text=analysis.response_text,
        )
        analysis.chat_posted = posted
        analysis.chat_post_error = error

    store.append_analysis(analysis)
    return analysis


class ReflectionLoop:
    def __init__(
        self,
        store: IntentStore,
        *,
        send_chat_url: str = "",
        speech_delay_seconds: float = SPEECH_REFLECT_SECONDS,
        chat_delay_seconds: float = CHAT_REFLECT_SECONDS,
        max_batch: int = MAX_REFLECT_BATCH,
    ) -> None:
        self.store = store
        self.send_chat_url = send_chat_url
        self.speech_delay_seconds = max(0.1, speech_delay_seconds)
        self.chat_delay_seconds = max(0.1, chat_delay_seconds)
        self.max_batch = max(1, max_batch)
        self._pending: dict[str, list[Observation]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return sum(len(rows) for rows in self._pending.values())

    async def submit_many(self, observations: list[Observation]) -> dict[str, int]:
        queued = 0
        skipped = 0
        for observation in observations:
            if observation.from_bot or not observation.text.strip():
                skipped += 1
                continue
            await self.submit(observation)
            queued += 1
        return {"queued": queued, "skipped": skipped}

    async def submit(self, observation: Observation) -> None:
        key = _conversation_key(observation)
        immediate = observation.direct_address or _looks_contradictory(observation.text)
        async with self._lock:
            pending = self._pending.setdefault(key, [])
            pending.append(observation)
            should_flush = (
                immediate
                or len(pending) >= self.max_batch
            )
            if should_flush:
                self._cancel_timer_locked(key)
                asyncio.create_task(self.flush_key(key))
                return

            delay = self.chat_delay_seconds if observation.modality == "chat" else self.speech_delay_seconds
            self._schedule_timer_locked(key, delay)

    def _cancel_timer_locked(self, key: str) -> None:
        task = self._timers.pop(key, None)
        if task and not task.done():
            task.cancel()

    def _schedule_timer_locked(self, key: str, delay: float) -> None:
        self._cancel_timer_locked(key)
        self._timers[key] = asyncio.create_task(self._delayed_flush(key, delay))

    async def _delayed_flush(self, key: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            await self.flush_key(key)
        except asyncio.CancelledError:
            return

    async def flush_key(self, key: str) -> IntentAnalysis | None:
        async with self._lock:
            observations = self._pending.pop(key, [])
            self._cancel_timer_locked(key)
        if not observations:
            return None
        return await reflect_observations(
            self.store,
            observations,
            send_chat_url=self.send_chat_url,
        )

    async def flush_all(self) -> list[IntentAnalysis]:
        async with self._lock:
            keys = list(self._pending.keys())
        analyses: list[IntentAnalysis] = []
        for key in keys:
            analysis = await self.flush_key(key)
            if analysis is not None:
                analyses.append(analysis)
        return analyses

    async def close(self) -> None:
        async with self._lock:
            tasks = list(self._timers.values())
            self._timers.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


def create_app(store: IntentStore | None = None) -> FastAPI:
    intent_store = store or IntentStore()
    reflector = ReflectionLoop(intent_store, send_chat_url=SEND_CHAT_URL)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            await reflector.close()

    app = FastAPI(
        title="Alfred Intent Alignment Sink",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.intent_store = intent_store
    app.state.reflector = reflector

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "intent-alignment",
            "data_dir": str(intent_store.data_dir),
            "sources": len(intent_store.sources),
            "memories": len(intent_store.memories),
            "analyses": len(intent_store.analyses),
            "pending_observations": reflector.pending_count,
            "speech_reflect_seconds": reflector.speech_delay_seconds,
            "chat_reflect_seconds": reflector.chat_delay_seconds,
        }

    @app.get("/sources")
    async def sources() -> dict[str, Any]:
        return intent_store.source_overview()

    @app.get("/search")
    async def search(q: str = Query(..., min_length=1), limit: int = 5) -> dict[str, Any]:
        return {"query": q, "hits": [hit.model_dump() for hit in intent_store.search(q, limit=limit)]}

    @app.get("/memories")
    async def memories(limit: int = 50) -> dict[str, Any]:
        rows = intent_store.memories[-max(1, min(limit, 500)) :]
        return {"memories": [row.model_dump(mode="json") for row in rows]}

    @app.post("/memories")
    async def add_memory(request: ManualMemoryRequest) -> dict[str, Any]:
        memory = intent_store.append_memory(MemoryRecord(**request.model_dump()))
        return {"ok": True, "memory": memory.model_dump(mode="json")}

    @app.get("/analyses")
    async def analyses(limit: int = 50) -> dict[str, Any]:
        rows = intent_store.analyses[-max(1, min(limit, 500)) :]
        return {"analyses": [row.model_dump(mode="json") for row in rows]}

    @app.get("/prompt")
    async def prompt_policy() -> dict[str, Any]:
        return {
            "role": "Intent Alignment real-time reflector",
            "cadence": {
                "speech": (
                    "Reflect after the live speech stream has been quiet for "
                    f"{reflector.speech_delay_seconds:g}s, or sooner on direct address, "
                    "contradiction-like language, or max batch."
                ),
                "chat": (
                    "Reflect after a short chat burst quiets for "
                    f"{reflector.chat_delay_seconds:g}s, or immediately on direct address."
                ),
            },
            "default_action": "keep_listening",
            "actions": ["keep_listening", "persist_memory", "retrieve_context", "respond"],
            "speak_only_when": [
                "directly addressed",
                "retrieved context indicates a likely contradiction",
                "a missing owner or context would materially block alignment",
            ],
        }

    @app.post("/analyze")
    async def analyze(request: AnalyzeRequest) -> dict[str, Any]:
        try:
            observation = Observation(
                event_id=request.event_id,
                event_type=request.event_type or "manual.analysis",
                modality="chat",
                text=request.text,
                speaker=request.speaker,
                meeting_id=request.meeting_id,
                thread_id=request.thread_id,
                direct_address=_is_direct_address(request.text),
            )
            analysis = await reflect_observations(
                intent_store,
                [observation],
                send_chat_url=SEND_CHAT_URL,
                persist_memory=request.persist_memory,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if analysis is None:
            raise HTTPException(status_code=400, detail="No useful observation to analyze")
        return {"ok": True, "analysis": analysis.model_dump(mode="json")}

    @app.post("/reflect/flush")
    async def flush_reflection() -> dict[str, Any]:
        analyses_out = await reflector.flush_all()
        return {
            "ok": True,
            "analyses": [analysis.model_dump(mode="json") for analysis in analyses_out],
            "pending_observations": reflector.pending_count,
        }

    @app.post("/v2/events")
    async def ingest_v2_event(request: Request) -> dict[str, Any]:
        envelope = await request.json()
        if not isinstance(envelope, dict):
            raise HTTPException(status_code=400, detail="Expected a JSON envelope object")
        intent_store.append_event(envelope)

        observations = _normalize_event(envelope)
        queued = await reflector.submit_many(observations)

        return {
            "ok": True,
            "event_id": envelope.get("event_id"),
            "event_type": envelope.get("event_type"),
            "observations": len(observations),
            **queued,
            "pending_observations": reflector.pending_count,
        }

    @app.post("/events")
    async def ingest_event_alias(request: Request) -> dict[str, Any]:
        return await ingest_v2_event(request)

    return app


app = create_app()
