"""
SQLite persistence for Alfred sessions.

Five canonical tables — the shape our UI and future replay tools can
rely on:

    sessions         one row per meeting
    meeting_events   one row per ledger entry (speech / chat / alfred / system)
    extractions      one row per AlfredExtraction Alfred emits
    tool_calls       one row per agent tool invocation
    dossier_items    current decisions / open_questions / action_items / risks
                     (overwritten on each extraction merge — this is the
                     "latest state" view the UI reads)

The writer is synchronous because sqlite3 is synchronous; FastAPI handlers
call ``asyncio.to_thread`` when it matters. Writes are idempotent by
primary key / unique constraints so re-ingest is safe.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel

from .models import (
    ActionItem,
    AlfredExtraction,
    AnalysisItem,
    Decision,
    InterviewSession,
    MeetingEvent,
    OpenQuestion,
    Risk,
    ToolCallRecord,
)

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    candidate_name TEXT,
    meeting_url TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    conversation_reference_id TEXT,
    graph_chat_thread_id TEXT,
    alfred_muted INTEGER DEFAULT 0,
    running_summary TEXT DEFAULT '',
    topics_json TEXT DEFAULT '[]',
    notes_json TEXT DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS meeting_events (
    session_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    source TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    text TEXT DEFAULT '',
    role TEXT DEFAULT 'unknown',
    speaker_id TEXT,
    display_name TEXT,
    message_id TEXT,
    reply_to_message_id TEXT,
    from_bot INTEGER DEFAULT 0,
    confidence REAL,
    raw_json TEXT,
    PRIMARY KEY (session_id, event_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_meeting_events_session_ts
    ON meeting_events(session_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS extractions (
    session_id TEXT NOT NULL,
    response_id TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    trigger_event_id TEXT,
    speaker_id TEXT,
    rationale TEXT DEFAULT '',
    running_summary TEXT DEFAULT '',
    topics_json TEXT DEFAULT '[]',
    notes_json TEXT DEFAULT '[]',
    decisions_json TEXT DEFAULT '[]',
    open_questions_json TEXT DEFAULT '[]',
    action_items_json TEXT DEFAULT '[]',
    risks_json TEXT DEFAULT '[]',
    raw_model_output_json TEXT,
    PRIMARY KEY (session_id, response_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_extractions_session_ts
    ON extractions(session_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS tool_calls (
    session_id TEXT NOT NULL,
    tool_call_id TEXT NOT NULL,
    response_id TEXT,
    tool_name TEXT NOT NULL,
    timestamp_utc TEXT NOT NULL,
    ok INTEGER DEFAULT 1,
    error TEXT,
    arguments_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    PRIMARY KEY (session_id, tool_call_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session_ts
    ON tool_calls(session_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS dossier_items (
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('decision','open_question','action_item','risk')),
    item_id TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (session_id, kind, item_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_dossier_items_session
    ON dossier_items(session_id, kind);
"""


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        raise TypeError(f"Not JSON serializable: {type(obj).__name__}")

    return json.dumps(value, default=_default, ensure_ascii=False)


class SessionStore:
    """Synchronous sqlite-backed store for Alfred sessions."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(SCHEMA)
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            isolation_level=None,  # autocommit
            timeout=10.0,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        return conn

    # -- Writes ----------------------------------------------------------

    def upsert_session(self, session: InterviewSession) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, candidate_name, meeting_url, started_at, ended_at,
                    conversation_reference_id, graph_chat_thread_id, alfred_muted,
                    running_summary, topics_json, notes_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    candidate_name = excluded.candidate_name,
                    meeting_url = excluded.meeting_url,
                    ended_at = excluded.ended_at,
                    conversation_reference_id = excluded.conversation_reference_id,
                    graph_chat_thread_id = excluded.graph_chat_thread_id,
                    alfred_muted = excluded.alfred_muted,
                    running_summary = excluded.running_summary,
                    topics_json = excluded.topics_json,
                    notes_json = excluded.notes_json
                """,
                (
                    session.session_id,
                    session.candidate_name,
                    session.meeting_url,
                    session.started_at,
                    session.ended_at,
                    session.conversation_reference_id,
                    session.graph_chat_thread_id,
                    1 if session.alfred_muted else 0,
                    session.running_summary or "",
                    _json(session.topics),
                    _json(session.notes),
                ),
            )

    def append_meeting_event(self, session_id: str, event: MeetingEvent) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO meeting_events (
                    session_id, event_id, kind, source, timestamp_utc,
                    text, role, speaker_id, display_name, message_id,
                    reply_to_message_id, from_bot, confidence, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    event.event_id,
                    event.kind,
                    event.source,
                    event.timestamp_utc,
                    event.text or "",
                    event.role or "unknown",
                    event.speaker_id,
                    event.display_name,
                    event.message_id,
                    event.reply_to_message_id,
                    1 if event.from_bot else 0,
                    event.confidence,
                    _json(event.raw) if event.raw is not None else None,
                ),
            )

    def append_extraction(self, session_id: str, item: AnalysisItem) -> None:
        extraction: AlfredExtraction | None = item.extraction
        if extraction is None:
            return
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO extractions (
                    session_id, response_id, timestamp_utc, trigger_event_id,
                    speaker_id, rationale, running_summary, topics_json,
                    notes_json, decisions_json, open_questions_json,
                    action_items_json, risks_json, raw_model_output_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    item.response_id,
                    item.timestamp_utc,
                    item.trigger_event_id,
                    item.speaker_id,
                    extraction.rationale or "",
                    extraction.running_summary or "",
                    _json(extraction.topics),
                    _json(extraction.notes),
                    _json([d.model_dump() for d in extraction.decisions]),
                    _json([q.model_dump() for q in extraction.open_questions]),
                    _json([a.model_dump() for a in extraction.action_items]),
                    _json([r.model_dump() for r in extraction.risks]),
                    _json(item.raw_model_output) if item.raw_model_output else None,
                ),
            )
            for tc in item.tool_calls or []:
                self._insert_tool_call(conn, session_id, item.response_id, tc)
            self._upsert_dossier_items(conn, session_id, extraction)

    def _insert_tool_call(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        response_id: str | None,
        tc: ToolCallRecord,
    ) -> None:
        conn.execute(
            """
            INSERT OR REPLACE INTO tool_calls (
                session_id, tool_call_id, response_id, tool_name,
                timestamp_utc, ok, error, arguments_json, result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                tc.id,
                response_id,
                tc.tool_name,
                tc.timestamp_utc,
                1 if tc.ok else 0,
                tc.error,
                _json(tc.arguments),
                _json(tc.result),
            ),
        )

    def _upsert_dossier_items(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        extraction: AlfredExtraction,
    ) -> None:
        now = _iso_now()
        buckets: Iterable[tuple[str, list]] = (
            ("decision", extraction.decisions),
            ("open_question", extraction.open_questions),
            ("action_item", extraction.action_items),
            ("risk", extraction.risks),
        )
        for kind, items in buckets:
            for item in items:
                first_seen = getattr(item, "first_seen_at", None) or now
                conn.execute(
                    """
                    INSERT INTO dossier_items (
                        session_id, kind, item_id, first_seen_at, updated_at, payload_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, kind, item_id) DO UPDATE SET
                        updated_at = excluded.updated_at,
                        payload_json = excluded.payload_json
                    """,
                    (
                        session_id,
                        kind,
                        item.id,
                        first_seen,
                        now,
                        _json(item.model_dump()),
                    ),
                )

    # -- Reads -----------------------------------------------------------

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT session_id, candidate_name, meeting_url, started_at,
                       ended_at, running_summary
                FROM sessions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (int(limit),),
            )
            return [dict(row) for row in cur.fetchall()]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                SELECT session_id, candidate_name, meeting_url, started_at,
                       ended_at, conversation_reference_id, graph_chat_thread_id,
                       alfred_muted, running_summary, topics_json, notes_json
                FROM sessions WHERE session_id = ?
                """,
                (session_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            data = dict(row)
            data["alfred_muted"] = bool(data["alfred_muted"])
            data["topics"] = json.loads(data.pop("topics_json") or "[]")
            data["notes"] = json.loads(data.pop("notes_json") or "[]")
            return data

    def get_ledger(self, session_id: str, limit: int | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT event_id, kind, source, timestamp_utc, text, role, speaker_id,"
            " display_name, message_id, reply_to_message_id, from_bot, confidence"
            " FROM meeting_events WHERE session_id = ? ORDER BY timestamp_utc ASC"
        )
        params: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            cur = conn.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["from_bot"] = bool(row["from_bot"])
        return rows

    def get_extractions(
        self,
        session_id: str,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT response_id, timestamp_utc, trigger_event_id, speaker_id,"
            " rationale, running_summary, topics_json, notes_json, decisions_json,"
            " open_questions_json, action_items_json, risks_json"
            " FROM extractions WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if since:
            query += " AND timestamp_utc > ?"
            params.append(since)
        query += " ORDER BY timestamp_utc ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            cur = conn.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["topics"] = json.loads(row.pop("topics_json") or "[]")
            row["notes"] = json.loads(row.pop("notes_json") or "[]")
            row["decisions"] = json.loads(row.pop("decisions_json") or "[]")
            row["open_questions"] = json.loads(row.pop("open_questions_json") or "[]")
            row["action_items"] = json.loads(row.pop("action_items_json") or "[]")
            row["risks"] = json.loads(row.pop("risks_json") or "[]")
        return rows

    def get_dossier(self, session_id: str) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {
            "decisions": [],
            "open_questions": [],
            "action_items": [],
            "risks": [],
        }
        kind_to_key = {
            "decision": "decisions",
            "open_question": "open_questions",
            "action_item": "action_items",
            "risk": "risks",
        }
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT kind, payload_json FROM dossier_items"
                " WHERE session_id = ? ORDER BY first_seen_at ASC",
                (session_id,),
            )
            for row in cur.fetchall():
                key = kind_to_key.get(row["kind"])
                if key is None:
                    continue
                out[key].append(json.loads(row["payload_json"]))
        return out

    def get_tool_calls(
        self,
        session_id: str,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT tool_call_id, response_id, tool_name, timestamp_utc, ok,"
            " error, arguments_json, result_json"
            " FROM tool_calls WHERE session_id = ? ORDER BY timestamp_utc ASC"
        )
        params: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            cur = conn.execute(query, params)
            rows = [dict(row) for row in cur.fetchall()]
        for row in rows:
            row["ok"] = bool(row["ok"])
            row["arguments"] = json.loads(row.pop("arguments_json") or "{}")
            row["result"] = json.loads(row.pop("result_json") or "{}")
        return rows


def build_store(db_path: str | Path) -> SessionStore:
    return SessionStore(Path(db_path))


# Optional decision-reference item helper — enables a simple synthesis path
# later where you'd want a derived entry per type. Left out here by design:
# dossier_items is the canonical "latest state" view.

_ALL_ROW_TYPES = (
    Decision,
    OpenQuestion,
    ActionItem,
    Risk,
)
