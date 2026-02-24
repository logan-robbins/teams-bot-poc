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
    / "legionmeet_platform"
    / "specs"
    / "talestral.json"
)
os.environ.setdefault("PRODUCT_SPEC_PATH", str(TEST_PRODUCT_SPEC_PATH))


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


@pytest_asyncio.fixture(autouse=True)
async def reset_state(client: AsyncClient) -> AsyncIterator[None]:
    """
    Reset application state before each test.

    Ensures test isolation by clearing session state and stats.
    """
    from transcript_sink import app

    # Access state through lifespan-initialized app.state
    if hasattr(app.state, "session_manager"):
        session_manager = app.state.session_manager
        if session_manager.is_active:
            session_manager.end_session()
        session_manager._session = None
        session_manager._speaker_roles = {}

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

    yield

    # Cleanup after test
    if hasattr(app.state, "session_manager"):
        session_manager = app.state.session_manager
        if session_manager.is_active:
            session_manager.end_session()
        session_manager._session = None
        session_manager._speaker_roles = {}


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
        assert data["service"] == "LegionMeet Transcript Service"
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
