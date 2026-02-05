"""
Integration tests for Interview Agent components.

Tests InterviewSessionManager, AnalysisOutputWriter, and mock data generators.

Last Grunted: 02/05/2026
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from interview_agent.models import (
    AnalysisItem,
    InterviewSession,
    SessionAnalysis,
    SpeakerMapping,
    TranscriptEvent,
)
from interview_agent.output import (
    AnalysisOutputWriter,
    OutputReadError,
    OutputWriteError,
)
from interview_agent.session import InterviewSessionManager
from tests.mock_data import (
    generate_analysis_item,
    generate_interview_conversation,
    generate_session_analysis,
    generate_session_start_event,
    generate_session_stop_event,
    generate_transcript_event,
    generate_v2_event_dict,
)


# =============================================================================
# Mock Data Generator Tests
# =============================================================================

class TestMockDataGenerators:
    """Test mock data generators produce valid objects."""
    
    def test_generate_session_start_event(self):
        """Session start event has correct structure."""
        event = generate_session_start_event()
        
        assert event.event_type == "session_started"
        assert event.text is None
        assert event.speaker_id is None
        assert event.timestamp_utc is not None
        assert "Z" in event.timestamp_utc
        assert event.metadata is not None
        assert event.metadata.provider == "deepgram"
    
    def test_generate_session_stop_event(self):
        """Session stop event has correct structure."""
        event = generate_session_stop_event()
        
        assert event.event_type == "session_stopped"
        assert event.text is None
        assert event.speaker_id is None
        assert event.timestamp_utc is not None
    
    def test_generate_transcript_event_final(self):
        """Final transcript event has all required fields."""
        text = "I have 5 years of Python experience."
        event = generate_transcript_event(
            speaker_id="speaker_1",
            text=text,
            event_type="final",
        )
        
        assert event.event_type == "final"
        assert event.text == text
        assert event.speaker_id == "speaker_1"
        assert event.timestamp_utc is not None
        assert event.audio_start_ms is not None
        assert event.audio_end_ms is not None
        assert event.audio_end_ms > event.audio_start_ms
        assert 0 <= event.confidence <= 1
        assert event.metadata is not None
    
    def test_generate_transcript_event_partial(self):
        """Partial transcript events are valid."""
        event = generate_transcript_event(
            speaker_id="speaker_0",
            text="Working on...",
            event_type="partial",
        )
        
        assert event.event_type == "partial"
        assert event.text == "Working on..."
    
    def test_generate_interview_conversation_structure(self):
        """Interview conversation has correct event sequence."""
        events = generate_interview_conversation(
            candidate_name="Test Candidate",
            num_exchanges=3,
            include_session_events=True,
            include_partials=False,
        )
        
        # Should have: session_start, opening, 3*(question+response), closing, session_stop
        # Plus possible follow-ups
        assert len(events) >= 9  # Minimum expected
        
        # First event should be session start
        assert events[0].event_type == "session_started"
        
        # Last event should be session stop
        assert events[-1].event_type == "session_stopped"
        
        # All middle events should be final transcripts
        for event in events[1:-1]:
            assert event.event_type == "final"
            assert event.text is not None
            assert len(event.text) > 10
    
    def test_generate_interview_conversation_speaker_ids(self):
        """Interview conversation alternates between speakers."""
        events = generate_interview_conversation(
            num_exchanges=2,
            interviewer_speaker_id="speaker_0",
            candidate_speaker_id="speaker_1",
            include_session_events=False,
        )
        
        # Extract transcript events only
        transcript_events = [e for e in events if e.event_type == "final"]
        
        # Should have mix of both speaker IDs
        speaker_ids = {e.speaker_id for e in transcript_events}
        assert "speaker_0" in speaker_ids
        assert "speaker_1" in speaker_ids
    
    def test_generate_interview_conversation_without_session_events(self):
        """Conversation without session events starts with transcript."""
        events = generate_interview_conversation(
            num_exchanges=2,
            include_session_events=False,
        )
        
        # First event should be a transcript, not session event
        assert events[0].event_type == "final"
        assert events[-1].event_type == "final"
    
    def test_generate_analysis_item(self):
        """Analysis item has valid scores and content."""
        item = generate_analysis_item(
            response_id="resp_test_001",
            speaker_id="speaker_1",
        )
        
        assert item.response_id == "resp_test_001"
        assert item.speaker_id == "speaker_1"
        assert 0 <= item.relevance_score <= 1
        assert 0 <= item.clarity_score <= 1
        assert len(item.response_text) > 20
        assert len(item.key_points) >= 0
        assert len(item.follow_up_suggestions) >= 1
    
    def test_generate_session_analysis(self):
        """Session analysis has correct structure and computed scores."""
        analysis = generate_session_analysis(
            session_id="int_test_001",
            candidate_name="Test User",
            num_items=3,
        )
        
        assert analysis.session_id == "int_test_001"
        assert analysis.candidate_name == "Test User"
        assert len(analysis.analysis_items) == 3
        assert analysis.total_responses_analyzed == 3
        assert analysis.overall_relevance is not None
        assert analysis.overall_clarity is not None
        assert 0 <= analysis.overall_relevance <= 1
        assert 0 <= analysis.overall_clarity <= 1
    
    def test_generate_v2_event_dict(self):
        """V2 event dict matches expected JSON schema."""
        data = generate_v2_event_dict(
            speaker_id="speaker_0",
            text="Hello world",
            event_type="final",
        )
        
        assert data["event_type"] == "final"
        assert data["text"] == "Hello world"
        assert data["speaker_id"] == "speaker_0"
        assert "timestamp_utc" in data
        assert "audio_start_ms" in data
        assert "audio_end_ms" in data
        assert "confidence" in data
        assert "metadata" in data
        assert data["metadata"]["provider"] == "deepgram"
        
        # Should parse into TranscriptEvent
        event = TranscriptEvent(**data)
        assert event.text == "Hello world"


# =============================================================================
# InterviewSessionManager Tests
# =============================================================================

class TestInterviewSessionManager:
    """Tests for InterviewSessionManager class."""
    
    def test_init_no_active_session(self):
        """Manager starts with no active session."""
        manager = InterviewSessionManager()
        
        assert manager.session is None
        assert manager.is_active is False
    
    def test_start_session(self):
        """Starting a session creates valid InterviewSession."""
        manager = InterviewSessionManager()
        
        session = manager.start_session(
            candidate_name="John Doe",
            meeting_url="https://teams.microsoft.com/l/meetup-join/test",
        )
        
        assert manager.is_active is True
        assert session.candidate_name == "John Doe"
        assert session.meeting_url == "https://teams.microsoft.com/l/meetup-join/test"
        assert session.session_id.startswith("int_")
        assert session.started_at is not None
        assert session.ended_at is None
        assert len(session.speaker_mappings) == 0
        assert len(session.transcript_events) == 0
    
    def test_end_session(self):
        """Ending a session sets ended_at timestamp."""
        manager = InterviewSessionManager()
        manager.start_session("Jane Doe", "https://teams.com/test")
        
        ended = manager.end_session()
        
        assert ended is not None
        assert ended.ended_at is not None
        assert manager.is_active is False
    
    def test_end_session_no_active(self):
        """Ending with no active session returns None."""
        manager = InterviewSessionManager()
        
        result = manager.end_session()
        
        assert result is None
    
    def test_map_speaker_candidate(self):
        """Mapping speaker to candidate role."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        
        manager.map_speaker("speaker_1", "candidate", "Test User")
        
        assert manager.get_speaker_role("speaker_1") == "candidate"
        assert manager.get_candidate_speaker_id() == "speaker_1"
        
        # Check session mappings
        mappings = manager.session.speaker_mappings
        assert len(mappings) == 1
        assert mappings[0].speaker_id == "speaker_1"
        assert mappings[0].role == "candidate"
    
    def test_map_speaker_interviewer(self):
        """Mapping speaker to interviewer role."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        
        manager.map_speaker("speaker_0", "interviewer", "HR Manager")
        
        assert manager.get_speaker_role("speaker_0") == "interviewer"
        assert manager.get_candidate_speaker_id() is None
    
    def test_map_speaker_invalid_role(self):
        """Invalid role raises ValueError."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        
        with pytest.raises(ValueError, match="Invalid role"):
            manager.map_speaker("speaker_0", "invalid_role")
    
    def test_map_speaker_no_session(self):
        """Mapping without active session raises ValueError."""
        manager = InterviewSessionManager()
        
        with pytest.raises(ValueError, match="No active session"):
            manager.map_speaker("speaker_0", "candidate")
    
    def test_add_transcript(self):
        """Adding transcript events to session."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        
        event = generate_transcript_event(
            speaker_id="speaker_0",
            text="Tell me about yourself.",
        )
        manager.add_transcript(event)
        
        assert len(manager.session.transcript_events) == 1
        assert manager.session.transcript_events[0].text == "Tell me about yourself."
    
    def test_add_transcript_no_session(self):
        """Adding transcript without session raises ValueError."""
        manager = InterviewSessionManager()
        event = generate_transcript_event("speaker_0", "Test")
        
        with pytest.raises(ValueError, match="No active session"):
            manager.add_transcript(event)
    
    def test_get_candidate_transcripts(self):
        """Retrieving only candidate transcripts."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        manager.map_speaker("speaker_0", "interviewer")
        manager.map_speaker("speaker_1", "candidate")
        
        # Add mixed transcripts
        manager.add_transcript(generate_transcript_event("speaker_0", "Question 1"))
        manager.add_transcript(generate_transcript_event("speaker_1", "Answer 1"))
        manager.add_transcript(generate_transcript_event("speaker_0", "Question 2"))
        manager.add_transcript(generate_transcript_event("speaker_1", "Answer 2"))
        
        candidate_transcripts = manager.get_candidate_transcripts()
        
        assert len(candidate_transcripts) == 2
        assert all(t.speaker_id == "speaker_1" for t in candidate_transcripts)
        assert candidate_transcripts[0].text == "Answer 1"
        assert candidate_transcripts[1].text == "Answer 2"
    
    def test_get_candidate_transcripts_with_limit(self):
        """Limiting number of candidate transcripts returned."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        manager.map_speaker("speaker_1", "candidate")
        
        for i in range(5):
            manager.add_transcript(generate_transcript_event("speaker_1", f"Response {i}"))
        
        transcripts = manager.get_candidate_transcripts(count=2)
        
        assert len(transcripts) == 2
        assert transcripts[0].text == "Response 3"  # Last 2
        assert transcripts[1].text == "Response 4"
    
    def test_get_recent_transcripts(self):
        """Getting recent transcripts from session."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        
        for i in range(15):
            manager.add_transcript(generate_transcript_event("speaker_0", f"Text {i}"))
        
        recent = manager.get_recent_transcripts(count=5)
        
        assert len(recent) == 5
        assert recent[0].text == "Text 10"
        assert recent[4].text == "Text 14"
    
    def test_get_session_context(self):
        """Session context generation for agent."""
        manager = InterviewSessionManager()
        manager.start_session("Alice Smith", "https://teams.com/meeting")
        manager.map_speaker("speaker_0", "interviewer")
        manager.map_speaker("speaker_1", "candidate")
        
        manager.add_transcript(generate_transcript_event("speaker_0", "Question"))
        manager.add_transcript(generate_transcript_event("speaker_1", "Answer"))
        
        context = manager.get_session_context()
        
        assert context["session_active"] is True
        assert context["candidate_name"] == "Alice Smith"
        assert context["meeting_url"] == "https://teams.com/meeting"
        assert context["candidate_speaker_id"] == "speaker_1"
        assert "speaker_0" in context["speaker_mappings"]
        assert "speaker_1" in context["speaker_mappings"]
        assert len(context["recent_conversation"]) == 2
        assert context["total_events"] == 2
        assert context["final_events"] == 2
    
    def test_get_session_context_no_session(self):
        """Session context when no session active."""
        manager = InterviewSessionManager()
        
        context = manager.get_session_context()
        
        assert context["session_active"] is False
        assert context["candidate_name"] is None
        assert len(context["recent_conversation"]) == 0
    
    def test_get_last_interviewer_question(self):
        """Getting the last question from interviewer."""
        manager = InterviewSessionManager()
        manager.start_session("Test User", "https://test.com")
        manager.map_speaker("speaker_0", "interviewer")
        manager.map_speaker("speaker_1", "candidate")
        
        manager.add_transcript(generate_transcript_event("speaker_0", "First question"))
        manager.add_transcript(generate_transcript_event("speaker_1", "First answer"))
        manager.add_transcript(generate_transcript_event("speaker_0", "Second question"))
        
        last_question = manager.get_last_interviewer_question()
        
        assert last_question == "Second question"
    
    def test_full_interview_simulation(self):
        """Full interview simulation using mock data."""
        manager = InterviewSessionManager()
        
        # Generate mock conversation
        events = generate_interview_conversation(
            candidate_name="Bob Wilson",
            num_exchanges=3,
            include_session_events=False,
        )
        
        # Start session and map speakers
        manager.start_session("Bob Wilson", "https://teams.com/interview")
        manager.map_speaker("speaker_0", "interviewer")
        manager.map_speaker("speaker_1", "candidate")
        
        # Add all transcript events
        for event in events:
            manager.add_transcript(event)
        
        # Verify state
        context = manager.get_session_context()
        assert context["session_active"] is True
        assert context["total_events"] == len(events)
        
        # Should have candidate responses
        candidate_transcripts = manager.get_candidate_transcripts()
        assert len(candidate_transcripts) >= 3
        
        # End session
        ended = manager.end_session()
        assert ended.ended_at is not None


# =============================================================================
# AnalysisOutputWriter Tests
# =============================================================================

class TestAnalysisOutputWriter:
    """Tests for AnalysisOutputWriter class."""
    
    @pytest.fixture
    def temp_output_dir(self):
        """Create temporary directory for test outputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    def test_init_creates_directory(self, temp_output_dir):
        """Writer creates output directory if missing."""
        output_path = temp_output_dir / "nested" / "output"
        
        writer = AnalysisOutputWriter(output_path)
        
        assert output_path.exists()
        assert output_path.is_dir()
    
    def test_write_analysis(self, temp_output_dir):
        """Writing complete session analysis."""
        writer = AnalysisOutputWriter(temp_output_dir)
        analysis = generate_session_analysis(
            session_id="test_session_001",
            candidate_name="Test Person",
            num_items=2,
        )
        
        output_path = writer.write_analysis("test_session_001", analysis)
        
        assert output_path.exists()
        assert output_path.name == "test_session_001_analysis.json"
        
        # Load and verify
        loaded = writer.load_analysis("test_session_001")
        assert loaded is not None
        assert loaded.session_id == "test_session_001"
        assert loaded.candidate_name == "Test Person"
        assert len(loaded.analysis_items) == 2
    
    def test_append_item_new_file(self, temp_output_dir):
        """Appending item creates new file if none exists."""
        writer = AnalysisOutputWriter(temp_output_dir)
        item = generate_analysis_item(response_id="resp_001")
        
        output_path = writer.append_item("new_session", item)
        
        assert output_path.exists()
        
        loaded = writer.load_analysis("new_session")
        assert loaded is not None
        assert len(loaded.analysis_items) == 1
        assert loaded.analysis_items[0].response_id == "resp_001"
    
    def test_append_item_existing_file(self, temp_output_dir):
        """Appending item to existing analysis file."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Write initial analysis
        analysis = generate_session_analysis(
            session_id="append_test",
            num_items=1,
        )
        writer.write_analysis("append_test", analysis)
        
        # Append new item
        new_item = generate_analysis_item(response_id="resp_new")
        writer.append_item("append_test", new_item)
        
        # Verify
        loaded = writer.load_analysis("append_test")
        assert len(loaded.analysis_items) == 2
        assert loaded.total_responses_analyzed == 2
    
    def test_load_analysis_not_found(self, temp_output_dir):
        """Loading non-existent analysis returns None."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        result = writer.load_analysis("nonexistent_session")
        
        assert result is None
    
    def test_list_sessions(self, temp_output_dir):
        """Listing all sessions with analysis files."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Write multiple analyses
        for i in range(3):
            analysis = generate_session_analysis(session_id=f"session_{i:03d}")
            writer.write_analysis(f"session_{i:03d}", analysis)
        
        sessions = writer.list_sessions()
        
        assert len(sessions) == 3
        assert "session_000" in sessions
        assert "session_001" in sessions
        assert "session_002" in sessions
    
    def test_delete_analysis(self, temp_output_dir):
        """Deleting an analysis file."""
        writer = AnalysisOutputWriter(temp_output_dir)
        analysis = generate_session_analysis(session_id="to_delete")
        writer.write_analysis("to_delete", analysis)
        
        # Verify exists
        assert writer.load_analysis("to_delete") is not None
        
        # Delete
        result = writer.delete_analysis("to_delete")
        
        assert result is True
        assert writer.load_analysis("to_delete") is None
    
    def test_delete_analysis_not_found(self, temp_output_dir):
        """Deleting non-existent file returns False."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        result = writer.delete_analysis("nonexistent")
        
        assert result is False
    
    def test_overall_scores_computed(self, temp_output_dir):
        """Overall scores are computed on write."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Create analysis with items but no overall scores
        analysis = SessionAnalysis(
            session_id="score_test",
            candidate_name="Test",
            started_at="2026-01-31T10:00:00.000Z",
            analysis_items=[
                AnalysisItem(
                    response_id="r1",
                    response_text="Response 1",
                    relevance_score=0.8,
                    clarity_score=0.9,
                ),
                AnalysisItem(
                    response_id="r2",
                    response_text="Response 2",
                    relevance_score=0.6,
                    clarity_score=0.7,
                ),
            ],
        )
        
        writer.write_analysis("score_test", analysis)
        
        loaded = writer.load_analysis("score_test")
        assert loaded.overall_relevance == pytest.approx(0.7, abs=0.01)
        assert loaded.overall_clarity == pytest.approx(0.8, abs=0.01)
        assert loaded.total_responses_analyzed == 2


# =============================================================================
# Model Validation Tests
# =============================================================================

class TestModelValidation:
    """Test Pydantic model validation."""
    
    def test_transcript_event_confidence_bounds(self):
        """Confidence must be between 0 and 1."""
        # Valid
        event = TranscriptEvent(
            event_type="final",
            text="Test",
            timestamp_utc="2026-01-31T10:00:00.000Z",
            confidence=0.95,
        )
        assert event.confidence == 0.95
        
        # Invalid - should raise
        with pytest.raises(ValueError):
            TranscriptEvent(
                event_type="final",
                text="Test",
                timestamp_utc="2026-01-31T10:00:00.000Z",
                confidence=1.5,
            )
    
    def test_analysis_item_score_bounds(self):
        """Analysis scores must be between 0 and 1."""
        # Valid
        item = AnalysisItem(
            response_id="test",
            response_text="Test response",
            relevance_score=0.0,
            clarity_score=1.0,
        )
        assert item.relevance_score == 0.0
        assert item.clarity_score == 1.0
        
        # Invalid relevance
        with pytest.raises(ValueError):
            AnalysisItem(
                response_id="test",
                response_text="Test",
                relevance_score=-0.1,
                clarity_score=0.5,
            )
        
        # Invalid clarity
        with pytest.raises(ValueError):
            AnalysisItem(
                response_id="test",
                response_text="Test",
                relevance_score=0.5,
                clarity_score=1.1,
            )
    
    def test_speaker_mapping_required_fields(self):
        """Speaker mapping requires speaker_id and role."""
        mapping = SpeakerMapping(
            speaker_id="speaker_0",
            role="candidate",
        )
        assert mapping.speaker_id == "speaker_0"
        assert mapping.role == "candidate"
        assert mapping.name is None  # Optional
    
    def test_session_analysis_compute_overall_scores_empty(self):
        """Computing scores with no items sets None."""
        analysis = SessionAnalysis(
            session_id="empty",
            candidate_name="Test",
            started_at="2026-01-31T10:00:00.000Z",
            analysis_items=[],
        )
        
        analysis.compute_overall_scores()
        
        assert analysis.overall_relevance is None
        assert analysis.overall_clarity is None
        assert analysis.total_responses_analyzed == 0


# =============================================================================
# Output Exception Tests
# =============================================================================

class TestOutputExceptions:
    """Tests for AnalysisOutputWriter custom exceptions."""
    
    @pytest.fixture
    def temp_output_dir(self):
        """Create temporary directory for test outputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)
    
    def test_output_write_error_attributes(self):
        """OutputWriteError has correct attributes."""
        cause = OSError("Permission denied")
        path = Path("/test/path")
        
        error = OutputWriteError(path, cause)
        
        assert error.path == path
        assert error.cause == cause
        assert "Permission denied" in str(error)
        assert "/test/path" in str(error)
    
    def test_output_read_error_attributes(self):
        """OutputReadError has correct attributes."""
        cause = json.JSONDecodeError("Invalid JSON", doc="", pos=0)
        path = Path("/test/path")
        
        error = OutputReadError(path, cause)
        
        assert error.path == path
        assert error.cause == cause
        assert "/test/path" in str(error)
    
    def test_load_analysis_invalid_json_raises_read_error(self, temp_output_dir):
        """Loading invalid JSON raises OutputReadError."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Write invalid JSON directly
        invalid_file = temp_output_dir / "invalid_session_analysis.json"
        with open(invalid_file, "w") as f:
            f.write("{not valid json")
        
        with pytest.raises(OutputReadError) as exc_info:
            writer.load_analysis("invalid_session")
        
        assert exc_info.value.path == invalid_file
        assert isinstance(exc_info.value.cause, json.JSONDecodeError)
    
    def test_load_analysis_invalid_schema_raises_read_error(self, temp_output_dir):
        """Loading valid JSON with invalid schema raises OutputReadError."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Write valid JSON but invalid schema
        invalid_file = temp_output_dir / "bad_schema_analysis.json"
        with open(invalid_file, "w") as f:
            json.dump({"invalid": "schema", "no_required_fields": True}, f)
        
        with pytest.raises(OutputReadError) as exc_info:
            writer.load_analysis("bad_schema")
        
        assert exc_info.value.path == invalid_file
    
    def test_append_item_with_corrupted_file_raises_error(self, temp_output_dir):
        """Appending to corrupted file raises OutputReadError."""
        writer = AnalysisOutputWriter(temp_output_dir)
        
        # Create corrupted file
        corrupted_file = temp_output_dir / "corrupted_analysis.json"
        with open(corrupted_file, "w") as f:
            f.write("not json at all {{{")
        
        item = generate_analysis_item(response_id="test_item")
        
        with pytest.raises(OutputReadError) as exc_info:
            writer.append_item("corrupted", item)
        
        assert exc_info.value.path == corrupted_file


# =============================================================================
# Pubsub Tests
# =============================================================================

class TestAgentThoughtPublisher:
    """Tests for AgentThoughtPublisher."""
    
    @pytest.fixture
    def publisher(self):
        """Create a fresh publisher instance."""
        from interview_agent.pubsub import AgentThoughtPublisher
        return AgentThoughtPublisher(max_history=10)
    
    @pytest.mark.asyncio
    async def test_subscribe_returns_queue(self, publisher):
        """Subscribing returns an asyncio Queue."""
        import asyncio
        
        queue = await publisher.subscribe()
        
        assert isinstance(queue, asyncio.Queue)
        assert publisher.subscriber_count == 1
    
    @pytest.mark.asyncio
    async def test_publish_broadcasts_to_subscribers(self, publisher):
        """Published thoughts are sent to all subscribers."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        queue1 = await publisher.subscribe()
        queue2 = await publisher.subscribe()
        
        thought = AgentThought(
            thought_type=ThoughtType.ANALYSIS,
            content="Test analysis",
        )
        await publisher.publish(thought)
        
        # Both queues should receive the thought
        received1 = await queue1.get()
        received2 = await queue2.get()
        
        assert received1.content == "Test analysis"
        assert received2.content == "Test analysis"
    
    @pytest.mark.asyncio
    async def test_unsubscribe_removes_queue(self, publisher):
        """Unsubscribing removes the queue from subscribers."""
        queue = await publisher.subscribe()
        assert publisher.subscriber_count == 1
        
        await publisher.unsubscribe(queue)
        assert publisher.subscriber_count == 0
    
    @pytest.mark.asyncio
    async def test_get_history_returns_copy(self, publisher):
        """get_history returns a copy of history list."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        thought = AgentThought(
            thought_type=ThoughtType.SYSTEM,
            content="System message",
        )
        await publisher.publish(thought)
        
        history = await publisher.get_history()
        
        assert len(history) == 1
        assert history[0].content == "System message"
        
        # Modifying returned list shouldn't affect internal history
        history.clear()
        internal_history = await publisher.get_history()
        assert len(internal_history) == 1
    
    @pytest.mark.asyncio
    async def test_clear_history_empties_history(self, publisher):
        """clear_history removes all thoughts from history."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        await publisher.publish(AgentThought(
            thought_type=ThoughtType.ANALYSIS,
            content="Test 1",
        ))
        await publisher.publish(AgentThought(
            thought_type=ThoughtType.ANALYSIS,
            content="Test 2",
        ))
        
        history_before = await publisher.get_history()
        assert len(history_before) == 2
        
        await publisher.clear_history()
        
        history_after = await publisher.get_history()
        assert len(history_after) == 0
    
    @pytest.mark.asyncio
    async def test_history_limit_enforced(self, publisher):
        """History respects max_history limit."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        # Publisher has max_history=10
        for i in range(15):
            await publisher.publish(AgentThought(
                thought_type=ThoughtType.OBSERVATION,
                content=f"Message {i}",
            ))
        
        history = await publisher.get_history()
        
        assert len(history) == 10
        # Should have the most recent messages (5-14)
        assert history[0].content == "Message 5"
        assert history[-1].content == "Message 14"
    
    @pytest.mark.asyncio
    async def test_new_subscriber_gets_history(self, publisher):
        """New subscribers receive the existing history."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        # Publish some thoughts first
        await publisher.publish(AgentThought(
            thought_type=ThoughtType.SYSTEM,
            content="Old message 1",
        ))
        await publisher.publish(AgentThought(
            thought_type=ThoughtType.SYSTEM,
            content="Old message 2",
        ))
        
        # Subscribe after publishing
        queue = await publisher.subscribe()
        
        # Should immediately have the historical messages
        msg1 = await queue.get()
        msg2 = await queue.get()
        
        assert msg1.content == "Old message 1"
        assert msg2.content == "Old message 2"
    
    @pytest.mark.asyncio
    async def test_publish_analysis_convenience_method(self, publisher):
        """publish_analysis creates proper AgentThought."""
        from interview_agent.pubsub import ThoughtType
        
        queue = await publisher.subscribe()
        
        await publisher.publish_analysis(
            content="Strong technical response",
            speaker_id="speaker_1",
            relevance_score=0.9,
            clarity_score=0.85,
            key_points=["Point 1", "Point 2"],
        )
        
        thought = await queue.get()
        
        assert thought.thought_type == ThoughtType.ANALYSIS
        assert thought.content == "Strong technical response"
        assert thought.speaker_id == "speaker_1"
        assert thought.relevance_score == 0.9
        assert thought.clarity_score == 0.85
        assert thought.key_points == ["Point 1", "Point 2"]
    
    @pytest.mark.asyncio
    async def test_publish_error_convenience_method(self, publisher):
        """publish_error creates ERROR type thought."""
        from interview_agent.pubsub import ThoughtType
        
        queue = await publisher.subscribe()
        
        await publisher.publish_error("Analysis failed: timeout")
        
        thought = await queue.get()
        
        assert thought.thought_type == ThoughtType.ERROR
        assert thought.content == "Analysis failed: timeout"
    
    @pytest.mark.asyncio
    async def test_get_subscriber_count_async(self, publisher):
        """get_subscriber_count returns accurate async-safe count."""
        await publisher.subscribe()
        await publisher.subscribe()
        
        count = await publisher.get_subscriber_count()
        
        assert count == 2
    
    @pytest.mark.asyncio
    async def test_agent_thought_to_dict(self):
        """AgentThought.to_dict produces correct dictionary."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        
        thought = AgentThought(
            thought_type=ThoughtType.ANALYSIS,
            content="Test content",
            speaker_id="speaker_0",
            relevance_score=0.8,
        )
        
        data = thought.to_dict()
        
        assert data["thought_type"] == "analysis"
        assert data["content"] == "Test content"
        assert data["speaker_id"] == "speaker_0"
        assert data["relevance_score"] == 0.8
        assert "timestamp" in data
    
    @pytest.mark.asyncio
    async def test_agent_thought_to_json(self):
        """AgentThought.to_json produces valid JSON string."""
        from interview_agent.pubsub import AgentThought, ThoughtType
        import json
        
        thought = AgentThought(
            thought_type=ThoughtType.OBSERVATION,
            content="Observation content",
        )
        
        json_str = thought.to_json()
        parsed = json.loads(json_str)
        
        assert parsed["thought_type"] == "observation"
        assert parsed["content"] == "Observation content"
