"""
Pydantic models for Interview Analysis Agent.

Defines data structures for transcript events (v2 with diarization),
interview sessions, and analysis outputs.

Last Grunted: 02/05/2026
"""

from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field


class EventMetadata(BaseModel):
    """
    Optional metadata attached to transcript events.
    
    Contains additional context such as meeting details,
    audio processing info, or custom application data.
    """
    meeting_id: Optional[str] = None
    call_id: Optional[str] = None
    raw_response: Optional[dict] = None
    provider: Optional[str] = None  # "azure_speech", "deepgram", etc.


class EventError(BaseModel):
    """
    Error details for error events.
    
    Captures error code and message from STT provider failures.
    """
    code: str = Field(..., description="Error code from STT provider")
    message: str = Field(..., description="Human-readable error message")


class TranscriptEvent(BaseModel):
    """
    Transcript event from STT provider (v2 format with speaker diarization).
    
    This model represents real-time transcript data received from the C# bot,
    including speaker identification for multi-participant meetings.
    
    Event Types:
        - "partial": Interim recognition result (still processing)
        - "final": Completed recognition result
        - "session_started": STT session began
        - "session_stopped": STT session ended
        - "error": Recognition error occurred
    
    Example:
        >>> event = TranscriptEvent(
        ...     event_type="final",
        ...     text="Tell me about your experience with Python.",
        ...     timestamp_utc="2026-01-31T10:30:00.000Z",
        ...     speaker_id="speaker_0",
        ...     confidence=0.95
        ... )
    """
    event_type: str = Field(
        ...,
        description="Event type: 'partial', 'final', 'session_started', 'session_stopped', 'error'"
    )
    text: Optional[str] = Field(
        default=None,
        description="Transcript text content (null for status events)"
    )
    timestamp_utc: str = Field(
        ...,
        description="ISO 8601 UTC timestamp when event occurred"
    )
    speaker_id: Optional[str] = Field(
        default=None,
        description="Speaker identifier from diarization: 'speaker_0', 'speaker_1', etc."
    )
    audio_start_ms: Optional[float] = Field(
        default=None,
        description="Audio offset start in milliseconds from session start"
    )
    audio_end_ms: Optional[float] = Field(
        default=None,
        description="Audio offset end in milliseconds from session start"
    )
    confidence: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Recognition confidence score (0.0 to 1.0)"
    )
    metadata: Optional[EventMetadata] = Field(
        default=None,
        description="Optional metadata with provider-specific details"
    )
    error: Optional[EventError] = Field(
        default=None,
        description="Error details (only for error events)"
    )


class SpeakerMapping(BaseModel):
    """
    Maps a speaker ID to a role in the interview.
    
    Used to track which speaker_id corresponds to the candidate
    vs the interviewer(s).
    """
    speaker_id: str = Field(..., description="Speaker ID from diarization (e.g., 'speaker_0')")
    role: str = Field(..., description="Role: 'candidate' or 'interviewer'")
    name: Optional[str] = Field(default=None, description="Display name if known")


class InterviewSession(BaseModel):
    """
    Tracks an active interview session.
    
    Contains session metadata, speaker mappings, and accumulated transcript.
    The session is initialized with the candidate's name and meeting URL,
    then speaker mappings are added as diarization identifies participants.
    
    Example:
        >>> session = InterviewSession(
        ...     session_id="int_20260131_103000",
        ...     candidate_name="John Smith",
        ...     meeting_url="https://teams.microsoft.com/l/meetup-join/...",
        ...     started_at="2026-01-31T10:30:00.000Z"
        ... )
    """
    session_id: str = Field(..., description="Unique session identifier")
    candidate_name: str = Field(..., description="Name of the candidate being interviewed")
    meeting_url: str = Field(..., description="Teams meeting join URL")
    started_at: str = Field(..., description="ISO 8601 UTC timestamp when session started")
    ended_at: Optional[str] = Field(default=None, description="ISO 8601 UTC timestamp when session ended")
    speaker_mappings: list[SpeakerMapping] = Field(
        default_factory=list,
        description="List of speaker ID to role mappings"
    )
    transcript_events: list[TranscriptEvent] = Field(
        default_factory=list,
        description="All transcript events received during session"
    )


class AnalysisItem(BaseModel):
    """
    Analysis of a single candidate response.
    
    Generated by the Interview Analyzer agent after processing
    a candidate's response to an interview question.
    
    Scores:
        - relevance_score: How well the response addresses the question (0-1)
        - clarity_score: How clearly the response is articulated (0-1)
    
    Example:
        >>> item = AnalysisItem(
        ...     response_id="resp_001",
        ...     question_text="Tell me about your Python experience.",
        ...     response_text="I've worked with Python for 5 years...",
        ...     relevance_score=0.85,
        ...     clarity_score=0.90,
        ...     key_points=["5 years experience", "Backend development"],
        ...     follow_up_suggestions=["Ask about specific frameworks used"]
        ... )
    """
    response_id: str = Field(..., description="Unique identifier for this response")
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        description="When analysis was generated"
    )
    question_text: Optional[str] = Field(
        default=None,
        description="The interviewer question that prompted this response"
    )
    response_text: str = Field(..., description="The candidate's response text")
    speaker_id: Optional[str] = Field(
        default=None,
        description="Speaker ID of the candidate"
    )
    relevance_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How relevant the response is to the question (0.0 to 1.0)"
    )
    clarity_score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="How clearly the response is articulated (0.0 to 1.0)"
    )
    key_points: list[str] = Field(
        default_factory=list,
        description="Key points extracted from the response"
    )
    follow_up_suggestions: list[str] = Field(
        default_factory=list,
        description="Suggested follow-up questions for the interviewer"
    )
    raw_model_output: Optional[dict] = Field(
        default=None,
        description="Raw output from the analysis model (for debugging)"
    )


class SessionAnalysis(BaseModel):
    """
    Complete analysis output for an interview session.
    
    Aggregates all individual response analyses along with session metadata
    and summary statistics.
    
    Example:
        >>> analysis = SessionAnalysis(
        ...     session_id="int_20260131_103000",
        ...     candidate_name="John Smith",
        ...     analysis_items=[item1, item2, item3],
        ...     overall_relevance=0.82,
        ...     overall_clarity=0.88
        ... )
    """
    session_id: str = Field(..., description="Session this analysis belongs to")
    candidate_name: str = Field(..., description="Name of the candidate")
    started_at: str = Field(..., description="Session start timestamp")
    ended_at: Optional[str] = Field(default=None, description="Session end timestamp")
    analysis_items: list[AnalysisItem] = Field(
        default_factory=list,
        description="List of individual response analyses"
    )
    overall_relevance: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Average relevance score across all responses"
    )
    overall_clarity: Optional[float] = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Average clarity score across all responses"
    )
    total_responses_analyzed: int = Field(
        default=0,
        description="Total number of candidate responses analyzed"
    )
    checklist_state: list[dict[str, str | None]] = Field(
        default_factory=list,
        description="Checklist snapshot at latest analysis write"
    )
    
    def compute_overall_scores(self) -> None:
        """
        Compute overall scores from individual analysis items.
        
        Updates the following instance attributes:
            - overall_relevance: Average relevance score, or None if no items
            - overall_clarity: Average clarity score, or None if no items  
            - total_responses_analyzed: Count of analysis items
        """
        if not self.analysis_items:
            self.overall_relevance = None
            self.overall_clarity = None
            self.total_responses_analyzed = 0
            return
        
        self.total_responses_analyzed = len(self.analysis_items)
        self.overall_relevance = sum(
            item.relevance_score for item in self.analysis_items
        ) / len(self.analysis_items)
        self.overall_clarity = sum(
            item.clarity_score for item in self.analysis_items
        ) / len(self.analysis_items)
