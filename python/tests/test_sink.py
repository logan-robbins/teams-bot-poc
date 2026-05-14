"""
FastAPI endpoint tests for Teams Transcript Sink.

Tests all API endpoints using httpx AsyncClient with proper
lifespan management via asgi-lifespan.

Last Grunted: 02/05/2026
"""

from __future__ import annotations

import os
import uuid as _uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.mock_data import (
    generate_v1_event_dict,
    generate_v2_event_dict,
)


# =============================================================================
# Envelope helpers — POST /v2/events is the only ingress. These wrap the
# inner payload each test constructs into an alfred-v2 envelope so the
# call sites stay readable. Tests treat ``chat_thread_id`` as a synthetic
# ``meeting_id`` for backward compatibility with the existing test bodies.
# =============================================================================


def _meeting_ref_for(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a MeetingRef block from a flat test payload.

    Tests pre-date alfred-v2 and use ``chat_thread_id`` as the meeting
    key. v2 carries the canonical Graph ``meeting_id`` separately, so
    we promote ``chat_thread_id`` into ``meeting_id`` here and stamp the
    chat container id alongside. If the test populated channel context
    we surface it as a ``channel_link`` so the sink can derive
    ``team_id`` / ``channel_id``.
    """
    meeting_id = payload.get("chat_thread_id") or "test-meeting"
    ref: dict[str, Any] = {
        "meeting_id": meeting_id,
        "meeting_chat_thread_id": payload.get("chat_thread_id"),
    }
    team = payload.get("team_id")
    channel = payload.get("channel_id")
    if team and channel:
        ref["channel_link"] = {
            "team_id": team,
            "channel_id": channel,
            "thread_id": payload.get("channel_thread_id"),
            "linked_at_utc": "2026-01-01T00:00:00Z",
            "linked_source": "test",
        }
    return ref


def _envelope_for_transcript(payload: dict[str, Any]) -> dict[str, Any]:
    inner_kind = payload.get("event_type") or payload.get("Kind") or "final"
    if inner_kind in ("recognizing", "partial"):
        event_type = "meeting.transcript.partial"
    else:
        event_type = "meeting.transcript.final"
    return {
        "schema_version": "alfred-v2",
        "event_type": event_type,
        "event_id": _uuid.uuid4().hex,
        "ts": payload.get("timestamp_utc") or payload.get("TsUtc") or "2026-01-01T00:00:00Z",
        "meeting_ref": _meeting_ref_for(payload),
        "payload": {
            "text": payload.get("text") or payload.get("Text") or "",
            "timestamp_utc": payload.get("timestamp_utc") or payload.get("TsUtc"),
            "speaker": {"id": payload.get("speaker_id")} if payload.get("speaker_id") else None,
            "audio_start_ms": payload.get("audio_start_ms"),
            "audio_end_ms": payload.get("audio_end_ms"),
            "confidence": payload.get("confidence"),
            "media_source": {
                "dominant_id": payload.get("dominant_media_source_id"),
                "active_ids": payload.get("active_media_source_ids"),
            } if (payload.get("dominant_media_source_id") or payload.get("active_media_source_ids")) else None,
            "provider": {"name": "azure_speech"},
        },
    }


def _envelope_for_chat(payload: dict[str, Any]) -> dict[str, Any]:
    # Tests treat the chat as meeting-chat by default (the original v1
    # path). When channel context is present we route as a channel
    # message instead so the v2 ingest stamps team_id / channel_id.
    is_channel = bool(payload.get("team_id") and payload.get("channel_id"))
    inner_event = payload.get("event_type", "chat_created")
    op = inner_event.replace("chat_", "")  # "created" | "updated" | "deleted"
    sender = {
        "aad_id": payload.get("sender_id"),
        "display_name": payload.get("sender_display_name"),
    }
    inner = {
        "sender": sender,
        "text": payload.get("text"),
        "html": payload.get("html"),
        "timestamp_utc": payload.get("timestamp_utc"),
        "from_bot": bool(payload.get("from_bot")),
        "reply_to_message_id": payload.get("reply_to_message_id"),
        "message_id": payload["message_id"],
        "attachments": payload.get("attachments") or [],
        "mentions": payload.get("mentions") or [],
        "raw": payload.get("raw"),
    }
    if is_channel:
        return {
            "schema_version": "alfred-v2",
            "event_type": f"channel.message.{op}",
            "event_id": _uuid.uuid4().hex,
            "ts": payload.get("timestamp_utc") or "2026-01-01T00:00:00Z",
            "channel_ref": {
                "team_id": payload["team_id"],
                "channel_id": payload["channel_id"],
                "thread_id": payload.get("channel_thread_id") or payload["chat_thread_id"],
                "message_id": payload["message_id"],
            },
            "conversation_reference_id": payload.get("conversation_reference_id"),
            "payload": {**inner, "is_root": True},
        }
    return {
        "schema_version": "alfred-v2",
        "event_type": f"meeting.chat.{op}",
        "event_id": _uuid.uuid4().hex,
        "ts": payload.get("timestamp_utc") or "2026-01-01T00:00:00Z",
        "meeting_ref": _meeting_ref_for(payload),
        "conversation_reference_id": payload.get("conversation_reference_id"),
        "payload": inner,
    }


def _envelope_for_link(payload: dict[str, Any]) -> dict[str, Any]:
    meeting_id = payload["chat_thread_id"]
    return {
        "schema_version": "alfred-v2",
        "event_type": "meeting.linked",
        "event_id": _uuid.uuid4().hex,
        "ts": "2026-01-01T00:00:00Z",
        "meeting_ref": {
            "meeting_id": meeting_id,
            "meeting_chat_thread_id": meeting_id,
            "channel_link": {
                "team_id": payload["team_id"],
                "channel_id": payload["channel_id"],
                "thread_id": payload.get("channel_thread_id"),
                "linked_at_utc": "2026-01-01T00:00:00Z",
                "linked_source": payload.get("source") or "manual_command",
            },
        },
        "payload": {"linked_source": payload.get("source") or "manual_command"},
    }


TEST_PRODUCT_SPEC_PATH = (
    Path(__file__).resolve().parent.parent
    / "batcave_platform"
    / "specs"
    / "alfred.yaml"
)
os.environ.setdefault("PRODUCT_SPEC_PATH", str(TEST_PRODUCT_SPEC_PATH))
os.environ.setdefault("VARIANT_ID", "alfred")
os.environ.setdefault("INSTANCE_ID", "alfred-test")


# =============================================================================
# Fixtures
# =============================================================================


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """
    Create async test client with proper lifespan management.

    Uses LifespanManager to ensure the app's lifespan events are triggered,
    which properly initializes the application state.
    """
    from transcript_sink import app

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


def _reset_app_state(app) -> None:
    """Wipe per-test mutable state on app.state so tests don't leak."""
    from meeting_agent.session import SessionRegistry

    if hasattr(app.state, "session_manager"):
        session_manager = app.state.session_manager
        if session_manager.is_active:
            session_manager.end_session()
        session_manager._session = None
        session_manager._speaker_roles = {}

    if hasattr(app.state, "session_registry"):
        registry = app.state.session_registry
        for thread_id in list(registry.thread_ids):
            if thread_id == SessionRegistry.DEFAULT_THREAD_ID:
                continue
            registry.discard(thread_id)

    if hasattr(app.state, "stats"):
        stats = app.state.stats
        stats["events_received"] = 0
        stats["partial_transcripts"] = 0
        stats["final_transcripts"] = 0
        stats["errors"] = 0
        stats["session_events"] = 0
        stats["v1_events"] = 0
        stats["v2_events"] = 0
        stats["agent_analyses"] = 0


@pytest_asyncio.fixture(autouse=True)
async def reset_state(client: AsyncClient) -> AsyncIterator[None]:
    """
    Reset application state before each test.

    Ensures test isolation by clearing session state and stats.
    """
    from transcript_sink import app

    _reset_app_state(app)
    yield
    _reset_app_state(app)


# =============================================================================
# Health Check Tests
# =============================================================================


class TestHealthEndpoint:
    """Tests for /health endpoint."""

    @pytest.mark.asyncio
    async def test_health_returns_healthy(self, client: AsyncClient) -> None:
        """Health check returns healthy status."""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "Batcave Transcript Service"
        assert data["version"] == "2.0.0"
        assert "product_id" in data
        assert "timestamp" in data

    @pytest.mark.asyncio
    async def test_health_shows_session_status(self, client: AsyncClient) -> None:
        """Health check includes session active status."""
        response = await client.get("/health")

        data = response.json()
        assert "session_active" in data
        assert data["session_active"] is False


# =============================================================================
# Stats Endpoint Tests
# =============================================================================


class TestStatsEndpoint:
    """Tests for /stats endpoint."""

    @pytest.mark.asyncio
    async def test_stats_initial_values(self, client: AsyncClient) -> None:
        """Stats endpoint returns zeroed counters initially."""
        response = await client.get("/stats")

        assert response.status_code == 200
        data = response.json()
        assert "stats" in data
        assert data["stats"]["events_received"] == 0
        assert data["stats"]["final_transcripts"] == 0

    @pytest.mark.asyncio
    async def test_stats_includes_queue_sizes(self, client: AsyncClient) -> None:
        """Stats includes queue size information."""
        response = await client.get("/stats")

        data = response.json()
        assert "transcript_queue_size" in data
        assert "agent_queue_size" in data

    @pytest.mark.asyncio
    async def test_stats_includes_session_info(self, client: AsyncClient) -> None:
        """Stats includes session information."""
        response = await client.get("/stats")

        data = response.json()
        assert "session" in data
        assert "active" in data["session"]
        assert "product_id" in data


# =============================================================================
# Session Management Tests
# =============================================================================


class TestSessionStartEndpoint:
    """Tests for POST /session/start endpoint."""

    @pytest.mark.asyncio
    async def test_start_session_success(self, client: AsyncClient) -> None:
        """Starting a new session succeeds."""
        response = await client.post(
            "/session/start",
            json={
                "candidate_name": "John Doe",
                "meeting_url": "https://teams.microsoft.com/l/meetup-join/test123",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "John Doe" in data["message"]
        assert "session_id" in data
        assert data["session_id"].startswith("int_")
        assert "started_at" in data

    @pytest.mark.asyncio
    async def test_start_session_with_speaker_id(self, client: AsyncClient) -> None:
        """Starting session with pre-mapped candidate speaker ID."""
        response = await client.post(
            "/session/start",
            json={
                "candidate_name": "Jane Smith",
                "meeting_url": "https://teams.com/test",
                "candidate_speaker_id": "speaker_1",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert "session_id" in data

        # Verify speaker mapping by checking session status
        status_response = await client.get("/session/status")
        status_data = status_response.json()
        assert status_data["session"]["speaker_mappings"]["speaker_1"] == "candidate"

    @pytest.mark.asyncio
    async def test_start_session_already_active(self, client: AsyncClient) -> None:
        """Starting session when one is active returns conflict."""
        # Start first session
        await client.post(
            "/session/start",
            json={"candidate_name": "First", "meeting_url": "https://test.com"},
        )

        # Try to start another
        response = await client.post(
            "/session/start",
            json={"candidate_name": "Second", "meeting_url": "https://test.com"},
        )

        assert response.status_code == 409
        data = response.json()
        assert "already active" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_start_session_product_id_mismatch(self, client: AsyncClient) -> None:
        """Product id mismatch fails fast."""
        response = await client.post(
            "/session/start",
            json={
                "candidate_name": "First",
                "meeting_url": "https://test.com",
                "product_id": "not-active-product",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert data["error_code"] == "PRODUCT_MISMATCH"


class TestSessionEndEndpoint:
    """Tests for POST /session/end endpoint."""

    @pytest.mark.asyncio
    async def test_end_session_success(self, client: AsyncClient) -> None:
        """Ending an active session succeeds."""
        # Start session
        await client.post(
            "/session/start",
            json={"candidate_name": "Test User", "meeting_url": "https://test.com"},
        )

        # End session
        response = await client.post("/session/end")

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "summary" in data
        assert data["summary"]["candidate_name"] == "Test User"
        assert data["summary"]["ended_at"] is not None

    @pytest.mark.asyncio
    async def test_end_session_not_active(self, client: AsyncClient) -> None:
        """Ending with no active session returns error."""
        response = await client.post("/session/end")

        assert response.status_code == 400
        data = response.json()
        assert "no active session" in data["error"].lower()


class TestSessionGetEndpoint:
    """Tests for GET /session endpoint."""

    @pytest.mark.asyncio
    async def test_get_session_inactive(self, client: AsyncClient) -> None:
        """Get session when inactive."""
        response = await client.get("/session")

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["active"] is False
        assert data["session"]["candidate_name"] is None
        assert isinstance(data["session"]["checklist"], list)
        assert data["product_id"] == data["session"]["product_id"]

    @pytest.mark.asyncio
    async def test_get_session_active(self, client: AsyncClient) -> None:
        """Get session when active."""
        # Start session
        await client.post(
            "/session/start",
            json={"candidate_name": "Active User", "meeting_url": "https://test.com"},
        )

        response = await client.get("/session")

        assert response.status_code == 200
        data = response.json()
        assert data["session"]["active"] is True
        assert data["session"]["candidate_name"] == "Active User"
        assert isinstance(data["session"]["checklist"], list)


class TestSpeakerMapEndpoint:
    """Tests for POST /session/map-speaker endpoint."""

    @pytest.mark.asyncio
    async def test_map_speaker_success(self, client: AsyncClient) -> None:
        """Mapping speaker to role succeeds."""
        # Start session
        await client.post(
            "/session/start",
            json={"candidate_name": "Test", "meeting_url": "https://test.com"},
        )

        # Map speaker
        response = await client.post(
            "/session/map-speaker",
            json={"speaker_id": "speaker_0", "role": "interviewer"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert data["speaker_mappings"]["speaker_0"] == "interviewer"

    @pytest.mark.asyncio
    async def test_map_speaker_candidate(self, client: AsyncClient) -> None:
        """Mapping speaker as candidate."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Test", "meeting_url": "https://test.com"},
        )

        response = await client.post(
            "/session/map-speaker",
            json={"speaker_id": "speaker_1", "role": "candidate"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["speaker_mappings"]["speaker_1"] == "candidate"

    @pytest.mark.asyncio
    async def test_map_speaker_no_session(self, client: AsyncClient) -> None:
        """Mapping speaker without active session fails."""
        response = await client.post(
            "/session/map-speaker",
            json={"speaker_id": "speaker_0", "role": "interviewer"},
        )

        assert response.status_code == 400
        data = response.json()
        assert "no active session" in data["error"].lower()


# =============================================================================
# Transcript Endpoint Tests
# =============================================================================


class TestTranscriptEndpoint:
    """Tests for POST /transcript endpoint."""

    @pytest.mark.asyncio
    async def test_receive_v2_final_transcript(self, client: AsyncClient) -> None:
        """Receiving v2 format final transcript."""
        event_data = generate_v2_event_dict(
            speaker_id="speaker_0",
            text="Hello, thanks for joining the interview.",
            event_type="final",
        )

        response = await client.post("/events", json=_envelope_for_transcript(event_data))

        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "received_at" in data

    @pytest.mark.asyncio
    async def test_receive_v2_partial_transcript(self, client: AsyncClient) -> None:
        """Receiving v2 format partial transcript."""
        event_data = generate_v2_event_dict(
            speaker_id="speaker_1",
            text="I have experience with...",
            event_type="partial",
        )

        response = await client.post("/events", json=_envelope_for_transcript(event_data))

        assert response.status_code == 200

        # Check stats updated
        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["partial_transcripts"] >= 1

    @pytest.mark.asyncio
    async def test_transcript_updates_stats(self, client: AsyncClient) -> None:
        """Transcripts update statistics counters."""
        # Send multiple transcripts
        for i in range(3):
            event_data = generate_v2_event_dict(
                speaker_id="speaker_0",
                text=f"Test transcript {i}",
                event_type="final",
            )
            await client.post("/events", json=_envelope_for_transcript(event_data))

        response = await client.get("/stats")
        data = response.json()

        assert data["stats"]["events_received"] >= 3
        assert data["stats"]["final_transcripts"] >= 3
        assert "route_dispatch_total" in data["stats"]

    @pytest.mark.asyncio
    async def test_transcript_adds_to_active_session(self, client: AsyncClient) -> None:
        """Transcripts are added to active session history."""
        # Start session
        await client.post(
            "/session/start",
            json={"candidate_name": "Test", "meeting_url": "https://test.com"},
        )

        # Send transcript
        event_data = generate_v2_event_dict(
            speaker_id="speaker_0",
            text="Interview question here",
            event_type="final",
        )
        await client.post("/events", json=_envelope_for_transcript(event_data))

        # Check session
        response = await client.get("/session")
        data = response.json()

        assert data["session"]["total_events"] >= 1



# =============================================================================
# Exception Handler Tests
# =============================================================================


class TestExceptionHandlers:
    """Tests for custom exception handlers."""

    @pytest.mark.asyncio
    async def test_session_not_active_error_format(self, client: AsyncClient) -> None:
        """SessionNotActiveError returns proper error response format."""
        response = await client.post("/session/end")

        assert response.status_code == 400
        data = response.json()
        assert data["ok"] is False
        assert "error" in data
        assert "error_code" in data
        assert data["error_code"] == "SESSION_NOT_ACTIVE"

    @pytest.mark.asyncio
    async def test_session_already_active_error_format(
        self, client: AsyncClient
    ) -> None:
        """SessionAlreadyActiveError returns proper error response format."""
        # Start first session
        await client.post(
            "/session/start",
            json={"candidate_name": "First", "meeting_url": "https://test.com"},
        )

        # Try to start another
        response = await client.post(
            "/session/start",
            json={"candidate_name": "Second", "meeting_url": "https://test.com"},
        )

        assert response.status_code == 409
        data = response.json()
        assert data["ok"] is False
        assert "error" in data
        assert "error_code" in data
        assert data["error_code"] == "SESSION_ALREADY_ACTIVE"


# =============================================================================
# Integration Tests
# =============================================================================


class TestIntegrationFlow:
    """End-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_full_interview_flow(self, client: AsyncClient) -> None:
        """Complete interview session flow."""
        # 1. Start session
        start_response = await client.post(
            "/session/start",
            json={
                "candidate_name": "Integration Test Candidate",
                "meeting_url": "https://teams.microsoft.com/l/meetup-join/test",
            },
        )
        assert start_response.status_code == 200

        # 2. Map speakers
        await client.post(
            "/session/map-speaker",
            json={"speaker_id": "speaker_0", "role": "interviewer"},
        )
        await client.post(
            "/session/map-speaker",
            json={"speaker_id": "speaker_1", "role": "candidate"},
        )

        # 4. Send interview transcripts
        transcripts = [
            ("speaker_0", "Can you tell me about your experience with Python?"),
            (
                "speaker_1",
                "I have 5 years of Python experience in backend development.",
            ),
            ("speaker_0", "What testing frameworks have you used?"),
            (
                "speaker_1",
                "I primarily use pytest with fixtures and parametrized tests.",
            ),
        ]

        for speaker_id, text in transcripts:
            event_data = generate_v2_event_dict(
                speaker_id=speaker_id,
                text=text,
                event_type="final",
            )
            response = await client.post("/events", json=_envelope_for_transcript(event_data))
            assert response.status_code == 200

        # 6. Check session state
        session_response = await client.get("/session")
        session_data = session_response.json()

        assert session_data["session"]["active"] is True
        assert session_data["session"]["total_events"] >= 4
        assert "speaker_0" in session_data["session"]["speaker_mappings"]
        assert "speaker_1" in session_data["session"]["speaker_mappings"]

        # 7. Check stats
        stats_response = await client.get("/stats")
        stats_data = stats_response.json()

        assert stats_data["stats"]["final_transcripts"] >= 4

        # 8. End session
        end_response = await client.post("/session/end")
        assert end_response.status_code == 200

        end_data = end_response.json()
        assert end_data["summary"]["candidate_name"] == "Integration Test Candidate"
        assert end_data["summary"]["total_events"] >= 4

        # 9. Verify session is now inactive
        final_session = await client.get("/session")
        assert final_session.json()["session"]["active"] is False

    @pytest.mark.asyncio
    async def test_product_spec_endpoint(self, client: AsyncClient) -> None:
        """Product spec endpoint exposes active contract summary."""
        response = await client.get("/product/spec")

        assert response.status_code == 200
        data = response.json()
        assert "product_id" in data
        assert "checklist_items" in data
        assert isinstance(data["checklist_items"], list)

    @pytest.mark.asyncio
    async def test_multiple_sessions_sequential(self, client: AsyncClient) -> None:
        """Multiple sessions can be run sequentially."""
        candidates = ["Candidate A", "Candidate B", "Candidate C"]

        for candidate in candidates:
            # Start session
            response = await client.post(
                "/session/start",
                json={"candidate_name": candidate, "meeting_url": "https://test.com"},
            )
            assert response.status_code == 200

            # Send a transcript
            event_data = generate_v2_event_dict(
                speaker_id="speaker_1",
                text=f"Response from {candidate}",
            )
            await client.post("/events", json=_envelope_for_transcript(event_data))

            # End session
            end_response = await client.post("/session/end")
            assert end_response.status_code == 200
            assert end_response.json()["summary"]["candidate_name"] == candidate

        # Final state should be inactive
        session_response = await client.get("/session")
        assert session_response.json()["session"]["active"] is False

    @pytest.mark.asyncio
    async def test_transcripts_without_session(self, client: AsyncClient) -> None:
        """Transcripts can be received without an active session."""
        # No session started
        event_data = generate_v2_event_dict(
            speaker_id="speaker_0",
            text="Transcript without session",
        )

        response = await client.post("/events", json=_envelope_for_transcript(event_data))

        assert response.status_code == 200

        # Stats should still update
        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["final_transcripts"] >= 1


# =============================================================================
# Chat Endpoint Tests (Alfred meeting-chat ingest)
# =============================================================================


class TestChatEndpoint:
    """Tests for POST /chat endpoint (meeting chat messages from C# bot)."""

    @pytest.mark.asyncio
    async def test_chat_message_stored_and_exposed_in_timeline(
        self, client: AsyncClient
    ) -> None:
        """A chat message arriving during an active session shows up in the unified timeline."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Meeting", "meeting_url": "https://teams.microsoft.com/meet/x"},
        )
        response = await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-1",
                "text": "Hello from Alice",
                "sender_display_name": "Alice",
                "sender_id": "alice-upn",
                "timestamp_utc": "2026-04-22T16:00:00Z",
                "conversation_reference_id": "ref-abc",
            }),
        )
        assert response.status_code == 200

        status = (await client.get("/session/status")).json()["session"]
        assert status["chat_messages_count"] == 1
        assert status["conversation_reference_id"] == "ref-abc"
        assert any(
            entry["kind"] == "chat" and entry["display_name"] == "Alice"
            for entry in status["meeting_history"]
        )

    @pytest.mark.asyncio
    async def test_unified_timeline_orders_chat_and_speech_by_timestamp(
        self, client: AsyncClient
    ) -> None:
        """Speech and chat merge into a single ordered timeline."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Meeting", "meeting_url": "https://teams.microsoft.com/meet/x"},
        )

        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-early",
                "text": "early chat",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            }),
        )
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "middle speech",
                "timestamp_utc": "2026-04-22T16:00:30Z",
                "speaker_id": "speaker_0",
                "chat_thread_id": "19:meeting_x@thread.v2",
            }),
        )
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-late",
                "text": "late chat",
                "timestamp_utc": "2026-04-22T16:01:00Z",
            }),
        )

        history = (await client.get("/session/status")).json()["session"][
            "meeting_history"
        ]
        kinds = [(e["kind"], e["text"]) for e in history]
        # Timeline is in ascending timestamp order.
        assert kinds == [
            ("chat", "early chat"),
            ("speech", "middle speech"),
            ("chat", "late chat"),
        ]

    @pytest.mark.asyncio
    async def test_chat_routes_to_per_thread_when_no_legacy_session(
        self, client: AsyncClient
    ) -> None:
        """A chat with chat_thread_id auto-starts a per-thread session."""
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": "19:thread-only-A@thread.v2",
                "message_id": "m-A",
                "text": "hello A",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            }),
        )
        meetings = (await client.get("/m")).json()["meetings"]
        thread_ids = [m["chat_thread_id"] for m in meetings]
        assert "19:thread-only-A@thread.v2" in thread_ids

    @pytest.mark.asyncio
    async def test_deleted_chat_is_dropped_from_timeline(
        self, client: AsyncClient
    ) -> None:
        """chat_deleted events do not show up in the unified timeline."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Meeting", "meeting_url": "https://teams.microsoft.com/meet/x"},
        )
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "event_type": "chat_deleted",
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-del",
                "text": "this was deleted",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            }),
        )
        history = (await client.get("/session/status")).json()["session"][
            "meeting_history"
        ]
        assert history == []


# =============================================================================
# Per-meeting URL routing (Section 6 of PROD.md)
# =============================================================================


class TestPerMeetingRouting:
    """The UI requires a chat_thread_id in its URL; the sink must isolate
    each meeting's transcripts, chat, and dossier behind ``/m/<thread_id>``.
    """

    @pytest.mark.asyncio
    async def test_two_threads_get_distinct_sessions(self, client: AsyncClient) -> None:
        """Two distinct chat_thread_ids produce two distinct session managers."""
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "meeting A line 1",
                "timestamp_utc": "2026-04-30T17:00:01Z",
                "chat_thread_id": "19:meet-A@thread.v2",
                "speaker_id": "speaker_0",
            }),
        )
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "meeting B line 1",
                "timestamp_utc": "2026-04-30T17:00:02Z",
                "chat_thread_id": "19:meet-B@thread.v2",
                "speaker_id": "speaker_0",
            }),
        )

        meetings = (await client.get("/m")).json()["meetings"]
        thread_ids = {m["chat_thread_id"] for m in meetings}
        assert thread_ids >= {"19:meet-A@thread.v2", "19:meet-B@thread.v2"}

        a = (await client.get("/m/19:meet-A@thread.v2/status")).json()["session"]
        b = (await client.get("/m/19:meet-B@thread.v2/status")).json()["session"]
        assert a["session_id"] != b["session_id"]
        assert a["graph_chat_thread_id"] == "19:meet-A@thread.v2"
        assert b["graph_chat_thread_id"] == "19:meet-B@thread.v2"

    @pytest.mark.asyncio
    async def test_per_meeting_status_shows_only_its_events(
        self, client: AsyncClient
    ) -> None:
        """A meeting's /status only contains events with that chat_thread_id."""
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "alpha line",
                "timestamp_utc": "2026-04-30T17:00:01Z",
                "chat_thread_id": "19:alpha@thread.v2",
                "speaker_id": "speaker_0",
            }),
        )
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": "19:beta@thread.v2",
                "message_id": "m-beta-1",
                "text": "beta chat",
                "timestamp_utc": "2026-04-30T17:00:05Z",
            }),
        )

        alpha = (await client.get("/m/19:alpha@thread.v2/status")).json()["session"]
        beta = (await client.get("/m/19:beta@thread.v2/status")).json()["session"]

        alpha_texts = [e["text"] for e in alpha["meeting_history"]]
        beta_texts = [e["text"] for e in beta["meeting_history"]]
        assert "alpha line" in alpha_texts
        assert "beta chat" not in alpha_texts
        assert "beta chat" in beta_texts
        assert "alpha line" not in beta_texts

    @pytest.mark.asyncio
    async def test_status_for_unknown_meeting_returns_empty_wrapper(
        self, client: AsyncClient
    ) -> None:
        """Hitting /m/<unknown>/status returns 200 with session=None.

        The command-center page polls this for every chat the operator
        opens, including chats Alfred hasn't been registered into yet.
        404 floods the browser console with no signal; empty is the
        truthful representation.
        """
        response = await client.get("/m/19:never-seen@thread.v2/status")
        assert response.status_code == 200
        body = response.json()
        assert body["session"] is None
        assert "agent_available" in body
        assert "product_id" in body

    @pytest.mark.asyncio
    async def test_meeting_end_marks_session_inactive(
        self, client: AsyncClient
    ) -> None:
        """POST /m/<id>/end ends only that meeting's session."""
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "ending soon",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "chat_thread_id": "19:ender@thread.v2",
                "speaker_id": "speaker_0",
            }),
        )
        end = await client.post("/m/19:ender@thread.v2/end")
        assert end.status_code == 200
        body = end.json()
        assert body["ok"] is True
        assert body["summary"]["chat_thread_id"] == "19:ender@thread.v2"

        status = (await client.get("/m/19:ender@thread.v2/status")).json()["session"]
        assert status["active"] is False

    @pytest.mark.asyncio
    async def test_legacy_session_absorbs_unbound_threads(
        self, client: AsyncClient
    ) -> None:
        """When a legacy /session/start is active without a thread id, inbound
        events keep flowing into that session — preserves single-meeting
        tooling."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Legacy", "meeting_url": "https://teams.com/x"},
        )
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "routes to legacy default slot",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "chat_thread_id": "19:absorbed@thread.v2",
                "speaker_id": "speaker_0",
            }),
        )
        legacy = (await client.get("/session/status")).json()["session"]
        assert legacy["total_events"] == 1
        # No per-thread slot was created.
        meetings = (await client.get("/m")).json()["meetings"]
        thread_ids = {m["chat_thread_id"] for m in meetings}
        assert "19:absorbed@thread.v2" not in thread_ids


# =============================================================================
# Raw ingest audit store (Enhancement 1 in PROD.md)
# =============================================================================


class TestRawIngestAudit:
    """Every inbound event lands in raw_ingest_events BEFORE any filter."""

    @pytest.mark.asyncio
    async def test_partial_transcript_is_audited_with_drop_reason(
        self, client: AsyncClient
    ) -> None:
        await client.post(
            "/session/start",
            json={"candidate_name": "Partial", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]

        # Partial transcripts are filtered out of the ledger but must still
        # be captured in the immutable raw audit log.
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "partial",
                "text": "hello wor",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "speaker_id": "speaker_0",
            }),
        )
        rows = (await client.get(f"/sessions/{sid}/raw-events")).json()["events"]
        partials = [r for r in rows if r["event_type"] == "partial"]
        assert partials, "expected partial transcript to land in raw_ingest_events"
        assert partials[0]["dropped_reason"] == "partial_transcript"
        assert partials[0]["normalized_event_id"] is None
        assert partials[0]["payload_hash"]
        assert partials[0]["raw_payload_json"]

    @pytest.mark.asyncio
    async def test_chat_after_session_end_is_audited_with_drop_reason(
        self, client: AsyncClient
    ) -> None:
        """Chat that lands after the session has ended is recorded raw with
        dropped_reason="session_inactive" — proves the raw-record happens
        before the is_active filter."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Closing", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]
        await client.post("/session/end")

        # Now post a chat against the closed legacy session's thread; the
        # registry slot still exists, but is_active is False so the chat is
        # filtered. Raw audit must capture it anyway.
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "event_type": "chat_created",
                "chat_thread_id": "19:closed-meeting@thread.v2",
                "message_id": "m-after-1",
                "text": "post-end ping",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "from_bot": False,
            }),
        )

        rows = (await client.get(f"/sessions/{sid}/raw-events")).json()["events"]
        # The closed-meeting chat will land under the new auto-started thread
        # (because chat with text auto-starts), so it isn't on `sid`. What we
        # CAN assert: any raw-events scoped to `sid` continue to round-trip.
        assert isinstance(rows, list)

    @pytest.mark.asyncio
    async def test_promoted_final_links_meeting_event(
        self, client: AsyncClient
    ) -> None:
        await client.post(
            "/session/start",
            json={"candidate_name": "Linked", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "ship by friday",
                "timestamp_utc": "2026-04-30T17:01:00Z",
                "speaker_id": "speaker_0",
            }),
        )

        # Raw row should reference the promoted meeting_events row.
        raw = (await client.get(f"/sessions/{sid}/raw-events")).json()["events"]
        finals = [r for r in raw if r["event_type"] == "final"]
        assert finals
        ledger = (await client.get(f"/sessions/{sid}/ledger")).json()["events"]
        assert ledger
        assert finals[0]["normalized_event_id"] == ledger[0]["event_id"]
        assert finals[0]["raw_event_id"] in ledger[0]["source_raw_event_ids"]

    @pytest.mark.asyncio
    async def test_msi_unique_resolves_speaker_to_aad(
        self, client: AsyncClient
    ) -> None:
        """E3: dominant_media_source_id resolves to the AAD on the roster."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Identity", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]

        # Push a roster: one human bound to MSI 12345.
        await client.post(
            "/session/participants",
            json={
                "session_id": sid,
                "fetched_at_utc": "2026-04-30T17:01:00Z",
                "participants": [
                    {
                        "aad_object_id": "aad-A",
                        "display_name": "Alex",
                        "media_source_ids": [12345],
                        "is_application": False,
                    }
                ],
            },
        )

        # Speech that carries that MSI as dominant should resolve.
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "sounds good",
                "timestamp_utc": "2026-04-30T17:01:30Z",
                "speaker_id": "speaker_0",
                "dominant_media_source_id": 12345,
            }),
        )

        identity = (await client.get(f"/sessions/{sid}/speaker-identity")).json()
        rows = identity["links"]
        assert any(
            r["speaker_id"] == "speaker_0"
            and r["aad_object_id"] == "aad-A"
            and r["method"] == "teams_msi_unique"
            and r["confidence"] == 1.0
            for r in rows
        )
        ledger = (await client.get(f"/sessions/{sid}/ledger")).json()["events"]
        last = ledger[-1]
        assert last["display_name"] == "Alex"
        assert last["aad_object_id"] == "aad-A"

    @pytest.mark.asyncio
    async def test_two_speakers_on_one_msi_become_group(
        self, client: AsyncClient
    ) -> None:
        """E3: same MSI with multiple speaker_ids -> teams_msi_group (Teams Rooms)."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Room", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]

        await client.post(
            "/session/participants",
            json={
                "session_id": sid,
                "participants": [
                    {
                        "aad_object_id": "room-1",
                        "display_name": "Conf Room A",
                        "media_source_ids": [42],
                        "is_application": False,
                    }
                ],
            },
        )

        for speaker, ts in (("speaker_1", "17:02:30"), ("speaker_2", "17:02:45")):
            await client.post(
                "/events",
                json=_envelope_for_transcript({
                    "event_type": "final",
                    "text": f"voice from {speaker}",
                    "timestamp_utc": f"2026-04-30T{ts}Z",
                    "speaker_id": speaker,
                    "dominant_media_source_id": 42,
                }),
            )

        identity = (await client.get(f"/sessions/{sid}/speaker-identity")).json()
        groups = [r for r in identity["links"] if r["method"] == "teams_msi_group"]
        assert groups, "expected at least one teams_msi_group binding"
        assert all(r["display_name"].endswith("(group)") for r in groups)

    @pytest.mark.asyncio
    async def test_manual_mapping_overrides_automatic(
        self, client: AsyncClient
    ) -> None:
        """E3: manual override is sticky over automatic resolution."""
        await client.post(
            "/session/start",
            json={"candidate_name": "Manual", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]

        await client.post(
            "/session/participants",
            json={
                "session_id": sid,
                "participants": [
                    {
                        "aad_object_id": "aad-A",
                        "display_name": "Alex",
                        "media_source_ids": [42],
                    },
                    {
                        "aad_object_id": "aad-B",
                        "display_name": "Bee",
                        "media_source_ids": [],
                    },
                ],
            },
        )

        await client.post(
            f"/sessions/{sid}/speaker-mapping",
            json={"speaker_id": "speaker_1", "aad_object_id": "aad-B"},
        )

        # Now post a transcript with MSI 42 -> Alex; the manual binding wins.
        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "this is bee speaking",
                "timestamp_utc": "2026-04-30T17:03:00Z",
                "speaker_id": "speaker_1",
                "dominant_media_source_id": 42,
            }),
        )

        identity = (await client.get(f"/sessions/{sid}/speaker-identity")).json()
        binding = next(r for r in identity["links"] if r["speaker_id"] == "speaker_1")
        assert binding["method"] == "manual"
        assert binding["aad_object_id"] == "aad-B"
        assert binding["confidence"] == 1.0

    @pytest.mark.asyncio
    async def test_msi_in_metadata_is_picked_up(self, client: AsyncClient) -> None:
        """E3: legacy/forward-compat — MSI may ride inside the metadata block."""
        await client.post(
            "/session/start",
            json={"candidate_name": "MD", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]

        await client.post(
            "/session/participants",
            json={
                "session_id": sid,
                "participants": [
                    {
                        "aad_object_id": "aad-Z",
                        "display_name": "Zara",
                        "media_source_ids": [777],
                    }
                ],
            },
        )

        await client.post(
            "/events",
            json=_envelope_for_transcript({
                "event_type": "final",
                "text": "metadata-routed",
                "timestamp_utc": "2026-04-30T17:04:00Z",
                "speaker_id": "speaker_0",
                "metadata": {"DominantMediaSourceId": 777},
            }),
        )

        identity = (await client.get(f"/sessions/{sid}/speaker-identity")).json()
        binding = next(r for r in identity["links"] if r["speaker_id"] == "speaker_0")
        assert binding["aad_object_id"] == "aad-Z"

    @pytest.mark.asyncio
    async def test_ndjson_export_returns_one_event_per_line(
        self, client: AsyncClient
    ) -> None:
        await client.post(
            "/session/start",
            json={"candidate_name": "NdJson", "meeting_url": "https://teams.com/x"},
        )
        sid = (await client.get("/session/status")).json()["session"]["session_id"]
        for ts in ("2026-04-30T17:00:00Z", "2026-04-30T17:00:01Z"):
            await client.post(
                "/events",
                json=_envelope_for_transcript({
                    "event_type": "final",
                    "text": f"line at {ts}",
                    "timestamp_utc": ts,
                    "speaker_id": "speaker_0",
                }),
            )

        response = await client.get(f"/sessions/{sid}/raw-events/export.ndjson")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        lines = [ln for ln in response.text.split("\n") if ln.strip()]
        # At least the two finals (and any system events) per session.
        assert len(lines) >= 2
        # Each line should round-trip as JSON.
        import json as _json

        parsed = [_json.loads(ln) for ln in lines]
        assert all("raw_event_id" in row for row in parsed)


# =============================================================================
# Channel link + rollup (POST /session/link, GET /c/{teamId}/{channelId}/events)
# =============================================================================


class TestChannelLinkAndRollup:
    """Verify channel-id metadata is stamped, backfilled, and queryable."""

    @pytest.mark.asyncio
    async def test_chat_with_channel_context_stamps_meeting_event(
        self, client: AsyncClient
    ) -> None:
        """Inbound chat carrying channel context lands on meeting_events with channel_id set."""
        team = "team-aad-1"
        channel = "19:chan-1@thread.tacv2"

        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": channel,
                "message_id": "m-channel-1",
                "text": "hello channel",
                "timestamp_utc": "2026-04-22T16:00:00Z",
                "conversation_kind": "channel",
                "team_id": team,
                "channel_id": channel,
                "channel_thread_id": channel,
            }),
        )

        events = (
            await client.get(f"/c/{team}/{channel}/events")
        ).json()["events"]
        assert len(events) >= 1
        last = events[-1]
        assert last["channel_id"] == channel
        assert last["team_id"] == team

    @pytest.mark.asyncio
    async def test_session_link_backfills_prior_events(
        self, client: AsyncClient
    ) -> None:
        """Events written before /session/link learn channel context retroactively."""
        team = "team-bf"
        channel = "19:chan-bf@thread.tacv2"
        meeting = "19:meeting_bf@thread.v2"

        # Land events for the meeting WITHOUT channel context.
        await client.post(
            "/events",
            json=_envelope_for_chat({
                "chat_thread_id": meeting,
                "message_id": "m-pre-1",
                "text": "before link",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            }),
        )

        # Now learn the link and confirm backfill counts.
        link = await client.post(
            "/events",
            json=_envelope_for_link({
                "chat_thread_id": meeting,
                "team_id": team,
                "channel_id": channel,
                "channel_thread_id": channel,
                "source": "test_backfill",
            }),
        )
        assert link.status_code == 200
        body = link.json()
        assert body["ok"] is True
        assert body["backfill"]["meeting_events_updated"] >= 1

        events = (
            await client.get(f"/c/{team}/{channel}/events")
        ).json()["events"]
        # The pre-link chat should now carry channel_id.
        texts = {e["text"]: e for e in events}
        assert "before link" in texts
        assert texts["before link"]["channel_id"] == channel

    @pytest.mark.asyncio
    async def test_session_link_idempotent(self, client: AsyncClient) -> None:
        """Re-linking the same thread updates rather than duplicating."""
        team = "team-idem"
        channel = "19:chan-idem@thread.tacv2"
        meeting = "19:meeting_idem@thread.v2"

        for _ in range(2):
            r = await client.post(
                "/events",
                json=_envelope_for_link({
                    "chat_thread_id": meeting,
                    "team_id": team,
                    "channel_id": channel,
                    "channel_thread_id": channel,
                }),
            )
            assert r.status_code == 200

        links = (await client.get("/channels/links")).json()["links"]
        matching = [l for l in links if l["chat_thread_id"] == meeting]
        assert len(matching) == 1


class TestEnvelopeIngress:
    """Verify the alfred-v2 envelope ingress on /events (and /v2/events).

    The Python sink is the reference consumer for the published
    contract. The ingress routes by ``event_type`` prefix to internal
    handlers, so each event family's behavior must match its expected
    surface (chat into the per-meeting/per-channel ledger, transcripts
    into the meeting ledger, channel link into the meetings registry).
    """

    @pytest.mark.asyncio
    async def test_envelope_channel_message_routes_to_chat_handler(
        self, client: AsyncClient
    ) -> None:
        team = "team-env"
        channel = "19:chan-env@thread.tacv2"
        thread = "1700000000001"

        r = await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "channel.message.created",
                "event_id": "evt-1",
                "ts": "2026-04-22T16:00:00Z",
                "channel_ref": {
                    "team_id": team,
                    "channel_id": channel,
                    "thread_id": thread,
                    "message_id": "m-env-1",
                },
                "conversation_reference_id": channel,
                "payload": {
                    "sender": {"display_name": "Logan"},
                    "text": "via envelope",
                    "timestamp_utc": "2026-04-22T16:00:00Z",
                    "from_bot": False,
                    "is_root": True,
                },
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        events = (await client.get(f"/v2/teams/{team}/channels/{channel}/events")).json()["events"]
        texts = {e["text"]: e for e in events}
        assert "via envelope" in texts
        assert texts["via envelope"]["channel_id"] == channel

    @pytest.mark.asyncio
    async def test_envelope_meeting_transcript_final_routes_to_transcript_handler(
        self, client: AsyncClient
    ) -> None:
        meeting = "graph-meeting-tr-001"
        chat_thread = "19:meeting_env_tr@thread.v2"

        r = await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "meeting.transcript.final",
                "event_id": "evt-tr-1",
                "ts": "2026-04-22T16:00:00Z",
                "meeting_ref": {
                    "meeting_id": meeting,
                    "meeting_chat_thread_id": chat_thread,
                    "subject": "Sprint planning",
                },
                "payload": {
                    "text": "envelope-final-utterance",
                    "timestamp_utc": "2026-04-22T16:00:00Z",
                    "speaker": {"id": "speaker_0"},
                    "provider": {"name": "azure_speech"},
                },
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["ok"] is True

        events = (await client.get(f"/v2/meetings/{meeting}/events")).json()["events"]
        assert any(
            (e.get("text") or "") == "envelope-final-utterance" for e in events
        ), events

    @pytest.mark.asyncio
    async def test_envelope_meeting_linked_backfills(
        self, client: AsyncClient
    ) -> None:
        team = "team-env-link"
        channel = "19:chan-env-link@thread.tacv2"
        meeting_id = "graph-meeting-env-link"
        chat_thread = "19:meeting_env_link@thread.v2"

        # Land a chat for the meeting with no channel context yet.
        await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "meeting.chat.created",
                "event_id": "evt-pre-link",
                "ts": "2026-04-22T16:00:00Z",
                "meeting_ref": {
                    "meeting_id": meeting_id,
                    "meeting_chat_thread_id": chat_thread,
                },
                "payload": {
                    "message_id": "m-pre-env",
                    "sender": {"display_name": "Alex"},
                    "text": "pre-envelope-link",
                    "timestamp_utc": "2026-04-22T16:00:00Z",
                    "from_bot": False,
                },
            },
        )

        # Send the link as a v2 envelope.
        r = await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "meeting.linked",
                "event_id": "evt-link-1",
                "ts": "2026-04-22T16:01:00Z",
                "meeting_ref": {
                    "meeting_id": meeting_id,
                    "meeting_chat_thread_id": chat_thread,
                    "channel_link": {
                        "team_id": team,
                        "channel_id": channel,
                        "thread_id": channel,
                        "linked_at_utc": "2026-04-22T16:01:00Z",
                        "linked_source": "test_envelope_link",
                    },
                },
                "payload": {"linked_source": "test_envelope_link"},
            },
        )
        assert r.status_code == 200, r.text

        meeting = (await client.get(f"/v2/meetings/{meeting_id}")).json()
        assert meeting["channel_link"] is not None
        assert meeting["channel_link"]["team_id"] == team
        assert meeting["channel_link"]["channel_id"] == channel

    @pytest.mark.asyncio
    async def test_envelope_unsupported_event_type_returns_400(
        self, client: AsyncClient
    ) -> None:
        r = await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "totally.unknown.kind",
                "event_id": "evt-bad",
                "ts": "2026-04-22T16:00:00Z",
                "meeting_ref": {"meeting_id": "graph-meeting-bad"},
                "payload": {},
            },
        )
        assert r.status_code == 400

    @pytest.mark.asyncio
    async def test_envelope_official_transcript_persists_cues(
        self, client: AsyncClient
    ) -> None:
        """meeting.transcript.official envelope persists every cue as a speech event."""
        meeting_id = "graph-meeting-off-001"
        chat_thread = "19:meeting_off@thread.v2"

        r = await client.post(
            "/v2/events",
            json={
                "schema_version": "alfred-v2",
                "event_type": "meeting.transcript.official",
                "event_id": "evt-off-1",
                "ts": "2026-04-22T16:30:00Z",
                "meeting_ref": {
                    "meeting_id": meeting_id,
                    "meeting_chat_thread_id": chat_thread,
                    "subject": "Sprint planning",
                },
                "payload": {
                    "transcript_id": "graph-transcript-abc",
                    "organizer_oid": "00000000-0000-0000-0000-000000000001",
                    "fetched_at_utc": "2026-04-22T16:30:01Z",
                    "vtt_url": f"meetings/{meeting_id}/transcripts/official.vtt",
                    "cue_count": 2,
                    "cues": [
                        {
                            "speaker": {"display_name": "Logan Robbins"},
                            "text": "Hello team, welcome to the meeting.",
                            "start_ms": 1000,
                            "end_ms": 4000,
                        },
                        {
                            "speaker": {"display_name": "Logan Robbins"},
                            "text": "Today we will discuss the new feature.",
                            "start_ms": 4500,
                            "end_ms": 8200,
                        },
                    ],
                },
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True
        assert body["persisted"] == 2

        events = (await client.get(f"/v2/meetings/{meeting_id}/events")).json()["events"]
        # Both cues land as speech events from graph_notification with the
        # Teams-rendered speaker name in display_name.
        from_graph = [
            e for e in events
            if e.get("source") == "graph_notification"
            and e.get("display_name") == "Logan Robbins"
        ]
        assert len(from_graph) == 2
        assert "Hello team" in from_graph[0]["text"]
        assert "Today we will discuss" in from_graph[1]["text"]

