"""
Batcave Transcript Service v2

Receives real-time transcript events from the C# bot with speaker diarization,
manages interview sessions, and integrates with the interview analysis agent.

Endpoints:
    POST /transcript       - Receive transcript events (v1 and v2 format)
    POST /session/start    - Start new interview session
    POST /session/map-speaker - Map speaker ID to role
    GET  /session/status   - Get current session info
    POST /session/end      - End session, trigger final analysis
    GET  /health           - Health check
    GET  /stats            - Statistics

Target deployment: https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io (behind TLS proxy)
Internal binding: configured by SINK_HOST/SINK_PORT (default 0.0.0.0:8765)
"""

from __future__ import annotations

import asyncio
import hashlib
import json as _json_mod
import logging
import os
import uuid as _uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Annotated, Any, AsyncIterator, TypedDict

import aiofiles
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from meeting_agent.debounce import (
    DEFAULT_MAX_BATCH,
    DEFAULT_QUIET_WINDOW_SECONDS,
    drain_with_debounce,
)
from meeting_agent.events import AlfredEventBus, detect_direct_address, format_sse
from meeting_agent.identity import ParticipantResolver
from meeting_agent.models import (
    AnalysisItem,
    ChatMessage,
    MeetingEvent,
    Participant,
    RawIngestEvent,
    SessionAnalysis,
    TranscriptEvent,
)
from meeting_agent.output import AnalysisOutputWriter
from meeting_agent.persistence import SessionStore, build_store
from meeting_agent.session import InterviewSessionManager, SessionRegistry
from meeting_agent.checklist_state import ChecklistDefinition, ChecklistStateManager
from batcave_platform import (
    AgentTool,
    PLATFORM_NAME,
    load_product_spec,
)
from batcave_platform.routes import RouteOrchestrator, build_route_orchestrator
from variants import VariantPlugin, load_variant

# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

@dataclass(frozen=True)
class RuntimeConfig:
    """Runtime config for multi-instance sink execution."""

    variant_id: str
    product_spec_path: str
    instance_id: str
    sink_host: str
    sink_port: int
    output_dir: Path
    transcript_file: Path
    store_db_path: Path


def load_runtime_config() -> RuntimeConfig:
    """Load runtime config from environment with strict validation."""
    variant_id = (os.environ.get("VARIANT_ID", "alfred") or "").strip().lower()
    if not variant_id:
        raise RuntimeError("VARIANT_ID resolved to empty value.")

    product_spec_path = (os.environ.get("PRODUCT_SPEC_PATH") or "").strip()
    if not product_spec_path:
        raise RuntimeError(
            "PRODUCT_SPEC_PATH is required. Provide a product spec path at runtime."
        )

    instance_id = (os.environ.get("INSTANCE_ID", variant_id) or "").strip()
    if not instance_id:
        raise RuntimeError(
            "INSTANCE_ID resolved to empty value. Set INSTANCE_ID or VARIANT_ID."
        )

    sink_host = (os.environ.get("SINK_HOST", "0.0.0.0") or "").strip()
    if not sink_host:
        raise RuntimeError("SINK_HOST resolved to empty value.")

    sink_port_raw = (os.environ.get("SINK_PORT", "8765") or "").strip()
    if not sink_port_raw:
        raise RuntimeError("SINK_PORT resolved to empty value.")

    try:
        sink_port = int(sink_port_raw)
    except ValueError as exc:
        raise RuntimeError(f"SINK_PORT must be an integer. Got: {sink_port_raw}") from exc

    if sink_port < 1 or sink_port > 65535:
        raise RuntimeError(f"SINK_PORT must be in range 1-65535. Got: {sink_port}.")

    output_override = os.environ.get("OUTPUT_DIR")
    if output_override:
        output_dir = Path(output_override).expanduser()
    elif "INSTANCE_ID" in os.environ:
        output_dir = Path(__file__).parent / "output" / instance_id
    else:
        output_dir = Path(__file__).parent / "output"

    transcript_override = os.environ.get("TRANSCRIPT_FILE")
    if transcript_override:
        transcript_file = Path(transcript_override).expanduser()
    else:
        desktop_path = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / "Desktop"
        if instance_id == "default":
            transcript_file = desktop_path / "meeting_transcript.txt"
        else:
            transcript_file = desktop_path / f"meeting_transcript_{instance_id}.txt"

    store_override = os.environ.get("STORE_DB_PATH")
    if store_override:
        store_db_path = Path(store_override).expanduser()
    else:
        store_db_path = output_dir / "alfred.sqlite3"

    return RuntimeConfig(
        variant_id=variant_id,
        product_spec_path=product_spec_path,
        instance_id=instance_id,
        sink_host=sink_host,
        sink_port=sink_port,
        output_dir=output_dir,
        transcript_file=transcript_file,
        store_db_path=store_db_path,
    )


RUNTIME_CONFIG = load_runtime_config()
PRODUCT_SPEC, PRODUCT_SPEC_PATH = load_product_spec(RUNTIME_CONFIG.product_spec_path)
VARIANT = load_variant(RUNTIME_CONFIG.variant_id)
ROUTES = build_route_orchestrator(PRODUCT_SPEC)

# Output directory for analysis results
OUTPUT_DIR = RUNTIME_CONFIG.output_dir

# Transcript file path - save to Desktop for easy access (Windows VM default)
TRANSCRIPT_FILE = RUNTIME_CONFIG.transcript_file

# CORS configuration - modify for production
CORS_ORIGINS: list[str] = [
    "http://localhost:8501",  # Streamlit default
    "http://localhost:3000",  # Common React dev port
    "https://ca-alfred-api.gentlewater-5aa74a73.eastus.azurecontainerapps.io",
]

# =============================================================================
# Optional Agent Import (graceful degradation)
# =============================================================================

try:
    from meeting_agent.agent import InterviewAnalyzer
    from meeting_agent.checklist import ChecklistAgent
    from meeting_agent.pubsub import ThoughtType, get_publisher

    AGENT_AVAILABLE = True
    logger.info("Alfred analyzer loaded successfully")
except ImportError as e:
    AGENT_AVAILABLE = False
    InterviewAnalyzer = None  # type: ignore[misc, assignment]
    ChecklistAgent = None  # type: ignore[misc, assignment]
    get_publisher = None  # type: ignore[misc, assignment]
    ThoughtType = None  # type: ignore[misc, assignment]
    logger.warning(
        "meeting_agent.agent module not available: %s. "
        "Agent analysis features disabled.",
        e,
    )


# =============================================================================
# Enums
# =============================================================================


class SpeakerRole(str, Enum):
    """Speaker roles in an interview."""

    CANDIDATE = "candidate"
    INTERVIEWER = "interviewer"
    UNKNOWN = "unknown"


# =============================================================================
# Request Models
# =============================================================================


class TranscriptEventRequest(BaseModel):
    """
    Request model for transcript events.

    Supports both v1 and v2 formats through field aliases.
    """

    # v2 fields (canonical)
    event_type: str | None = Field(
        default=None,
        description="Event type: partial, final, session_started, session_stopped, error",
    )
    text: str | None = Field(default=None, description="Transcript text content")
    timestamp_utc: str | None = Field(
        default=None, description="UTC timestamp in ISO format"
    )
    chat_thread_id: str | None = Field(
        default=None,
        description="Teams chat thread id (meeting id) the event belongs to",
    )
    speaker_id: str | None = Field(
        default=None, description="Speaker identifier (e.g., speaker_0)"
    )
    audio_start_ms: float | None = Field(
        default=None, description="Audio start time in milliseconds"
    )
    audio_end_ms: float | None = Field(
        default=None, description="Audio end time in milliseconds"
    )
    confidence: float | None = Field(
        default=None, ge=0.0, le=1.0, description="Recognition confidence"
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Additional metadata"
    )
    dominant_media_source_id: int | None = Field(
        default=None,
        description=(
            "Teams MediaSourceId most recently flagged dominant by the "
            "Graph Communications Media SDK at publish time (E3)."
        ),
    )
    active_media_source_ids: list[int] | None = Field(
        default=None,
        description=(
            "Snapshot of active MediaSourceIds at publish time, from "
            "AudioMediaBuffer.ActiveSpeakers (E3)."
        ),
    )
    team_id: str | None = Field(
        default=None,
        description=(
            "Teams team (group) id, when the bot has learned that this "
            "meeting was spawned from a channel. Stamped on every "
            "downstream MeetingEvent + RawIngestEvent so analytics can "
            "group transcripts by channel_id."
        ),
    )
    channel_id: str | None = Field(
        default=None,
        description="Teams channel id, paired with team_id.",
    )
    channel_thread_id: str | None = Field(
        default=None,
        description=(
            "Parent channel's conversation id (19:{channelId}@thread.tacv2). "
            "Lets meetings spawned from a channel roll up under it."
        ),
    )

    # v1 fields (legacy aliases)
    Kind: str | None = Field(default=None, description="Legacy v1 event kind")
    Text: str | None = Field(default=None, description="Legacy v1 text")
    TsUtc: str | None = Field(default=None, description="Legacy v1 timestamp")
    Details: str | None = Field(default=None, description="Legacy v1 error details")

    model_config = {"extra": "ignore"}


class SessionStartRequest(BaseModel):
    """Request to start a new interview session."""

    candidate_name: str = Field(..., min_length=1, description="Name of the candidate")
    meeting_url: str = Field(..., min_length=1, description="Teams meeting join URL")
    product_id: str | None = Field(
        default=None,
        description="Optional product id. Must match active product spec when provided.",
    )
    candidate_speaker_id: str | None = Field(
        default=None,
        description="Optional speaker_id to map to candidate (can be mapped later)",
    )


class SpeakerMapRequest(BaseModel):
    """Request to map a speaker ID to a role."""

    speaker_id: str = Field(
        ..., min_length=1, description="Speaker identifier: speaker_0, speaker_1, etc."
    )
    role: SpeakerRole = Field(..., description="Role: candidate or interviewer")


class MuteRequest(BaseModel):
    """Body for POST /m/{chat_thread_id}/mute."""

    muted: bool


class ParticipantRosterEntry(BaseModel):
    """One participant pushed by the C# bot from ICall.Participants (E3)."""

    aad_object_id: str = Field(..., min_length=1)
    display_name: str | None = None
    user_principal_name: str | None = None
    media_source_ids: list[int] = Field(default_factory=list)
    is_in_lobby: bool = False
    role: str | None = None
    is_application: bool = False
    first_seen_at_utc: str | None = None
    last_seen_at_utc: str | None = None

    model_config = {"extra": "ignore"}


class ParticipantsUpdateRequest(BaseModel):
    """Body of POST /session/participants from the C# bot (E3)."""

    session_id: str | None = Field(
        default=None,
        description="Optional explicit session_id; if omitted, chat_thread_id is required",
    )
    chat_thread_id: str | None = None
    fetched_at_utc: str | None = None
    participants: list[ParticipantRosterEntry] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class SpeakerMappingOverride(BaseModel):
    """Body of POST /sessions/{id}/speaker-mapping (manual override, E3)."""

    speaker_id: str = Field(..., min_length=1)
    aad_object_id: str = Field(..., min_length=1)


class ChatMessageRequest(BaseModel):
    """Chat or channel-message event pushed from the C# bot to POST /chat.

    ``chat_thread_id`` is the canonical session key. For meeting chats it is
    ``19:meeting_xxx@thread.v2``; for Teams channels the C# bot maps to
    ``19:{channel_id}@thread.tacv2`` so both ingress paths key the same
    Python session. ``conversation_kind`` distinguishes the source and
    ``team_id`` / ``channel_id`` are populated for channel events.
    """

    event_type: str = Field(
        default="chat_created",
        description="chat_created | chat_updated | chat_deleted",
    )
    chat_thread_id: str = Field(..., min_length=1)
    message_id: str = Field(..., min_length=1)
    text: str | None = None
    html: str | None = None
    sender_id: str | None = None
    sender_display_name: str | None = None
    timestamp_utc: str = Field(..., min_length=1)
    conversation_reference_id: str | None = None
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    mentions: list[dict[str, Any]] = Field(default_factory=list)
    reply_to_message_id: str | None = None
    from_bot: bool = False
    raw: dict[str, Any] | None = None
    conversation_kind: str | None = Field(
        default=None,
        description="meeting_chat | channel | group_chat | personal | unknown",
    )
    team_id: str | None = Field(
        default=None,
        description="Teams team (group) id when the event is in a team channel.",
    )
    channel_id: str | None = Field(
        default=None,
        description="Teams channel id when the event is in a team channel.",
    )
    channel_thread_id: str | None = Field(
        default=None,
        description=(
            "Parent channel's conversation id (19:{channelId}@thread.tacv2). "
            "For a channel post equals chat_thread_id; for a meeting "
            "spawned from the channel points at the parent."
        ),
    )

    model_config = {"extra": "ignore"}


# =============================================================================
# Response Models
# =============================================================================


class BaseResponse(BaseModel):
    """Base response model with common fields."""

    ok: bool = Field(..., description="Whether the operation succeeded")
    message: str | None = Field(default=None, description="Optional status message")


class ErrorResponse(BaseModel):
    """Standard error response."""

    ok: bool = Field(default=False)
    error: str = Field(..., description="Error description")
    error_code: str | None = Field(default=None, description="Machine-readable error code")


class TranscriptResponse(BaseResponse):
    """Response for transcript submission."""

    received_at: str = Field(..., description="Server receipt timestamp")


class SessionStartResponse(BaseResponse):
    """Response for session start."""

    session_id: str = Field(..., description="Unique session identifier")
    started_at: str = Field(..., description="Session start timestamp")


class SpeakerMapResponse(BaseResponse):
    """Response for speaker mapping."""

    speaker_mappings: dict[str, str] = Field(
        default_factory=dict, description="Current speaker-to-role mappings"
    )


class SessionStatusResponse(BaseModel):
    """Session status information."""

    active: bool = Field(..., description="Whether a session is currently active")
    session_id: str | None = Field(default=None, description="Current session ID")
    candidate_name: str | None = Field(default=None, description="Candidate name")
    meeting_url: str | None = Field(default=None, description="Meeting URL")
    started_at: str | None = Field(default=None, description="Session start time")
    speaker_mappings: dict[str, str] = Field(
        default_factory=dict, description="Speaker-to-role mappings"
    )
    recent_conversation: list[dict[str, str | None]] = Field(
        default_factory=list,
        description="Recent final transcript turns with speaker role annotations",
    )
    meeting_history: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Unified timeline: speech turns + chat messages, ordered by timestamp",
    )
    chat_messages_count: int = Field(
        default=0, description="Total meeting chat messages ingested"
    )
    conversation_reference_id: str | None = Field(
        default=None,
        description="Captured ConversationReference id for the meeting chat (once bot sees first chat)",
    )
    graph_chat_thread_id: str | None = Field(default=None)
    prompt_cache_key: str | None = Field(default=None)
    latest_response_id: str | None = Field(default=None)
    running_summary: str = Field(default="")
    topics: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    open_questions: list[dict[str, Any]] = Field(default_factory=list)
    action_items: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    total_events: int = Field(default=0, description="Total transcript events received")
    final_events: int = Field(default=0, description="Final transcript events")
    analysis_count: int = Field(default=0, description="Number of analyses performed")
    checklist: list[dict[str, str | None]] = Field(
        default_factory=list, description="Current checklist state"
    )
    product_id: str = Field(..., description="Active product id")


class SessionStatusWrapper(BaseModel):
    """Wrapper for session status response."""

    session: SessionStatusResponse
    agent_available: bool = Field(..., description="Whether agent analysis is available")
    product_id: str = Field(..., description="Active product id")


class SessionAnalysisResponse(BaseResponse):
    """Persisted session analysis payload for UI polling."""

    session_id: str | None = Field(default=None, description="Requested session ID")
    analysis: SessionAnalysis | None = Field(
        default=None,
        description="Current persisted analysis, if available",
    )


class SessionEndResponse(BaseResponse):
    """Response for session end."""

    summary: dict[str, Any] = Field(..., description="Session summary statistics")


class HealthResponse(BaseModel):
    """Health check response."""

    status: str = Field(..., description="Service health status")
    service: str = Field(..., description="Service name")
    version: str = Field(..., description="Service version")
    timestamp: str = Field(..., description="Current server timestamp")
    agent_available: bool = Field(..., description="Whether agent is available")
    session_active: bool = Field(..., description="Whether a session is active")
    variant_id: str = Field(..., description="Active variant ID")
    product_id: str = Field(..., description="Active product id")
    instance_id: str = Field(..., description="Active instance ID")


class StatsResponse(BaseModel):
    """Statistics response."""

    stats: dict[str, Any] = Field(..., description="Service statistics")
    transcript_queue_size: int = Field(..., description="Pending transcript events")
    agent_queue_size: int = Field(..., description="Pending agent analysis items")
    session: dict[str, Any] = Field(..., description="Current session info")
    agent_available: bool = Field(..., description="Whether agent is available")
    output_directory: str = Field(..., description="Analysis output directory path")
    variant_id: str = Field(..., description="Active variant ID")
    product_id: str = Field(..., description="Active product id")
    instance_id: str = Field(..., description="Active instance ID")


class ProductSpecResponse(BaseModel):
    """Public summary of active product spec."""

    platform: str
    product_id: str
    display_name: str
    spec_path: str
    checklist_items: list[dict[str, Any]]
    agent: dict[str, Any]
    outputs: list[dict[str, Any]]


# =============================================================================
# Application State (Type-safe Lifespan State)
# =============================================================================


class AppStats(TypedDict):
    """Application statistics tracking."""

    events_received: int
    partial_transcripts: int
    final_transcripts: int
    errors: int
    session_events: int
    v1_events: int
    v2_events: int
    agent_analyses: int
    checklist_updates: int
    route_dispatch_total: int
    route_dispatch_failures: int
    started_at: str


AgentQueueItem = tuple[str, MeetingEvent]


class AppState(TypedDict):
    """Type-safe application state managed by lifespan."""

    session_manager: InterviewSessionManager
    session_registry: SessionRegistry
    output_writer: AnalysisOutputWriter
    store: SessionStore
    event_bus: AlfredEventBus
    transcript_queue: asyncio.Queue[TranscriptEvent]
    agent_queue: asyncio.Queue[AgentQueueItem]
    stats: AppStats
    agent_task: asyncio.Task[None] | None
    variant_plugin: VariantPlugin
    checklist_manager: ChecklistStateManager
    route_orchestrator: RouteOrchestrator
    checklist_agent: ChecklistAgent | None
    last_enqueued_event_ids: dict[str, str]
    participant_resolver: ParticipantResolver


def get_initial_stats() -> AppStats:
    """Create initial statistics dictionary."""
    return AppStats(
        events_received=0,
        partial_transcripts=0,
        final_transcripts=0,
        errors=0,
        session_events=0,
        v1_events=0,
        v2_events=0,
        agent_analyses=0,
        checklist_updates=0,
        route_dispatch_total=0,
        route_dispatch_failures=0,
        started_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


# =============================================================================
# Custom Exceptions
# =============================================================================


class TranscriptServiceError(Exception):
    """Base exception for transcript service errors."""

    def __init__(
        self,
        message: str,
        status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR,
        error_code: str | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(message)


class SessionNotActiveError(TranscriptServiceError):
    """Raised when operation requires an active session."""

    def __init__(self, message: str = "No active session. Start a session first.") -> None:
        super().__init__(
            message=message,
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="SESSION_NOT_ACTIVE",
        )


class SessionAlreadyActiveError(TranscriptServiceError):
    """Raised when trying to start a session when one is already active."""

    def __init__(
        self, message: str = "Session already active. End current session first."
    ) -> None:
        super().__init__(
            message=message,
            status_code=status.HTTP_409_CONFLICT,
            error_code="SESSION_ALREADY_ACTIVE",
        )


# =============================================================================
# Dependencies
# =============================================================================


def get_app_state(request: Request) -> AppState:
    """
    Dependency to retrieve application state from request.

    Args:
        request: The incoming request object.

    Returns:
        AppState dictionary from lifespan context.

    Raises:
        RuntimeError: If state is not properly initialized.
    """
    state = getattr(request, "state", None)
    if state is None:
        raise RuntimeError("Application state not initialized")
    return AppState(
        session_manager=state.session_manager,
        session_registry=state.session_registry,
        output_writer=state.output_writer,
        store=state.store,
        event_bus=state.event_bus,
        transcript_queue=state.transcript_queue,
        agent_queue=state.agent_queue,
        stats=state.stats,
        agent_task=state.agent_task,
        variant_plugin=state.variant_plugin,
        checklist_manager=state.checklist_manager,
        route_orchestrator=state.route_orchestrator,
        checklist_agent=state.checklist_agent,
        last_enqueued_event_ids=state.last_enqueued_event_ids,
        participant_resolver=state.participant_resolver,
    )


# Type alias for dependency injection
AppStateDep = Annotated[AppState, Depends(get_app_state)]


# =============================================================================
# Event Normalization (v1 -> v2)
# =============================================================================


def normalize_v1_to_v2(request: TranscriptEventRequest, stats: AppStats) -> dict[str, Any]:
    """
    Convert v1 format to v2 format.

    Args:
        request: The incoming transcript event request.
        stats: Application statistics to update.

    Returns:
        Normalized v2 format dictionary.
    """
    # Check if this is v1 format (has "Kind" key)
    if request.Kind is not None:
        kind = request.Kind

        # Map v1 event types to v2
        event_type_map = {
            "recognizing": "partial",
            "recognized": "final",
            "session_started": "session_started",
            "session_stopped": "session_stopped",
            "canceled": "error",
        }

        v2_payload: dict[str, Any] = {
            "event_type": event_type_map.get(kind.lower(), kind.lower()),
            "text": request.Text,
            "timestamp_utc": (
                request.TsUtc
                or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        }

        # Handle error details
        if request.Details:
            v2_payload["metadata"] = {"raw_response": {"error_details": request.Details}}

        stats["v1_events"] += 1
        return v2_payload

    # Already v2 format - build from request fields
    stats["v2_events"] += 1
    return {
        "event_type": request.event_type,
        "text": request.text,
        "timestamp_utc": (
            request.timestamp_utc
            or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "chat_thread_id": request.chat_thread_id,
        "speaker_id": request.speaker_id,
        "audio_start_ms": request.audio_start_ms,
        "audio_end_ms": request.audio_end_ms,
        "confidence": request.confidence,
        "metadata": request.metadata,
    }


def _extract_msi_hints(
    request: TranscriptEventRequest,
) -> tuple[int | None, list[int] | None]:
    """Pull MSI hints from the wire payload (top-level or metadata)."""
    dominant = request.dominant_media_source_id
    active = request.active_media_source_ids

    md = request.metadata or {}
    if dominant is None:
        candidate = md.get("dominant_media_source_id") or md.get(
            "DominantMediaSourceId"
        )
        if isinstance(candidate, (int, float)):
            dominant = int(candidate)
        elif isinstance(candidate, str) and candidate.strip().isdigit():
            dominant = int(candidate.strip())
    if active is None:
        candidate_list = md.get("active_media_source_ids") or md.get(
            "ActiveMediaSourceIds"
        )
        if isinstance(candidate_list, list):
            active = [int(x) for x in candidate_list if isinstance(x, (int, float, str))
                      and (isinstance(x, (int, float)) or str(x).strip().isdigit())]
            if not active:
                active = None
    return dominant, active


# =============================================================================
# File Operations (Async)
# =============================================================================


async def save_transcript_to_file(event: TranscriptEvent) -> None:
    """
    Append transcript to file on desktop asynchronously.

    Args:
        event: The transcript event to save.
    """
    try:
        TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)

        async with aiofiles.open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
            if event.event_type == "session_started":
                await f.write(f"\n{'=' * 60}\n")
                await f.write(f"NEW SESSION STARTED: {event.timestamp_utc}\n")
                await f.write(f"{'=' * 60}\n\n")
            elif event.event_type == "final" and event.text:
                speaker_info = f"[{event.speaker_id}]" if event.speaker_id else ""
                await f.write(f"[{event.timestamp_utc}]{speaker_info} {event.text}\n")
            elif event.event_type == "session_stopped":
                await f.write(f"\n--- Session ended: {event.timestamp_utc} ---\n\n")

        logger.debug("Saved to file: %s", TRANSCRIPT_FILE)
    except OSError as e:
        logger.error("Failed to save transcript to file: %s", e)


def build_checklist_manager() -> ChecklistStateManager:
    """Create checklist state manager from active product spec."""
    definitions = tuple(
        ChecklistDefinition(
            id=item.id,
            label=item.label,
            keywords=item.keywords,
        )
        for item in PRODUCT_SPEC.checklist.items
    )
    return ChecklistStateManager(definitions)


async def dispatch_route_payload(
    route_orchestrator: RouteOrchestrator,
    stats: AppStats,
    payload: dict[str, Any],
) -> None:
    """Dispatch payload to all configured routes and update stats."""
    results = await route_orchestrator.dispatch_all(payload)
    stats["route_dispatch_total"] += len(results)
    failures = [result for result in results if not result.ok]
    stats["route_dispatch_failures"] += len(failures)
    for failed in failures:
        logger.warning(
            "Route dispatch failed: route_id=%s route_type=%s detail=%s",
            failed.route_id,
            failed.route_type,
            failed.detail,
        )


def maybe_auto_map_candidate(session_manager: InterviewSessionManager) -> str | None:
    """
    Auto-map the dominant speaker to candidate when there is no manual mapping.
    """
    current_candidate = session_manager.get_candidate_speaker_id()
    if current_candidate is not None:
        return current_candidate

    inferred_candidate = session_manager.infer_candidate_speaker_id()
    if (
        inferred_candidate is not None
        and session_manager.session is not None
        and session_manager.get_speaker_role(inferred_candidate) is None
    ):
        session_manager.map_speaker(
            inferred_candidate,
            "candidate",
            session_manager.session.candidate_name,
        )
        logger.info("Auto-mapped candidate speaker to %s", inferred_candidate)

    return session_manager.get_candidate_speaker_id() or inferred_candidate


def resolve_manager_for_inbound(
    registry: SessionRegistry,
    chat_thread_id: str | None,
    *,
    auto_start: bool,
) -> tuple[InterviewSessionManager, str]:
    """Pick the per-meeting session manager for an inbound transcript/chat.

    Returns ``(manager, resolved_chat_thread_id)``. The resolved id is what
    keys the agent queue and SSE filter for this event.

    Routing rules:

    * If ``chat_thread_id`` is empty, route to the legacy default slot.
    * If the default slot has an active legacy session (started via
      ``POST /session/start`` without a thread id), prefer it when the
      inbound thread id matches or is unset — keeps single-meeting tooling
      and tests working unchanged.
    * Otherwise, look up (and optionally auto-start) the per-thread slot.
    """
    thread_id = (chat_thread_id or "").strip()
    if not thread_id:
        thread_id = SessionRegistry.DEFAULT_THREAD_ID

    default_mgr = registry.get(SessionRegistry.DEFAULT_THREAD_ID)
    if default_mgr is not None and default_mgr.is_active and default_mgr.session is not None:
        bound = (default_mgr.session.graph_chat_thread_id or "").strip()
        if (
            thread_id == SessionRegistry.DEFAULT_THREAD_ID
            or not bound
            or bound == thread_id
        ):
            return default_mgr, SessionRegistry.DEFAULT_THREAD_ID

    if auto_start:
        manager = registry.get_or_start(thread_id, candidate_name="Meeting", meeting_url="")
    else:
        manager = registry.get_or_create(thread_id)
    return manager, thread_id


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_json(payload: Any) -> str:
    """Deterministic JSON for hashing (sorted keys, no extraneous whitespace)."""
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    return _json_mod.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def resolve_channel_context(
    store: SessionStore,
    chat_thread_id: str | None,
    *,
    team_id: str | None = None,
    channel_id: str | None = None,
    channel_thread_id: str | None = None,
) -> tuple[str | None, str | None, str | None]:
    """Resolve the channel-context tuple stamped on every event.

    Precedence: explicit values from the inbound request first
    (the C# bot stamps these once it has learned the context), then
    fall back to the persisted ``session_channel_links`` row for this
    thread (set via ``POST /session/link``). Returning Nones is fine —
    they indicate "no channel context known yet". A subsequent
    ``/session/link`` will backfill prior rows.
    """
    if team_id and channel_id:
        return team_id, channel_id, channel_thread_id
    if not chat_thread_id:
        return team_id, channel_id, channel_thread_id
    try:
        link = store.get_channel_link(chat_thread_id)
    except Exception as exc:  # noqa: BLE001 - resolver must not break ingest
        logger.debug("channel link lookup failed for %s: %s", chat_thread_id, exc)
        return team_id, channel_id, channel_thread_id
    if not link:
        return team_id, channel_id, channel_thread_id
    return (
        team_id or link.get("team_id"),
        channel_id or link.get("channel_id"),
        channel_thread_id or link.get("channel_thread_id"),
    )


def record_raw(
    store: SessionStore,
    *,
    source: str,
    event_type: str,
    payload: Any,
    session_id: str | None = None,
    provider_timestamp_utc: str | None = None,
    speaker_or_sender_id: str | None = None,
    dropped_reason: str | None = None,
    normalized_payload: Any | None = None,
    normalized_event_id: str | None = None,
    team_id: str | None = None,
    channel_id: str | None = None,
    channel_thread_id: str | None = None,
) -> str:
    """Record an inbound payload to the raw audit store and return its raw_event_id.

    Called from every ingress route BEFORE any filter (partial drop,
    session-active drop, echo suppression). The returned id is back-linked
    onto the promoted ``MeetingEvent.source_raw_event_ids`` when the event
    becomes a ledger row, or left dangling with ``dropped_reason`` set when
    the event was filtered out. ``team_id``/``channel_id``/``channel_thread_id``
    are optional and let analytics filter raw audit rows by channel without
    a join.
    """
    raw_payload_json = _stable_json(payload)
    raw_event = RawIngestEvent(
        raw_event_id=_uuid.uuid4().hex,
        session_id=session_id,
        received_at_utc=_now_utc_iso(),
        provider_timestamp_utc=provider_timestamp_utc,
        source=source,  # type: ignore[arg-type]
        event_type=event_type,
        speaker_or_sender_id=speaker_or_sender_id,
        payload_hash=hashlib.sha256(raw_payload_json.encode("utf-8")).hexdigest(),
        raw_payload_json=raw_payload_json,
        normalized_payload_json=(
            _stable_json(normalized_payload) if normalized_payload is not None else None
        ),
        normalized_event_id=normalized_event_id,
        dropped_reason=dropped_reason,  # type: ignore[arg-type]
        team_id=team_id,
        channel_id=channel_id,
        channel_thread_id=channel_thread_id,
    )
    try:
        store.record_raw_ingest_event(raw_event)
    except Exception as exc:  # noqa: BLE001 - audit failures must not break ingest
        logger.warning("raw_ingest_events insert failed: %s", exc)
    return raw_event.raw_event_id


def _patch_raw_promotion(
    store: SessionStore,
    raw_event_id: str | None,
    *,
    session_id: str | None = None,
    normalized_event_id: str | None = None,
    dropped_reason: str | None = None,
) -> None:
    if not raw_event_id:
        return
    try:
        store.update_raw_ingest_promotion(
            raw_event_id,
            session_id=session_id,
            normalized_event_id=normalized_event_id,
            dropped_reason=dropped_reason,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("raw_ingest_events update failed: %s", exc)


async def enqueue_analysis_event(
    state: AppState,
    chat_thread_id: str,
    event: MeetingEvent | None,
) -> None:
    """Queue a normalized meeting event for Alfred live-turn analysis."""
    if event is None:
        return

    manager = state["session_registry"].get(chat_thread_id)
    if manager is None or manager.session is None:
        return

    if event.kind == "speech" and not event.text.strip():
        return
    if event.kind == "chat" and (event.from_bot or not event.text.strip()):
        return

    last_event_id = state["last_enqueued_event_ids"].get(manager.session.session_id)
    if last_event_id == event.event_id:
        return

    await state["agent_queue"].put((chat_thread_id, event))
    state["last_enqueued_event_ids"][manager.session.session_id] = event.event_id
    logger.info(
        "Queued Alfred analysis event kind=%s event_id=%s thread=%s",
        event.kind,
        event.event_id,
        chat_thread_id,
    )


# =============================================================================
# Agent Processing
# =============================================================================


async def agent_processing_loop(
    agent_queue: asyncio.Queue[AgentQueueItem],
    session_registry: SessionRegistry,
    output_writer: AnalysisOutputWriter,
    store: SessionStore,
    event_bus: AlfredEventBus,
    stats: AppStats,
    variant_plugin: VariantPlugin,
    checklist_manager: ChecklistStateManager,
    route_orchestrator: RouteOrchestrator,
) -> None:
    """Background task that processes unified meeting events through Alfred.

    With multi-session routing, the queue carries ``(chat_thread_id, event)``
    tuples. The analyzer is constructed lazily per-thread so each Alfred
    instance binds to that meeting's ``InterviewSessionManager``.
    """
    logger.info("Agent processing loop started")

    publisher = get_publisher() if (AGENT_AVAILABLE and get_publisher) else None
    analyzers: dict[str, Any] = {}

    def _resolve_analyzer(manager: InterviewSessionManager):
        if not (AGENT_AVAILABLE and InterviewAnalyzer):
            return None
        thread_id = manager.session.graph_chat_thread_id if manager.session else None
        key = thread_id or id(manager)
        analyzer = analyzers.get(key)
        if analyzer is not None:
            return analyzer
        try:
            analyzer = InterviewAnalyzer(
                model=PRODUCT_SPEC.agent.model,
                session_manager=manager,
                publish_thoughts=True,
                reasoning_effort=PRODUCT_SPEC.agent.reasoning_effort,
                instructions=PRODUCT_SPEC.agent.prompt_template,
                send_chat_url=os.environ.get("BOT_SEND_CHAT_URL") or None,
                intervention_policy=PRODUCT_SPEC.agent.intervention_policy,
            )
            analyzers[key] = analyzer
            logger.info("Alfred analyzer initialized for thread=%s", thread_id)
            return analyzer
        except Exception as e:
            logger.error("Failed to initialize Alfred analyzer: %s", e)
            return None

    response_counter = 0

    while True:
        try:
            queue_item, batch_size = await drain_with_debounce(
                agent_queue,
                quiet_window_seconds=DEFAULT_QUIET_WINDOW_SECONDS,
                max_batch=DEFAULT_MAX_BATCH,
            )
            del batch_size  # logged inside the helper
            chat_thread_id, event = queue_item

            session_manager = session_registry.get(chat_thread_id)
            if session_manager is None:
                continue

            if not event.text:
                continue

            logger.info(
                "AGENT_INPUT [%s/%s/thread=%s]: %s...",
                event.kind,
                event.speaker_id,
                chat_thread_id,
                event.text[:100],
            )

            analyzer = _resolve_analyzer(session_manager)
            if analyzer and session_manager.is_active:
                try:
                    analysis_context = session_manager.get_agent_context_snapshot(
                        trigger_event=event,
                    )
                    analysis_context = variant_plugin.build_analysis_context(
                        analysis_context,
                        event,
                    )
                    analysis_context["direct_address"] = detect_direct_address(event.text)

                    analysis_item: AnalysisItem = await analyzer.analyze_async(
                        response_text=event.text,
                        context=analysis_context,
                        speaker_id=event.speaker_id,
                    )
                    analysis_item = variant_plugin.transform_analysis_item(analysis_item)
                    session_manager.apply_extraction(analysis_item.extraction)
                    latest_response_id = None
                    if analysis_item.raw_model_output:
                        latest_response_id = analysis_item.raw_model_output.get("latest_response_id")
                    session_manager.mark_agent_progress(event.event_id, latest_response_id)

                    # Persist — session snapshot, extraction, tool calls,
                    # dossier upserts. Writes are sync but small; done on
                    # the agent thread so we don't block the event loop
                    # tail with FastAPI handlers.
                    if session_manager.session is not None:
                        sid = session_manager.session.session_id
                        try:
                            store.upsert_session(session_manager.session)
                            store.append_extraction(sid, analysis_item)
                        except Exception as persist_exc:  # noqa: BLE001
                            logger.warning(
                                "Persistence write failed for extraction: %s",
                                persist_exc,
                            )

                        # Fan out to SSE subscribers.
                        if analysis_item.extraction is not None:
                            await event_bus.publish(
                                "extraction",
                                analysis_item.extraction,
                                session_id=sid,
                            )
                            for bucket, key in (
                                (analysis_item.extraction.decisions, "decision"),
                                (analysis_item.extraction.open_questions, "open_question"),
                                (analysis_item.extraction.action_items, "action_item"),
                                (analysis_item.extraction.risks, "risk"),
                            ):
                                for item_obj in bucket:
                                    await event_bus.publish(
                                        "dossier_upsert",
                                        {"kind": key, "item": item_obj.model_dump()},
                                        session_id=sid,
                                    )
                        for tc in analysis_item.tool_calls or []:
                            await event_bus.publish(
                                "tool_call", tc, session_id=sid
                            )
                        await event_bus.publish(
                            "session_state",
                            {
                                "session_id": sid,
                                "running_summary": session_manager.session.running_summary,
                                "topics": list(session_manager.session.topics),
                                "alfred_muted": session_manager.session.alfred_muted,
                            },
                            session_id=sid,
                        )

                    stats["agent_analyses"] += 1
                    response_counter += 1

                    if session_manager.session:
                        output_writer.append_item(
                            session_manager.session.session_id,
                            analysis_item,
                            checklist_state=checklist_manager.snapshot(),
                        )
                        payload = {
                            "event_type": "analysis",
                            "product_id": PRODUCT_SPEC.product_id,
                            "instance_id": RUNTIME_CONFIG.instance_id,
                            "session_id": session_manager.session.session_id,
                            "analysis_item": analysis_item.model_dump(),
                            "checklist": checklist_manager.snapshot(),
                            "session_context": session_manager.get_session_context(),
                        }
                        await dispatch_route_payload(
                            route_orchestrator=route_orchestrator,
                            stats=stats,
                            payload=payload,
                        )

                    tool_call_count = len(analysis_item.tool_calls or [])
                    logger.info(
                        "Analysis #%d complete: extraction_ok=%s tool_calls=%d",
                        response_counter,
                        analysis_item.extraction is not None,
                        tool_call_count,
                    )

                except Exception as e:
                    logger.error("Alfred analysis failed: %s", e, exc_info=True)
                    if publisher:
                        await publisher.publish_error(f"Alfred analysis failed: {e!s}")
            else:
                if not AGENT_AVAILABLE:
                    logger.debug("Skipping analysis - agent not available")
                elif not session_manager.is_active:
                    logger.debug("Skipping analysis - no active session")

        except asyncio.CancelledError:
            logger.info("Agent processing loop cancelled")
            break
        except Exception as e:
            logger.error("Error in agent processing loop: %s", e, exc_info=True)
            await asyncio.sleep(1)


# =============================================================================
# Exception Handlers
# =============================================================================


async def transcript_service_error_handler(
    request: Request, exc: TranscriptServiceError
) -> JSONResponse:
    """
    Handle TranscriptServiceError exceptions.

    Args:
        request: The incoming request.
        exc: The exception that was raised.

    Returns:
        JSONResponse with error details.
    """
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            ok=False,
            error=exc.message,
            error_code=exc.error_code,
        ).model_dump(),
    )


async def generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle unexpected exceptions.

    Args:
        request: The incoming request.
        exc: The exception that was raised.

    Returns:
        JSONResponse with generic error message.
    """
    logger.error("Unhandled exception: %s", exc, exc_info=True)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            ok=False,
            error="Internal server error",
            error_code="INTERNAL_ERROR",
        ).model_dump(),
    )


# =============================================================================
# FastAPI App Lifespan
# =============================================================================


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """
    Manage application lifespan with type-safe state.

    Initializes all shared resources on startup and cleans up on shutdown.

    Args:
        app: The FastAPI application instance.

    Yields:
        Dictionary of application state to be attached to requests.
    """
    logger.info("Starting Batcave Transcript Service v2")
    logger.info(
        "Runtime: platform=%s product=%s variant=%s instance=%s host=%s port=%d",
        PLATFORM_NAME,
        PRODUCT_SPEC.product_id,
        VARIANT.variant_id,
        RUNTIME_CONFIG.instance_id,
        RUNTIME_CONFIG.sink_host,
        RUNTIME_CONFIG.sink_port,
    )
    logger.info("Product spec path: %s", PRODUCT_SPEC_PATH)

    # Initialize output directory, analysis writer, and SQLite store
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_writer = AnalysisOutputWriter(OUTPUT_DIR)
    logger.info("Analysis output directory: %s", OUTPUT_DIR)
    store = build_store(RUNTIME_CONFIG.store_db_path)
    logger.info("Session store: %s", RUNTIME_CONFIG.store_db_path)
    event_bus = AlfredEventBus()
    logger.info("Event bus initialized")

    # Initialize state components
    session_registry = SessionRegistry()
    # Legacy singleton manager — registered under DEFAULT_THREAD_ID so the
    # existing /session/* routes (used by tests + tooling) keep returning the
    # same manager instance they always have.
    session_manager = session_registry.get_or_create(SessionRegistry.DEFAULT_THREAD_ID)
    checklist_manager = build_checklist_manager()
    transcript_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()
    agent_queue: asyncio.Queue[AgentQueueItem] = asyncio.Queue()
    last_enqueued_event_ids: dict[str, str] = {}
    stats = get_initial_stats()
    route_orchestrator = ROUTES
    logger.info("Enabled output routes: %d", route_orchestrator.route_count)

    checklist_agent: ChecklistAgent | None = None
    if AgentTool.CHECKLIST_AGENT in PRODUCT_SPEC.agent.tools:
        if not AGENT_AVAILABLE or ChecklistAgent is None:
            raise RuntimeError(
                "agent.tools includes 'checklist_agent' but checklist agent is unavailable."
            )

        def _checklist_callback(item: str, status: str, reason: str) -> None:
            updated = checklist_manager.update(
                item=item,
                status=status,
                reason=reason,
                source="tool",
            )
            if updated:
                stats["checklist_updates"] += 1

        checklist_agent = ChecklistAgent(checklist_callback=_checklist_callback)

    # Start agent processing loop
    agent_task = asyncio.create_task(
        agent_processing_loop(
            agent_queue,
            session_registry,
            output_writer,
            store,
            event_bus,
            stats,
            VARIANT,
            checklist_manager,
            route_orchestrator,
        )
    )

    participant_resolver = ParticipantResolver(store)

    # Yield state to be attached to app.state
    state = {
        "session_manager": session_manager,
        "session_registry": session_registry,
        "output_writer": output_writer,
        "store": store,
        "event_bus": event_bus,
        "transcript_queue": transcript_queue,
        "agent_queue": agent_queue,
        "stats": stats,
        "agent_task": agent_task,
        "variant_plugin": VARIANT,
        "checklist_manager": checklist_manager,
        "route_orchestrator": route_orchestrator,
        "checklist_agent": checklist_agent,
        "last_enqueued_event_ids": last_enqueued_event_ids,
        "participant_resolver": participant_resolver,
    }

    yield state

    # Shutdown
    logger.info("Shutting down...")
    await event_bus.close()
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass


# =============================================================================
# FastAPI Application
# =============================================================================


app = FastAPI(
    title=f"Batcave Transcript Service ({PRODUCT_SPEC.product_id})",
    version="2.0.0",
    description="Receives diarized transcripts and integrates with Batcave modality analysis pipelines",
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    max_age=3600,
)

# Register exception handlers
app.add_exception_handler(TranscriptServiceError, transcript_service_error_handler)
app.add_exception_handler(Exception, generic_exception_handler)


# =============================================================================
# Endpoints
# =============================================================================


@app.post("/transcript", response_model=TranscriptResponse)
async def receive_transcript(
    request: TranscriptEventRequest,
    state: AppStateDep,
) -> TranscriptResponse:
    """
    Receive transcript events from C# bot.

    Supports both v1 and v2 formats:

    v1 format (legacy):
        {
            "Kind": "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled",
            "Text": "transcript text",
            "TsUtc": "2026-01-28T20:33:12.3456789Z"
        }

    v2 format (diarized):
        {
            "event_type": "partial" | "final" | "session_started" | "session_stopped" | "error",
            "text": "transcript text",
            "timestamp_utc": "2026-01-28T20:33:12.3456789Z",
            "speaker_id": "speaker_0",
            "audio_start_ms": 1234.5,
            "audio_end_ms": 5678.9,
            "confidence": 0.95
        }

    Args:
        request: The transcript event request (validated by Pydantic).
        state: Application state from dependency injection.

    Returns:
        TranscriptResponse confirming receipt.
    """
    stats = state["stats"]
    session_registry = state["session_registry"]
    output_writer = state["output_writer"]
    store = state["store"]
    event_bus = state["event_bus"]
    transcript_queue = state["transcript_queue"]
    variant_plugin = state["variant_plugin"]
    checklist_manager = state["checklist_manager"]
    route_orchestrator = state["route_orchestrator"]
    checklist_agent = state["checklist_agent"]

    # Normalize v1 to v2 format
    normalized = normalize_v1_to_v2(request, stats)

    # Parse into TranscriptEvent model
    event = TranscriptEvent(**normalized)

    # Resolve the per-meeting session manager by chat_thread_id. Auto-start
    # the meeting session on first inbound final transcript so the bot does
    # not need an explicit /session/start round-trip. Partial transcripts and
    # session_started events do not auto-create.
    auto_start = event.event_type == "final" and bool((event.text or "").strip())
    session_manager, chat_thread_id = resolve_manager_for_inbound(
        session_registry,
        event.chat_thread_id,
        auto_start=auto_start,
    )

    # Capture an immutable raw-audit row BEFORE any filter (partial drop,
    # session-active drop, normalization mutation). Promoted ledger rows
    # back-link via MeetingEvent.source_raw_event_ids.
    resolved_session_id = (
        session_manager.session.session_id if session_manager.session else None
    )
    transcript_team_id, transcript_channel_id, transcript_channel_thread_id = (
        resolve_channel_context(
            store,
            chat_thread_id,
            team_id=request.team_id,
            channel_id=request.channel_id,
            channel_thread_id=request.channel_thread_id,
        )
    )
    raw_event_id = record_raw(
        store,
        source="stt",
        event_type=event.event_type or "unknown",
        payload=request.model_dump(),
        session_id=resolved_session_id,
        provider_timestamp_utc=event.timestamp_utc,
        speaker_or_sender_id=event.speaker_id,
        normalized_payload=normalized,
        team_id=transcript_team_id,
        channel_id=transcript_channel_id,
        channel_thread_id=transcript_channel_thread_id,
    )

    # Update stats
    stats["events_received"] += 1

    if event.event_type == "partial":
        stats["partial_transcripts"] += 1
        logger.debug("[PARTIAL] [%s] %s", event.speaker_id, event.text)
        _patch_raw_promotion(
            store,
            raw_event_id,
            session_id=resolved_session_id,
            dropped_reason="partial_transcript",
        )

    elif event.event_type == "final":
        stats["final_transcripts"] += 1
        logger.info("[FINAL] [%s] %s", event.speaker_id, event.text)

        # Save to file (async)
        await save_transcript_to_file(event)

        # Add to session if active
        if session_manager.is_active:
            session_manager.add_transcript(event, raw_event_ids=[raw_event_id])

            # E3: resolve speech to a Teams participant. Pull MSI hints from
            # the wire payload (top-level fields or metadata block).
            resolver = state["participant_resolver"]
            dominant_msi, active_msis = _extract_msi_hints(request)
            sid_for_resolve = (
                session_manager.session.session_id if session_manager.session else None
            )
            if sid_for_resolve and event.speaker_id:
                link = resolver.resolve_speech(
                    sid_for_resolve,
                    event.speaker_id,
                    dominant_msi,
                    active_msis,
                )
                latest_for_patch = session_manager.get_latest_meeting_event()
                if (
                    latest_for_patch is not None
                    and latest_for_patch.speaker_id == event.speaker_id
                    and link.aad_object_id
                ):
                    latest_for_patch.aad_object_id = link.aad_object_id
                    latest_for_patch.display_name = link.display_name
                    latest_for_patch.participant_id = link.aad_object_id
                    if link.last_dominant_msi is not None:
                        latest_for_patch.media_source_id = str(link.last_dominant_msi)

            speaker_role = (
                session_manager.get_speaker_role(event.speaker_id) if event.speaker_id else None
            ) or "unknown"

            if event.text and checklist_manager.apply_alfred_heuristic(
                text=event.text,
                speaker_role=speaker_role,
            ):
                stats["checklist_updates"] += 1

            if checklist_agent and event.text:
                checklist_result = await checklist_agent.analyze_for_checklist(
                    speaker_id=event.speaker_id or "unknown",
                    speaker_role=speaker_role,
                    text=event.text,
                    conversation_history=session_manager.get_session_context().get(
                        "recent_conversation", []
                    ),
                )
                if checklist_result:
                    updated = checklist_manager.update(
                        item=str(checklist_result.get("item", "")),
                        status=str(checklist_result.get("status", "")),
                        reason=str(checklist_result.get("reason", "")),
                        source="tool",
                    )
                    if updated:
                        stats["checklist_updates"] += 1

            if session_manager.session:
                current_analysis = output_writer.load_analysis(
                    session_manager.session.session_id
                )
                if current_analysis is not None:
                    current_analysis.running_summary = session_manager.session.running_summary
                    current_analysis.topics = list(session_manager.session.topics)
                    current_analysis.notes = list(session_manager.session.notes)
                    current_analysis.decisions = list(session_manager.session.decisions)
                    current_analysis.open_questions = list(session_manager.session.open_questions)
                    current_analysis.action_items = list(session_manager.session.action_items)
                    current_analysis.risks = list(session_manager.session.risks)
                    current_analysis.checklist_state = checklist_manager.snapshot()
                    output_writer.write_analysis(
                        session_manager.session.session_id, current_analysis
                    )

                await dispatch_route_payload(
                    route_orchestrator=route_orchestrator,
                    stats=stats,
                    payload={
                        "event_type": "transcript",
                        "product_id": PRODUCT_SPEC.product_id,
                        "instance_id": RUNTIME_CONFIG.instance_id,
                        "session_id": session_manager.session.session_id,
                        "transcript_event": event.model_dump(),
                        "checklist": checklist_manager.snapshot(),
                        "session_context": session_manager.get_session_context(),
                    },
                )

            maybe_auto_map_candidate(session_manager)
            latest_event = session_manager.get_latest_meeting_event()
            if session_manager.session is not None and latest_event is not None:
                sid = session_manager.session.session_id
                if transcript_team_id and not latest_event.team_id:
                    latest_event.team_id = transcript_team_id
                if transcript_channel_id and not latest_event.channel_id:
                    latest_event.channel_id = transcript_channel_id
                if transcript_channel_thread_id and not latest_event.channel_thread_id:
                    latest_event.channel_thread_id = transcript_channel_thread_id
                try:
                    store.upsert_session(session_manager.session)
                    store.append_meeting_event(sid, latest_event)
                except Exception as persist_exc:  # noqa: BLE001
                    logger.warning("Persistence write failed for transcript event: %s", persist_exc)
                _patch_raw_promotion(
                    store,
                    raw_event_id,
                    session_id=sid,
                    normalized_event_id=latest_event.event_id,
                )
                await event_bus.publish("ledger_append", latest_event, session_id=sid)
            await enqueue_analysis_event(state, chat_thread_id, latest_event)
        else:
            # Final transcript landed before any session existed; record the
            # drop reason so audit shows it was received-but-not-promoted.
            _patch_raw_promotion(
                store,
                raw_event_id,
                dropped_reason="session_inactive",
            )

    elif event.event_type == "session_started":
        stats["session_events"] += 1
        logger.info("Speech recognition session started")
        await save_transcript_to_file(event)

    elif event.event_type == "session_stopped":
        stats["session_events"] += 1
        logger.info("Speech recognition session stopped")
        await save_transcript_to_file(event)

    elif event.event_type == "error":
        stats["errors"] += 1
        if event.error:
            error_msg = f"{event.error.code}: {event.error.message}"
        elif event.metadata and event.metadata.raw_response:
            error_msg = str(event.metadata.raw_response)
        else:
            error_msg = "Unknown error"
        logger.error("Speech recognition error: %s", error_msg)

    await variant_plugin.on_transcript(event, session_manager.get_session_context())

    # Push to general transcript queue for other consumers
    await transcript_queue.put(event)

    return TranscriptResponse(
        ok=True,
        received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


@app.post("/chat", response_model=TranscriptResponse)
async def receive_chat_message(
    request: ChatMessageRequest,
    state: AppStateDep,
) -> TranscriptResponse:
    """
    Receive a meeting chat message from the C# bot.

    Every meeting chat message (human or bot-echoed) flows through here
    and becomes a first-class event in the unified meeting timeline.
    """
    stats = state["stats"]
    session_registry = state["session_registry"]
    variant_plugin = state["variant_plugin"]
    route_orchestrator = state["route_orchestrator"]
    store = state["store"]
    event_bus = state["event_bus"]

    stats["events_received"] += 1

    chat = ChatMessage(
        event_type=request.event_type,  # type: ignore[arg-type]
        chat_thread_id=request.chat_thread_id,
        message_id=request.message_id,
        text=request.text,
        html=request.html,
        sender_id=request.sender_id,
        sender_display_name=request.sender_display_name,
        timestamp_utc=request.timestamp_utc,
        conversation_reference_id=request.conversation_reference_id,
        attachments=request.attachments,
        mentions=request.mentions,
        reply_to_message_id=request.reply_to_message_id,
        from_bot=request.from_bot,
        raw=request.raw,
        conversation_kind=request.conversation_kind,
        team_id=request.team_id,
        channel_id=request.channel_id,
        channel_thread_id=request.channel_thread_id,
    )

    # Auto-start the meeting session on first non-deleted chat for an unseen
    # thread so the UI gets a session immediately, without needing an explicit
    # /session/start call.
    auto_start = (
        chat.event_type != "chat_deleted"
        and bool((chat.text or "").strip())
    )
    session_manager, chat_thread_id = resolve_manager_for_inbound(
        session_registry,
        chat.chat_thread_id,
        auto_start=auto_start,
    )

    # Raw audit BEFORE the is_active filter (so pre-session chats are still
    # captured) and BEFORE echo-suppression (so bot echoes are still audited).
    raw_source = "graph_notification" if chat.raw is not None else "bot_framework"
    resolved_session_id = (
        session_manager.session.session_id if session_manager.session else None
    )
    chat_team_id, chat_channel_id, chat_channel_thread_id = resolve_channel_context(
        store,
        chat_thread_id,
        team_id=request.team_id,
        channel_id=request.channel_id,
        channel_thread_id=request.channel_thread_id,
    )
    raw_event_id = record_raw(
        store,
        source=raw_source,
        event_type=chat.event_type,
        payload=request.model_dump(),
        session_id=resolved_session_id,
        provider_timestamp_utc=chat.timestamp_utc,
        speaker_or_sender_id=chat.sender_id,
        team_id=chat_team_id,
        channel_id=chat_channel_id,
        channel_thread_id=chat_channel_thread_id,
    )

    if session_manager.is_active:
        promoted_event = session_manager.add_chat_message(
            chat, raw_event_ids=[raw_event_id]
        )
        is_echo = session_manager.is_expected_bot_echo(chat)
        should_analyze = not is_echo
        if is_echo:
            _patch_raw_promotion(
                store,
                raw_event_id,
                session_id=resolved_session_id,
                dropped_reason="echo_suppressed",
            )

        # E3: resolve chat sender by AAD when possible (Graph notification
        # path carries a real AAD; bot-framework path may not).
        if promoted_event is not None and session_manager.session is not None:
            resolver = state["participant_resolver"]
            resolved = resolver.resolve_chat_sender(
                session_manager.session.session_id, chat
            )
            if resolved is not None:
                promoted_event.aad_object_id = resolved["aad_object_id"]
                promoted_event.participant_id = resolved["aad_object_id"]
                if resolved.get("display_name"):
                    promoted_event.display_name = resolved["display_name"]

        if session_manager.session:
            await dispatch_route_payload(
                route_orchestrator=route_orchestrator,
                stats=stats,
                payload={
                    "event_type": "chat",
                    "product_id": PRODUCT_SPEC.product_id,
                    "instance_id": RUNTIME_CONFIG.instance_id,
                    "session_id": session_manager.session.session_id,
                    "chat_message": chat.model_dump(),
                    "session_context": session_manager.get_session_context(),
                },
            )

        logger.info(
            "[CHAT] %s from %s (%s): %s",
            chat.event_type,
            chat.sender_display_name or chat.sender_id or "unknown",
            "bot" if chat.from_bot else "human",
            (chat.text or "")[:120],
        )

        latest_event = session_manager.get_latest_meeting_event()
        if session_manager.session is not None and latest_event is not None:
            sid = session_manager.session.session_id
            if chat_team_id and not latest_event.team_id:
                latest_event.team_id = chat_team_id
            if chat_channel_id and not latest_event.channel_id:
                latest_event.channel_id = chat_channel_id
            if chat_channel_thread_id and not latest_event.channel_thread_id:
                latest_event.channel_thread_id = chat_channel_thread_id
            try:
                store.upsert_session(session_manager.session)
                store.append_meeting_event(sid, latest_event)
            except Exception as persist_exc:  # noqa: BLE001
                logger.warning("Persistence write failed for chat event: %s", persist_exc)
            if promoted_event is latest_event or (
                promoted_event is not None
                and promoted_event.event_id == latest_event.event_id
            ):
                _patch_raw_promotion(
                    store,
                    raw_event_id,
                    session_id=sid,
                    normalized_event_id=latest_event.event_id,
                )
            await event_bus.publish("ledger_append", latest_event, session_id=sid)
        elif promoted_event is None and chat.event_type != "chat_deleted":
            # Duplicate message_id — already in the ledger from a prior call.
            _patch_raw_promotion(
                store,
                raw_event_id,
                session_id=resolved_session_id,
                dropped_reason="duplicate_message_id",
            )

        if should_analyze:
            await enqueue_analysis_event(state, chat_thread_id, latest_event)
    else:
        # No active session — record the drop reason on the raw row so audit
        # shows the inbound chat was received but not promoted.
        _patch_raw_promotion(
            store,
            raw_event_id,
            dropped_reason="session_inactive",
        )

    await variant_plugin.on_chat_message(chat, session_manager.get_session_context())

    return TranscriptResponse(
        ok=True,
        received_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    )


@app.post("/session/start", response_model=SessionStartResponse)
async def start_session(
    request: SessionStartRequest,
    state: AppStateDep,
) -> SessionStartResponse:
    """
    Start a new interview session.

    Args:
        request: Session start parameters.
        state: Application state from dependency injection.

    Returns:
        SessionStartResponse with session details.

    Raises:
        SessionAlreadyActiveError: If a session is already active.
    """
    session_manager = state["session_manager"]
    output_writer = state["output_writer"]
    checklist_manager = state["checklist_manager"]
    route_orchestrator = state["route_orchestrator"]
    stats = state["stats"]
    variant_plugin = state["variant_plugin"]
    store = state["store"]
    event_bus = state["event_bus"]

    if session_manager.is_active:
        raise SessionAlreadyActiveError()

    if request.product_id and request.product_id != PRODUCT_SPEC.product_id:
        raise TranscriptServiceError(
            message=(
                f"Requested product_id '{request.product_id}' does not match "
                f"active product '{PRODUCT_SPEC.product_id}'."
            ),
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="PRODUCT_MISMATCH",
        )

    checklist_manager.reset()

    session = session_manager.start_session(
        candidate_name=request.candidate_name,
        meeting_url=request.meeting_url,
    )
    state["last_enqueued_event_ids"].pop(session.session_id, None)

    # Map candidate speaker if provided
    if request.candidate_speaker_id:
        session_manager.map_speaker(
            request.candidate_speaker_id,
            "candidate",
            request.candidate_name,
        )

    # Initialize analysis file and session row in the store
    analysis = SessionAnalysis(
        session_id=session.session_id,
        candidate_name=request.candidate_name,
        started_at=session.started_at,
        checklist_state=checklist_manager.snapshot(),
    )
    output_writer.write_analysis(session.session_id, analysis)
    try:
        store.upsert_session(session)
    except Exception as persist_exc:  # noqa: BLE001
        logger.warning("Persistence write failed on session start: %s", persist_exc)
    await event_bus.publish(
        "session_started",
        {
            "session_id": session.session_id,
            "candidate_name": session.candidate_name,
            "meeting_url": session.meeting_url,
            "started_at": session.started_at,
        },
        session_id=session.session_id,
    )

    # Publish session start to real-time stream
    if AGENT_AVAILABLE and get_publisher:
        publisher = get_publisher()
        await publisher.publish_system(
            f"Interview session started for {request.candidate_name}"
        )

    logger.info(
        "Interview session started for: %s (%s)",
        request.candidate_name,
        session.session_id,
    )
    await variant_plugin.on_session_start(session_manager.get_session_context())

    await dispatch_route_payload(
        route_orchestrator=route_orchestrator,
        stats=stats,
        payload={
            "event_type": "session_started",
            "product_id": PRODUCT_SPEC.product_id,
            "instance_id": RUNTIME_CONFIG.instance_id,
            "session_id": session.session_id,
            "session_context": session_manager.get_session_context(),
            "checklist": checklist_manager.snapshot(),
        },
    )

    return SessionStartResponse(
        ok=True,
        message=f"Session started for {request.candidate_name}",
        session_id=session.session_id,
        started_at=session.started_at,
    )


@app.post("/session/map-speaker", response_model=SpeakerMapResponse)
async def map_speaker(
    request: SpeakerMapRequest,
    state: AppStateDep,
) -> SpeakerMapResponse:
    """
    Map a speaker ID to a role.

    Args:
        request: Speaker mapping parameters.
        state: Application state from dependency injection.

    Returns:
        SpeakerMapResponse with updated mappings.

    Raises:
        SessionNotActiveError: If no session is active.
    """
    session_manager = state["session_manager"]

    if not session_manager.is_active:
        raise SessionNotActiveError()

    session_manager.map_speaker(request.speaker_id, request.role.value)

    return SpeakerMapResponse(
        ok=True,
        message=f"Mapped {request.speaker_id} to {request.role.value}",
        speaker_mappings=session_manager.get_session_context()["speaker_mappings"],
    )


@app.get("/session/status", response_model=SessionStatusWrapper)
async def get_session_status(state: AppStateDep) -> SessionStatusWrapper:
    """
    Get current session information.

    Args:
        state: Application state from dependency injection.

    Returns:
        SessionStatusWrapper with session details and agent availability.
    """
    session_manager = state["session_manager"]
    checklist_manager = state["checklist_manager"]
    stats = state["stats"]

    context = session_manager.get_session_context()

    session_status = SessionStatusResponse(
        active=context["session_active"],
        session_id=context.get("session_id"),
        candidate_name=context.get("candidate_name"),
        meeting_url=context.get("meeting_url"),
        started_at=context.get("started_at"),
        speaker_mappings=context.get("speaker_mappings", {}),
        recent_conversation=context.get("recent_conversation", []),
        meeting_history=context.get("meeting_history", []),
        chat_messages_count=context.get("chat_messages_count", 0),
        conversation_reference_id=context.get("conversation_reference_id"),
        graph_chat_thread_id=context.get("graph_chat_thread_id"),
        prompt_cache_key=context.get("prompt_cache_key"),
        latest_response_id=context.get("latest_response_id"),
        running_summary=context.get("running_summary", ""),
        topics=context.get("topics", []),
        notes=context.get("notes", []),
        decisions=context.get("decisions", []),
        open_questions=context.get("open_questions", []),
        action_items=context.get("action_items", []),
        risks=context.get("risks", []),
        total_events=context.get("total_events", 0),
        final_events=context.get("final_events", 0),
        analysis_count=stats["agent_analyses"],
        checklist=checklist_manager.snapshot(),
        product_id=PRODUCT_SPEC.product_id,
    )

    return SessionStatusWrapper(
        session=session_status,
        agent_available=AGENT_AVAILABLE,
        product_id=PRODUCT_SPEC.product_id,
    )


@app.get("/session/analysis", response_model=SessionAnalysisResponse)
async def get_session_analysis(
    state: AppStateDep,
    session_id: str | None = None,
) -> SessionAnalysisResponse:
    """Return the current persisted analysis for the active or requested session."""
    session_manager = state["session_manager"]
    output_writer = state["output_writer"]

    resolved_session_id = session_id
    if resolved_session_id is None and session_manager.session is not None:
        resolved_session_id = session_manager.session.session_id

    if resolved_session_id is None:
        return SessionAnalysisResponse(
            ok=True,
            message="No session selected",
            session_id=None,
            analysis=None,
        )

    try:
        analysis = output_writer.load_analysis(resolved_session_id)
    except Exception as exc:
        logger.error("Failed to load analysis for %s: %s", resolved_session_id, exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to load session analysis",
        ) from exc

    return SessionAnalysisResponse(
        ok=True,
        message="Analysis loaded" if analysis is not None else "No analysis yet",
        session_id=resolved_session_id,
        analysis=analysis,
    )


@app.get("/session/events")
async def stream_session_events(
    state: AppStateDep,
    request: Request,
    session_id: str | None = None,
) -> StreamingResponse:
    """Server-Sent Events stream for live UI updates.

    Emits: ``ledger_append`` (MeetingEvent), ``extraction`` (AlfredExtraction),
    ``dossier_upsert`` ({kind, item}), ``tool_call`` (ToolCallRecord),
    ``session_state`` (summary/topics/muted), ``session_started``,
    ``session_ended``, and periodic ``heartbeat`` comments to defeat proxy
    idle timeouts.
    """
    event_bus = state["event_bus"]

    async def generator() -> AsyncIterator[bytes]:
        heartbeat_seconds = 15.0
        # Initial comment so the connection is established and proxies flush.
        yield b": alfred-sse-stream\n\n"

        subscription = event_bus.subscribe(session_filter=session_id)
        iterator = subscription.__aiter__()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        iterator.__anext__(), timeout=heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                except StopAsyncIteration:
                    break
                yield format_sse(event)
        finally:
            aclose = getattr(subscription, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/session", response_model=SessionStatusWrapper)
async def get_session(state: AppStateDep) -> SessionStatusWrapper:
    """
    Get current session info (alias for /session/status).

    Args:
        state: Application state from dependency injection.

    Returns:
        SessionStatusWrapper with session details.
    """
    return await get_session_status(state)


@app.post("/session/end", response_model=SessionEndResponse)
async def end_session(state: AppStateDep) -> SessionEndResponse:
    """
    End the current session and finalize analysis.

    Args:
        state: Application state from dependency injection.

    Returns:
        SessionEndResponse with session summary.

    Raises:
        SessionNotActiveError: If no session is active.
    """
    session_manager = state["session_manager"]
    output_writer = state["output_writer"]
    checklist_manager = state["checklist_manager"]
    route_orchestrator = state["route_orchestrator"]
    stats = state["stats"]
    variant_plugin = state["variant_plugin"]
    store = state["store"]
    event_bus = state["event_bus"]

    if not session_manager.is_active:
        raise SessionNotActiveError("No active session to end.")

    session = session_manager.session
    if not session:
        raise SessionNotActiveError("No session data available.")

    session_id = session.session_id
    candidate_name = session.candidate_name
    started_at = session.started_at

    # End the session
    ended_session = session_manager.end_session()

    # Finalize analysis file
    if ended_session:
        try:
            analysis = output_writer.load_analysis(session_id)
            if analysis:
                analysis.ended_at = ended_session.ended_at
                analysis.checklist_state = checklist_manager.snapshot()
                analysis.running_summary = ended_session.running_summary
                analysis.topics = list(ended_session.topics)
                analysis.notes = list(ended_session.notes)
                analysis.decisions = list(ended_session.decisions)
                analysis.open_questions = list(ended_session.open_questions)
                analysis.action_items = list(ended_session.action_items)
                analysis.risks = list(ended_session.risks)
                analysis.compute_overall_scores()
                output_writer.write_analysis(session_id, analysis)
            try:
                store.upsert_session(ended_session)
            except Exception as persist_exc:  # noqa: BLE001
                logger.warning("Persistence write failed on session end: %s", persist_exc)
        except Exception as e:
            logger.error("Failed to finalize analysis: %s", e)

    summary = {
        "session_id": session_id,
        "candidate_name": candidate_name,
        "started_at": started_at,
        "ended_at": (
            ended_session.ended_at
            if ended_session
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "total_events": len(ended_session.meeting_events) if ended_session else 0,
        "analyses_generated": stats["agent_analyses"],
    }

    # Publish session end to real-time stream
    if AGENT_AVAILABLE and get_publisher:
        publisher = get_publisher()
        await publisher.publish_system(
            f"Interview session ended for {candidate_name}. "
            f"Analyzed {stats['agent_analyses']} responses."
        )

    logger.info("Session ended: %s", session_id)
    await variant_plugin.on_session_end(summary)
    state["last_enqueued_event_ids"].pop(session_id, None)

    await event_bus.publish("session_ended", summary, session_id=session_id)

    await dispatch_route_payload(
        route_orchestrator=route_orchestrator,
        stats=stats,
        payload={
            "event_type": "session_ended",
            "product_id": PRODUCT_SPEC.product_id,
            "instance_id": RUNTIME_CONFIG.instance_id,
            "session_id": session_id,
            "summary": summary,
            "checklist": checklist_manager.snapshot(),
        },
    )

    return SessionEndResponse(
        ok=True,
        message="Session ended",
        summary=summary,
    )


@app.get("/health", response_model=HealthResponse)
async def health(state: AppStateDep) -> HealthResponse:
    """
    Health check endpoint.

    Args:
        state: Application state from dependency injection.

    Returns:
        HealthResponse with service status.
    """
    session_manager = state["session_manager"]

    return HealthResponse(
        status="healthy",
        service="Batcave Transcript Service",
        version="2.0.0",
        timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        agent_available=AGENT_AVAILABLE,
        session_active=session_manager.is_active,
        variant_id=VARIANT.variant_id,
        product_id=PRODUCT_SPEC.product_id,
        instance_id=RUNTIME_CONFIG.instance_id,
    )


@app.get("/stats", response_model=StatsResponse)
async def get_stats(state: AppStateDep) -> StatsResponse:
    """
    Get current statistics.

    Args:
        state: Application state from dependency injection.

    Returns:
        StatsResponse with service statistics.
    """
    session_manager = state["session_manager"]
    stats = state["stats"]
    transcript_queue = state["transcript_queue"]
    agent_queue = state["agent_queue"]

    context = session_manager.get_session_context()

    return StatsResponse(
        stats=dict(stats),
        transcript_queue_size=transcript_queue.qsize(),
        agent_queue_size=agent_queue.qsize(),
        session={
            "active": context["session_active"],
            "session_id": context.get("session_id"),
            "candidate_name": context.get("candidate_name"),
            "total_events": context.get("total_events", 0),
            "final_events": context.get("final_events", 0),
        },
        agent_available=AGENT_AVAILABLE,
        output_directory=str(OUTPUT_DIR),
        variant_id=VARIANT.variant_id,
        product_id=PRODUCT_SPEC.product_id,
        instance_id=RUNTIME_CONFIG.instance_id,
    )


@app.get("/product/spec", response_model=ProductSpecResponse)
async def get_product_spec() -> ProductSpecResponse:
    """Return active product spec summary for UI/client contract discovery."""
    return ProductSpecResponse(
        platform=PLATFORM_NAME,
        product_id=PRODUCT_SPEC.product_id,
        display_name=PRODUCT_SPEC.display_name,
        spec_path=str(PRODUCT_SPEC_PATH),
        checklist_items=[
            {
                "id": item.id,
                "label": item.label,
            }
            for item in PRODUCT_SPEC.checklist.items
        ],
        agent={
            "model": PRODUCT_SPEC.agent.model,
            "reasoning_effort": PRODUCT_SPEC.agent.reasoning_effort,
            "has_custom_prompt": bool(PRODUCT_SPEC.agent.prompt_template),
            "tools": [tool.value for tool in PRODUCT_SPEC.agent.tools],
        },
        outputs=[
            {
                "id": route.id,
                "type": route.type.value,
                "enabled": route.enabled,
            }
            for route in PRODUCT_SPEC.outputs.routes
        ],
    )


# =============================================================================
# History Endpoints (SQLite-backed)
# =============================================================================


class SessionSummaryRow(BaseModel):
    session_id: str
    candidate_name: str | None = None
    meeting_url: str | None = None
    started_at: str
    ended_at: str | None = None
    running_summary: str = ""


class SessionListResponse(BaseModel):
    sessions: list[SessionSummaryRow]


class LedgerResponse(BaseModel):
    session_id: str
    events: list[dict[str, Any]]


class DossierResponse(BaseModel):
    session_id: str
    decisions: list[dict[str, Any]]
    open_questions: list[dict[str, Any]]
    action_items: list[dict[str, Any]]
    risks: list[dict[str, Any]]


class ExtractionsResponse(BaseModel):
    session_id: str
    extractions: list[dict[str, Any]]


class ToolCallsResponse(BaseModel):
    session_id: str
    tool_calls: list[dict[str, Any]]


@app.get("/sessions", response_model=SessionListResponse)
async def list_sessions(state: AppStateDep, limit: int = 100) -> SessionListResponse:
    """List recent persisted sessions (most recent first)."""
    rows = await asyncio.to_thread(state["store"].list_sessions, limit)
    return SessionListResponse(
        sessions=[SessionSummaryRow(**row) for row in rows],
    )


# =============================================================================
# Per-meeting routes (URL-keyed by chat_thread_id)
# =============================================================================
#
# The UI requires a chat_thread_id in its URL (`/m/<chat_thread_id>`) so
# anyone hitting the dossier only ever sees the single meeting they were
# in. There is intentionally no other auth on these routes — the
# chat_thread_id IS the access boundary. All audio + chat events are
# tagged with chat_thread_id at ingestion (via TranscriptEvent /
# ChatMessage) and routed to the matching session manager so the data is
# tracked by meeting.


class MeetingListEntry(BaseModel):
    """Public summary of one meeting in the registry."""

    chat_thread_id: str
    session_id: str | None = None
    candidate_name: str | None = None
    meeting_url: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    active: bool = False
    total_events: int = 0


class MeetingListResponse(BaseModel):
    meetings: list[MeetingListEntry]


def _build_meeting_entry(thread_id: str, manager: InterviewSessionManager) -> MeetingListEntry:
    session = manager.session
    return MeetingListEntry(
        chat_thread_id=thread_id,
        session_id=session.session_id if session else None,
        candidate_name=session.candidate_name if session else None,
        meeting_url=session.meeting_url if session else None,
        started_at=session.started_at if session else None,
        ended_at=session.ended_at if session else None,
        active=manager.is_active,
        total_events=len(session.meeting_events) if session else 0,
    )


def _resolve_meeting_or_404(
    state: AppState,
    chat_thread_id: str,
) -> InterviewSessionManager:
    manager = state["session_registry"].get(chat_thread_id)
    if manager is None:
        raise TranscriptServiceError(
            message=f"No meeting registered for chat_thread_id='{chat_thread_id}'.",
            status_code=status.HTTP_404_NOT_FOUND,
            error_code="MEETING_NOT_FOUND",
        )
    return manager


@app.get("/m", response_model=MeetingListResponse)
async def list_meetings(state: AppStateDep) -> MeetingListResponse:
    """List meetings (active + recently ended) currently in the registry."""
    registry = state["session_registry"]
    entries: list[MeetingListEntry] = []
    for thread_id in registry.thread_ids:
        if thread_id == SessionRegistry.DEFAULT_THREAD_ID:
            # Hide the legacy compatibility slot from the per-meeting list —
            # it has no real chat_thread_id anyone could hit in a URL.
            continue
        manager = registry.get(thread_id)
        if manager is None:
            continue
        entries.append(_build_meeting_entry(thread_id, manager))
    # Most recently started first.
    entries.sort(key=lambda e: e.started_at or "", reverse=True)
    return MeetingListResponse(meetings=entries)


@app.get("/m/{chat_thread_id:path}/status", response_model=SessionStatusWrapper)
async def get_meeting_status(
    chat_thread_id: str,
    state: AppStateDep,
) -> SessionStatusWrapper:
    """Snapshot for the UI to seed the dossier on mount."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    checklist_manager = state["checklist_manager"]
    stats = state["stats"]
    context = manager.get_session_context()

    session_status = SessionStatusResponse(
        active=context["session_active"],
        session_id=context.get("session_id"),
        candidate_name=context.get("candidate_name"),
        meeting_url=context.get("meeting_url"),
        started_at=context.get("started_at"),
        speaker_mappings=context.get("speaker_mappings", {}),
        recent_conversation=context.get("recent_conversation", []),
        meeting_history=context.get("meeting_history", []),
        chat_messages_count=context.get("chat_messages_count", 0),
        conversation_reference_id=context.get("conversation_reference_id"),
        graph_chat_thread_id=context.get("graph_chat_thread_id") or chat_thread_id,
        prompt_cache_key=context.get("prompt_cache_key"),
        latest_response_id=context.get("latest_response_id"),
        running_summary=context.get("running_summary", ""),
        topics=context.get("topics", []),
        notes=context.get("notes", []),
        decisions=context.get("decisions", []),
        open_questions=context.get("open_questions", []),
        action_items=context.get("action_items", []),
        risks=context.get("risks", []),
        total_events=context.get("total_events", 0),
        final_events=context.get("final_events", 0),
        analysis_count=stats["agent_analyses"],
        checklist=checklist_manager.snapshot(),
        product_id=PRODUCT_SPEC.product_id,
    )

    return SessionStatusWrapper(
        session=session_status,
        agent_available=AGENT_AVAILABLE,
        product_id=PRODUCT_SPEC.product_id,
    )


@app.get("/m/{chat_thread_id:path}/events")
async def stream_meeting_events(
    chat_thread_id: str,
    state: AppStateDep,
    request: Request,
) -> StreamingResponse:
    """Server-Sent Events stream filtered to one meeting's session_id."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    event_bus = state["event_bus"]
    session_filter = (
        manager.session.session_id if manager.session is not None else None
    )

    async def generator() -> AsyncIterator[bytes]:
        heartbeat_seconds = 15.0
        yield b": alfred-sse-stream\n\n"

        subscription = event_bus.subscribe(session_filter=session_filter)
        iterator = subscription.__aiter__()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(
                        iterator.__anext__(), timeout=heartbeat_seconds
                    )
                except asyncio.TimeoutError:
                    yield b": keep-alive\n\n"
                    continue
                except StopAsyncIteration:
                    break
                yield format_sse(event)
        finally:
            aclose = getattr(subscription, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001
                    pass

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/m/{chat_thread_id:path}/ledger", response_model=LedgerResponse)
async def get_meeting_ledger(
    chat_thread_id: str,
    state: AppStateDep,
    limit: int | None = None,
) -> LedgerResponse:
    """Persisted ledger for a meeting."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    if manager.session is None:
        return LedgerResponse(session_id="", events=[])
    sid = manager.session.session_id
    events = await asyncio.to_thread(state["store"].get_ledger, sid, limit)
    return LedgerResponse(session_id=sid, events=events)


@app.get("/m/{chat_thread_id:path}/dossier", response_model=DossierResponse)
async def get_meeting_dossier(
    chat_thread_id: str,
    state: AppStateDep,
) -> DossierResponse:
    """Latest intent-alignment state for a meeting."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    if manager.session is None:
        return DossierResponse(
            session_id="",
            decisions=[],
            open_questions=[],
            action_items=[],
            risks=[],
        )
    sid = manager.session.session_id
    bundle = await asyncio.to_thread(state["store"].get_dossier, sid)
    return DossierResponse(session_id=sid, **bundle)


@app.post("/m/{chat_thread_id:path}/end", response_model=SessionEndResponse)
async def end_meeting_session(
    chat_thread_id: str,
    state: AppStateDep,
) -> SessionEndResponse:
    """End the meeting's session. Bot calls this on call termination."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    if not manager.is_active:
        raise SessionNotActiveError(
            f"No active session for chat_thread_id='{chat_thread_id}'."
        )

    output_writer = state["output_writer"]
    checklist_manager = state["checklist_manager"]
    stats = state["stats"]
    variant_plugin = state["variant_plugin"]
    store = state["store"]
    event_bus = state["event_bus"]
    route_orchestrator = state["route_orchestrator"]

    session = manager.session
    assert session is not None
    session_id = session.session_id
    candidate_name = session.candidate_name
    started_at = session.started_at

    ended_session = manager.end_session()

    if ended_session:
        try:
            analysis = output_writer.load_analysis(session_id)
            if analysis:
                analysis.ended_at = ended_session.ended_at
                analysis.checklist_state = checklist_manager.snapshot()
                analysis.running_summary = ended_session.running_summary
                analysis.topics = list(ended_session.topics)
                analysis.notes = list(ended_session.notes)
                analysis.decisions = list(ended_session.decisions)
                analysis.open_questions = list(ended_session.open_questions)
                analysis.action_items = list(ended_session.action_items)
                analysis.risks = list(ended_session.risks)
                analysis.compute_overall_scores()
                output_writer.write_analysis(session_id, analysis)
            try:
                store.upsert_session(ended_session)
            except Exception as persist_exc:  # noqa: BLE001
                logger.warning("Persistence write failed on session end: %s", persist_exc)
        except Exception as e:
            logger.error("Failed to finalize analysis: %s", e)

    summary = {
        "session_id": session_id,
        "chat_thread_id": chat_thread_id,
        "candidate_name": candidate_name,
        "started_at": started_at,
        "ended_at": (
            ended_session.ended_at
            if ended_session
            else datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        ),
        "total_events": len(ended_session.meeting_events) if ended_session else 0,
        "analyses_generated": stats["agent_analyses"],
    }

    state["last_enqueued_event_ids"].pop(session_id, None)

    await event_bus.publish("session_ended", summary, session_id=session_id)
    await variant_plugin.on_session_end(summary)
    await dispatch_route_payload(
        route_orchestrator=route_orchestrator,
        stats=stats,
        payload={
            "event_type": "session_ended",
            "product_id": PRODUCT_SPEC.product_id,
            "instance_id": RUNTIME_CONFIG.instance_id,
            "session_id": session_id,
            "chat_thread_id": chat_thread_id,
            "summary": summary,
            "checklist": checklist_manager.snapshot(),
        },
    )

    return SessionEndResponse(
        ok=True,
        message=f"Session ended for chat_thread_id='{chat_thread_id}'",
        summary=summary,
    )


@app.post("/m/{chat_thread_id:path}/mute")
async def set_meeting_mute(
    chat_thread_id: str,
    body: MuteRequest,
    state: AppStateDep,
) -> dict[str, bool]:
    """Toggle Alfred's mute state for this meeting."""
    manager = _resolve_meeting_or_404(state, chat_thread_id)
    if manager.session is None:
        raise SessionNotActiveError(f"No active session for chat_thread_id='{chat_thread_id}'.")
    manager.session.alfred_muted = body.muted
    await asyncio.to_thread(state["store"].upsert_session, manager.session)
    sid = manager.session.session_id
    await state["event_bus"].publish(
        "session_state",
        {
            "session_id": sid,
            "running_summary": manager.session.running_summary,
            "topics": list(manager.session.topics),
            "alfred_muted": manager.session.alfred_muted,
        },
        session_id=sid,
    )
    return {"alfred_muted": manager.session.alfred_muted}


class SessionChannelLinkRequest(BaseModel):
    """Body of POST /session/link.

    Lets the C# bot tell the sink "this meeting/chat thread belongs to
    this Teams channel". Stamps every future event for the thread with
    channel context AND backfills prior ``meeting_events`` /
    ``raw_ingest_events`` rows so analytics can group by ``channel_id``
    alone, regardless of whether the link was known when the event
    landed.
    """

    chat_thread_id: str = Field(..., min_length=1)
    team_id: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1)
    channel_thread_id: str | None = Field(
        default=None,
        description="Parent channel's conversation id (19:{channelId}@thread.tacv2).",
    )
    source: str | None = Field(
        default=None,
        description=(
            "Free-form tag — e.g. 'channel_meeting_announcement', "
            "'bot_framework_channeldata' — for forensic provenance."
        ),
    )

    model_config = {"extra": "ignore"}


@app.post("/session/link")
async def link_session_to_channel(
    request: SessionChannelLinkRequest,
    state: AppStateDep,
) -> dict[str, Any]:
    """Persist a session ↔ channel link and backfill prior events."""
    store: SessionStore = state["store"]
    counts = await asyncio.to_thread(
        store.link_session_to_channel,
        request.chat_thread_id,
        request.team_id,
        request.channel_id,
        request.channel_thread_id,
        request.source,
    )
    logger.info(
        "Linked session chat_thread_id=%s -> team=%s channel=%s; backfilled %s",
        request.chat_thread_id,
        request.team_id,
        request.channel_id,
        counts,
    )
    return {"ok": True, "link": request.model_dump(), "backfill": counts}


@app.get("/session/link/{chat_thread_id:path}")
async def get_session_channel_link(
    chat_thread_id: str,
    state: AppStateDep,
) -> dict[str, Any]:
    """Return the channel link (if any) for a chat thread."""
    store: SessionStore = state["store"]
    link = await asyncio.to_thread(store.get_channel_link, chat_thread_id)
    if link is None:
        return {"ok": True, "link": None}
    return {"ok": True, "link": link}


@app.get("/channels/links")
async def list_session_channel_links(state: AppStateDep) -> dict[str, Any]:
    """Return all session ↔ channel links."""
    store: SessionStore = state["store"]
    links = await asyncio.to_thread(store.list_channel_links)
    return {"ok": True, "count": len(links), "links": links}


@app.get("/c/{team_id}/{channel_id}/events")
async def get_channel_events(
    team_id: str,
    channel_id: str,
    state: AppStateDep,
    since: str | None = None,
    until: str | None = None,
    kinds: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Return every event (chat / STT / system) tagged with this channel.

    Aggregates the channel's own session AND every meeting session that
    has been linked to it via /session/link, ordered by timestamp_utc.
    Use this for offline analytics or replay across an entire channel's
    history (channel posts + every meeting + every transcript).

    Query params:
      - since: ISO 8601 timestamp lower bound (inclusive).
      - until: ISO 8601 timestamp upper bound (inclusive).
      - kinds: comma-separated list of "speech", "chat", "system".
      - limit: max rows to return.
    """
    store: SessionStore = state["store"]
    parsed_kinds = [k.strip() for k in kinds.split(",") if k.strip()] if kinds else None
    events = await asyncio.to_thread(
        store.get_channel_ledger,
        channel_id,
        team_id=team_id,
        since=since,
        until=until,
        kinds=parsed_kinds,
        limit=limit,
    )
    return {
        "ok": True,
        "team_id": team_id,
        "channel_id": channel_id,
        "count": len(events),
        "events": events,
    }


@app.get("/sessions/{session_id}/ledger", response_model=LedgerResponse)
async def get_session_ledger(
    session_id: str,
    state: AppStateDep,
    limit: int | None = None,
) -> LedgerResponse:
    """Return the persisted meeting-event ledger for a session."""
    events = await asyncio.to_thread(state["store"].get_ledger, session_id, limit)
    return LedgerResponse(session_id=session_id, events=events)


@app.get("/sessions/{session_id}/dossier", response_model=DossierResponse)
async def get_session_dossier(session_id: str, state: AppStateDep) -> DossierResponse:
    """Return Alfred's latest intent-alignment state for a session."""
    bundle = await asyncio.to_thread(state["store"].get_dossier, session_id)
    return DossierResponse(session_id=session_id, **bundle)


@app.get("/sessions/{session_id}/extractions", response_model=ExtractionsResponse)
async def get_session_extractions(
    session_id: str,
    state: AppStateDep,
    since: str | None = None,
    limit: int | None = None,
) -> ExtractionsResponse:
    """Return Alfred's per-tick extraction history for a session."""
    rows = await asyncio.to_thread(
        state["store"].get_extractions, session_id, since, limit
    )
    return ExtractionsResponse(session_id=session_id, extractions=rows)


@app.get("/sessions/{session_id}/tool-calls", response_model=ToolCallsResponse)
async def get_session_tool_calls(
    session_id: str,
    state: AppStateDep,
    limit: int | None = None,
) -> ToolCallsResponse:
    """Return the audit log of agent tool calls for a session."""
    rows = await asyncio.to_thread(state["store"].get_tool_calls, session_id, limit)
    return ToolCallsResponse(session_id=session_id, tool_calls=rows)


class ParticipantsResponse(BaseModel):
    session_id: str
    participants: list[dict[str, Any]]


class SpeakerIdentityResponse(BaseModel):
    session_id: str
    links: list[dict[str, Any]]


@app.post("/session/participants")
async def upsert_session_participants(
    request: ParticipantsUpdateRequest,
    state: AppStateDep,
) -> dict[str, Any]:
    """Receive a Teams participant roster snapshot from the C# bot (E3).

    Either ``session_id`` or ``chat_thread_id`` is required to bind the
    payload to a session. Each call is recorded raw, then upserted into
    ``meeting_participants`` + ``participant_msi_bindings``.
    """
    store = state["store"]
    registry = state["session_registry"]

    session_id = request.session_id
    if session_id is None and request.chat_thread_id:
        manager = registry.get(request.chat_thread_id) or registry.get_or_start(
            request.chat_thread_id
        )
        if manager.session is not None:
            session_id = manager.session.session_id
    if session_id is None:
        raise TranscriptServiceError(
            message="Either session_id or chat_thread_id must be provided",
            status_code=status.HTTP_400_BAD_REQUEST,
            error_code="MISSING_SESSION_BINDING",
        )

    raw_event_id = record_raw(
        store,
        source="teams_media",
        event_type="participants_updated",
        payload=request.model_dump(),
        session_id=session_id,
        provider_timestamp_utc=request.fetched_at_utc,
    )

    resolver = state["participant_resolver"]
    participants = [
        Participant(
            aad_object_id=entry.aad_object_id,
            display_name=entry.display_name,
            user_principal_name=entry.user_principal_name,
            media_source_ids=list(entry.media_source_ids),
            is_in_lobby=entry.is_in_lobby,
            role=entry.role,
            is_application=entry.is_application,
            first_seen_at_utc=entry.first_seen_at_utc,
            last_seen_at_utc=entry.last_seen_at_utc,
        )
        for entry in request.participants
    ]
    resolver.upsert_participants(session_id, participants)
    logger.info(
        "Upserted %d participant(s) for session=%s (raw=%s)",
        len(participants),
        session_id,
        raw_event_id,
    )
    return {"ok": True, "session_id": session_id, "count": len(participants)}


@app.post("/sessions/{session_id}/speaker-mapping")
async def post_speaker_mapping(
    session_id: str,
    body: SpeakerMappingOverride,
    state: AppStateDep,
) -> dict[str, Any]:
    """Manually bind a speaker_id to an AAD; sticky over automatic resolution."""
    store = state["store"]
    raw_event_id = record_raw(
        store,
        source="system",
        event_type="manual_speaker_mapping",
        payload=body.model_dump(),
        session_id=session_id,
    )
    link = state["participant_resolver"].set_manual_mapping(
        session_id, body.speaker_id, body.aad_object_id
    )
    logger.info(
        "Manual speaker mapping: session=%s speaker=%s -> aad=%s (raw=%s)",
        session_id,
        body.speaker_id,
        body.aad_object_id,
        raw_event_id,
    )
    return {"ok": True, "link": link.model_dump()}


@app.get("/sessions/{session_id}/participants", response_model=ParticipantsResponse)
async def get_session_participants(
    session_id: str,
    state: AppStateDep,
) -> ParticipantsResponse:
    rows = await asyncio.to_thread(state["store"].get_participants, session_id)
    return ParticipantsResponse(session_id=session_id, participants=rows)


@app.get(
    "/sessions/{session_id}/speaker-identity",
    response_model=SpeakerIdentityResponse,
)
async def get_session_speaker_identity(
    session_id: str,
    state: AppStateDep,
) -> SpeakerIdentityResponse:
    rows = await asyncio.to_thread(state["store"].get_speaker_identity_links, session_id)
    return SpeakerIdentityResponse(session_id=session_id, links=rows)


class RawEventsResponse(BaseModel):
    session_id: str
    events: list[dict[str, Any]]


@app.get("/sessions/{session_id}/raw-events", response_model=RawEventsResponse)
async def get_session_raw_events(
    session_id: str,
    state: AppStateDep,
    since: str | None = None,
    limit: int | None = None,
) -> RawEventsResponse:
    """Return the immutable raw-audit rows ingested for a session."""
    rows = await asyncio.to_thread(
        state["store"].get_raw_events, session_id, since, limit
    )
    return RawEventsResponse(session_id=session_id, events=rows)


@app.get("/sessions/{session_id}/raw-events/export.ndjson")
async def export_session_raw_events_ndjson(
    session_id: str,
    state: AppStateDep,
) -> StreamingResponse:
    """Stream the session's raw audit log as one-event-per-line NDJSON."""
    store = state["store"]

    def _producer() -> AsyncIterator[bytes]:
        async def _gen() -> AsyncIterator[bytes]:
            for row in store.iter_raw_events(session_id):
                yield (_json_mod.dumps(row, default=str) + "\n").encode("utf-8")

        return _gen()

    return StreamingResponse(
        _producer(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="raw-events-{session_id}.ndjson"',
            "Cache-Control": "no-cache",
        },
    )


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Batcave Transcript Service v2")
    logger.info("=" * 60)
    logger.info(
        "Binding to: http://%s:%d",
        RUNTIME_CONFIG.sink_host,
        RUNTIME_CONFIG.sink_port,
    )
    logger.info(
        "Platform: %s, Product: %s (%s), Variant: %s (%s), Instance: %s",
        PLATFORM_NAME,
        PRODUCT_SPEC.product_id,
        PRODUCT_SPEC.display_name,
        VARIANT.variant_id,
        VARIANT.display_name,
        RUNTIME_CONFIG.instance_id,
    )
    logger.info("Product spec path: %s", PRODUCT_SPEC_PATH)
    logger.info("")
    logger.info("Endpoints:")
    logger.info("  POST /transcript          - Receive transcript events")
    logger.info("  POST /session/start       - Start interview session")
    logger.info("  POST /session/map-speaker - Map speaker to role")
    logger.info("  GET  /session/status      - Get session info")
    logger.info("  POST /session/end         - End session")
    logger.info("  GET  /health              - Health check")
    logger.info("  GET  /stats               - Statistics")
    logger.info("  GET  /product/spec        - Active product spec summary")
    logger.info("")
    logger.info("Transcripts saved to: %s", TRANSCRIPT_FILE)
    logger.info("Analysis output: %s", OUTPUT_DIR)
    logger.info("Enabled routes: %d", ROUTES.route_count)
    logger.info("Agent available: %s", AGENT_AVAILABLE)
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host=RUNTIME_CONFIG.sink_host,
        port=RUNTIME_CONFIG.sink_port,
        log_level="info",
    )
