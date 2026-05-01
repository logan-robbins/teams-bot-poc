"""
Pydantic models for the meeting-agent pipeline.

Alfred consumes speech + chat as a unified append-only meeting ledger and
emits one structured ``AlfredExtraction`` per analyzed event. Outbound
meeting-chat sends are side effects produced by tool calls (see
``meeting_agent.tools.send_to_meeting_chat``), not by a magic ``action``
enum in the extraction payload.
"""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Ingress event contracts
# ---------------------------------------------------------------------------


class EventMetadata(BaseModel):
    """Optional metadata attached to transcript events."""

    meeting_id: Optional[str] = None
    call_id: Optional[str] = None
    raw_response: Optional[dict] = None
    provider: Optional[str] = None
    participant_id: Optional[str] = None
    aad_object_id: Optional[str] = None
    media_source_id: Optional[str] = None
    display_name: Optional[str] = None


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
    chat_thread_id: Optional[str] = Field(
        default=None,
        description="Teams chat thread id (meeting id) the event belongs to",
    )
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
    """A single meeting-chat message ingested from the C# bot."""

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


class MeetingEvent(BaseModel):
    """Append-only normalized meeting ledger event used by Alfred."""

    event_id: str
    kind: Literal["speech", "chat", "system"]
    timestamp_utc: str
    source: Literal["teams_media", "bot_framework", "graph_notification", "alfred", "system"]
    text: str = ""
    html: Optional[str] = None
    speaker_id: Optional[str] = None
    participant_id: Optional[str] = None
    aad_object_id: Optional[str] = None
    media_source_id: Optional[str] = None
    display_name: Optional[str] = None
    role: str = "unknown"
    message_id: Optional[str] = None
    reply_to_message_id: Optional[str] = None
    from_bot: bool = False
    transcript_provider: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    raw: Optional[dict] = None


class OutboundChatIntent(BaseModel):
    """Recent outbound Alfred chat intent used for bot-echo suppression."""

    text: str
    normalized_text: str
    timestamp_utc: str = Field(default_factory=utc_now_iso)
    reply_to_message_id: Optional[str] = None


# ---------------------------------------------------------------------------
# Intent-alignment extraction types
# ---------------------------------------------------------------------------


DecisionStatus = Literal["tentative", "committed", "superseded"]
QuestionStatus = Literal["open", "answered", "deferred"]
ActionItemStatus = Literal["proposed", "owned", "done"]
RiskSeverity = Literal["low", "medium", "high"]


class Decision(BaseModel):
    """Something the meeting has committed to."""

    id: str
    text: str
    committed_by: list[str] = Field(default_factory=list)
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    first_seen_at: str = Field(default_factory=utc_now_iso)
    source_event_ids: list[str] = Field(default_factory=list)
    status: DecisionStatus = "tentative"


class OpenQuestion(BaseModel):
    """A question raised in the meeting that is not yet resolved."""

    id: str
    text: str
    raised_by: Optional[str] = None
    answer: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    first_seen_at: str = Field(default_factory=utc_now_iso)
    source_event_ids: list[str] = Field(default_factory=list)
    status: QuestionStatus = "open"


class ActionItem(BaseModel):
    """A commitment: who does what, by when."""

    id: str
    text: str
    owner: Optional[str] = None
    due: Optional[str] = None
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    first_seen_at: str = Field(default_factory=utc_now_iso)
    source_event_ids: list[str] = Field(default_factory=list)
    status: ActionItemStatus = "proposed"


class Risk(BaseModel):
    """A concern Alfred has flagged."""

    id: str
    text: str
    severity: RiskSeverity = "medium"
    confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    first_seen_at: str = Field(default_factory=utc_now_iso)
    source_event_ids: list[str] = Field(default_factory=list)


class AlfredExtraction(BaseModel):
    """
    Alfred's per-tick structured output.

    This is purely *what Alfred thinks*. Outbound actions (sending into the
    meeting chat) happen via the ``send_to_meeting_chat`` tool and do NOT
    live on this payload.

    All list fields are *deltas for this tick* (what is newly observed),
    except ``running_summary``/``topics`` which replace prior values. The
    session layer merges deltas into the session's rolling state.
    """

    rationale: str = Field(
        default="",
        description="One-line justification of why Alfred did (or did not) act this tick",
    )
    running_summary: str = Field(
        default="",
        description="Full running summary replacing prior (markdown); <=150 words",
    )
    topics: list[str] = Field(
        default_factory=list,
        description="Current discovered topics (running list; replaces prior)",
    )
    notes: list[str] = Field(
        default_factory=list,
        description="New notes added this tick (delta — not the full list)",
    )
    decisions: list[Decision] = Field(
        default_factory=list,
        description="New or updated decisions this tick",
    )
    open_questions: list[OpenQuestion] = Field(
        default_factory=list,
        description="New or updated open questions this tick",
    )
    action_items: list[ActionItem] = Field(
        default_factory=list,
        description="New or updated action items this tick",
    )
    risks: list[Risk] = Field(
        default_factory=list,
        description="New or updated risks this tick",
    )


# Backwards-compat alias during migration (still imported by a handful of
# call sites that merely need "something named AlfredAction"). New code
# should use AlfredExtraction directly.
AlfredAction = AlfredExtraction


class ToolCallRecord(BaseModel):
    """Audit record of one tool invocation made by the agent."""

    id: str = Field(default_factory=lambda: f"tc_{utc_now_iso()}")
    tool_name: str
    timestamp_utc: str = Field(default_factory=utc_now_iso)
    arguments: dict = Field(default_factory=dict)
    ok: bool = True
    result: dict = Field(default_factory=dict)
    error: Optional[str] = None


class AnalysisItem(BaseModel):
    """Envelope for per-turn analysis output."""

    response_id: str
    timestamp_utc: str = Field(default_factory=utc_now_iso)
    question_text: Optional[str] = None
    response_text: str
    speaker_id: Optional[str] = None
    trigger_event_id: Optional[str] = None
    relevance_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    clarity_score: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    key_points: list[str] = Field(default_factory=list)
    follow_up_suggestions: list[str] = Field(default_factory=list)
    extraction: Optional[AlfredExtraction] = Field(
        default=None,
        description="Alfred's structured extraction for this tick",
    )
    tool_calls: list[ToolCallRecord] = Field(
        default_factory=list,
        description="Tool invocations the agent made while producing this item",
    )
    raw_model_output: Optional[dict] = None


class InterviewSession(BaseModel):
    """
    Tracks an active meeting session.

    Name kept for compatibility with existing code, but the state is
    meeting-generic and Alfred-specific.
    """

    session_id: str
    candidate_name: str = Field(..., description="Primary meeting subject label (freeform)")
    meeting_url: str
    started_at: str
    ended_at: Optional[str] = None
    speaker_mappings: list[SpeakerMapping] = Field(default_factory=list)
    transcript_events: list[TranscriptEvent] = Field(default_factory=list)
    chat_messages: list[ChatMessage] = Field(default_factory=list)
    meeting_events: list[MeetingEvent] = Field(default_factory=list)
    conversation_reference_id: Optional[str] = Field(
        default=None,
        description="Captured once the first chat message arrives; used by the send tool",
    )
    graph_chat_thread_id: Optional[str] = None
    prompt_cache_key: Optional[str] = None
    latest_response_id: Optional[str] = None
    latest_agent_cursor: int = 0
    last_compaction_at: Optional[str] = None
    running_summary: str = ""
    topics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    alfred_muted: bool = False
    outbound_chat_intents: list[OutboundChatIntent] = Field(default_factory=list)
    # Rolling intent-alignment state — merged from each AlfredExtraction.
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)


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
    running_summary: str = ""
    topics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    open_questions: list[OpenQuestion] = Field(default_factory=list)
    action_items: list[ActionItem] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)

    def compute_overall_scores(self) -> None:
        """Compute overall scores from scored analysis items (no-op for Alfred items)."""

        scored = [
            item
            for item in self.analysis_items
            if item.relevance_score is not None and item.clarity_score is not None
        ]
        self.total_responses_analyzed = len(self.analysis_items)
        if not scored:
            self.overall_relevance = None
            self.overall_clarity = None
            return
        self.overall_relevance = sum(i.relevance_score for i in scored) / len(scored)
        self.overall_clarity = sum(i.clarity_score for i in scored) / len(scored)
