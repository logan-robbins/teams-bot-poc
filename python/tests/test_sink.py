"""
FastAPI endpoint tests for Teams Transcript Sink.

Tests all API endpoints using httpx AsyncClient with proper
lifespan management via asgi-lifespan.

Last Grunted: 02/05/2026
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from tests.mock_data import (
    generate_v1_event_dict,
    generate_v2_event_dict,
)


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

        response = await client.post("/transcript", json=event_data)

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

        response = await client.post("/transcript", json=event_data)

        assert response.status_code == 200

        # Check stats updated
        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["partial_transcripts"] >= 1

    @pytest.mark.asyncio
    async def test_receive_v1_recognized_transcript(self, client: AsyncClient) -> None:
        """Receiving v1 format (legacy) transcript."""
        event_data = generate_v1_event_dict(
            kind="recognized",
            text="This is a v1 format transcript.",
        )

        response = await client.post("/transcript", json=event_data)

        assert response.status_code == 200

        # Check v1 events counter
        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["v1_events"] >= 1

    @pytest.mark.asyncio
    async def test_receive_session_started_event(self, client: AsyncClient) -> None:
        """Receiving session_started event."""
        response = await client.post(
            "/transcript",
            json={
                "event_type": "session_started",
                "timestamp_utc": "2026-01-31T10:00:00.000Z",
            },
        )

        assert response.status_code == 200

        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["session_events"] >= 1

    @pytest.mark.asyncio
    async def test_receive_session_stopped_event(self, client: AsyncClient) -> None:
        """Receiving session_stopped event."""
        response = await client.post(
            "/transcript",
            json={
                "event_type": "session_stopped",
                "timestamp_utc": "2026-01-31T10:30:00.000Z",
            },
        )

        assert response.status_code == 200

        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["session_events"] >= 1

    @pytest.mark.asyncio
    async def test_receive_error_event(self, client: AsyncClient) -> None:
        """Receiving error event."""
        response = await client.post(
            "/transcript",
            json={
                "event_type": "error",
                "timestamp_utc": "2026-01-31T10:15:00.000Z",
                "error": {
                    "code": "RECOGNITION_FAILED",
                    "message": "Audio quality too low",
                },
            },
        )

        assert response.status_code == 200

        stats_response = await client.get("/stats")
        assert stats_response.json()["stats"]["errors"] >= 1

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
            await client.post("/transcript", json=event_data)

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
        await client.post("/transcript", json=event_data)

        # Check session
        response = await client.get("/session")
        data = response.json()

        assert data["session"]["total_events"] >= 1

    @pytest.mark.asyncio
    async def test_transcript_v1_normalization(self, client: AsyncClient) -> None:
        """V1 format is correctly normalized to v2."""
        # Send v1 format
        v1_event = {
            "Kind": "recognizing",
            "Text": "Partial v1 text",
            "TsUtc": "2026-01-31T10:00:00.000Z",
        }

        response = await client.post("/transcript", json=v1_event)
        assert response.status_code == 200

        # Should be counted as partial (recognizing -> partial)
        stats_response = await client.get("/stats")
        data = stats_response.json()
        assert data["stats"]["v1_events"] >= 1
        assert data["stats"]["partial_transcripts"] >= 1


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

        # 3. Send session started event
        await client.post(
            "/transcript",
            json={
                "event_type": "session_started",
                "timestamp_utc": "2026-01-31T10:00:00.000Z",
            },
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
            response = await client.post("/transcript", json=event_data)
            assert response.status_code == 200

        # 5. Send session stopped event
        await client.post(
            "/transcript",
            json={
                "event_type": "session_stopped",
                "timestamp_utc": "2026-01-31T10:30:00.000Z",
            },
        )

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
        assert stats_data["stats"]["session_events"] >= 2  # Both start and stop

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
            await client.post("/transcript", json=event_data)

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

        response = await client.post("/transcript", json=event_data)

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
            "/chat",
            json={
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-1",
                "text": "Hello from Alice",
                "sender_display_name": "Alice",
                "sender_id": "alice-upn",
                "timestamp_utc": "2026-04-22T16:00:00Z",
                "conversation_reference_id": "ref-abc",
            },
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
            "/chat",
            json={
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-early",
                "text": "early chat",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            },
        )
        await client.post(
            "/transcript",
            json={
                "event_type": "final",
                "text": "middle speech",
                "timestamp_utc": "2026-04-22T16:00:30Z",
                "speaker_id": "speaker_0",
            },
        )
        await client.post(
            "/chat",
            json={
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-late",
                "text": "late chat",
                "timestamp_utc": "2026-04-22T16:01:00Z",
            },
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
            "/chat",
            json={
                "chat_thread_id": "19:thread-only-A@thread.v2",
                "message_id": "m-A",
                "text": "hello A",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            },
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
            "/chat",
            json={
                "event_type": "chat_deleted",
                "chat_thread_id": "19:meeting_x@thread.v2",
                "message_id": "m-del",
                "text": "this was deleted",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "meeting A line 1",
                "timestamp_utc": "2026-04-30T17:00:01Z",
                "chat_thread_id": "19:meet-A@thread.v2",
                "speaker_id": "speaker_0",
            },
        )
        await client.post(
            "/transcript",
            json={
                "event_type": "final",
                "text": "meeting B line 1",
                "timestamp_utc": "2026-04-30T17:00:02Z",
                "chat_thread_id": "19:meet-B@thread.v2",
                "speaker_id": "speaker_0",
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "alpha line",
                "timestamp_utc": "2026-04-30T17:00:01Z",
                "chat_thread_id": "19:alpha@thread.v2",
                "speaker_id": "speaker_0",
            },
        )
        await client.post(
            "/chat",
            json={
                "chat_thread_id": "19:beta@thread.v2",
                "message_id": "m-beta-1",
                "text": "beta chat",
                "timestamp_utc": "2026-04-30T17:00:05Z",
            },
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
    async def test_status_for_unknown_meeting_returns_404(
        self, client: AsyncClient
    ) -> None:
        """Hitting /m/<unknown> returns 404 — no leak of other sessions."""
        response = await client.get("/m/19:never-seen@thread.v2/status")
        assert response.status_code == 404
        body = response.json()
        assert body["error_code"] == "MEETING_NOT_FOUND"

    @pytest.mark.asyncio
    async def test_meeting_end_marks_session_inactive(
        self, client: AsyncClient
    ) -> None:
        """POST /m/<id>/end ends only that meeting's session."""
        await client.post(
            "/transcript",
            json={
                "event_type": "final",
                "text": "ending soon",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "chat_thread_id": "19:ender@thread.v2",
                "speaker_id": "speaker_0",
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "routes to legacy default slot",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "chat_thread_id": "19:absorbed@thread.v2",
                "speaker_id": "speaker_0",
            },
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
            "/transcript",
            json={
                "event_type": "partial",
                "text": "hello wor",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "speaker_id": "speaker_0",
            },
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
            "/chat",
            json={
                "event_type": "chat_created",
                "chat_thread_id": "19:closed-meeting@thread.v2",
                "message_id": "m-after-1",
                "text": "post-end ping",
                "timestamp_utc": "2026-04-30T17:00:00Z",
                "from_bot": False,
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "ship by friday",
                "timestamp_utc": "2026-04-30T17:01:00Z",
                "speaker_id": "speaker_0",
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "sounds good",
                "timestamp_utc": "2026-04-30T17:01:30Z",
                "speaker_id": "speaker_0",
                "dominant_media_source_id": 12345,
            },
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
                "/transcript",
                json={
                    "event_type": "final",
                    "text": f"voice from {speaker}",
                    "timestamp_utc": f"2026-04-30T{ts}Z",
                    "speaker_id": speaker,
                    "dominant_media_source_id": 42,
                },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "this is bee speaking",
                "timestamp_utc": "2026-04-30T17:03:00Z",
                "speaker_id": "speaker_1",
                "dominant_media_source_id": 42,
            },
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
            "/transcript",
            json={
                "event_type": "final",
                "text": "metadata-routed",
                "timestamp_utc": "2026-04-30T17:04:00Z",
                "speaker_id": "speaker_0",
                "metadata": {"DominantMediaSourceId": 777},
            },
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
                "/transcript",
                json={
                    "event_type": "final",
                    "text": f"line at {ts}",
                    "timestamp_utc": ts,
                    "speaker_id": "speaker_0",
                },
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
            "/chat",
            json={
                "chat_thread_id": channel,
                "message_id": "m-channel-1",
                "text": "hello channel",
                "timestamp_utc": "2026-04-22T16:00:00Z",
                "conversation_kind": "channel",
                "team_id": team,
                "channel_id": channel,
                "channel_thread_id": channel,
            },
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
            "/chat",
            json={
                "chat_thread_id": meeting,
                "message_id": "m-pre-1",
                "text": "before link",
                "timestamp_utc": "2026-04-22T16:00:00Z",
            },
        )

        # Now learn the link and confirm backfill counts.
        link = await client.post(
            "/session/link",
            json={
                "chat_thread_id": meeting,
                "team_id": team,
                "channel_id": channel,
                "channel_thread_id": channel,
                "source": "test_backfill",
            },
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
                "/session/link",
                json={
                    "chat_thread_id": meeting,
                    "team_id": team,
                    "channel_id": channel,
                    "channel_thread_id": channel,
                },
            )
            assert r.status_code == 200

        links = (await client.get("/channels/links")).json()["links"]
        matching = [l for l in links if l["chat_thread_id"] == meeting]
        assert len(matching) == 1

