"""
Python Transcript Receiver with v2 Diarization Support and Interview Agent Integration

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
Internal binding: http://0.0.0.0:8765
"""

from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
import logging
import os
import uuid
from pathlib import Path
from enum import Enum

from pydantic import BaseModel, Field

# Import interview agent components
from interview_agent.models import (
    TranscriptEvent,
    AnalysisItem,
    SessionAnalysis,
)
from interview_agent.session import InterviewSessionManager
from interview_agent.output import AnalysisOutputWriter


# =============================================================================
# Logging Configuration
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


# =============================================================================
# Configuration
# =============================================================================

# Output directory for analysis results
OUTPUT_DIR = Path(__file__).parent / "output"

# Transcript file path - save to Desktop for easy access (Windows VM default)
DESKTOP_PATH = Path(os.environ.get("USERPROFILE", os.path.expanduser("~"))) / "Desktop"
TRANSCRIPT_FILE = DESKTOP_PATH / "meeting_transcript.txt"


# =============================================================================
# Try to import InterviewAnalyzer (graceful if unavailable)
# =============================================================================

try:
    from interview_agent.agent import InterviewAnalyzer
    AGENT_AVAILABLE = True
    logger.info("InterviewAnalyzer loaded successfully")
except ImportError as e:
    AGENT_AVAILABLE = False
    InterviewAnalyzer = None  # type: ignore
    logger.warning(
        f"interview_agent.agent module not available: {e}. "
        "Agent analysis features disabled. Ensure openai-agents is installed and OPENAI_API_KEY is set."
    )


# =============================================================================
# Request/Response Models
# =============================================================================

class SpeakerRole(str, Enum):
    """Speaker roles in interview"""
    CANDIDATE = "candidate"
    INTERVIEWER = "interviewer"
    UNKNOWN = "unknown"


class SessionStartRequest(BaseModel):
    """Request to start a new interview session"""
    candidate_name: str = Field(..., description="Name of the candidate")
    meeting_url: str = Field(..., description="Teams meeting join URL")
    candidate_speaker_id: Optional[str] = Field(
        None,
        description="Optional speaker_id to map to candidate (can be mapped later)"
    )


class SpeakerMapRequest(BaseModel):
    """Request to map a speaker ID to a role"""
    speaker_id: str = Field(..., description="Speaker identifier: speaker_0, speaker_1, etc.")
    role: SpeakerRole = Field(..., description="Role: candidate or interviewer")


class SessionStatusResponse(BaseModel):
    """Session status response"""
    active: bool
    session_id: Optional[str] = None
    candidate_name: Optional[str] = None
    meeting_url: Optional[str] = None
    started_at: Optional[str] = None
    speaker_mappings: Dict[str, str] = Field(default_factory=dict)
    total_events: int = 0
    final_events: int = 0
    analysis_count: int = 0


# =============================================================================
# Global State
# =============================================================================

# Session manager
session_manager: InterviewSessionManager = InterviewSessionManager()

# Analysis output writer (initialized on startup)
output_writer: Optional[AnalysisOutputWriter] = None

# Async queue for agent consumption
transcript_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()

# Agent analysis queue (for candidate transcripts only)
agent_queue: asyncio.Queue[TranscriptEvent] = asyncio.Queue()

# Stats tracking
stats = {
    "events_received": 0,
    "partial_transcripts": 0,
    "final_transcripts": 0,
    "errors": 0,
    "session_events": 0,
    "v1_events": 0,
    "v2_events": 0,
    "agent_analyses": 0,
    "started_at": datetime.utcnow().isoformat() + "Z",
}

# Background task handle
_agent_task: Optional[asyncio.Task] = None


# =============================================================================
# Event Normalization (v1 -> v2)
# =============================================================================

def normalize_v1_to_v2(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Convert v1 format to v2 format.

    v1 format (from C# bot):
        {
            "Kind": "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled",
            "Text": "transcript text" | null,
            "TsUtc": "2026-01-28T20:33:12.3456789Z",
            "Details": "optional error details" | null
        }

    v2 format:
        {
            "event_type": "partial" | "final" | "session_started" | "session_stopped" | "error",
            "text": "transcript text" | null,
            "timestamp_utc": "2026-01-28T20:33:12.3456789Z",
            "speaker_id": "speaker_0" | null,
            ...
        }
    """
    # Check if this is v1 format (has "Kind" key)
    if "Kind" in payload or "kind" in payload:
        kind = payload.get("Kind") or payload.get("kind", "unknown")

        # Map v1 event types to v2
        event_type_map = {
            "recognizing": "partial",
            "recognized": "final",
            "session_started": "session_started",
            "session_stopped": "session_stopped",
            "canceled": "error",
        }

        v2_payload = {
            "event_type": event_type_map.get(kind.lower(), kind.lower()),
            "text": payload.get("Text") or payload.get("text"),
            "timestamp_utc": payload.get("TsUtc") or payload.get("tsUtc") or datetime.utcnow().isoformat() + "Z",
        }

        # Handle error details
        details = payload.get("Details") or payload.get("details")
        if details:
            v2_payload["metadata"] = {"raw_response": {"error_details": details}}

        stats["v1_events"] += 1
        return v2_payload

    # Already v2 format
    stats["v2_events"] += 1
    return payload


# =============================================================================
# File Operations
# =============================================================================

def save_transcript_to_file(event: TranscriptEvent) -> None:
    """Append transcript to file on desktop"""
    try:
        DESKTOP_PATH.mkdir(parents=True, exist_ok=True)

        with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
            if event.event_type == "session_started":
                f.write(f"\n{'='*60}\n")
                f.write(f"NEW SESSION STARTED: {event.timestamp_utc}\n")
                f.write(f"{'='*60}\n\n")
            elif event.event_type == "final" and event.text:
                speaker_info = f"[{event.speaker_id}]" if event.speaker_id else ""
                f.write(f"[{event.timestamp_utc}]{speaker_info} {event.text}\n")
            elif event.event_type == "session_stopped":
                f.write(f"\n--- Session ended: {event.timestamp_utc} ---\n\n")

        logger.debug(f"Saved to file: {TRANSCRIPT_FILE}")
    except Exception as e:
        logger.error(f"Failed to save transcript to file: {e}")


# =============================================================================
# Agent Processing
# =============================================================================

async def agent_processing_loop() -> None:
    """
    Background task that processes candidate transcripts through the interview agent.

    Only processes:
    - "final" transcript events
    - From speakers mapped as "candidate"
    """
    logger.info("Agent processing loop started")

    # Initialize analyzer if available
    analyzer = None
    if AGENT_AVAILABLE and InterviewAnalyzer:
        try:
            analyzer = InterviewAnalyzer()
            logger.info("InterviewAnalyzer initialized")
        except Exception as e:
            logger.error(f"Failed to initialize InterviewAnalyzer: {e}")

    response_counter = 0

    while True:
        try:
            # Wait for next candidate transcript
            event = await agent_queue.get()

            if not event.text:
                continue

            logger.info(f"AGENT_INPUT [{event.speaker_id}]: {event.text[:100]}...")

            # Process with agent if available
            if analyzer and session_manager.is_active:
                try:
                    # Get conversation context
                    context = session_manager.get_session_context()
                    last_question = session_manager.get_last_interviewer_question()

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

                    # Run analysis (returns AnalysisItem)
                    analysis_item = await analyzer.analyze_async(
                        response_text=event.text,
                        context=analysis_context,
                        speaker_id=event.speaker_id,
                    )

                    stats["agent_analyses"] += 1
                    response_counter += 1

                    # Write to output file
                    if output_writer and session_manager.session:
                        output_writer.append_item(
                            session_manager.session.session_id,
                            analysis_item
                        )

                    logger.info(
                        f"Analysis complete: relevance={analysis_item.relevance_score:.2f}, "
                        f"clarity={analysis_item.clarity_score:.2f}"
                    )

                except Exception as e:
                    logger.error(f"Agent analysis failed: {e}", exc_info=True)
            else:
                if not AGENT_AVAILABLE:
                    logger.debug("Skipping analysis - agent not available")
                elif not session_manager.is_active:
                    logger.debug("Skipping analysis - no active session")

        except asyncio.CancelledError:
            logger.info("Agent processing loop cancelled")
            break
        except Exception as e:
            logger.error(f"Error in agent processing loop: {e}", exc_info=True)
            await asyncio.sleep(1)


# =============================================================================
# FastAPI App
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifespan"""
    global _agent_task, output_writer

    # Startup
    logger.info("Starting Teams Transcript Sink v2")

    # Initialize output directory and writer
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_writer = AnalysisOutputWriter(OUTPUT_DIR)
    logger.info(f"Analysis output directory: {OUTPUT_DIR}")

    # Start agent processing loop
    _agent_task = asyncio.create_task(agent_processing_loop())

    yield

    # Shutdown
    logger.info("Shutting down...")
    if _agent_task:
        _agent_task.cancel()
        try:
            await _agent_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title="Teams Transcript Sink",
    version="2.0.0",
    description="Receives diarized transcripts and integrates with interview analysis agent",
    lifespan=lifespan,
)


# =============================================================================
# Endpoints
# =============================================================================

@app.post("/transcript")
async def receive_transcript(req: Request):
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
    """
    try:
        payload = await req.json()

        # Normalize v1 to v2 format
        normalized = normalize_v1_to_v2(payload)

        # Parse into TranscriptEvent model
        event = TranscriptEvent(**normalized)

        # Update stats
        stats["events_received"] += 1

        if event.event_type == "partial":
            stats["partial_transcripts"] += 1
            logger.debug(f"[PARTIAL] [{event.speaker_id}] {event.text}")

        elif event.event_type == "final":
            stats["final_transcripts"] += 1
            logger.info(f"[FINAL] [{event.speaker_id}] {event.text}")

            # Save to file
            save_transcript_to_file(event)

            # Add to session if active
            if session_manager.is_active:
                session_manager.add_transcript(event)

                # Queue for agent if from candidate
                candidate_id = session_manager.get_candidate_speaker_id()
                if candidate_id and event.speaker_id == candidate_id:
                    await agent_queue.put(event)
                    logger.debug(f"Queued for agent analysis: {event.text[:50]}...")

        elif event.event_type == "session_started":
            stats["session_events"] += 1
            logger.info("Speech recognition session started")
            save_transcript_to_file(event)

        elif event.event_type == "session_stopped":
            stats["session_events"] += 1
            logger.info("Speech recognition session stopped")
            save_transcript_to_file(event)

        elif event.event_type == "error":
            stats["errors"] += 1
            if event.error:
                error_msg = f"{event.error.code}: {event.error.message}"
            elif event.metadata and event.metadata.raw_response:
                error_msg = str(event.metadata.raw_response)
            else:
                error_msg = "Unknown error"
            logger.error(f"Speech recognition error: {error_msg}")

        # Push to general transcript queue for other consumers
        await transcript_queue.put(event)

        return {"ok": True, "received_at": datetime.utcnow().isoformat() + "Z"}

    except Exception as e:
        logger.error(f"Error processing transcript: {e}", exc_info=True)
        stats["errors"] += 1
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)}
        )


@app.post("/session/start")
async def start_session(request: SessionStartRequest):
    """
    Start a new interview session.

    Request body:
        {
            "candidate_name": "John Doe",
            "meeting_url": "https://teams.microsoft.com/...",
            "candidate_speaker_id": "speaker_0"  // optional
        }
    """
    if session_manager.is_active:
        raise HTTPException(
            status_code=409,
            detail="Session already active. End current session first."
        )

    session = session_manager.start_session(
        candidate_name=request.candidate_name,
        meeting_url=request.meeting_url
    )

    # Map candidate speaker if provided
    if request.candidate_speaker_id:
        session_manager.map_speaker(
            request.candidate_speaker_id,
            "candidate",
            request.candidate_name
        )

    # Initialize analysis file
    if output_writer:
        analysis = SessionAnalysis(
            session_id=session.session_id,
            candidate_name=request.candidate_name,
            started_at=session.started_at,
        )
        output_writer.write_analysis(session.session_id, analysis)

    logger.info(f"Interview session started for: {request.candidate_name} ({session.session_id})")

    return {
        "ok": True,
        "message": f"Session started for {request.candidate_name}",
        "session_id": session.session_id,
        "started_at": session.started_at,
    }


@app.post("/session/map-speaker")
async def map_speaker(request: SpeakerMapRequest):
    """
    Map a speaker ID to a role.

    Request body:
        {
            "speaker_id": "speaker_0",
            "role": "candidate"  // or "interviewer"
        }
    """
    if not session_manager.is_active:
        raise HTTPException(
            status_code=400,
            detail="No active session. Start a session first."
        )

    session_manager.map_speaker(request.speaker_id, request.role.value)

    return {
        "ok": True,
        "message": f"Mapped {request.speaker_id} to {request.role.value}",
        "speaker_mappings": session_manager.get_session_context()["speaker_mappings"],
    }


@app.get("/session/status")
async def get_session_status():
    """Get current session information"""
    context = session_manager.get_session_context()

    response = SessionStatusResponse(
        active=context["session_active"],
        session_id=context.get("session_id"),
        candidate_name=context.get("candidate_name"),
        meeting_url=context.get("meeting_url"),
        started_at=context.get("started_at"),
        speaker_mappings=context.get("speaker_mappings", {}),
        total_events=context.get("total_events", 0),
        final_events=context.get("final_events", 0),
        analysis_count=stats["agent_analyses"],
    )

    return {
        "session": response.model_dump(),
        "agent_available": AGENT_AVAILABLE,
    }


# Alias for backwards compatibility
@app.get("/session")
async def get_session():
    """Get current session info (alias for /session/status)"""
    return await get_session_status()


@app.post("/session/end")
async def end_session():
    """
    End the current session and finalize analysis.

    Returns session summary with statistics.
    """
    if not session_manager.is_active:
        raise HTTPException(
            status_code=400,
            detail="No active session to end."
        )

    session = session_manager.session
    if not session:
        raise HTTPException(status_code=400, detail="No session data available.")

    session_id = session.session_id
    candidate_name = session.candidate_name
    started_at = session.started_at

    # End the session
    ended_session = session_manager.end_session()

    # Finalize analysis file
    if output_writer and ended_session:
        try:
            analysis = output_writer.load_analysis(session_id)
            if analysis:
                analysis.ended_at = ended_session.ended_at
                analysis.compute_overall_scores()
                output_writer.write_analysis(session_id, analysis)
        except Exception as e:
            logger.error(f"Failed to finalize analysis: {e}")

    summary = {
        "session_id": session_id,
        "candidate_name": candidate_name,
        "started_at": started_at,
        "ended_at": ended_session.ended_at if ended_session else datetime.utcnow().isoformat() + "Z",
        "total_events": len(ended_session.transcript_events) if ended_session else 0,
        "analyses_generated": stats["agent_analyses"],
    }

    logger.info(f"Session ended: {session_id}")

    return {
        "ok": True,
        "message": "Session ended",
        "summary": summary,
    }


@app.get("/health")
async def health():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "service": "Teams Transcript Sink",
        "version": "2.0.0",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "agent_available": AGENT_AVAILABLE,
        "session_active": session_manager.is_active,
    }


@app.get("/stats")
async def get_stats():
    """Get current statistics"""
    context = session_manager.get_session_context()

    return {
        "stats": stats,
        "transcript_queue_size": transcript_queue.qsize(),
        "agent_queue_size": agent_queue.qsize(),
        "session": {
            "active": context["session_active"],
            "session_id": context.get("session_id"),
            "candidate_name": context.get("candidate_name"),
            "total_events": context.get("total_events", 0),
            "final_events": context.get("final_events", 0),
        },
        "agent_available": AGENT_AVAILABLE,
        "output_directory": str(OUTPUT_DIR),
    }


# =============================================================================
# Main Entry Point
# =============================================================================

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Teams Transcript Sink v2")
    logger.info("=" * 60)
    logger.info(f"Binding to: http://0.0.0.0:8765")
    logger.info(f"Target FQDN: https://agent.qmachina.com")
    logger.info("")
    logger.info("Endpoints:")
    logger.info("  POST /transcript          - Receive transcript events")
    logger.info("  POST /session/start       - Start interview session")
    logger.info("  POST /session/map-speaker - Map speaker to role")
    logger.info("  GET  /session/status      - Get session info")
    logger.info("  POST /session/end         - End session")
    logger.info("  GET  /health              - Health check")
    logger.info("  GET  /stats               - Statistics")
    logger.info("")
    logger.info(f"Transcripts saved to: {TRANSCRIPT_FILE}")
    logger.info(f"Analysis output: {OUTPUT_DIR}")
    logger.info(f"Agent available: {AGENT_AVAILABLE}")
    logger.info("=" * 60)

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8765,
        log_level="info",
    )
