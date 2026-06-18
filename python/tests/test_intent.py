from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from intent import IntentStore, MemoryRecord, create_app


@pytest.mark.asyncio
async def test_search_finds_sample_source(tmp_path):
    app = create_app(IntentStore(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/search", params={"q": "sqlite azure files postgres"})

    assert response.status_code == 200
    hits = response.json()["hits"]
    assert hits
    assert hits[0]["id"] == "persistence-postgres"


@pytest.mark.asyncio
async def test_v2_event_analyzes_and_persists_decision_memory(tmp_path):
    app = create_app(IntentStore(tmp_path))
    envelope = {
        "schema_version": "alfred-v2",
        "event_type": "meeting.chat.created",
        "event_id": "evt-intent-1",
        "ts": "2026-06-17T20:00:00Z",
        "meeting_ref": {
            "meeting_id": "19:meeting_demo@thread.v2",
            "meeting_chat_thread_id": "19:meeting_demo@thread.v2",
        },
        "payload": {
            "message_id": "m1",
            "sender": {"display_name": "Logan"},
            "text": "We decided to keep Postgres and avoid sqlite on Azure Files.",
            "timestamp_utc": "2026-06-17T20:00:00Z",
            "from_bot": False,
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v2/events", json=envelope)
        flushed = await client.post("/reflect/flush")
        memories = await client.get("/memories")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["queued"] == 1
    analyses = flushed.json()["analyses"]
    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis["alignment_state"] == "aligned"
    assert any(signal["kind"] == "decision" for signal in analysis["signals"])
    assert analysis["persisted_memory"] is not None
    assert len(memories.json()["memories"]) == 1


@pytest.mark.asyncio
async def test_manual_analyze_can_suppress_memory(tmp_path):
    app = create_app(IntentStore(tmp_path))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/analyze",
            json={
                "text": "We decided to test this without writing a memory.",
                "persist_memory": False,
            },
        )
        memories = await client.get("/memories")

    assert response.status_code == 200
    analysis = response.json()["analysis"]
    assert analysis["persisted_memory"] is None
    assert len(memories.json()["memories"]) == 0


@pytest.mark.asyncio
async def test_monitor_ui_and_state_show_pending_observation(tmp_path):
    app = create_app(IntentStore(tmp_path))
    envelope = {
        "schema_version": "alfred-v2",
        "event_type": "meeting.transcript.final",
        "event_id": "evt-ui-1",
        "ts": "2026-06-17T20:03:00Z",
        "meeting_ref": {
            "meeting_id": "19:meeting_demo@thread.v2",
            "meeting_chat_thread_id": "19:meeting_demo@thread.v2",
        },
        "payload": {
            "speaker": {"display_name": "Alex"},
            "text": "We agreed the UI should show pending intent observations.",
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ui = await client.get("/ui")
        root = await client.get("/")
        posted = await client.post("/v2/events", json=envelope)
        state = await client.get("/state")

    assert ui.status_code == 200
    assert "text/html" in ui.headers["content-type"]
    assert "Intent Alignment Monitor" in ui.text
    assert root.status_code == 200
    assert posted.json()["queued"] == 1
    body = state.json()
    assert body["pending_observations"] == 1
    assert body["pending"]["conversations"][0]["observations"][0]["event_id"] == "evt-ui-1"


@pytest.mark.asyncio
async def test_live_final_utterances_batch_before_reflection(tmp_path):
    app = create_app(IntentStore(tmp_path))
    base = {
        "schema_version": "alfred-v2",
        "event_type": "meeting.transcript.final",
        "ts": "2026-06-17T20:05:00Z",
        "meeting_ref": {
            "meeting_id": "19:meeting_demo@thread.v2",
            "meeting_chat_thread_id": "19:meeting_demo@thread.v2",
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.post(
            "/v2/events",
            json={
                **base,
                "event_id": "speech-1",
                "payload": {
                    "speaker": {"display_name": "Alex"},
                    "text": "We agreed the client route should point at the sink URL.",
                },
            },
        )
        second = await client.post(
            "/v2/events",
            json={
                **base,
                "event_id": "speech-2",
                "payload": {
                    "speaker": {"display_name": "Maya"},
                    "text": "Actually let's go back to v1 for this integration.",
                },
            },
        )
        flushed = await client.post("/reflect/flush")

    assert first.json()["queued"] == 1
    assert second.json()["queued"] == 1
    analyses = flushed.json()["analyses"]
    assert len(analyses) == 1
    analysis = analyses[0]
    assert analysis["event_type"] == "reflection.batch"
    assert analysis["observation_count"] == 2
    assert analysis["alignment_state"] == "possible_misalignment"
    assert analysis["next_action"] == "respond"
    assert any(signal["kind"] == "contradiction" for signal in analysis["signals"])


@pytest.mark.asyncio
async def test_official_transcript_is_not_realtime_input(tmp_path):
    app = create_app(IntentStore(tmp_path))
    envelope = {
        "schema_version": "alfred-v2",
        "event_type": "meeting.transcript.official",
        "event_id": "evt-official",
        "ts": "2026-06-17T20:05:00Z",
        "meeting_ref": {
            "meeting_id": "19:meeting_demo@thread.v2",
            "meeting_chat_thread_id": "19:meeting_demo@thread.v2",
        },
        "payload": {
            "cues": [
                {
                    "speaker": {"display_name": "Alex"},
                    "text": "Post-meeting official transcript should not drive realtime awareness.",
                },
            ]
        },
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v2/events", json=envelope)
        flushed = await client.post("/reflect/flush")

    assert response.status_code == 200
    assert response.json()["observations"] == 0
    assert flushed.json()["analyses"] == []


def test_memory_reloads_into_index(tmp_path):
    store = IntentStore(tmp_path)
    store.append_memory(
        MemoryRecord(
            speaker="Nina",
            text="Remember that Intent Alignment should persist useful memories.",
            reason="test seed",
            tags=["memory_candidate"],
        )
    )

    reloaded = IntentStore(tmp_path)
    hits = reloaded.search("persist useful memories")
    assert hits
    assert hits[0].kind == "memory"
    assert "persist useful memories" in hits[0].text
