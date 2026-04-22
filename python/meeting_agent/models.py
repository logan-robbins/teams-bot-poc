"""
Pydantic models for the meeting-agent pipeline.

Models here are product-agnostic: they describe inputs and outputs of the
analysis pipeline, not any specific persona. Alfred-specific action output
is defined alongside because it's emitted by the same agent and flows
through the same AnalysisItem envelope.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


class EventMetadata(BaseModel):
    """Optional metadata attached to transcript events."""
    meeting_id: Optional[str] = None
    call_id: Optional[str] = None
    raw_response: Optional[dict] = None
    provider: Optional[str] = None  # "azure_speech", "deepgram", etc.


class EventError(BaseModel):
    """Error details for error events."""
    code: str = Field(..., description="Error code from STT provider")
    message: str = Field(..., description="Human-readable error message")


class TranscriptEvent(BaseModel):
    """Transcript event from STT provider (v2 format with speaker diarization)."""
    event_type: str = Field(
        ...,
        description="Event type: 'partial', 'final', 'session_started', 'session_stopped', 'error'",
    )
    text: Optional[str] = None
    timestamp_utc: str = Field(..., description="ISO 8601 UTC timestamp when event occurred")
    speaker_id: Optional[str] = None
    audio_start_ms: Optional[float] = None
    audio_end_ms: Optional[float] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    metadata: Optional[EventMetadata] = None
    error: Optional[EventError] = None


class SpeakerMapping(BaseModel):
    """Maps a speaker ID to a role in the meeting."""
    speaker_id: str
    role: str = Field(..., description="Role: 'candidate', 'interviewer', 'participant', 'bot'")
    name: Optional[str] = None


class ChatMessage(BaseModel):
    """
    A single meeting-chat message ingested from the C# bot.

    The bot forwards every meeting chat message (human or bot) through
    POST /chat on the sink. These are first-class timeline events alongside
    transcript turns, and the agent reads them both.
    """
    event_type: Literal["chat_created", "chat_updated", "chat_deleted"] = Field(
        default="chat_created"
    )
    chat_thread_id: str = Field(..., description="Teams chat thread id backing the meeting")
    message_id: str = Field(..., description="Teams chat message id (for reply/edit threading)")
    text: Optional[str] = Field(default=None, description="Plain-text message body")
    html: Optional[str] = Field(default=None, description="HTML body as rendered in Teams")
    sender_id: Optional[str] = None
    sender_display_name: Optional[str] = None
    timestamp_utc: str
    conversation_reference_id: Optional[str] = Field(
        default=None,
        description="Stable key used by the C# bot to call adapter.ContinueConversationAsync",
    )
    attachments: list[dict] = Field(default_factory=list)
    mentions: list[dict] = Field(default_factory=list)
    reply_to_message_id: Optional[str] = Field(
        default=None,
        description="Message id this one is threaded under, if any",
    )
    from_bot: bool = Field(
        default=False,
        description="True when this chat was sent by our own bot (our outbound echo)",
    )
    raw: Optional[dict] = Field(default=None, description="Raw Graph chatMessage body")


class InterviewSession(BaseModel):
    """
    Tracks an active meeting session.

    Name kept as InterviewSession for now to minimise churn across the
    code base; the schema is generic (speech + chat). `candidate_name`
    acts as a freeform label for the primary meeting subject.
    """
    session_id: str
    candidate_name: str = Field(..., description="Primary meeting subject label (freeform)")
    meeting_url: str
    started_at: str
    ended_at: Optional[str] = None
    speaker_mappings: list[SpeakerMapping] = Field(default_factory=list)
    transcript_events: list[TranscriptEvent] = Field(default_factory=list)
    chat_messages: list[ChatMessage] = Field(
        default_factory=list,
        description="All chat messages ingested during the session (merged into the timeline alongside speech)",
    )
    conversation_reference_id: Optional[str] = Field(
        default=None,
        description="Captured once the first chat message arrives; used by the sink to emit send-intent payloads",
    )


class AlfredAction(BaseModel):
    """
    Alfred's per-turn decision + note-taking output.

    The agent emits one of these on each transcript or chat tick.
    SILENT → just update notes/summary/topics; do not post.
    SEND   → post `chat_text` to the meeting chat.
    ASK    → same as SEND, semantically framed as a clarifying question.
    """
    action: Literal["SILENT", "SEND", "ASK"]
    rationale: str = Field(..., description="One-line justification for the decision")
    chat_text: Optional[str] = Field(
        default=None,
        description="Body to post when action is SEND or ASK; required for those actions",
    )
    mentions: list[str] = Field(default_factory=list, description="@-mention handles to attach")
    reply_to_message_id: Optional[str] = None
    notes: list[str] = Field(
        default_factory=list,
        description="NEW notes added this tick (delta — not the full list)",
    )
    running_summary: str = Field(
        default="",
        description="Full running summary replacing prior (markdown)",
    )
    topics: list[str] = Field(
        default_factory=list,
        description="Current discovered topics (running; replaces prior)",
    )


class AnalysisItem(BaseModel):
    """
    Envelope for per-turn analysis output.

    The relevance/clarity scores are optional because Alfred doesn't
    compute them — they're retained for legacy interview-style use.
    Alfred-specific output goes into `alfred_action`.
    """
    response_id: str
    timestamp_utc: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    )
    question_text: Optional[str] = None
    response_text: str
    speaker_id: Optional[str] = None
    relevance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    clarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    key_points: list[str] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list)
    alfred_action: Optional[AlfredAction] = Field(
        default=None,
        description="Alfred's per-turn decision when the Alfred analyzer produced this item",
    )
    raw_model_output: Optional[dict] = None


class SessionAnalysis(BaseModel):
    """Complete analysis output for a meeting session."""
    session_id: str
    candidate_name: str
    started_at: str
    ended_at: Optional[str] = None
    analysis_items: list[AnalysisItem] = Field(default_factory=list)
    overall_relevance: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    overall_clarity: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    total_responses_analyzed: int = 0
    checklist_state: list[dict[str, str | None]] = Field(default_factory=list)
    running_summary: str = Field(
        default="",
        description="Alfred's latest running summary (replaces prior)",
    )
    topics: list[str] = Field(default_factory=list)

    def compute_overall_scores(self) -> None:
        """Compute overall scores from scored analysis items (no-op for Alfred items)."""
        scored = [
            item for item in self.analysis_items
            if item.relevance_score is not None and item.clarity_score is not None
        ]
        self.total_responses_analyzed = len(self.analysis_items)
        if not scored:
            self.overall_relevance = None
            self.overall_clarity = None
            return
        self.overall_relevance = sum(i.relevance_score for i in scored) / len(scored)
        self.overall_clarity = sum(i.clarity_score for i in scored) / len(scored)
