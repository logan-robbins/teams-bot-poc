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
    Participant,
    RawIngestEvent,
    Risk,
    SpeakerIdentityLink,
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
    participant_id TEXT,
    aad_object_id TEXT,
    media_source_id TEXT,
    display_name TEXT,
    message_id TEXT,
    reply_to_message_id TEXT,
    from_bot INTEGER DEFAULT 0,
    confidence REAL,
    raw_json TEXT,
    source_raw_event_ids_json TEXT DEFAULT '[]',
    superseded_by TEXT,
    PRIMARY KEY (session_id, event_id),
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);
CREATE INDEX IF NOT EXISTS idx_meeting_events_session_ts
    ON meeting_events(session_id, timestamp_utc);

CREATE TABLE IF NOT EXISTS raw_ingest_events (
    raw_event_id           TEXT PRIMARY KEY,
    session_id             TEXT,
    received_at_utc        TEXT NOT NULL,
    provider_timestamp_utc TEXT,
    source                 TEXT NOT NULL,
    event_type             TEXT NOT NULL,
    speaker_or_sender_id   TEXT,
    payload_hash           TEXT NOT NULL,
    raw_payload_json       TEXT NOT NULL,
    normalized_payload_json TEXT,
    normalized_event_id    TEXT,
    dropped_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_raw_ingest_session_received
    ON raw_ingest_events(session_id, received_at_utc);
CREATE INDEX IF NOT EXISTS idx_raw_ingest_payload_hash
    ON raw_ingest_events(payload_hash);

CREATE TABLE IF NOT EXISTS meeting_participants (
    session_id        TEXT NOT NULL,
    aad_object_id     TEXT NOT NULL,
    display_name      TEXT,
    upn               TEXT,
    is_application    INTEGER NOT NULL DEFAULT 0,
    role              TEXT,
    first_seen_at_utc TEXT,
    last_seen_at_utc  TEXT,
    PRIMARY KEY (session_id, aad_object_id)
);

CREATE TABLE IF NOT EXISTS participant_msi_bindings (
    session_id        TEXT NOT NULL,
    media_source_id   INTEGER NOT NULL,
    aad_object_id     TEXT NOT NULL,
    first_seen_at_utc TEXT,
    last_seen_at_utc  TEXT,
    PRIMARY KEY (session_id, media_source_id)
);

CREATE TABLE IF NOT EXISTS speaker_identity_links (
    session_id        TEXT NOT NULL,
    speaker_id        TEXT NOT NULL,
    aad_object_id     TEXT,
    display_name      TEXT,
    confidence        REAL,
    method            TEXT,
    last_dominant_msi INTEGER,
    updated_at_utc    TEXT,
    PRIMARY KEY (session_id, speaker_id)
);

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
            self._migrate(conn)
            conn.commit()

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """Best-effort additive column migrations for previously-deployed DBs."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(meeting_events)")}
        additive: list[tuple[str, str]] = [
            ("participant_id", "TEXT"),
            ("aad_object_id", "TEXT"),
            ("media_source_id", "TEXT"),
            ("source_raw_event_ids_json", "TEXT DEFAULT '[]'"),
            ("superseded_by", "TEXT"),
        ]
        for column, decl in additive:
            if column in existing:
                continue
            conn.execute(f"ALTER TABLE meeting_events ADD COLUMN {column} {decl}")

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
                    text, role, speaker_id, participant_id, aad_object_id,
                    media_source_id, display_name, message_id,
                    reply_to_message_id, from_bot, confidence, raw_json,
                    source_raw_event_ids_json, superseded_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    event.participant_id,
                    event.aad_object_id,
                    event.media_source_id,
                    event.display_name,
                    event.message_id,
                    event.reply_to_message_id,
                    1 if event.from_bot else 0,
                    event.confidence,
                    _json(event.raw) if event.raw is not None else None,
                    _json(event.source_raw_event_ids or []),
                    event.superseded_by,
                ),
            )

    def record_raw_ingest_event(self, raw: RawIngestEvent) -> None:
        """Insert (or replace) an immutable raw audit row.

        See ``RawIngestEvent`` for field meanings. INSERT OR REPLACE means
        idempotent re-ingest is safe: the same payload hash + raw_event_id
        round-trip yields the same row.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO raw_ingest_events (
                    raw_event_id, session_id, received_at_utc,
                    provider_timestamp_utc, source, event_type,
                    speaker_or_sender_id, payload_hash, raw_payload_json,
                    normalized_payload_json, normalized_event_id, dropped_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    raw.raw_event_id,
                    raw.session_id,
                    raw.received_at_utc,
                    raw.provider_timestamp_utc,
                    raw.source,
                    raw.event_type,
                    raw.speaker_or_sender_id,
                    raw.payload_hash,
                    raw.raw_payload_json,
                    raw.normalized_payload_json,
                    raw.normalized_event_id,
                    raw.dropped_reason,
                ),
            )

    # -- Participant identity (E3) ---------------------------------------

    def upsert_participant(
        self, session_id: str, participant: Participant
    ) -> None:
        """Idempotent upsert of one participant row plus its MSI bindings."""
        now = _iso_now()
        first_seen = participant.first_seen_at_utc or now
        last_seen = participant.last_seen_at_utc or now
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meeting_participants (
                    session_id, aad_object_id, display_name, upn,
                    is_application, role, first_seen_at_utc, last_seen_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, aad_object_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    upn = excluded.upn,
                    is_application = excluded.is_application,
                    role = excluded.role,
                    last_seen_at_utc = excluded.last_seen_at_utc
                """,
                (
                    session_id,
                    participant.aad_object_id,
                    participant.display_name,
                    participant.user_principal_name,
                    1 if participant.is_application else 0,
                    participant.role,
                    first_seen,
                    last_seen,
                ),
            )
            for msi in participant.media_source_ids:
                conn.execute(
                    """
                    INSERT INTO participant_msi_bindings (
                        session_id, media_source_id, aad_object_id,
                        first_seen_at_utc, last_seen_at_utc
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(session_id, media_source_id) DO UPDATE SET
                        aad_object_id = excluded.aad_object_id,
                        last_seen_at_utc = excluded.last_seen_at_utc
                    """,
                    (
                        session_id,
                        int(msi),
                        participant.aad_object_id,
                        first_seen,
                        last_seen,
                    ),
                )

    def get_participants(self, session_id: str) -> list[dict[str, Any]]:
        """Return participants joined with their bound MediaSourceIds."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT aad_object_id, display_name, upn, is_application, role,"
                " first_seen_at_utc, last_seen_at_utc"
                " FROM meeting_participants WHERE session_id = ?"
                " ORDER BY first_seen_at_utc ASC",
                (session_id,),
            ).fetchall()
            participants: list[dict[str, Any]] = []
            for row in rows:
                aad = row["aad_object_id"]
                msis = [
                    int(r["media_source_id"])
                    for r in conn.execute(
                        "SELECT media_source_id FROM participant_msi_bindings"
                        " WHERE session_id = ? AND aad_object_id = ?"
                        " ORDER BY first_seen_at_utc ASC",
                        (session_id, aad),
                    )
                ]
                participants.append(
                    {
                        "aad_object_id": aad,
                        "display_name": row["display_name"],
                        "upn": row["upn"],
                        "is_application": bool(row["is_application"]),
                        "role": row["role"],
                        "first_seen_at_utc": row["first_seen_at_utc"],
                        "last_seen_at_utc": row["last_seen_at_utc"],
                        "media_source_ids": msis,
                    }
                )
        return participants

    def get_participant_for_msi(
        self, session_id: str, media_source_id: int
    ) -> dict[str, Any] | None:
        """Lookup the participant currently bound to a MediaSourceId."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT b.aad_object_id, p.display_name, p.is_application"
                " FROM participant_msi_bindings b"
                " LEFT JOIN meeting_participants p"
                " ON p.session_id = b.session_id"
                " AND p.aad_object_id = b.aad_object_id"
                " WHERE b.session_id = ? AND b.media_source_id = ?",
                (session_id, int(media_source_id)),
            ).fetchone()
            if row is None:
                return None
            return {
                "aad_object_id": row["aad_object_id"],
                "display_name": row["display_name"],
                "is_application": bool(row["is_application"]),
            }

    def upsert_speaker_identity_link(
        self, session_id: str, link: SpeakerIdentityLink
    ) -> None:
        """Idempotent upsert of a (speaker_id) ↔ AAD binding."""
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO speaker_identity_links (
                    session_id, speaker_id, aad_object_id, display_name,
                    confidence, method, last_dominant_msi, updated_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, speaker_id) DO UPDATE SET
                    aad_object_id = excluded.aad_object_id,
                    display_name = excluded.display_name,
                    confidence = excluded.confidence,
                    method = excluded.method,
                    last_dominant_msi = excluded.last_dominant_msi,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (
                    session_id,
                    link.speaker_id,
                    link.aad_object_id,
                    link.display_name,
                    link.confidence,
                    link.method,
                    link.last_dominant_msi,
                    link.updated_at_utc,
                ),
            )

    def get_speaker_identity_link(
        self, session_id: str, speaker_id: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT speaker_id, aad_object_id, display_name, confidence,"
                " method, last_dominant_msi, updated_at_utc"
                " FROM speaker_identity_links"
                " WHERE session_id = ? AND speaker_id = ?",
                (session_id, speaker_id),
            ).fetchone()
            return dict(row) if row else None

    def get_speaker_identity_links(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT speaker_id, aad_object_id, display_name, confidence,"
                " method, last_dominant_msi, updated_at_utc"
                " FROM speaker_identity_links WHERE session_id = ?"
                " ORDER BY speaker_id ASC",
                (session_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def backfill_meeting_event_identity(
        self,
        session_id: str,
        speaker_id: str,
        *,
        aad_object_id: str | None,
        display_name: str | None,
        media_source_id: int | None,
    ) -> int:
        """Rewrite identity columns on prior speech rows for this speaker.

        Allowed by E2: the working ledger may be retroactively updated as
        long as raw_ingest_events stays immutable underneath.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE meeting_events
                   SET aad_object_id = ?,
                       display_name = ?,
                       media_source_id = ?,
                       participant_id = ?
                 WHERE session_id = ?
                   AND kind = 'speech'
                   AND speaker_id = ?
                """,
                (
                    aad_object_id,
                    display_name,
                    str(media_source_id) if media_source_id is not None else None,
                    aad_object_id,
                    session_id,
                    speaker_id,
                ),
            )
            return cur.rowcount or 0

    def update_raw_ingest_promotion(
        self,
        raw_event_id: str,
        *,
        session_id: str | None = None,
        normalized_payload_json: str | None = None,
        normalized_event_id: str | None = None,
        dropped_reason: str | None = None,
    ) -> None:
        """Patch a raw row after promotion / drop. Only non-None fields are written."""
        sets: list[str] = []
        params: list[Any] = []
        if session_id is not None:
            sets.append("session_id = ?")
            params.append(session_id)
        if normalized_payload_json is not None:
            sets.append("normalized_payload_json = ?")
            params.append(normalized_payload_json)
        if normalized_event_id is not None:
            sets.append("normalized_event_id = ?")
            params.append(normalized_event_id)
        if dropped_reason is not None:
            sets.append("dropped_reason = ?")
            params.append(dropped_reason)
        if not sets:
            return
        params.append(raw_event_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE raw_ingest_events SET {', '.join(sets)} WHERE raw_event_id = ?",
                params,
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
            " participant_id, aad_object_id, media_source_id, display_name,"
            " message_id, reply_to_message_id, from_bot, confidence,"
            " source_raw_event_ids_json, superseded_by"
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
            row["source_raw_event_ids"] = json.loads(
                row.pop("source_raw_event_ids_json") or "[]"
            )
        return rows

    def get_raw_events(
        self,
        session_id: str,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw_ingest_events rows for a session (oldest first)."""
        query = (
            "SELECT raw_event_id, session_id, received_at_utc, provider_timestamp_utc,"
            " source, event_type, speaker_or_sender_id, payload_hash, raw_payload_json,"
            " normalized_payload_json, normalized_event_id, dropped_reason"
            " FROM raw_ingest_events WHERE session_id = ?"
        )
        params: list[Any] = [session_id]
        if since:
            query += " AND received_at_utc > ?"
            params.append(since)
        query += " ORDER BY received_at_utc ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._connect() as conn:
            cur = conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def iter_raw_events(self, session_id: str) -> Iterable[dict[str, Any]]:
        """Generator over raw_ingest_events for streaming NDJSON export."""
        query = (
            "SELECT raw_event_id, session_id, received_at_utc, provider_timestamp_utc,"
            " source, event_type, speaker_or_sender_id, payload_hash, raw_payload_json,"
            " normalized_payload_json, normalized_event_id, dropped_reason"
            " FROM raw_ingest_events WHERE session_id = ? ORDER BY received_at_utc ASC"
        )
        with self._connect() as conn:
            cur = conn.execute(query, [session_id])
            for row in cur:
                yield dict(row)

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
