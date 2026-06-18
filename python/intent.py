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
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field

from meeting_agent.session import InterviewSessionManager
from meeting_agent.tools import AlfredAgentContext, SendResult, send_to_meeting_chat_impl


DATA_DIR = Path(os.environ.get("INTENT_DATA_DIR", "/tmp/alfred-intent")).expanduser()
SPEECH_REFLECT_SECONDS = float(os.environ.get("INTENT_SPEECH_REFLECT_SECONDS", "1"))
CHAT_REFLECT_SECONDS = float(os.environ.get("INTENT_CHAT_REFLECT_SECONDS", "1"))
MAX_REFLECT_BATCH = int(os.environ.get("INTENT_MAX_REFLECT_BATCH", "12"))
ROLLING_BUFFER_SIZE = int(os.environ.get("INTENT_ROLLING_BUFFER_SIZE", "60"))
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
    context_text: str | None = None
    context_observation_count: int = 0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
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


class ActivityRecord(BaseModel):
    id: str = Field(default_factory=lambda: f"act_{uuid.uuid4().hex[:12]}")
    created_at_utc: str = Field(default_factory=lambda: utc_now())
    kind: Literal["observation", "status", "analysis", "ignored_event"]
    text: str
    event_id: str | None = None
    event_type: str | None = None
    modality: Literal["speech", "chat"] | None = None
    speaker: str | None = None
    meeting_id: str | None = None
    thread_id: str | None = None
    alignment_state: str | None = None
    next_action: str | None = None
    search_query: str | None = None
    source_ids: list[str] = Field(default_factory=list)


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


class ActivityLog:
    def __init__(self, maxlen: int = 250) -> None:
        self._history: deque[ActivityRecord] = deque(maxlen=maxlen)
        self._subscribers: set[asyncio.Queue[ActivityRecord]] = set()
        self._lock = threading.Lock()

    def append(self, record: ActivityRecord) -> ActivityRecord:
        stale: list[asyncio.Queue[ActivityRecord]] = []
        with self._lock:
            self._history.append(record)
            subscribers = list(self._subscribers)
        for queue in subscribers:
            try:
                queue.put_nowait(record)
            except asyncio.QueueFull:
                stale.append(queue)
        if stale:
            with self._lock:
                for queue in stale:
                    self._subscribers.discard(queue)
        return record

    def snapshot(self, limit: int = 50) -> list[ActivityRecord]:
        with self._lock:
            rows = list(self._history)
        return rows[-max(1, min(limit, 200)) :]

    def subscribe(self) -> asyncio.Queue[ActivityRecord]:
        queue: asyncio.Queue[ActivityRecord] = asyncio.Queue(maxsize=100)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ActivityRecord]) -> None:
        with self._lock:
            self._subscribers.discard(queue)


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


def _tail(rows: list[Any], limit: int) -> list[Any]:
    return rows[-max(1, min(limit, 100)) :]


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
STACK_CHANGE_PATTERNS = (
    "should use",
    "should be using",
    "think we should",
    "i think we should",
    "use ",
    "using ",
    "go with",
    "switch to",
    "move to",
    "instead",
)
DATABASE_ALIASES: dict[str, tuple[str, ...]] = {
    "postgres": ("postgres", "postgresql"),
    "dynamodb": ("dynamodb", "dynamo db"),
    "sqlite": ("sqlite", "sqllite"),
    "mysql": ("mysql", "my sql"),
    "sqlserver": ("sql server", "sqlserver", "mssql"),
    "cosmosdb": ("cosmos db", "cosmosdb"),
    "mongodb": ("mongodb", "mongo db"),
}
DATABASE_LABELS = {
    "postgres": "Postgres",
    "dynamodb": "DynamoDB",
    "sqlite": "SQLite",
    "mysql": "MySQL",
    "sqlserver": "SQL Server",
    "cosmosdb": "Cosmos DB",
    "mongodb": "MongoDB",
}


def _contains_any(text: str, patterns: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(pattern in lower for pattern in patterns)


def _looks_contradictory(text: str) -> bool:
    lower = text.lower()
    if any(phrase in lower for phrase in ("avoid sqlite", "no sqlite", "not sqlite", "don't use sqlite", "do not use sqlite")):
        return False
    return _contains_any(text, CONTRADICTION_PATTERNS)


def _mentioned_databases(text: str | None) -> set[str]:
    lower = (text or "").lower()
    return {
        canonical
        for canonical, aliases in DATABASE_ALIASES.items()
        if any(alias in lower for alias in aliases)
    }


def _hit_text(hit: SearchHit) -> str:
    return f"{hit.title}\n{hit.summary}\n{hit.text}"


def _database_conflict(
    text: str,
    hits: list[SearchHit],
    context_text: str | None = None,
) -> tuple[str, str, SearchHit | None] | None:
    proposed = _mentioned_databases(text)
    if not proposed:
        return None
    if not (_contains_any(text, STACK_CHANGE_PATTERNS) or _contains_any(text, QUESTION_PATTERNS)):
        return None

    indexed_context = "\n".join(_hit_text(hit) for hit in hits)
    known = _mentioned_databases(indexed_context)
    known.update(_mentioned_databases(context_text) - proposed)
    conflicts = known - proposed
    if not conflicts:
        return None

    known_choice = "postgres" if "postgres" in conflicts else sorted(conflicts)[0]
    proposed_choice = "dynamodb" if "dynamodb" in proposed else sorted(proposed)[0]
    if known_choice == proposed_choice:
        return None

    conflict_hit = next(
        (hit for hit in hits if known_choice in _mentioned_databases(_hit_text(hit))),
        hits[0] if hits else None,
    )
    return known_choice, proposed_choice, conflict_hit


def _is_search_worthy(text: str, direct_address: bool) -> bool:
    return (
        direct_address
        or "remember" in text.lower()
        or bool(_mentioned_databases(text))
        or _contains_any(text, DECISION_PATTERNS)
        or _contains_any(text, ACTION_PATTERNS)
        or _contains_any(text, RISK_PATTERNS)
        or _contains_any(text, QUESTION_PATTERNS)
        or _looks_contradictory(text)
        or _contains_any(text, CONFIRMATION_PATTERNS)
    )


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
    context_text: str | None = None,
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

    retrieval_text = _reflection_text(body, context_text)
    hits = store.search(retrieval_text, limit=6)
    signals: list[IntentSignal] = []
    database_conflict = _database_conflict(body, hits, context_text)

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
    if database_conflict is not None:
        known_choice, proposed_choice, conflict_hit = database_conflict
        source_ids = [conflict_hit.id] if conflict_hit is not None else []
        signals.append(
            IntentSignal(
                kind="contradiction",
                confidence=0.86,
                evidence=(
                    f"Statement proposes {DATABASE_LABELS[proposed_choice]} while known context "
                    f"points to {DATABASE_LABELS[known_choice]}."
                ),
                source_ids=source_ids,
            )
        )
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
        context_text=(context_text or None),
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


def _observation_snapshot(observation: Observation) -> dict[str, Any]:
    return {
        "received_at_utc": observation.received_at_utc,
        "event_id": observation.event_id,
        "event_type": observation.event_type,
        "modality": observation.modality,
        "speaker": observation.speaker,
        "direct_address": observation.direct_address,
        "text": observation.text,
    }


def _format_observation_text(observations: list[Observation]) -> str:
    lines: list[str] = []
    for obs in observations:
        who = obs.speaker or "unknown"
        lines.append(f"{who}: {obs.text.strip()}")
    return "\n".join(lines)


def _format_context_text(observations: list[Observation]) -> str:
    useful = [obs for obs in observations if not obs.from_bot and obs.text.strip()]
    return _format_observation_text(useful)


def _reflection_text(current_text: str, context_text: str | None) -> str:
    context = (context_text or "").strip()
    current = current_text.strip()
    if not context or context == current:
        return current
    return f"Conversation so far:\n{context}\n\nLatest observations:\n{current}"


def _compact_topic(text: str, limit: int = 140) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def _status_topic(observations: list[Observation], fallback: str) -> str:
    spoken = " ".join(obs.text.strip() for obs in observations if obs.text.strip())
    if spoken:
        return _compact_topic(spoken)
    return _compact_topic(fallback)


def _importance_for(signals: list[IntentSignal], direct_address: bool) -> Literal["low", "medium", "high"]:
    kinds = {signal.kind for signal in signals}
    if direct_address or "contradiction" in kinds or "risk" in kinds:
        return "high"
    if kinds & {"decision", "action_item", "open_question", "confirmation", "memory_candidate"}:
        return "medium"
    return "low"


def _response_for(analysis: IntentAnalysis, direct_address: bool) -> str | None:
    top_hit = analysis.hits[0] if analysis.hits else None
    database_conflict = _database_conflict(analysis.text, analysis.hits, analysis.context_text)
    if database_conflict is not None:
        known_choice, proposed_choice, _hit = database_conflict
        return (
            "Quick check: I have prior context that we already decided on "
            f"{DATABASE_LABELS[known_choice]}. Are we intentionally changing that to "
            f"{DATABASE_LABELS[proposed_choice]}?"
        )
    if any(signal.kind == "contradiction" for signal in analysis.signals) and top_hit is not None:
        return (
            f"Quick check: I have prior context saying {top_hit.summary} "
            "Are we intentionally changing that direction?"
        )
    if any(signal.kind == "contradiction" for signal in analysis.signals):
        contradiction = next(signal for signal in analysis.signals if signal.kind == "contradiction")
        return (
            f"Quick check: I heard a possible conflict: {contradiction.evidence} "
            "Are we intentionally changing direction?"
        )
    if analysis.alignment_state == "possible_misalignment":
        return "Quick check: I heard a possible conflict with prior intent. Are we intentionally changing direction?"
    if direct_address and top_hit is not None:
        return f"I found relevant context: {top_hit.summary}"
    if direct_address:
        return "I heard you, but I do not have enough indexed context to answer confidently yet."
    return None


def _build_tool_context(
    observations: list[Observation],
    *,
    send_chat_url: str,
    trigger_text: str,
) -> AlfredAgentContext:
    latest = observations[-1]
    conversation_reference_id = next(
        (obs.conversation_reference_id for obs in reversed(observations) if obs.conversation_reference_id),
        None,
    )
    chat_thread_id = conversation_reference_id or latest.meeting_id or latest.thread_id
    manager = InterviewSessionManager()
    session = manager.start_session(
        candidate_name="Intent Alignment",
        meeting_url="alfred-v2-live-event",
        chat_thread_id=chat_thread_id,
    )
    session.conversation_reference_id = conversation_reference_id or chat_thread_id
    session.graph_chat_thread_id = chat_thread_id
    return AlfredAgentContext(
        session_manager=manager,
        send_chat_url=send_chat_url or None,
        trigger_text=trigger_text,
    )


async def _send_chat_response_with_tool(
    *,
    send_chat_url: str,
    observations: list[Observation],
    text: str | None,
) -> tuple[bool, str | None, list[dict[str, Any]]]:
    if not text:
        return False, None, []
    context = _build_tool_context(observations, send_chat_url=send_chat_url, trigger_text=text)
    result: SendResult = await send_to_meeting_chat_impl(context, text=text, kind="statement")
    return result.ok, result.reason, [record.model_dump(mode="json") for record in context.tool_records]


async def reflect_observations(
    store: IntentStore,
    observations: list[Observation],
    *,
    context_observations: list[Observation] | None = None,
    send_chat_url: str = "",
    persist_memory: bool | None = None,
    activity_log: ActivityLog | None = None,
) -> IntentAnalysis | None:
    useful = [obs for obs in observations if not obs.from_bot and obs.text.strip()]
    if not useful:
        return None

    combined_text = _format_observation_text(useful)
    context_text = _format_context_text(context_observations or useful)
    direct_address = any(obs.direct_address for obs in useful)
    latest = useful[-1]
    search_queries = [_status_topic(useful, combined_text)]
    if activity_log is not None:
        if _is_search_worthy(combined_text, direct_address):
            topic = search_queries[0]
            activity_log.append(
                ActivityRecord(
                    kind="status",
                    text=f"Searching sources to see if anything on {topic}",
                    event_id=latest.event_id,
                    event_type=latest.event_type if len(useful) == 1 else "reflection.batch",
                    meeting_id=latest.meeting_id,
                    thread_id=latest.thread_id,
                    search_query=topic,
                )
            )
        else:
            activity_log.append(
                ActivityRecord(
                    kind="status",
                    text="Haven't heard anything worth searching",
                    event_id=latest.event_id,
                    event_type=latest.event_type if len(useful) == 1 else "reflection.batch",
                    meeting_id=latest.meeting_id,
                    thread_id=latest.thread_id,
                )
            )

    analysis = analyze_intent(
        store,
        text=combined_text,
        context_text=context_text,
        speaker=latest.speaker if len(useful) == 1 else None,
        meeting_id=latest.meeting_id,
        thread_id=latest.thread_id,
        event_id=latest.event_id,
        event_type=latest.event_type if len(useful) == 1 else "reflection.batch",
        persist_memory=persist_memory,
        record=False,
    )
    analysis.observation_count = len(useful)
    analysis.context_observation_count = len(
        [obs for obs in (context_observations or useful) if not obs.from_bot and obs.text.strip()]
    )
    analysis.search_queries = search_queries
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
        posted, error, tool_calls = await _send_chat_response_with_tool(
            send_chat_url=send_chat_url,
            observations=useful,
            text=analysis.response_text,
        )
        analysis.chat_posted = posted
        analysis.chat_post_error = error
        analysis.tool_calls = tool_calls

    store.append_analysis(analysis)
    if activity_log is not None:
        activity_log.append(
            ActivityRecord(
                kind="analysis",
                text=analysis.rationale,
                event_id=analysis.event_id,
                event_type=analysis.event_type,
                speaker=analysis.speaker,
                meeting_id=analysis.meeting_id,
                thread_id=analysis.thread_id,
                alignment_state=analysis.alignment_state,
                next_action=analysis.next_action,
                search_query=analysis.search_queries[0] if analysis.search_queries else None,
                source_ids=[hit.id for hit in analysis.hits[:3]],
            )
        )
    return analysis


class ReflectionLoop:
    def __init__(
        self,
        store: IntentStore,
        *,
        send_chat_url: str = "",
        activity_log: ActivityLog | None = None,
        speech_delay_seconds: float = SPEECH_REFLECT_SECONDS,
        chat_delay_seconds: float = CHAT_REFLECT_SECONDS,
        max_batch: int = MAX_REFLECT_BATCH,
        rolling_buffer_size: int = ROLLING_BUFFER_SIZE,
    ) -> None:
        self.store = store
        self.send_chat_url = send_chat_url
        self.activity_log = activity_log
        self.speech_delay_seconds = max(0.1, speech_delay_seconds)
        self.chat_delay_seconds = max(0.1, chat_delay_seconds)
        self.max_batch = max(1, max_batch)
        self.rolling_buffer_size = max(1, rolling_buffer_size)
        self._pending: dict[str, list[Observation]] = {}
        self._history: dict[str, list[Observation]] = {}
        self._rolling: dict[str, deque[Observation]] = {}
        self._timers: dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()

    @property
    def pending_count(self) -> int:
        return sum(len(rows) for rows in self._pending.values())

    async def snapshot(self) -> dict[str, Any]:
        async with self._lock:
            conversations = []
            for key, rows in sorted(self._pending.items()):
                conversations.append(
                    {
                        "key": key,
                        "observations": [_observation_snapshot(row) for row in rows],
                    }
                )
            rolling = []
            for key, rows in sorted(self._rolling.items()):
                rolling.append(
                    {
                        "key": key,
                        "observations": [_observation_snapshot(row) for row in rows],
                        "total_context_observations": len(self._history.get(key, [])),
                    }
                )
            return {
                "count": sum(len(row["observations"]) for row in conversations),
                "conversations": conversations,
                "rolling": rolling,
            }

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
            history = self._history.setdefault(key, [])
            history.append(observation)
            rolling = self._rolling.setdefault(key, deque(maxlen=self.rolling_buffer_size))
            rolling.append(observation)
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
            context_observations = list(self._history.get(key, []))
            self._cancel_timer_locked(key)
        if not observations:
            return None
        return await reflect_observations(
            self.store,
            observations,
            context_observations=context_observations,
            send_chat_url=self.send_chat_url,
            activity_log=self.activity_log,
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


def _monitor_html() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Intent Alignment Monitor</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fa;
      --panel: #ffffff;
      --ink: #1b1f24;
      --muted: #667085;
      --line: #d7dce2;
      --accent: #1463ff;
      --good: #087443;
      --warn: #b45309;
      --bad: #b42318;
      --soft: #eef4ff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 22px;
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    h1, h2, h3, p { margin: 0; }
    h1 { font-size: 20px; font-weight: 650; letter-spacing: 0; }
    h2 { font-size: 15px; font-weight: 650; margin-bottom: 10px; }
    h3 { font-size: 13px; font-weight: 650; margin-bottom: 4px; }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: white;
      border-radius: 6px;
      padding: 8px 11px;
      font: inherit;
      cursor: pointer;
    }
    button.secondary {
      background: white;
      color: var(--accent);
    }
    button:disabled {
      cursor: wait;
      opacity: .65;
    }
    main {
      padding: 18px 22px 28px;
      max-width: 1500px;
      margin: 0 auto;
    }
    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      min-width: 280px;
    }
    .muted { color: var(--muted); }
    .grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 12px;
      margin-bottom: 14px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
    }
    .metric {
      min-height: 88px;
    }
    .metric .value {
      font-size: 26px;
      font-weight: 700;
      margin-top: 8px;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1.4fr) minmax(320px, .8fr);
      gap: 14px;
      align-items: start;
    }
    .stack {
      display: grid;
      gap: 14px;
    }
    .row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }
    .list {
      display: grid;
      gap: 10px;
      max-height: 680px;
      overflow: auto;
      padding-right: 2px;
    }
    .item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px;
      background: #fff;
    }
    .item.aligned { border-left: 4px solid var(--good); }
    .item.possible_misalignment { border-left: 4px solid var(--bad); }
    .item.needs_context { border-left: 4px solid var(--warn); }
    .item.no_signal { border-left: 4px solid var(--line); }
    .item.observation { border-left: 4px solid var(--accent); }
    .item.status { border-left: 4px solid var(--warn); background: #fffaf0; }
    .item.analysis { border-left: 4px solid var(--good); }
    .item.ignored_event { border-left: 4px solid var(--line); background: #fafafa; }
    .tags {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }
    .tag {
      display: inline-flex;
      align-items: center;
      min-height: 22px;
      border-radius: 999px;
      background: var(--soft);
      color: #174ea6;
      padding: 2px 8px;
      font-size: 12px;
      white-space: nowrap;
    }
    .mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 12px;
    }
    .text {
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      margin-top: 8px;
    }
    .small {
      font-size: 12px;
    }
    .empty {
      color: var(--muted);
      border: 1px dashed var(--line);
      border-radius: 8px;
      padding: 14px;
      background: #fff;
    }
    .status-dot {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      display: inline-block;
      background: var(--warn);
      margin-right: 7px;
    }
    .status-dot.ok { background: var(--good); }
    @media (max-width: 980px) {
      header { align-items: flex-start; flex-direction: column; }
      .toolbar { justify-content: flex-start; min-width: 0; width: 100%; }
      .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .layout { grid-template-columns: 1fr; }
    }
    @media (max-width: 560px) {
      main, header { padding-left: 14px; padding-right: 14px; }
      .grid { grid-template-columns: 1fr; }
      .toolbar { flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Intent Alignment Monitor</h1>
      <p class="muted small" id="subtitle">Waiting for state...</p>
    </div>
    <div class="toolbar">
      <span class="small"><span id="status-dot" class="status-dot"></span><span id="status-text">Loading</span></span>
      <button class="secondary" id="refresh">Refresh</button>
      <button id="flush">Flush Pending</button>
    </div>
  </header>
  <main>
    <section class="grid">
      <div class="panel metric"><h2>Pending</h2><div id="pending-count" class="value">0</div><p class="muted small">observations queued</p></div>
      <div class="panel metric"><h2>Analyses</h2><div id="analysis-count" class="value">0</div><p class="muted small">recently retained</p></div>
      <div class="panel metric"><h2>Memories</h2><div id="memory-count" class="value">0</div><p class="muted small">persisted records</p></div>
      <div class="panel metric"><h2>Cadence</h2><div id="cadence" class="value">-</div><p class="muted small">speech / chat seconds</p></div>
    </section>
    <section class="layout">
      <div class="stack">
        <div class="panel">
          <div class="row"><h2>Live Activity</h2><span class="muted small" id="stream-status">connecting stream</span></div>
          <div class="list" id="activity"></div>
        </div>
        <div class="panel">
          <div class="row"><h2>Latest Analyses</h2><span class="muted small" id="updated-at"></span></div>
          <div class="list" id="analyses"></div>
        </div>
        <div class="panel">
          <h2>Pending Observations</h2>
          <div class="list" id="pending"></div>
        </div>
        <div class="panel">
          <h2>Rolling Context</h2>
          <div class="list" id="rolling"></div>
        </div>
      </div>
      <div class="stack">
        <div class="panel">
          <h2>Memory</h2>
          <div class="list" id="memories"></div>
        </div>
        <div class="panel">
          <h2>Sources</h2>
          <div class="list" id="sources"></div>
        </div>
      </div>
    </section>
  </main>
  <script>
    const stateUrl = "/state?limit=20";
    const els = {
      subtitle: document.getElementById("subtitle"),
      statusDot: document.getElementById("status-dot"),
      statusText: document.getElementById("status-text"),
      pendingCount: document.getElementById("pending-count"),
      analysisCount: document.getElementById("analysis-count"),
      memoryCount: document.getElementById("memory-count"),
      cadence: document.getElementById("cadence"),
      activity: document.getElementById("activity"),
      streamStatus: document.getElementById("stream-status"),
      analyses: document.getElementById("analyses"),
      pending: document.getElementById("pending"),
      rolling: document.getElementById("rolling"),
      memories: document.getElementById("memories"),
      sources: document.getElementById("sources"),
      updatedAt: document.getElementById("updated-at"),
      refresh: document.getElementById("refresh"),
      flush: document.getElementById("flush"),
    };

    function el(tag, className, text) {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }

    function empty(text) {
      return el("div", "empty", text);
    }

    function tags(values) {
      const wrap = el("div", "tags");
      values.filter(Boolean).forEach((value) => wrap.appendChild(el("span", "tag", value)));
      return wrap;
    }

    function renderAnalyses(rows) {
      els.analyses.replaceChildren();
      if (!rows.length) {
        els.analyses.appendChild(empty("No analyses yet."));
        return;
      }
      rows.forEach((row) => {
        const item = el("article", `item ${row.alignment_state || "no_signal"}`);
        item.appendChild(el("h3", null, row.alignment_state || "unknown"));
        item.appendChild(el("p", "muted small", `${row.created_at_utc || ""} | ${row.next_action || ""} | ${row.importance || ""}`));
        item.appendChild(el("p", "text", row.text || ""));
        item.appendChild(el("p", "muted small", row.rationale || ""));
        const signalKinds = (row.signals || []).map((signal) => signal.kind);
        const hitIds = (row.hits || []).slice(0, 3).map((hit) => `hit:${hit.id}`);
        item.appendChild(tags([...signalKinds, ...hitIds]));
        if (row.response_text) item.appendChild(el("p", "text small", `response: ${row.response_text}`));
        els.analyses.appendChild(item);
      });
    }

    function renderActivity(rows) {
      els.activity.replaceChildren();
      if (!rows.length) {
        els.activity.appendChild(empty("No live activity yet."));
        return;
      }
      rows.forEach((row) => {
        const item = el("article", `item ${row.kind || ""}`);
        const title = row.kind === "status"
          ? "Agent"
          : row.kind === "analysis"
            ? `Analysis: ${row.alignment_state || "unknown"}`
            : row.kind === "ignored_event"
              ? "Ignored"
              : `${row.modality || "event"} ${row.speaker ? `from ${row.speaker}` : ""}`.trim();
        item.appendChild(el("h3", null, title));
        item.appendChild(el("p", "muted small", `${row.created_at_utc || ""} | ${row.event_type || ""}`));
        item.appendChild(el("p", "text", row.text || ""));
        const labels = [];
        if (row.next_action) labels.push(row.next_action);
        if (row.search_query) labels.push(`search:${row.search_query}`);
        (row.source_ids || []).slice(0, 3).forEach((source) => labels.push(`hit:${source}`));
        if (labels.length) item.appendChild(tags(labels));
        els.activity.appendChild(item);
      });
    }

    function renderPending(pending) {
      els.pending.replaceChildren();
      const conversations = pending.conversations || [];
      if (!conversations.length) {
        els.pending.appendChild(empty("No pending observations."));
        return;
      }
      conversations.forEach((conversation) => {
        const item = el("article", "item");
        item.appendChild(el("h3", null, conversation.key));
        (conversation.observations || []).forEach((obs) => {
          item.appendChild(el("p", "muted small", `${obs.received_at_utc || ""} | ${obs.modality || ""} | ${obs.speaker || "unknown"}`));
          item.appendChild(el("p", "text", obs.text || ""));
        });
        els.pending.appendChild(item);
      });
    }

    function renderRolling(rows) {
      els.rolling.replaceChildren();
      if (!rows.length) {
        els.rolling.appendChild(empty("No rolling context yet."));
        return;
      }
      rows.forEach((conversation) => {
        const item = el("article", "item");
        item.appendChild(el("h3", null, conversation.key));
        item.appendChild(el("p", "muted small", `${conversation.total_context_observations || 0} total observations`));
        (conversation.observations || []).forEach((obs) => {
          item.appendChild(el("p", "muted small", `${obs.received_at_utc || ""} | ${obs.modality || ""} | ${obs.speaker || "unknown"}`));
          item.appendChild(el("p", "text", obs.text || ""));
        });
        els.rolling.appendChild(item);
      });
    }

    function renderMemories(rows) {
      els.memories.replaceChildren();
      if (!rows.length) {
        els.memories.appendChild(empty("No memories persisted yet."));
        return;
      }
      rows.forEach((row) => {
        const item = el("article", "item");
        item.appendChild(el("h3", null, row.reason || "memory"));
        item.appendChild(el("p", "muted small", `${row.created_at_utc || ""} | ${row.speaker || "unknown"}`));
        item.appendChild(el("p", "text", row.text || ""));
        item.appendChild(tags(row.tags || []));
        els.memories.appendChild(item);
      });
    }

    function renderSources(rows) {
      els.sources.replaceChildren();
      if (!rows.length) {
        els.sources.appendChild(empty("No indexed sources."));
        return;
      }
      rows.forEach((row) => {
        const item = el("article", "item");
        item.appendChild(el("h3", null, row.source));
        item.appendChild(el("p", "muted small", `${row.documents} documents`));
        item.appendChild(tags(row.top_terms || []));
        els.sources.appendChild(item);
      });
    }

    async function refresh() {
      const response = await fetch(stateUrl, { cache: "no-store" });
      if (!response.ok) throw new Error(`state HTTP ${response.status}`);
      const state = await response.json();
      els.statusDot.classList.toggle("ok", Boolean(state.ok));
      els.statusText.textContent = state.ok ? "Online" : "Not ready";
      els.subtitle.textContent = state.service || "intent-alignment";
      els.pendingCount.textContent = state.pending_observations;
      els.analysisCount.textContent = state.analysis_count;
      els.memoryCount.textContent = state.memory_count;
      els.cadence.textContent = `${state.speech_reflect_seconds}s / ${state.chat_reflect_seconds}s`;
      els.updatedAt.textContent = state.generated_at_utc;
      renderAnalyses(state.analyses || []);
      renderActivity(state.activity || []);
      renderPending(state.pending || { conversations: [] });
      renderRolling(state.rolling || []);
      renderMemories(state.memories || []);
      renderSources((state.source_overview || {}).sources || []);
    }

    async function flush() {
      els.flush.disabled = true;
      try {
        await fetch("/reflect/flush", { method: "POST" });
        await refresh();
      } finally {
        els.flush.disabled = false;
      }
    }

    els.refresh.addEventListener("click", refresh);
    els.flush.addEventListener("click", flush);
    let activityRows = [];
    if ("EventSource" in window) {
      const source = new EventSource("/stream?limit=20");
      source.addEventListener("open", () => {
        els.streamStatus.textContent = "stream connected";
      });
      source.addEventListener("snapshot", (event) => {
        const state = JSON.parse(event.data);
        activityRows = state.activity || [];
        renderActivity(activityRows);
      });
      source.addEventListener("activity", (event) => {
        activityRows.unshift(JSON.parse(event.data));
        activityRows = activityRows.slice(0, 40);
        renderActivity(activityRows);
      });
      source.addEventListener("error", () => {
        els.streamStatus.textContent = "stream retrying";
      });
    } else {
      els.streamStatus.textContent = "polling";
    }
    refresh().catch((error) => {
      els.statusText.textContent = error.message;
    });
    setInterval(() => refresh().catch(() => {}), 5000);
  </script>
</body>
</html>"""


async def _state_payload(
    intent_store: IntentStore,
    reflector: ReflectionLoop,
    activity_log: ActivityLog,
    *,
    limit: int = 20,
) -> dict[str, Any]:
    safe_limit = max(1, min(limit, 100))
    pending = await reflector.snapshot()
    recent_analyses = list(reversed(_tail(intent_store.analyses, safe_limit)))
    recent_memories = list(reversed(_tail(intent_store.memories, safe_limit)))
    recent_activity = list(reversed(activity_log.snapshot(safe_limit * 2)))
    return {
        "ok": True,
        "service": "intent-alignment",
        "generated_at_utc": utc_now(),
        "data_dir": str(intent_store.data_dir),
        "source_count": len(intent_store.sources),
        "memory_count": len(intent_store.memories),
        "analysis_count": len(intent_store.analyses),
        "pending_observations": pending["count"],
        "speech_reflect_seconds": reflector.speech_delay_seconds,
        "chat_reflect_seconds": reflector.chat_delay_seconds,
        "max_reflect_batch": reflector.max_batch,
        "rolling_buffer_size": reflector.rolling_buffer_size,
        "send_chat_configured": bool(reflector.send_chat_url),
        "pending": pending,
        "rolling": pending["rolling"],
        "rolling_observations": sum(len(row["observations"]) for row in pending["rolling"]),
        "activity": [row.model_dump(mode="json") for row in recent_activity],
        "analyses": [row.model_dump(mode="json") for row in recent_analyses],
        "memories": [row.model_dump(mode="json") for row in recent_memories],
        "source_overview": intent_store.source_overview(),
    }


def _sse(event: str, payload: dict[str, Any]) -> str:
    data = json.dumps(payload, separators=(",", ":"), default=str)
    return f"event: {event}\ndata: {data}\n\n"


def create_app(store: IntentStore | None = None) -> FastAPI:
    intent_store = store or IntentStore()
    activity_log = ActivityLog()
    reflector = ReflectionLoop(intent_store, send_chat_url=SEND_CHAT_URL, activity_log=activity_log)

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
    app.state.activity_log = activity_log

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

    @app.get("/state")
    async def state(limit: int = 20) -> dict[str, Any]:
        return await _state_payload(intent_store, reflector, activity_log, limit=limit)

    @app.get("/stream")
    async def stream(limit: int = 20) -> StreamingResponse:
        async def generate():
            queue = activity_log.subscribe()
            try:
                yield _sse("snapshot", await _state_payload(intent_store, reflector, activity_log, limit=limit))
                while True:
                    try:
                        record = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield _sse("activity", record.model_dump(mode="json"))
            finally:
                activity_log.unsubscribe(queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def root_ui() -> HTMLResponse:
        return HTMLResponse(_monitor_html())

    @app.get("/ui", response_class=HTMLResponse)
    async def monitor_ui() -> HTMLResponse:
        return HTMLResponse(_monitor_html())

    @app.get("/prompt")
    async def prompt_policy() -> dict[str, Any]:
        return {
            "role": "Intent Alignment real-time reflector",
            "mechanical_controls": {
                "speech": "Azure Speech emits live final utterance segments after 3s silence or a 20s maximum segment duration.",
                "reflection": (
                    "The sink schedules reflection with per-conversation asyncio timers before any analysis runs; "
                    "the analyzer does not decide how long to wait."
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
                activity_log=activity_log,
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
        for observation in observations:
            if observation.from_bot or not observation.text.strip():
                continue
            activity_log.append(
                ActivityRecord(
                    kind="observation",
                    text=observation.text,
                    event_id=observation.event_id,
                    event_type=observation.event_type,
                    modality=observation.modality,
                    speaker=observation.speaker,
                    meeting_id=observation.meeting_id,
                    thread_id=observation.thread_id,
                )
            )
        if not observations:
            event_type = _first_text(envelope.get("event_type")) or "unknown"
            if event_type == "meeting.transcript.official":
                text = "Ignored post-meeting official transcript; waiting for live speech or chat"
            else:
                text = f"Ignored unsupported event type {event_type}"
            activity_log.append(
                ActivityRecord(
                    kind="ignored_event",
                    text=text,
                    event_id=_first_text(envelope.get("event_id")),
                    event_type=event_type,
                )
            )

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
