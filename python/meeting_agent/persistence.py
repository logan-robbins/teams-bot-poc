"""
PostgreSQL persistence for Alfred sessions.

Synchronous psycopg 3 store. Same table shape and same public API as the
prior sqlite implementation; callers see no behavioural change beyond the
``build_store`` signature.

Five canonical tables — the shape our UI and future replay tools can
rely on:

    sessions         one row per meeting
    meeting_events   one row per ledger entry (speech / chat / alfred / system)
    extractions      one row per AlfredExtraction Alfred emits
    tool_calls       one row per agent tool invocation
    dossier_items    current decisions / open_questions / action_items / risks
                     (overwritten on each extraction merge — this is the
                     "latest state" view the UI reads)

Writes are idempotent by primary key / unique constraints so re-ingest is
safe. FastAPI handlers call ``asyncio.to_thread`` when latency matters.
"""

from __future__ import annotations

import json
import logging
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
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


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
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
    )
    """,
    """
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
        confidence DOUBLE PRECISION,
        raw_json TEXT,
        source_raw_event_ids_json TEXT DEFAULT '[]',
        superseded_by TEXT,
        team_id TEXT,
        channel_id TEXT,
        channel_thread_id TEXT,
        PRIMARY KEY (session_id, event_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meeting_events_session_ts ON meeting_events(session_id, timestamp_utc)",
    """
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
        dropped_reason         TEXT,
        team_id                TEXT,
        channel_id             TEXT,
        channel_thread_id      TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_ingest_session_received ON raw_ingest_events(session_id, received_at_utc)",
    "CREATE INDEX IF NOT EXISTS idx_raw_ingest_payload_hash ON raw_ingest_events(payload_hash)",
    # Session ↔ channel link. Lets a meeting spawned from a channel inherit
    # (team_id, channel_id, channel_thread_id) so analytics can group every
    # event by channel_id alone. Populated by POST /session/link from the
    # C# bot when channel context becomes known (often AFTER first events
    # have already been written), and used to backfill prior events.
    """
    CREATE TABLE IF NOT EXISTS session_channel_links (
        chat_thread_id    TEXT PRIMARY KEY,
        team_id           TEXT NOT NULL,
        channel_id        TEXT NOT NULL,
        channel_thread_id TEXT,
        source            TEXT,
        linked_at_utc     TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_session_channel_links_channel ON session_channel_links(channel_id)",
    """
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
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS participant_msi_bindings (
        session_id        TEXT NOT NULL,
        media_source_id   BIGINT NOT NULL,
        aad_object_id     TEXT NOT NULL,
        first_seen_at_utc TEXT,
        last_seen_at_utc  TEXT,
        PRIMARY KEY (session_id, media_source_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS speaker_identity_links (
        session_id        TEXT NOT NULL,
        speaker_id        TEXT NOT NULL,
        aad_object_id     TEXT,
        display_name      TEXT,
        confidence        DOUBLE PRECISION,
        method            TEXT,
        last_dominant_msi BIGINT,
        updated_at_utc    TEXT,
        PRIMARY KEY (session_id, speaker_id)
    )
    """,
    """
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
        PRIMARY KEY (session_id, response_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_extractions_session_ts ON extractions(session_id, timestamp_utc)",
    """
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
        PRIMARY KEY (session_id, tool_call_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_tool_calls_session_ts ON tool_calls(session_id, timestamp_utc)",
    """
    CREATE TABLE IF NOT EXISTS dossier_items (
        session_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('decision','open_question','action_item','risk')),
        item_id TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (session_id, kind, item_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_dossier_items_session ON dossier_items(session_id, kind)",
    # alfred-v2: canonical Graph onlineMeeting registry. Populated from
    # meeting.* events; the meeting_id here is the Graph onlineMeeting id
    # (URL-safe base64), NOT the chat thread id. ``meeting_chat_thread_id``
    # is the sub-resource chat container (``19:meeting_xxx@thread.v2``);
    # channel_link.* mirrors the bot's MeetingChannelLink. Best-effort
    # subject / organizer / scheduled times are stamped from MeetingRef
    # on each event so reads after restart still get the latest values.
    """
    CREATE TABLE IF NOT EXISTS meetings (
        meeting_id TEXT PRIMARY KEY,
        meeting_chat_thread_id TEXT,
        subject TEXT,
        organizer_aad_id TEXT,
        organizer_display_name TEXT,
        scheduled_start_utc TEXT,
        scheduled_end_utc TEXT,
        actual_start_utc TEXT,
        actual_end_utc TEXT,
        channel_team_id TEXT,
        channel_team_display_name TEXT,
        channel_id TEXT,
        channel_display_name TEXT,
        channel_thread_id TEXT,
        channel_linked_at_utc TEXT,
        channel_linked_source TEXT,
        last_event_utc TEXT,
        created_at_utc TEXT NOT NULL,
        updated_at_utc TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_meetings_chat_thread ON meetings(meeting_chat_thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_meetings_channel ON meetings(channel_team_id, channel_id)",
    # Operator-uploaded transcript content. Used when the bot's Graph-based
    # transcript fetcher can't auto-retrieve (e.g. RSC limitations); an
    # operator drops the file from the Teams meeting chat into the web UI
    # and POSTs it to /v2/meetings/{meeting_id}/transcript-upload. The
    # v2/meetings/{id}/transcript endpoint serves this content with
    # source="operator_upload" when present, falling back to the bot-
    # written blob otherwise. Keyed on the meeting_id the operator supplied
    # (which may be the chat thread id when canonical isn't available).
    """
    CREATE TABLE IF NOT EXISTS transcript_uploads (
        meeting_id      TEXT PRIMARY KEY,
        txt             TEXT,
        vtt             TEXT,
        subject         TEXT,
        uploaded_at_utc TEXT NOT NULL
    )
    """,
    # alfred-v2: raw envelope archive. One row per inbound POST to
    # /v2/events so we can always replay the wire history without
    # re-deriving from the per-session ledger.
    """
    CREATE TABLE IF NOT EXISTS raw_ingest_envelopes (
        envelope_id        TEXT PRIMARY KEY,
        schema_version     TEXT NOT NULL,
        event_type         TEXT NOT NULL,
        ts                 TEXT NOT NULL,
        received_at_utc    TEXT NOT NULL,
        meeting_id         TEXT,
        team_id            TEXT,
        channel_id         TEXT,
        thread_id          TEXT,
        raw_json           TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_raw_ingest_envelopes_meeting ON raw_ingest_envelopes(meeting_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_raw_ingest_envelopes_channel ON raw_ingest_envelopes(team_id, channel_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_raw_ingest_envelopes_event_type ON raw_ingest_envelopes(event_type, ts)",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    def _default(obj: Any) -> Any:
        if isinstance(obj, BaseModel):
            return obj.model_dump()
        raise TypeError(f"Not JSON serializable: {type(obj).__name__}")

    return json.dumps(value, default=_default, ensure_ascii=False)


def _column_exists(conn: psycopg.Connection, table: str, column: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM information_schema.columns"
        " WHERE table_schema = current_schema()"
        "   AND table_name = %s AND column_name = %s",
        (table, column),
    ).fetchone()
    return row is not None


class SessionStore:
    """Synchronous Postgres-backed store for Alfred sessions."""

    def __init__(self, connection_string: str) -> None:
        if not connection_string or not connection_string.strip():
            raise RuntimeError(
                "SessionStore requires a non-empty PostgreSQL connection string."
            )
        self._connection_string = connection_string
        self._lock = threading.Lock()
        self._pool: ConnectionPool | None = None
        self._initialized = False
        self._init_lock = threading.Lock()

    def _ensure_pool(self) -> ConnectionPool:
        if self._pool is not None and self._initialized:
            return self._pool
        with self._init_lock:
            if self._pool is None:
                self._pool = ConnectionPool(
                    conninfo=self._connection_string,
                    min_size=1,
                    max_size=5,
                    kwargs={"row_factory": dict_row, "autocommit": True},
                    open=True,
                )
                self._pool.wait()
            if not self._initialized:
                with self._pool.connection() as conn:
                    with conn.transaction():
                        for stmt in _SCHEMA_STATEMENTS:
                            conn.execute(stmt)
                        self._migrate(conn)
                self._initialized = True
        return self._pool

    @contextmanager
    def _connect(self) -> Iterator[psycopg.Connection]:
        """Yield a pooled connection. Kept on the public surface because
        ``identity.py`` issues ad-hoc reads against it."""
        pool = self._ensure_pool()
        with pool.connection() as conn:
            yield conn

    def close(self) -> None:
        with self._init_lock:
            if self._pool is not None:
                self._pool.close()
                self._pool = None
                self._initialized = False

    def _migrate(self, conn: psycopg.Connection) -> None:
        """Best-effort additive column migrations for previously-deployed DBs."""
        meeting_additive: list[tuple[str, str]] = [
            ("participant_id", "TEXT"),
            ("aad_object_id", "TEXT"),
            ("media_source_id", "TEXT"),
            ("source_raw_event_ids_json", "TEXT DEFAULT '[]'"),
            ("superseded_by", "TEXT"),
            ("team_id", "TEXT"),
            ("channel_id", "TEXT"),
            ("channel_thread_id", "TEXT"),
        ]
        for column, decl in meeting_additive:
            if _column_exists(conn, "meeting_events", column):
                continue
            conn.execute(
                f"ALTER TABLE meeting_events ADD COLUMN IF NOT EXISTS {column} {decl}"
            )

        raw_additive: list[tuple[str, str]] = [
            ("team_id", "TEXT"),
            ("channel_id", "TEXT"),
            ("channel_thread_id", "TEXT"),
        ]
        for column, decl in raw_additive:
            if _column_exists(conn, "raw_ingest_events", column):
                continue
            conn.execute(
                f"ALTER TABLE raw_ingest_events ADD COLUMN IF NOT EXISTS {column} {decl}"
            )

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_meeting_events_channel_ts "
            "ON meeting_events(channel_id, timestamp_utc)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_raw_ingest_channel_received "
            "ON raw_ingest_events(channel_id, received_at_utc)"
        )

    # -- Writes ----------------------------------------------------------

    def upsert_session(self, session: InterviewSession) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, candidate_name, meeting_url, started_at, ended_at,
                    conversation_reference_id, graph_chat_thread_id, alfred_muted,
                    running_summary, topics_json, notes_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    candidate_name = EXCLUDED.candidate_name,
                    meeting_url = EXCLUDED.meeting_url,
                    ended_at = EXCLUDED.ended_at,
                    conversation_reference_id = EXCLUDED.conversation_reference_id,
                    graph_chat_thread_id = EXCLUDED.graph_chat_thread_id,
                    alfred_muted = EXCLUDED.alfred_muted,
                    running_summary = EXCLUDED.running_summary,
                    topics_json = EXCLUDED.topics_json,
                    notes_json = EXCLUDED.notes_json
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
                INSERT INTO meeting_events (
                    session_id, event_id, kind, source, timestamp_utc,
                    text, role, speaker_id, participant_id, aad_object_id,
                    media_source_id, display_name, message_id,
                    reply_to_message_id, from_bot, confidence, raw_json,
                    source_raw_event_ids_json, superseded_by,
                    team_id, channel_id, channel_thread_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, event_id) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    source = EXCLUDED.source,
                    timestamp_utc = EXCLUDED.timestamp_utc,
                    text = EXCLUDED.text,
                    role = EXCLUDED.role,
                    speaker_id = EXCLUDED.speaker_id,
                    participant_id = EXCLUDED.participant_id,
                    aad_object_id = EXCLUDED.aad_object_id,
                    media_source_id = EXCLUDED.media_source_id,
                    display_name = EXCLUDED.display_name,
                    message_id = EXCLUDED.message_id,
                    reply_to_message_id = EXCLUDED.reply_to_message_id,
                    from_bot = EXCLUDED.from_bot,
                    confidence = EXCLUDED.confidence,
                    raw_json = EXCLUDED.raw_json,
                    source_raw_event_ids_json = EXCLUDED.source_raw_event_ids_json,
                    superseded_by = EXCLUDED.superseded_by,
                    team_id = EXCLUDED.team_id,
                    channel_id = EXCLUDED.channel_id,
                    channel_thread_id = EXCLUDED.channel_thread_id
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
                    event.team_id,
                    event.channel_id,
                    event.channel_thread_id,
                ),
            )

    def record_raw_ingest_event(self, raw: RawIngestEvent) -> None:
        """Insert (or replace) an immutable raw audit row.

        See ``RawIngestEvent`` for field meanings. The conflict clause means
        idempotent re-ingest is safe: the same payload hash + raw_event_id
        round-trip yields the same row.
        """
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO raw_ingest_events (
                    raw_event_id, session_id, received_at_utc,
                    provider_timestamp_utc, source, event_type,
                    speaker_or_sender_id, payload_hash, raw_payload_json,
                    normalized_payload_json, normalized_event_id, dropped_reason,
                    team_id, channel_id, channel_thread_id
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (raw_event_id) DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    received_at_utc = EXCLUDED.received_at_utc,
                    provider_timestamp_utc = EXCLUDED.provider_timestamp_utc,
                    source = EXCLUDED.source,
                    event_type = EXCLUDED.event_type,
                    speaker_or_sender_id = EXCLUDED.speaker_or_sender_id,
                    payload_hash = EXCLUDED.payload_hash,
                    raw_payload_json = EXCLUDED.raw_payload_json,
                    normalized_payload_json = EXCLUDED.normalized_payload_json,
                    normalized_event_id = EXCLUDED.normalized_event_id,
                    dropped_reason = EXCLUDED.dropped_reason,
                    team_id = EXCLUDED.team_id,
                    channel_id = EXCLUDED.channel_id,
                    channel_thread_id = EXCLUDED.channel_thread_id
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
                    raw.team_id,
                    raw.channel_id,
                    raw.channel_thread_id,
                ),
            )

    # -- Channel link / backfill -----------------------------------------

    def link_session_to_channel(
        self,
        chat_thread_id: str,
        team_id: str,
        channel_id: str,
        channel_thread_id: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, int]:
        """Persist a session ↔ channel binding and backfill prior events.

        Used by ``POST /session/link`` so that meetings spawned from a
        channel inherit ``(team_id, channel_id, channel_thread_id)`` even
        on rows written before the link was known. Returns a count of
        rows updated per table so callers can verify the backfill.
        """
        if not chat_thread_id or not team_id or not channel_id:
            raise ValueError("chat_thread_id, team_id, and channel_id are required")

        with self._lock, self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO session_channel_links (
                        chat_thread_id, team_id, channel_id, channel_thread_id,
                        source, linked_at_utc
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (chat_thread_id) DO UPDATE SET
                        team_id = EXCLUDED.team_id,
                        channel_id = EXCLUDED.channel_id,
                        channel_thread_id = EXCLUDED.channel_thread_id,
                        source = EXCLUDED.source,
                        linked_at_utc = EXCLUDED.linked_at_utc
                    """,
                    (
                        chat_thread_id,
                        team_id,
                        channel_id,
                        channel_thread_id,
                        source,
                        _iso_now(),
                    ),
                )

                session_rows = conn.execute(
                    "SELECT session_id FROM sessions WHERE graph_chat_thread_id = %s",
                    (chat_thread_id,),
                ).fetchall()
                session_ids = [row["session_id"] for row in session_rows]

                meeting_updates = 0
                raw_updates = 0
                for sid in session_ids:
                    cur_m = conn.execute(
                        """
                        UPDATE meeting_events
                        SET team_id = %s, channel_id = %s, channel_thread_id = %s
                        WHERE session_id = %s
                        """,
                        (team_id, channel_id, channel_thread_id, sid),
                    )
                    meeting_updates += cur_m.rowcount or 0

                    cur_r = conn.execute(
                        """
                        UPDATE raw_ingest_events
                        SET team_id = %s, channel_id = %s, channel_thread_id = %s
                        WHERE session_id = %s
                        """,
                        (team_id, channel_id, channel_thread_id, sid),
                    )
                    raw_updates += cur_r.rowcount or 0

            return {
                "sessions_matched": len(session_ids),
                "meeting_events_updated": meeting_updates,
                "raw_ingest_events_updated": raw_updates,
            }

    def get_channel_link(self, chat_thread_id: str) -> Optional[dict[str, Optional[str]]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT team_id, channel_id, channel_thread_id, source, linked_at_utc
                FROM session_channel_links
                WHERE chat_thread_id = %s
                """,
                (chat_thread_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "chat_thread_id": chat_thread_id,
            "team_id": row["team_id"],
            "channel_id": row["channel_id"],
            "channel_thread_id": row["channel_thread_id"],
            "source": row["source"],
            "linked_at_utc": row["linked_at_utc"],
        }

    def list_channel_links(self) -> list[dict[str, Optional[str]]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT chat_thread_id, team_id, channel_id, channel_thread_id,
                       source, linked_at_utc
                FROM session_channel_links
                ORDER BY linked_at_utc DESC
                """
            ).fetchall()
        return [
            {
                "chat_thread_id": r["chat_thread_id"],
                "team_id": r["team_id"],
                "channel_id": r["channel_id"],
                "channel_thread_id": r["channel_thread_id"],
                "source": r["source"],
                "linked_at_utc": r["linked_at_utc"],
            }
            for r in rows
        ]

    # -- Participant identity (E3) ---------------------------------------

    def upsert_participant(
        self, session_id: str, participant: Participant
    ) -> None:
        """Idempotent upsert of one participant row plus its MSI bindings."""
        now = _iso_now()
        first_seen = participant.first_seen_at_utc or now
        last_seen = participant.last_seen_at_utc or now
        with self._lock, self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO meeting_participants (
                        session_id, aad_object_id, display_name, upn,
                        is_application, role, first_seen_at_utc, last_seen_at_utc
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, aad_object_id) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        upn = EXCLUDED.upn,
                        is_application = EXCLUDED.is_application,
                        role = EXCLUDED.role,
                        last_seen_at_utc = EXCLUDED.last_seen_at_utc
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
                        ) VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (session_id, media_source_id) DO UPDATE SET
                            aad_object_id = EXCLUDED.aad_object_id,
                            last_seen_at_utc = EXCLUDED.last_seen_at_utc
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
                " FROM meeting_participants WHERE session_id = %s"
                " ORDER BY first_seen_at_utc ASC",
                (session_id,),
            ).fetchall()
            participants: list[dict[str, Any]] = []
            for row in rows:
                aad = row["aad_object_id"]
                msi_rows = conn.execute(
                    "SELECT media_source_id FROM participant_msi_bindings"
                    " WHERE session_id = %s AND aad_object_id = %s"
                    " ORDER BY first_seen_at_utc ASC",
                    (session_id, aad),
                ).fetchall()
                msis = [int(r["media_source_id"]) for r in msi_rows]
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
                "SELECT b.aad_object_id AS aad_object_id, p.display_name AS display_name,"
                " p.is_application AS is_application"
                " FROM participant_msi_bindings b"
                " LEFT JOIN meeting_participants p"
                " ON p.session_id = b.session_id"
                " AND p.aad_object_id = b.aad_object_id"
                " WHERE b.session_id = %s AND b.media_source_id = %s",
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
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id, speaker_id) DO UPDATE SET
                    aad_object_id = EXCLUDED.aad_object_id,
                    display_name = EXCLUDED.display_name,
                    confidence = EXCLUDED.confidence,
                    method = EXCLUDED.method,
                    last_dominant_msi = EXCLUDED.last_dominant_msi,
                    updated_at_utc = EXCLUDED.updated_at_utc
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
                " WHERE session_id = %s AND speaker_id = %s",
                (session_id, speaker_id),
            ).fetchone()
            return dict(row) if row else None

    def get_speaker_identity_links(self, session_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT speaker_id, aad_object_id, display_name, confidence,"
                " method, last_dominant_msi, updated_at_utc"
                " FROM speaker_identity_links WHERE session_id = %s"
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
                   SET aad_object_id = %s,
                       display_name = %s,
                       media_source_id = %s,
                       participant_id = %s
                 WHERE session_id = %s
                   AND kind = 'speech'
                   AND speaker_id = %s
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
            sets.append("session_id = %s")
            params.append(session_id)
        if normalized_payload_json is not None:
            sets.append("normalized_payload_json = %s")
            params.append(normalized_payload_json)
        if normalized_event_id is not None:
            sets.append("normalized_event_id = %s")
            params.append(normalized_event_id)
        if dropped_reason is not None:
            sets.append("dropped_reason = %s")
            params.append(dropped_reason)
        if not sets:
            return
        params.append(raw_event_id)
        with self._lock, self._connect() as conn:
            conn.execute(
                f"UPDATE raw_ingest_events SET {', '.join(sets)} WHERE raw_event_id = %s",
                params,
            )

    def append_extraction(self, session_id: str, item: AnalysisItem) -> None:
        extraction: AlfredExtraction | None = item.extraction
        if extraction is None:
            return
        with self._lock, self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO extractions (
                        session_id, response_id, timestamp_utc, trigger_event_id,
                        speaker_id, rationale, running_summary, topics_json,
                        notes_json, decisions_json, open_questions_json,
                        action_items_json, risks_json, raw_model_output_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, response_id) DO UPDATE SET
                        timestamp_utc = EXCLUDED.timestamp_utc,
                        trigger_event_id = EXCLUDED.trigger_event_id,
                        speaker_id = EXCLUDED.speaker_id,
                        rationale = EXCLUDED.rationale,
                        running_summary = EXCLUDED.running_summary,
                        topics_json = EXCLUDED.topics_json,
                        notes_json = EXCLUDED.notes_json,
                        decisions_json = EXCLUDED.decisions_json,
                        open_questions_json = EXCLUDED.open_questions_json,
                        action_items_json = EXCLUDED.action_items_json,
                        risks_json = EXCLUDED.risks_json,
                        raw_model_output_json = EXCLUDED.raw_model_output_json
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
        conn: psycopg.Connection,
        session_id: str,
        response_id: str | None,
        tc: ToolCallRecord,
    ) -> None:
        conn.execute(
            """
            INSERT INTO tool_calls (
                session_id, tool_call_id, response_id, tool_name,
                timestamp_utc, ok, error, arguments_json, result_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id, tool_call_id) DO UPDATE SET
                response_id = EXCLUDED.response_id,
                tool_name = EXCLUDED.tool_name,
                timestamp_utc = EXCLUDED.timestamp_utc,
                ok = EXCLUDED.ok,
                error = EXCLUDED.error,
                arguments_json = EXCLUDED.arguments_json,
                result_json = EXCLUDED.result_json
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
        conn: psycopg.Connection,
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
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (session_id, kind, item_id) DO UPDATE SET
                        updated_at = EXCLUDED.updated_at,
                        payload_json = EXCLUDED.payload_json
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
            rows = conn.execute(
                """
                SELECT session_id, candidate_name, meeting_url, started_at,
                       ended_at, running_summary
                FROM sessions
                ORDER BY started_at DESC
                LIMIT %s
                """,
                (int(limit),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, candidate_name, meeting_url, started_at,
                       ended_at, conversation_reference_id, graph_chat_thread_id,
                       alfred_muted, running_summary, topics_json, notes_json
                FROM sessions WHERE session_id = %s
                """,
                (session_id,),
            ).fetchone()
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
            " source_raw_event_ids_json, superseded_by,"
            " team_id, channel_id, channel_thread_id"
            " FROM meeting_events WHERE session_id = %s ORDER BY timestamp_utc ASC"
        )
        params: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        for row in rows:
            row["from_bot"] = bool(row["from_bot"])
            row["source_raw_event_ids"] = json.loads(
                row.pop("source_raw_event_ids_json") or "[]"
            )
        return rows

    def get_channel_ledger(
        self,
        channel_id: str,
        *,
        team_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        kinds: list[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Return every meeting event tagged with this channel, ordered by time.

        This is the analytical lens that motivated the metadata work:
        one query that returns channel chat AND every meeting (chat +
        STT) the bot saw under that channel, merged on
        ``timestamp_utc``. Either rows are stamped at write time or
        backfilled by ``link_session_to_channel``; both are equivalent.
        """
        clauses = ["channel_id = %s"]
        params: list[Any] = [channel_id]
        if team_id:
            clauses.append("team_id = %s")
            params.append(team_id)
        if since:
            clauses.append("timestamp_utc >= %s")
            params.append(since)
        if until:
            clauses.append("timestamp_utc <= %s")
            params.append(until)
        if kinds:
            placeholders = ",".join(["%s"] * len(kinds))
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kinds)

        query = (
            "SELECT session_id, event_id, kind, source, timestamp_utc, text, role,"
            " speaker_id, participant_id, aad_object_id, media_source_id,"
            " display_name, message_id, reply_to_message_id, from_bot, confidence,"
            " source_raw_event_ids_json, superseded_by,"
            " team_id, channel_id, channel_thread_id"
            " FROM meeting_events"
            f" WHERE {' AND '.join(clauses)}"
            " ORDER BY timestamp_utc ASC"
        )
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))

        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
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
            " normalized_payload_json, normalized_event_id, dropped_reason,"
            " team_id, channel_id, channel_thread_id"
            " FROM raw_ingest_events WHERE session_id = %s"
        )
        params: list[Any] = [session_id]
        if since:
            query += " AND received_at_utc > %s"
            params.append(since)
        query += " ORDER BY received_at_utc ASC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def iter_raw_events(self, session_id: str) -> Iterable[dict[str, Any]]:
        """Generator over raw_ingest_events for streaming NDJSON export."""
        query = (
            "SELECT raw_event_id, session_id, received_at_utc, provider_timestamp_utc,"
            " source, event_type, speaker_or_sender_id, payload_hash, raw_payload_json,"
            " normalized_payload_json, normalized_event_id, dropped_reason,"
            " team_id, channel_id, channel_thread_id"
            " FROM raw_ingest_events WHERE session_id = %s ORDER BY received_at_utc ASC"
        )
        with self._connect() as conn:
            with conn.cursor(name="iter_raw_events") as cur:
                cur.itersize = 500
                cur.execute(query, [session_id])
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
            " FROM extractions WHERE session_id = %s"
        )
        params: list[Any] = [session_id]
        if since:
            query += " AND timestamp_utc > %s"
            params.append(since)
        query += " ORDER BY timestamp_utc ASC"
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
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
            rows = conn.execute(
                "SELECT kind, payload_json FROM dossier_items"
                " WHERE session_id = %s ORDER BY first_seen_at ASC",
                (session_id,),
            ).fetchall()
            for row in rows:
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
            " FROM tool_calls WHERE session_id = %s ORDER BY timestamp_utc ASC"
        )
        params: list[Any] = [session_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            rows = [dict(row) for row in conn.execute(query, params).fetchall()]
        for row in rows:
            row["ok"] = bool(row["ok"])
            row["arguments"] = json.loads(row.pop("arguments_json") or "{}")
            row["result"] = json.loads(row.pop("result_json") or "{}")
        return rows

    # -- alfred-v2 meetings registry -------------------------------------

    def upsert_meeting_metadata(
        self,
        meeting_id: str,
        *,
        meeting_chat_thread_id: Optional[str] = None,
        subject: Optional[str] = None,
        organizer_aad_id: Optional[str] = None,
        organizer_display_name: Optional[str] = None,
        scheduled_start_utc: Optional[str] = None,
        scheduled_end_utc: Optional[str] = None,
        actual_start_utc: Optional[str] = None,
        actual_end_utc: Optional[str] = None,
        channel_team_id: Optional[str] = None,
        channel_team_display_name: Optional[str] = None,
        channel_id: Optional[str] = None,
        channel_display_name: Optional[str] = None,
        channel_thread_id: Optional[str] = None,
        channel_linked_at_utc: Optional[str] = None,
        channel_linked_source: Optional[str] = None,
        last_event_utc: Optional[str] = None,
    ) -> None:
        """Upsert a meeting record. Only non-null fields replace existing values.

        ``meeting_id`` is the canonical Graph onlineMeeting id and is the
        sole primary key. Channel link fields are populated when a
        ``meeting.linked`` event arrives (or when the meeting's payload
        already carries a ``channel_link`` block).
        """
        now = _iso_now()
        with self._lock, self._connect() as conn:
            with conn.transaction():
                existing = conn.execute(
                    "SELECT * FROM meetings WHERE meeting_id = %s",
                    (meeting_id,),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        """
                        INSERT INTO meetings (
                            meeting_id, meeting_chat_thread_id, subject,
                            organizer_aad_id, organizer_display_name,
                            scheduled_start_utc, scheduled_end_utc,
                            actual_start_utc, actual_end_utc,
                            channel_team_id, channel_team_display_name,
                            channel_id, channel_display_name, channel_thread_id,
                            channel_linked_at_utc, channel_linked_source,
                            last_event_utc, created_at_utc, updated_at_utc
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            meeting_id,
                            meeting_chat_thread_id,
                            subject,
                            organizer_aad_id,
                            organizer_display_name,
                            scheduled_start_utc,
                            scheduled_end_utc,
                            actual_start_utc,
                            actual_end_utc,
                            channel_team_id,
                            channel_team_display_name,
                            channel_id,
                            channel_display_name,
                            channel_thread_id,
                            channel_linked_at_utc,
                            channel_linked_source,
                            last_event_utc,
                            now,
                            now,
                        ),
                    )
                    return

                row = dict(existing)
                updates = {
                    "meeting_chat_thread_id": meeting_chat_thread_id or row.get("meeting_chat_thread_id"),
                    "subject": subject or row.get("subject"),
                    "organizer_aad_id": organizer_aad_id or row.get("organizer_aad_id"),
                    "organizer_display_name": organizer_display_name or row.get("organizer_display_name"),
                    "scheduled_start_utc": scheduled_start_utc or row.get("scheduled_start_utc"),
                    "scheduled_end_utc": scheduled_end_utc or row.get("scheduled_end_utc"),
                    "actual_start_utc": actual_start_utc or row.get("actual_start_utc"),
                    "actual_end_utc": actual_end_utc or row.get("actual_end_utc"),
                    "channel_team_id": channel_team_id or row.get("channel_team_id"),
                    "channel_team_display_name": channel_team_display_name or row.get("channel_team_display_name"),
                    "channel_id": channel_id or row.get("channel_id"),
                    "channel_display_name": channel_display_name or row.get("channel_display_name"),
                    "channel_thread_id": channel_thread_id or row.get("channel_thread_id"),
                    "channel_linked_at_utc": channel_linked_at_utc or row.get("channel_linked_at_utc"),
                    "channel_linked_source": channel_linked_source or row.get("channel_linked_source"),
                    "last_event_utc": last_event_utc or row.get("last_event_utc"),
                    "updated_at_utc": now,
                }
                conn.execute(
                    """
                    UPDATE meetings SET
                        meeting_chat_thread_id = %(meeting_chat_thread_id)s,
                        subject = %(subject)s,
                        organizer_aad_id = %(organizer_aad_id)s,
                        organizer_display_name = %(organizer_display_name)s,
                        scheduled_start_utc = %(scheduled_start_utc)s,
                        scheduled_end_utc = %(scheduled_end_utc)s,
                        actual_start_utc = %(actual_start_utc)s,
                        actual_end_utc = %(actual_end_utc)s,
                        channel_team_id = %(channel_team_id)s,
                        channel_team_display_name = %(channel_team_display_name)s,
                        channel_id = %(channel_id)s,
                        channel_display_name = %(channel_display_name)s,
                        channel_thread_id = %(channel_thread_id)s,
                        channel_linked_at_utc = %(channel_linked_at_utc)s,
                        channel_linked_source = %(channel_linked_source)s,
                        last_event_utc = %(last_event_utc)s,
                        updated_at_utc = %(updated_at_utc)s
                    WHERE meeting_id = %(meeting_id)s
                    """,
                    {**updates, "meeting_id": meeting_id},
                )

    def get_meeting(self, meeting_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM meetings WHERE meeting_id = %s",
                (meeting_id,),
            ).fetchone()
        return dict(row) if row else None

    def upsert_uploaded_transcript(
        self,
        meeting_id: str,
        txt: Optional[str],
        vtt: Optional[str],
        subject: Optional[str],
    ) -> None:
        """Store an operator-uploaded transcript for ``meeting_id``.

        If ``subject`` is provided, also UPDATE the meetings registry so
        ``/v2/meetings`` returns the operator-supplied subject. Insert a
        meetings row if one doesn't exist yet (needed for "+Apps"
        meetings the bot never emitted a `meeting.created` for).
        """
        now_iso = _iso_now()
        with self._lock, self._connect() as conn:
            with conn.transaction():
                conn.execute(
                    """
                    INSERT INTO transcript_uploads
                        (meeting_id, txt, vtt, subject, uploaded_at_utc)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (meeting_id) DO UPDATE SET
                        txt = EXCLUDED.txt,
                        vtt = EXCLUDED.vtt,
                        subject = COALESCE(EXCLUDED.subject, transcript_uploads.subject),
                        uploaded_at_utc = EXCLUDED.uploaded_at_utc
                    """,
                    (meeting_id, txt, vtt, subject, now_iso),
                )
                if subject is not None and subject.strip():
                    conn.execute(
                        """
                        INSERT INTO meetings
                            (meeting_id, meeting_chat_thread_id, subject,
                             last_event_utc, created_at_utc, updated_at_utc)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        ON CONFLICT (meeting_id) DO UPDATE SET
                            subject = EXCLUDED.subject,
                            last_event_utc = EXCLUDED.last_event_utc,
                            updated_at_utc = EXCLUDED.updated_at_utc
                        """,
                        (meeting_id, meeting_id, subject.strip(), now_iso, now_iso, now_iso),
                    )

    def set_meeting_subject(self, meeting_id: str, subject: str) -> None:
        """Operator-set the meeting subject. Used to rename a meeting
        after upload (e.g. when the user didn't get a chance to set a
        title at upload time, or wants to fix it later). Creates a
        meetings row if one doesn't exist."""
        now_iso = _iso_now()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO meetings
                    (meeting_id, meeting_chat_thread_id, subject,
                     last_event_utc, created_at_utc, updated_at_utc)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (meeting_id) DO UPDATE SET
                    subject = EXCLUDED.subject,
                    updated_at_utc = EXCLUDED.updated_at_utc
                """,
                (meeting_id, meeting_id, subject, now_iso, now_iso, now_iso),
            )

    def get_uploaded_transcript(self, meeting_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT txt, vtt, subject, uploaded_at_utc FROM transcript_uploads"
                " WHERE meeting_id = %s",
                (meeting_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_meeting_by_chat_thread_id(self, chat_thread_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM meetings WHERE meeting_chat_thread_id = %s",
                (chat_thread_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_meetings_v2(
        self,
        limit: Optional[int] = None,
        team_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List meetings, newest first. Optional since/until filter the
        meeting by its most informative timestamp (actual start when known,
        scheduled start otherwise, created_at as the last fallback) —
        matches the ORDER BY column so a range query and a recency-bound
        query stay consistent."""
        clauses: list[str] = []
        params: list[Any] = []
        if team_id:
            clauses.append("channel_team_id = %s")
            params.append(team_id)
        if channel_id:
            clauses.append("channel_id = %s")
            params.append(channel_id)
        if since:
            clauses.append(
                "COALESCE(actual_start_utc, scheduled_start_utc, created_at_utc) >= %s"
            )
            params.append(since)
        if until:
            clauses.append(
                "COALESCE(actual_start_utc, scheduled_start_utc, created_at_utc) <= %s"
            )
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        query = (
            "SELECT * FROM meetings"
            + where
            + " ORDER BY COALESCE(last_event_utc, scheduled_start_utc, created_at_utc) DESC"
        )
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def search_meetings_by_subject(
        self,
        query: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Case-insensitive substring match against ``subject``.

        Empty / whitespace-only queries return the most recent meetings —
        the agent uses this as a "list_meetings" fallback.
        """
        normalized = (query or "").strip().lower()
        if not normalized:
            return self.list_meetings_v2(limit=limit)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM meetings"
                " WHERE LOWER(COALESCE(subject, '')) LIKE %s"
                " ORDER BY COALESCE(last_event_utc, scheduled_start_utc, created_at_utc) DESC"
                " LIMIT %s",
                (f"%{normalized}%", int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_envelope(
        self,
        envelope_id: str,
        *,
        schema_version: str,
        event_type: str,
        ts: str,
        raw_json: str,
        meeting_id: Optional[str] = None,
        team_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        thread_id: Optional[str] = None,
    ) -> bool:
        """Append a raw v2 envelope row. Idempotent on ``envelope_id``.

        Returns:
            True if a new row was inserted, False if the envelope_id
            was already present (duplicate). Callers should skip
            event dispatch when False — the analyzer has already
            processed this event in a prior call.

        Why this matters:
            Graph subscription notifications can be delivered more
            than once (documented Microsoft behavior), and during
            sink container rolling deploys the brief termination
            grace window can put a single event into both the
            terminating and starting replicas. Without dedup, the
            agent would analyze the same event twice and could
            produce duplicate `send_to_meeting_chat` calls.
        """
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO raw_ingest_envelopes (
                    envelope_id, schema_version, event_type, ts, received_at_utc,
                    meeting_id, team_id, channel_id, thread_id, raw_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (envelope_id) DO NOTHING
                """,
                (
                    envelope_id,
                    schema_version,
                    event_type,
                    ts,
                    _iso_now(),
                    meeting_id,
                    team_id,
                    channel_id,
                    thread_id,
                    raw_json,
                ),
            )
            return (cur.rowcount or 0) > 0

    def list_thread_messages(
        self,
        team_id: str,
        channel_id: str,
        thread_id: str,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        query = (
            "SELECT * FROM meeting_events"
            " WHERE team_id = %s AND channel_id = %s"
            "   AND (channel_thread_id = %s OR reply_to_message_id = %s OR message_id = %s)"
            " ORDER BY timestamp_utc ASC"
        )
        params: list[Any] = [team_id, channel_id, thread_id, thread_id, thread_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def list_threads_in_channel(
        self,
        team_id: str,
        channel_id: str,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Distinct thread heads observed in this channel ordered by most recent."""
        query = (
            "SELECT"
            "   COALESCE(channel_thread_id, message_id) AS thread_id,"
            "   MAX(timestamp_utc) AS last_activity_utc,"
            "   COUNT(*) AS message_count"
            " FROM meeting_events"
            " WHERE team_id = %s AND channel_id = %s AND kind = 'chat'"
            " GROUP BY COALESCE(channel_thread_id, message_id)"
            " ORDER BY last_activity_utc DESC"
        )
        params: list[Any] = [team_id, channel_id]
        if limit is not None:
            query += " LIMIT %s"
            params.append(int(limit))
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows if row["thread_id"]]


def build_store(connection_string: str) -> SessionStore:
    return SessionStore(connection_string)


_ALL_ROW_TYPES = (
    Decision,
    OpenQuestion,
    ActionItem,
    Risk,
)
