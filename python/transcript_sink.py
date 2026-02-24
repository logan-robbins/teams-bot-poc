"""
LegionMeet Transcript Service v2

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

Target deployment: https://agent.qmachina.com (behind TLS proxy)
Internal binding: configured by SINK_HOST/SINK_PORT (default 0.0.0.0:8765)
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from interview_agent.models import (
    AnalysisItem,
    SessionAnalysis,
    TranscriptEvent,
)
from interview_agent.output import AnalysisOutputWriter
from interview_agent.session import InterviewSessionManager
from interview_agent.checklist_state import ChecklistDefinition, ChecklistStateManager
from legionmeet_platform import (
    AgentTool,
    PLATFORM_NAME,
    load_product_spec,
)
from legionmeet_platform.routes import RouteOrchestrator, build_route_orchestrator
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


def load_runtime_config() -> RuntimeConfig:
    """Load runtime config from environment with strict validation."""
    variant_id = (os.environ.get("VARIANT_ID", "default") or "").strip().lower()
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

    return RuntimeConfig(
        variant_id=variant_id,
        product_spec_path=product_spec_path,
        instance_id=instance_id,
        sink_host=sink_host,
        sink_port=sink_port,
        output_dir=output_dir,
        transcript_file=transcript_file,
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
    "https://agent.qmachina.com",
]


# =============================================================================
# Optional Agent Import (graceful degradation)
# =============================================================================

try:
    from interview_agent.agent import InterviewAnalyzer
    from interview_agent.checklist import ChecklistAgent
    from interview_agent.pubsub import ThoughtType, get_publisher

    AGENT_AVAILABLE = True
    logger.info("InterviewAnalyzer loaded successfully")
except ImportError as e:
    AGENT_AVAILABLE = False
    InterviewAnalyzer = None  # type: ignore[misc, assignment]
    ChecklistAgent = None  # type: ignore[misc, assignment]
    get_publisher = None  # type: ignore[misc, assignment]
    ThoughtType = None  # type: ignore[misc, assignment]
    logger.warning(
        "interview_agent.agent module not available: %s. "
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


class AppState(TypedDict):
    """Type-safe application state managed by lifespan."""

    session_manager: InterviewSessionManager
    output_writer: AnalysisOutputWriter
    transcript_queue: asyncio.Queue[TranscriptEvent]
    agent_queue: asyncio.Queue[TranscriptEvent]
    stats: AppStats
    agent_task: asyncio.Task[None] | None
    variant_plugin: VariantPlugin
    checklist_manager: ChecklistStateManager
    route_orchestrator: RouteOrchestrator
    checklist_agent: ChecklistAgent | None


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
        output_writer=state.output_writer,
        transcript_queue=state.transcript_queue,
        agent_queue=state.agent_queue,
        stats=state.stats,
        agent_task=state.agent_task,
        variant_plugin=state.variant_plugin,
        checklist_manager=state.checklist_manager,
        route_orchestrator=state.route_orchestrator,
        checklist_agent=state.checklist_agent,
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
        "speaker_id": request.speaker_id,
        "audio_start_ms": request.audio_start_ms,
        "audio_end_ms": request.audio_end_ms,
        "confidence": request.confidence,
        "metadata": request.metadata,
    }


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


# =============================================================================
# Agent Processing
# =============================================================================


async def agent_processing_loop(
    agent_queue: asyncio.Queue[TranscriptEvent],
    session_manager: InterviewSessionManager,
    output_writer: AnalysisOutputWriter,
    stats: AppStats,
    variant_plugin: VariantPlugin,
    checklist_manager: ChecklistStateManager,
    route_orchestrator: RouteOrchestrator,
) -> None:
    """
    Background task that processes candidate transcripts through the interview agent.

    Only processes:
    - "final" transcript events
    - From speakers mapped as "candidate"

    Publishes real-time thoughts to the pub-sub system for Streamlit UI.

    Args:
        agent_queue: Queue of transcript events to process.
        session_manager: Session manager instance.
        output_writer: Output writer for analysis results.
        stats: Statistics dictionary to update.
        variant_plugin: Active variant plugin.
    """
    logger.info("Agent processing loop started")

    # Initialize analyzer if available
    analyzer = None
    publisher = None

    if AGENT_AVAILABLE and InterviewAnalyzer:
        try:
            analyzer = InterviewAnalyzer(
                model=PRODUCT_SPEC.agent.model,
                publish_thoughts=True,
                reasoning_effort=PRODUCT_SPEC.agent.reasoning_effort,
                instructions=PRODUCT_SPEC.agent.prompt_template,
            )
            publisher = get_publisher() if get_publisher else None
            logger.info("InterviewAnalyzer initialized with real-time publishing")
        except Exception as e:
            logger.error("Failed to initialize InterviewAnalyzer: %s", e)

    response_counter = 0

    while True:
        try:
            # Wait for next candidate transcript
            event = await agent_queue.get()

            if not event.text:
                continue

            logger.info("AGENT_INPUT [%s]: %s...", event.speaker_id, event.text[:100])

            # Process with agent if available
            if analyzer and session_manager.is_active:
                try:
                    # Get conversation context
                    context = session_manager.get_session_context()

                    # Build conversation history for context
                    conversation_history = []
                    for turn in context.get("recent_conversation", [])[-10:]:
                        conversation_history.append({
                            "role": turn.get("role", "unknown"),
                            "text": turn.get("text", ""),
                        })

                    analysis_context = {
                        "candidate_name": context.get("candidate_name", "Unknown"),
                        "conversation_history": conversation_history,
                    }
                    analysis_context = variant_plugin.build_analysis_context(
                        analysis_context,
                        event,
                    )

                    # Run analysis (publishes to pub-sub automatically)
                    analysis_item: AnalysisItem = await analyzer.analyze_async(
                        response_text=event.text,
                        context=analysis_context,
                        speaker_id=event.speaker_id,
                    )
                    analysis_item = variant_plugin.transform_analysis_item(analysis_item)

                    stats["agent_analyses"] += 1
                    response_counter += 1

                    # Write to output file
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

                    logger.info(
                        "Analysis #%d complete: relevance=%.2f, clarity=%.2f",
                        response_counter,
                        analysis_item.relevance_score,
                        analysis_item.clarity_score,
                    )

                except Exception as e:
                    logger.error("Agent analysis failed: %s", e, exc_info=True)
                    if publisher:
                        await publisher.publish_error(f"Analysis failed: {e!s}")
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
    logger.info("Starting LegionMeet Transcript Service v2")
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

    # Initialize output directory and writer
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_writer = AnalysisOutputWriter(OUTPUT_DIR)
    logger.info("Analysis output directory: %s", OUTPUT_DIR)

    # Initialize state components
    session_manager = InterviewSessionManager()
    checklist_manager = build_checklist_manager()
    transcript_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()
    agent_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()
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
            session_manager,
            output_writer,
            stats,
            VARIANT,
            checklist_manager,
            route_orchestrator,
        )
    )

    # Yield state to be attached to app.state
    state = {
        "session_manager": session_manager,
        "output_writer": output_writer,
        "transcript_queue": transcript_queue,
        "agent_queue": agent_queue,
        "stats": stats,
        "agent_task": agent_task,
        "variant_plugin": VARIANT,
        "checklist_manager": checklist_manager,
        "route_orchestrator": route_orchestrator,
        "checklist_agent": checklist_agent,
    }

    yield state

    # Shutdown
    logger.info("Shutting down...")
    agent_task.cancel()
    try:
        await agent_task
    except asyncio.CancelledError:
        pass


# =============================================================================
# FastAPI Application
# =============================================================================


app = FastAPI(
    title=f"LegionMeet Transcript Service ({PRODUCT_SPEC.product_id})",
    version="2.0.0",
    description="Receives diarized transcripts and integrates with LegionMeet modality analysis pipelines",
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
    session_manager = state["session_manager"]
    output_writer = state["output_writer"]
    transcript_queue = state["transcript_queue"]
    agent_queue = state["agent_queue"]
    variant_plugin = state["variant_plugin"]
    checklist_manager = state["checklist_manager"]
    route_orchestrator = state["route_orchestrator"]
    checklist_agent = state["checklist_agent"]

    # Normalize v1 to v2 format
    normalized = normalize_v1_to_v2(request, stats)

    # Parse into TranscriptEvent model
    event = TranscriptEvent(**normalized)

    # Update stats
    stats["events_received"] += 1

    if event.event_type == "partial":
        stats["partial_transcripts"] += 1
        logger.debug("[PARTIAL] [%s] %s", event.speaker_id, event.text)

    elif event.event_type == "final":
        stats["final_transcripts"] += 1
        logger.info("[FINAL] [%s] %s", event.speaker_id, event.text)

        # Save to file (async)
        await save_transcript_to_file(event)

        # Add to session if active
        if session_manager.is_active:
            session_manager.add_transcript(event)

            speaker_role = (
                session_manager.get_speaker_role(event.speaker_id) if event.speaker_id else None
            ) or "unknown"

            if event.text and checklist_manager.apply_talestral_heuristic(
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

            # Queue for agent if from candidate
            candidate_id = session_manager.get_candidate_speaker_id()
            if candidate_id and event.speaker_id == candidate_id:
                await agent_queue.put(event)
                logger.debug("Queued for agent analysis: %s...", event.text[:50])

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

    # Map candidate speaker if provided
    if request.candidate_speaker_id:
        session_manager.map_speaker(
            request.candidate_speaker_id,
            "candidate",
            request.candidate_name,
        )

    # Initialize analysis file
    analysis = SessionAnalysis(
        session_id=session.session_id,
        candidate_name=request.candidate_name,
        started_at=session.started_at,
        checklist_state=checklist_manager.snapshot(),
    )
    output_writer.write_analysis(session.session_id, analysis)

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
                analysis.compute_overall_scores()
                output_writer.write_analysis(session_id, analysis)
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
        "total_events": len(ended_session.transcript_events) if ended_session else 0,
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
        service="LegionMeet Transcript Service",
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
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("LegionMeet Transcript Service v2")
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
