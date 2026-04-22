"""Tests for the SQLite-backed SessionStore."""

from __future__ import annotations

from pathlib import Path

from meeting_agent.models import (
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
from meeting_agent.persistence import build_store


def _session(session_id: str = "s1") -> InterviewSession:
    return InterviewSession(
        session_id=session_id,
        candidate_name="Weekly staff sync",
        meeting_url="https://teams.microsoft.com/l/meetup-join/test",
        started_at="2026-04-22T16:00:00Z",
    )


def test_store_round_trips_session(tmp_path: Path) -> None:
    store = build_store(tmp_path / "alfred.sqlite3")
    session = _session()
    store.upsert_session(session)

    rows = store.list_sessions()
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["candidate_name"] == "Weekly staff sync"

    fetched = store.get_session("s1")
    assert fetched is not None
    assert fetched["meeting_url"].endswith("/test")
    assert fetched["alfred_muted"] is False


def test_store_appends_meeting_event(tmp_path: Path) -> None:
    store = build_store(tmp_path / "alfred.sqlite3")
    session = _session()
    store.upsert_session(session)

    event = MeetingEvent(
        event_id="speech:2026-04-22T16:01:00Z:s0",
        kind="speech",
        source="teams_media",
        timestamp_utc="2026-04-22T16:01:00Z",
        text="We need to decide by Friday.",
        role="participant",
        speaker_id="speaker_0",
    )
    store.append_meeting_event(session.session_id, event)

    ledger = store.get_ledger(session.session_id)
    assert len(ledger) == 1
    assert ledger[0]["event_id"] == event.event_id
    assert ledger[0]["text"] == "We need to decide by Friday."
    assert ledger[0]["from_bot"] is False


def test_store_writes_extraction_and_dossier(tmp_path: Path) -> None:
    store = build_store(tmp_path / "alfred.sqlite3")
    session = _session()
    store.upsert_session(session)

    extraction = AlfredExtraction(
        rationale="captured a commitment",
        running_summary="Kickoff discussion.",
        topics=["rollout"],
        notes=["owner unclear"],
        decisions=[
            Decision(id="d1", text="Ship v2 behind a feature flag", status="committed")
        ],
        open_questions=[
            OpenQuestion(id="q1", text="Who owns the rollout?", status="open")
        ],
        action_items=[
            ActionItem(id="a1", text="Draft rollout doc", owner="Mira", status="owned")
        ],
        risks=[Risk(id="r1", text="Legal review not scheduled", severity="medium")],
    )
    item = AnalysisItem(
        response_id="resp_1",
        response_text="We agreed to ship v2 behind a flag.",
        speaker_id="speaker_0",
        extraction=extraction,
        tool_calls=[
            ToolCallRecord(
                id="tc_1",
                tool_name="send_to_meeting_chat",
                arguments={"text": "Noted.", "kind": "statement"},
                ok=True,
                result={"ok": True, "message_id": "alfred_abc"},
            )
        ],
    )
    store.append_extraction(session.session_id, item)

    dossier = store.get_dossier(session.session_id)
    assert len(dossier["decisions"]) == 1
    assert dossier["decisions"][0]["text"].startswith("Ship v2")
    assert dossier["action_items"][0]["owner"] == "Mira"
    assert dossier["risks"][0]["severity"] == "medium"

    extractions = store.get_extractions(session.session_id)
    assert len(extractions) == 1
    assert extractions[0]["rationale"] == "captured a commitment"
    assert extractions[0]["decisions"][0]["id"] == "d1"

    tool_calls = store.get_tool_calls(session.session_id)
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_name"] == "send_to_meeting_chat"
    assert tool_calls[0]["ok"] is True


def test_store_dossier_upsert_by_id(tmp_path: Path) -> None:
    store = build_store(tmp_path / "alfred.sqlite3")
    session = _session()
    store.upsert_session(session)

    first = AnalysisItem(
        response_id="r1",
        response_text="one",
        extraction=AlfredExtraction(
            decisions=[Decision(id="d1", text="original", status="tentative")],
        ),
    )
    second = AnalysisItem(
        response_id="r2",
        response_text="two",
        extraction=AlfredExtraction(
            decisions=[Decision(id="d1", text="revised", status="committed")],
        ),
    )
    store.append_extraction(session.session_id, first)
    store.append_extraction(session.session_id, second)

    dossier = store.get_dossier(session.session_id)
    assert len(dossier["decisions"]) == 1
    assert dossier["decisions"][0]["text"] == "revised"
    assert dossier["decisions"][0]["status"] == "committed"
