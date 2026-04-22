#!/usr/bin/env python3
"""
Streamlit UI for Real-Time Interview Agent (LegionMeet modality runtime).

Provides a real-time interview simulation UI with:
- Live transcript display with speaker identification
- AI-powered coaching feedback and analysis
- Interview checklist tracking
- Session management and controls

Usage:
    uv run python transcript_sink.py  # Terminal 1
    uv run streamlit run streamlit_ui.py --server.port 8502  # Terminal 2
"""

from __future__ import annotations

import logging
import os
import random
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Final

import httpx
import streamlit as st
from legionmeet_platform import load_product_spec

# =============================================================================
# Logging Configuration
# =============================================================================

logger: logging.Logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

PRODUCT_SPEC, PRODUCT_SPEC_PATH = load_product_spec()
INSTANCE_ID: Final[str] = os.environ.get("INSTANCE_ID", PRODUCT_SPEC.product_id)
SINK_URL: Final[str] = os.environ.get("SINK_URL", "http://127.0.0.1:8765")
OUTPUT_DIR: Final[Path] = (
    Path(os.environ["OUTPUT_DIR"]).expanduser()
    if "OUTPUT_DIR" in os.environ
    else (
        Path(__file__).parent / "output" / INSTANCE_ID
        if "INSTANCE_ID" in os.environ
        else Path(__file__).parent / "output"
    )
)

# Simulation timing: MINIMUM 5 seconds between messages
MIN_DELAY_SECONDS: Final[float] = 5.0
MAX_DELAY_SECONDS: Final[float] = 7.0

# HTTP timeout for sink requests
HTTP_TIMEOUT_SECONDS: Final[float] = 10.0
HEALTH_CHECK_TIMEOUT_SECONDS: Final[float] = 2.0

# Event generation constants
MS_PER_WORD: Final[float] = 60.0
MIN_DURATION_JITTER_MS: Final[float] = 50.0
MAX_DURATION_JITTER_MS: Final[float] = 150.0
MIN_CONFIDENCE: Final[float] = 0.88
MAX_CONFIDENCE: Final[float] = 0.98
PAUSE_BETWEEN_MESSAGES_MS: Final[float] = 500.0

# UI refresh delays
IDLE_POLL_DELAY_SECONDS: Final[float] = 2.0
POST_RESTART_DELAY_SECONDS: Final[float] = 0.3

INTERVIEW_SCRIPT: Final[list[tuple[str, str]]] = [
    (speaker, text) for speaker, text in PRODUCT_SPEC.ui.interview_script
]
CHECKLIST_ITEMS: Final[list[dict[str, object]]] = [
    {
        "id": item.id,
        "label": item.label,
        "keywords": list(item.keywords),
    }
    for item in PRODUCT_SPEC.checklist.items
]


# =============================================================================
# Page Configuration
# =============================================================================

st.set_page_config(
    page_title=PRODUCT_SPEC.ui.page_title,
    page_icon=PRODUCT_SPEC.ui.page_icon,
    layout="wide",
    initial_sidebar_state="collapsed",
)


# =============================================================================
# Custom CSS
# =============================================================================

st.markdown("""
<style>
#MainMenu {visibility: hidden;}
footer {visibility: hidden;}
.stDeployButton {display: none;}

.main .block-container {
    padding: 1rem 2rem;
    max-width: 100%;
}

/* Panel styling */
div[data-testid="stVerticalBlock"] > div[data-testid="stVerticalBlockBorderWrapper"] {
    background: white;
    border-radius: 12px;
    border: 1px solid #E2E8F0;
    padding: 0;
}

/* Chat bubble styling */
.transcript-bubble {
    background: linear-gradient(135deg, #E0F2FE 0%, #BAE6FD 100%);
    color: #0C4A6E;
    padding: 0.75rem 1rem;
    border-radius: 12px;
    border-bottom-left-radius: 4px;
    margin-bottom: 0.5rem;
    max-width: 95%;
}

.analysis-bubble {
    background: linear-gradient(135deg, #D1FAE5 0%, #A7F3D0 100%);
    color: #064E3B;
    padding: 0.75rem 1rem;
    border-radius: 12px;
    border-bottom-right-radius: 4px;
    margin-bottom: 0.5rem;
    margin-left: auto;
    max-width: 95%;
}

.bubble-meta {
    font-size: 0.7rem;
    color: #64748B;
    margin-top: 0.25rem;
}

/* Checklist item */
.checklist-item {
    display: flex;
    align-items: center;
    gap: 0.75rem;
    padding: 0.6rem 0.75rem;
    background: #F8FAFC;
    border-radius: 8px;
    margin-bottom: 0.4rem;
}

.checklist-analyzing {
    background: #FEF3C7;
    border: 1px solid #F59E0B;
}

.checklist-complete {
    background: #D1FAE5;
    border: 1px solid #10B981;
}

.stoplight {
    width: 14px;
    height: 14px;
    border-radius: 50%;
    flex-shrink: 0;
}

.stoplight-pending { background: transparent; border: 2px solid #CBD5E1; }
.stoplight-analyzing { background: #F59E0B; border: 2px solid #F59E0B; }
.stoplight-complete { background: #10B981; border: 2px solid #10B981; }

/* Participant card */
.participant-card {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    padding: 0.5rem;
    background: #F1F5F9;
    border-radius: 8px;
    margin-bottom: 0.4rem;
}

.participant-icon {
    width: 28px;
    height: 28px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
}
</style>
""", unsafe_allow_html=True)


# =============================================================================
# State Management
# =============================================================================

class ChecklistStatus(str, Enum):
    """Status of an interview checklist item."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    COMPLETE = "complete"


@dataclass
class ChatMessage:
    """
    Represents a message in the interview chat display.

    Attributes:
        id: Unique message identifier.
        msg_type: Type of message ("transcript" or "analysis").
        content: Message text content.
        speaker: Speaker name/role.
        timestamp: Formatted timestamp string.
        session_id: Session this message belongs to.
        relevance: AI-computed relevance score (0-1).
        clarity: AI-computed clarity score (0-1).
        key_points: List of key observations from analysis.
        follow_ups: List of coaching suggestions for interviewer.
    """

    id: str
    msg_type: str
    content: str
    speaker: str
    timestamp: str
    session_id: str = ""
    relevance: float | None = None
    clarity: float | None = None
    key_points: list[str] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    related_text: str = ""


def init_state() -> None:
    """
    Initialize Streamlit session state for the interview simulation.

    Only initializes state on first run; subsequent calls are no-ops.
    This ensures state persists across Streamlit reruns.
    """
    if "init" not in st.session_state:
        st.session_state.init = True
        st.session_state.running = False
        st.session_state.index = 0
        st.session_state.messages: list[ChatMessage] = []
        st.session_state.seen_transcript_keys: set[str] = set()
        st.session_state.checklist = {
            item["id"]: ChecklistStatus.PENDING for item in CHECKLIST_ITEMS
        }
        st.session_state.audio_offset = 0.0
        st.session_state.analysis_count = 0
        st.session_state.current_session_id: str | None = None


# =============================================================================
# Helper Functions
# =============================================================================

def check_sink() -> bool:
    """
    Check if the transcript sink service is healthy.

    Returns:
        True if sink is reachable and healthy, False otherwise.
    """
    try:
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as client:
            response = client.get(f"{SINK_URL}/health")
            return response.status_code == 200
    except httpx.ConnectError:
        logger.debug("Sink connection failed")
        return False
    except httpx.TimeoutException:
        logger.debug("Sink health check timed out")
        return False


def fetch_session() -> dict[str, object] | None:
    """
    Fetch current session status from the transcript sink.

    Returns:
        Session status dictionary or None if unavailable.
    """
    try:
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as client:
            response = client.get(f"{SINK_URL}/session/status")
            if response.status_code == 200:
                return response.json()
    except httpx.ConnectError:
        logger.debug("Failed to fetch session: connection error")
    except httpx.TimeoutException:
        logger.debug("Failed to fetch session: timeout")
    return None


def fetch_analysis(session_id: str) -> dict[str, object] | None:
    """
    Fetch persisted analysis for a session from the sink API.

    The UI and API run in separate containers in production, so analysis must be
    loaded over HTTP rather than from the UI container filesystem.
    """
    try:
        with httpx.Client(timeout=HEALTH_CHECK_TIMEOUT_SECONDS) as client:
            response = client.get(
                f"{SINK_URL}/session/analysis",
                params={"session_id": session_id},
            )
            if response.status_code == 200:
                return response.json()
    except httpx.ConnectError:
        logger.debug("Failed to fetch analysis: connection error")
    except httpx.TimeoutException:
        logger.debug("Failed to fetch analysis: timeout")
    return None


def fmt_time(timestamp: str) -> str:
    """
    Format an ISO timestamp to HH:MM:SS display format.

    Args:
        timestamp: ISO 8601 timestamp string.

    Returns:
        Formatted time string, or truncated input on parse failure.
    """
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except (ValueError, AttributeError):
        return timestamp[:8] if len(timestamp) >= 8 else timestamp


def track_session(session_data: dict[str, object]) -> str:
    """
    Keep UI state aligned to the active sink session.

    Returns:
        Current session id or empty string if unavailable.
    """
    session_id = session_data.get("session_id", "")
    if not isinstance(session_id, str) or not session_id:
        return ""

    if st.session_state.current_session_id != session_id:
        st.session_state.current_session_id = session_id
        st.session_state.messages = []
        st.session_state.analysis_count = 0
        st.session_state.seen_transcript_keys = set()

    return session_id


def sync_checklist_state(session_payload: dict[str, object]) -> None:
    """Update UI checklist state from sink-owned session status payload."""
    session = session_payload.get("session", {})
    if not isinstance(session, dict):
        return

    checklist = session.get("checklist", [])
    if not isinstance(checklist, list):
        return

    for entry in checklist:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        raw_status = entry.get("status")
        if not isinstance(item_id, str):
            continue
        try:
            parsed_status = ChecklistStatus(str(raw_status))
        except ValueError:
            parsed_status = ChecklistStatus.PENDING
        st.session_state.checklist[item_id] = parsed_status


def sync_live_transcript(session_payload: dict[str, object]) -> None:
    """Populate transcript bubbles from the sink's live recent conversation."""
    session = session_payload.get("session", {})
    if not isinstance(session, dict) or not session.get("active"):
        return

    session_id = track_session(session)
    if not session_id:
        return

    candidate_name = session.get("candidate_name")
    if not isinstance(candidate_name, str) or not candidate_name:
        candidate_name = "Candidate"

    recent_conversation = session.get("recent_conversation", [])
    if not isinstance(recent_conversation, list):
        return

    for turn in recent_conversation:
        if not isinstance(turn, dict):
            continue

        text = turn.get("text")
        timestamp = turn.get("timestamp")
        role = turn.get("role")
        speaker_id = turn.get("speaker_id")

        if not isinstance(text, str) or not text.strip():
            continue
        if not isinstance(timestamp, str) or not timestamp:
            continue

        transcript_key = f"{session_id}|{timestamp}|{speaker_id}|{text}"
        if transcript_key in st.session_state.seen_transcript_keys:
            continue

        if role == "candidate":
            speaker = candidate_name
        elif role == "interviewer":
            speaker = "Interviewer"
        else:
            speaker = str(speaker_id or "Unknown")

        st.session_state.messages.append(
            ChatMessage(
                id=str(uuid.uuid4()),
                msg_type="transcript",
                content=text,
                speaker=speaker,
                timestamp=fmt_time(timestamp),
                session_id=session_id,
            )
        )
        st.session_state.seen_transcript_keys.add(transcript_key)


def get_detected_participants(session_data: dict[str, object]) -> list[tuple[str, str]]:
    """Build a participant list from explicit mappings or live diarization turns."""
    participants: list[tuple[str, str]] = []
    seen_speakers: set[str] = set()

    candidate_name = session_data.get("candidate_name")
    if not isinstance(candidate_name, str) or not candidate_name:
        candidate_name = "Candidate"

    mappings = session_data.get("speaker_mappings", {})
    if isinstance(mappings, dict):
        for speaker_id, role in mappings.items():
            if not isinstance(speaker_id, str):
                continue
            seen_speakers.add(speaker_id)
            if role == "candidate":
                participants.append((candidate_name, speaker_id))
            elif role == "interviewer":
                participants.append(("Interviewer", speaker_id))
            else:
                participants.append((str(role or speaker_id), speaker_id))

    recent_conversation = session_data.get("recent_conversation", [])
    if isinstance(recent_conversation, list):
        for turn in recent_conversation:
            if not isinstance(turn, dict):
                continue

            speaker_id = turn.get("speaker_id")
            role = turn.get("role")
            if not isinstance(speaker_id, str) or not speaker_id or speaker_id in seen_speakers:
                continue

            seen_speakers.add(speaker_id)
            if role == "candidate":
                participants.append((candidate_name, speaker_id))
            elif role == "interviewer":
                participants.append(("Interviewer", speaker_id))
            else:
                participants.append((speaker_id, speaker_id))

    return participants


def generate_event(speaker_id: str, text: str, audio_offset: float) -> dict[str, object]:
    """
    Generate high-fidelity v2 transcript event matching C# bot format.

    Args:
        speaker_id: The speaker identifier (e.g., "speaker_0").
        text: The transcript text content.
        audio_offset: Audio offset from session start in milliseconds.

    Returns:
        Dictionary containing the transcript event in v2 format.
    """
    word_count = len(text.split())
    duration_ms = word_count * MS_PER_WORD + random.uniform(
        MIN_DURATION_JITTER_MS, MAX_DURATION_JITTER_MS
    )

    return {
        "event_type": "final",
        "text": text,
        "timestamp_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "speaker_id": speaker_id,
        "audio_start_ms": round(audio_offset, 1),
        "audio_end_ms": round(audio_offset + duration_ms, 1),
        "confidence": round(random.uniform(MIN_CONFIDENCE, MAX_CONFIDENCE), 3),
        "metadata": {"provider": "deepgram", "model": "nova-3"},
    }


# =============================================================================
# Simulation Functions
# =============================================================================

def start_sim() -> bool:
    """
    Start a new simulation session.

    Clears old analysis files, ends any existing session, and initializes
    a fresh simulation with speaker mappings.

    Returns:
        True if simulation started successfully, False otherwise.
    """
    try:
        # Clear old analysis files to ensure fresh start
        try:
            for old_file in OUTPUT_DIR.glob("*_analysis.json"):
                old_file.unlink()
        except OSError as e:
            logger.debug("Could not clean up old analysis files: %s", e)

        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            # First, end any existing session to allow restart
            try:
                client.post(f"{SINK_URL}/session/end")
            except httpx.RequestError:
                pass  # Ignore if no session exists

            response = client.post(
                f"{SINK_URL}/session/start",
                json={
                    "candidate_name": PRODUCT_SPEC.ui.candidate_name,
                    "meeting_url": PRODUCT_SPEC.ui.meeting_url,
                    "product_id": PRODUCT_SPEC.product_id,
                },
            )
            if response.status_code != 200:
                st.error(f"Failed to start session: {response.text}")
                return False

            # Extract new session ID from response
            new_session_id: str = response.json().get("session_id", "")

            client.post(
                f"{SINK_URL}/session/map-speaker",
                json={"speaker_id": "speaker_0", "role": "interviewer"},
            )
            client.post(
                f"{SINK_URL}/session/map-speaker",
                json={"speaker_id": "speaker_1", "role": "candidate"},
            )
            client.post(
                f"{SINK_URL}/transcript",
                json={
                    "event_type": "session_started",
                    "text": None,
                    "timestamp_utc": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "speaker_id": None,
                    "metadata": {"provider": "deepgram"},
                },
            )

            st.session_state.running = True
            st.session_state.index = 0
            st.session_state.messages = []
            st.session_state.seen_transcript_keys = set()
            st.session_state.checklist = {
                item["id"]: ChecklistStatus.PENDING for item in CHECKLIST_ITEMS
            }
            st.session_state.audio_offset = 0.0
            st.session_state.analysis_count = 0
            # Set to new session ID so poll_analysis doesn't detect change and clear
            st.session_state.current_session_id = new_session_id
            return True

    except httpx.ConnectError as e:
        st.error(f"Connection error: Cannot reach sink at {SINK_URL}")
        logger.error("Start simulation connection error: %s", e)
    except httpx.TimeoutException as e:
        st.error("Connection timed out. Is the sink running?")
        logger.error("Start simulation timeout: %s", e)
    return False


def stop_sim() -> None:
    """
    Stop the current simulation session.

    Sends session_stopped event and ends the session on the sink.
    """
    st.session_state.running = False
    try:
        with httpx.Client(timeout=5.0) as client:
            client.post(
                f"{SINK_URL}/transcript",
                json={
                    "event_type": "session_stopped",
                    "text": None,
                    "timestamp_utc": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "speaker_id": None,
                    "metadata": {"provider": "deepgram"},
                },
            )
            client.post(f"{SINK_URL}/session/end")
    except httpx.RequestError as e:
        logger.debug("Error stopping simulation: %s", e)


def send_msg() -> bool:
    """
    Send the next message in the interview script.

    Advances the simulation by one message, updating checklist state
    and session tracking.

    Returns:
        True if message was sent successfully, False otherwise.
    """
    if st.session_state.index >= len(INTERVIEW_SCRIPT):
        stop_sim()
        return False

    speaker_id, text = INTERVIEW_SCRIPT[st.session_state.index]
    event = generate_event(speaker_id, text, st.session_state.audio_offset)

    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS) as client:
            response = client.post(f"{SINK_URL}/transcript", json=event)
            if response.status_code == 200:
                role = "Interviewer" if speaker_id == "speaker_0" else "Candidate"
                timestamp_str = event.get("timestamp_utc", "")
                if isinstance(timestamp_str, str):
                    formatted_time = fmt_time(timestamp_str)
                else:
                    formatted_time = ""

                st.session_state.messages.append(
                    ChatMessage(
                        id=str(uuid.uuid4()),
                        msg_type="transcript",
                        content=text,
                        speaker=role,
                        timestamp=formatted_time,
                        session_id=st.session_state.current_session_id or "",
                    )
                )

                st.session_state.audio_offset += (
                    len(text.split()) * MS_PER_WORD + PAUSE_BETWEEN_MESSAGES_MS
                )
                st.session_state.index += 1
                return True

    except httpx.ConnectError as e:
        st.error(f"Connection error: {e}")
        logger.error("Send message connection error: %s", e)
    except httpx.TimeoutException as e:
        st.error(f"Request timed out: {e}")
        logger.error("Send message timeout: %s", e)
    return False


def poll_analysis() -> None:
    """
    Poll for new analysis results and update the chat display.

    Reads analysis from the sink API and inserts coaching messages
    after their corresponding transcript messages.
    """
    try:
        # Get current session ID to find the right analysis file
        session = fetch_session()
        if not session:
            return
        sync_checklist_state(session)
        sync_live_transcript(session)

        session_data = session.get("session", {})
        if not isinstance(session_data, dict) or not session_data.get("active"):
            return

        session_id = track_session(session_data)
        if not session_id:
            return

        analysis_payload = fetch_analysis(session_id)
        if not analysis_payload:
            return

        data = analysis_payload.get("analysis")
        if not isinstance(data, dict):
            return

        items = data.get("analysis_items", [])
        if not isinstance(items, list):
            return

        if len(items) > st.session_state.analysis_count:
            for item in items[st.session_state.analysis_count :]:
                if not isinstance(item, dict):
                    continue

                response_text = item.get("response_text", "")
                raw_model_output = item.get("raw_model_output", {})
                reasoning_text = ""
                if isinstance(raw_model_output, dict):
                    raw_reasoning = raw_model_output.get("reasoning")
                    if isinstance(raw_reasoning, str):
                        reasoning_text = raw_reasoning.strip()

                bubble_text = reasoning_text or "Analysis received."

                # Create the coaching message
                coaching_msg = ChatMessage(
                    id=item.get("response_id", str(uuid.uuid4())),
                    msg_type="analysis",
                    content=bubble_text,
                    speaker="Coach",
                    timestamp=fmt_time(item.get("timestamp_utc", "")),
                    session_id=session_id,
                    relevance=item.get("relevance_score"),
                    clarity=item.get("clarity_score"),
                    key_points=item.get("key_points", []),
                    follow_ups=item.get("follow_up_suggestions", []),
                    related_text=response_text if isinstance(response_text, str) else "",
                )

                # Find the transcript message this analysis belongs to
                insert_index = -1
                for i, msg in enumerate(st.session_state.messages):
                    if msg.msg_type == "transcript" and msg.content == response_text:
                        next_idx = i + 1
                        if (
                            next_idx < len(st.session_state.messages)
                            and st.session_state.messages[next_idx].msg_type
                            == "analysis"
                        ):
                            insert_index = -1
                        else:
                            insert_index = next_idx
                        break

                if insert_index >= 0:
                    st.session_state.messages.insert(insert_index, coaching_msg)

            st.session_state.analysis_count = len(items)

    except ValueError as e:
        logger.debug("Invalid analysis payload: %s", e)


# =============================================================================
# Main Application
# =============================================================================

def main() -> None:
    """
    Main Streamlit application entry point.

    Renders the interview simulation UI with:
    - Header with simulation controls (start/stop/restart)
    - Left panel: Session info and participant details
    - Center panel: Live transcript and coaching analysis
    - Right panel: Interview checklist with progress tracking
    """
    init_state()
    active_session = fetch_session()
    active_session_data = (
        active_session.get("session", {})
        if isinstance(active_session, dict)
        else {}
    )
    live_active = isinstance(active_session_data, dict) and bool(active_session_data.get("active"))
    live_candidate = active_session_data.get("candidate_name") if live_active else None
    live_started_at = active_session_data.get("started_at") if live_active else None

    # Header with controls
    col_h1, col_h2, col_h3, col_h4, col_h5 = st.columns([3, 1, 1, 1, 2])
    with col_h1:
        st.markdown(f"### {PRODUCT_SPEC.ui.header_title}")
    with col_h2:
        if st.button("▶️ Demo", disabled=st.session_state.running, type="primary"):
            if check_sink():
                start_sim()
                st.rerun()
            else:
                st.error("Sink unavailable")
    with col_h3:
        if st.button("⏹️ End Demo", disabled=not st.session_state.running):
            stop_sim()
            st.rerun()
    with col_h4:
        if st.button("🔄 Restart Demo"):
            if st.session_state.running:
                stop_sim()
            time.sleep(POST_RESTART_DELAY_SECONDS)
            start_sim()
            st.rerun()
    with col_h5:
        sink_ok = check_sink()
        status = "🟢 Connected" if sink_ok else "🔴 Disconnected"
        status += f" | {PRODUCT_SPEC.product_id}:{INSTANCE_ID}"
        if st.session_state.running:
            status += f" | Demo ({st.session_state.index}/{len(INTERVIEW_SCRIPT)})"
        elif live_active:
            live_name = live_candidate if isinstance(live_candidate, str) and live_candidate else "Active"
            status += f" | Live: {live_name}"
        st.markdown(
            f"<div style='text-align:right;padding-top:0.5rem;'>{status}</div>",
            unsafe_allow_html=True,
        )

    st.divider()

    if live_active:
        candidate_label = live_candidate if isinstance(live_candidate, str) and live_candidate else "Unknown"
        started_label = fmt_time(live_started_at) if isinstance(live_started_at, str) else "Unknown"
        st.info(f"Live meeting active for {candidate_label}. Started at {started_label}.")
    elif st.session_state.running:
        st.caption("Demo mode is running against the sink.")
    
    # Three columns
    col_left, col_center, col_right = st.columns([1, 2.5, 1.5])
    
    # LEFT: Session Info
    with col_left:
        with st.container(border=True, height=500):
            st.markdown("**📋 Session Info**")
            
            if active_session:
                sync_checklist_state(active_session)
                sync_live_transcript(active_session)
            if live_active:
                s = active_session_data
                st.caption("Meeting ID")
                st.text(s.get("session_id", "N/A")[:20] + "...")
                
                st.caption("Status")
                st.text(f"Active since {fmt_time(s.get('started_at', ''))}")
                
                st.caption("Participants")
                participants = get_detected_participants(s)
                if participants:
                    for name, speaker_id in participants:
                        icon = "👤" if name == s.get("candidate_name", "Unknown") else "🎤"
                        st.markdown(f"{icon} **{name}** ({speaker_id})")
                else:
                    st.caption("No diarized speakers detected yet.")

                st.caption("Identity source")
                st.text("Diarization speaker IDs; not Teams roster names")

                st.caption("Statistics")
                c1, c2 = st.columns(2)
                c1.metric("Events", s.get("total_events", 0))
                c2.metric("Analyses", s.get("analysis_count", 0))
            else:
                st.info("No live meeting detected. Use Demo to test the UI.")
    
    # CENTER: Chat
    with col_center:
        with st.container(border=True, height=500):
            st.markdown("**💬 Interview Transcript & Analysis**")
            
            # Filter messages to only show those from the current session
            current_sid = st.session_state.current_session_id or ""
            session_messages = [m for m in st.session_state.messages if getattr(m, 'session_id', '') == current_sid]
            
            if not session_messages:
                if live_active:
                    st.info("Live session active. Waiting for transcript turns...")
                else:
                    st.info("No transcript yet. Start Demo or wait for live meeting audio.")
            else:
                for msg in session_messages:
                        if msg.msg_type == "transcript":
                            st.markdown(f"""
                            <div class="transcript-bubble">
                                <strong>{msg.speaker}:</strong> {msg.content}
                            </div>
                            <div class="bubble-meta">{msg.timestamp}</div>
                            """, unsafe_allow_html=True)
                        else:
                            scores = ""
                            if msg.relevance is not None:
                                scores += f"Rel: {msg.relevance:.2f} | "
                            if msg.clarity is not None:
                                scores += f"Clar: {msg.clarity:.2f}"
                            
                            # Show context - the snippet being analyzed
                            context_html = ""
                            if msg.related_text:
                                snippet = (
                                    msg.related_text[:120] + "..."
                                    if len(msg.related_text) > 120
                                    else msg.related_text
                                )
                                context_html = f'<div style="font-size:0.7rem;color:#065F46;font-style:italic;margin-bottom:0.5rem;padding:0.4rem;background:rgba(255,255,255,0.5);border-radius:4px;border-left:2px solid #10B981;">Re: "{snippet}"</div>'

                            reasoning_html = ""
                            if msg.content:
                                reasoning_html = (
                                    '<div style="margin-top:0.35rem;font-size:0.86rem;'
                                    'line-height:1.45;color:#064E3B;">'
                                    f"{msg.content}"
                                    "</div>"
                                )
                            
                            # NEW observations (key points)
                            observations = ""
                            if msg.key_points:
                                observations = '<div style="margin-top:0.5rem;"><strong style="font-size:0.75rem;color:#064E3B;">📋 New Observations:</strong><ul style="margin:0.25rem 0 0 1rem;padding:0;font-size:0.8rem;">'
                                for pt in msg.key_points:
                                    observations += f"<li>{pt}</li>"
                                observations += "</ul></div>"
                            
                            # Coaching tips (follow-up suggestions) - PROMINENT
                            coaching = ""
                            if hasattr(msg, 'follow_ups') and msg.follow_ups:
                                coaching = '<div style="margin-top:0.5rem;padding:0.5rem;background:#FEF3C7;border-radius:6px;border-left:3px solid #F59E0B;"><strong style="font-size:0.75rem;color:#92400E;">💡 Coach Tips:</strong><ul style="margin:0.25rem 0 0 1rem;padding:0;font-size:0.8rem;color:#78350F;">'
                                for tip in msg.follow_ups:
                                    coaching += f"<li>{tip}</li>"
                                coaching += "</ul></div>"
                            
                            st.markdown(f"""
                            <div class="analysis-bubble">
                                <div style="font-size:0.7rem;color:#047857;margin-bottom:0.25rem;">{scores}</div>
                                <div style="font-size:0.9rem;font-weight:600;">🎯 Interview Coach</div>
                                {context_html}
                                {reasoning_html}
                                {observations}
                                {coaching}
                            </div>
                            <div class="bubble-meta" style="text-align:right;">{msg.timestamp}</div>
                            """, unsafe_allow_html=True)
    
    # RIGHT: Checklist
    with col_right:
        with st.container(border=True, height=500):
            st.markdown("**✅ Interview Checklist**")
            
            for item in CHECKLIST_ITEMS:
                status = st.session_state.checklist.get(item["id"], ChecklistStatus.PENDING)
                
                if status == ChecklistStatus.COMPLETE:
                    item_class = "checklist-item checklist-complete"
                    light_class = "stoplight stoplight-complete"
                elif status == ChecklistStatus.ANALYZING:
                    item_class = "checklist-item checklist-analyzing"
                    light_class = "stoplight stoplight-analyzing"
                else:
                    item_class = "checklist-item"
                    light_class = "stoplight stoplight-pending"
                
                st.markdown(f"""
                <div class="{item_class}">
                    <div class="{light_class}"></div>
                    <span style="font-size:0.875rem;">{item["label"]}</span>
                </div>
                """, unsafe_allow_html=True)
            
            # Progress
            complete = sum(1 for s in st.session_state.checklist.values() if s == ChecklistStatus.COMPLETE)
            total = len(CHECKLIST_ITEMS)
            st.divider()
            st.progress(complete / total, text=f"{complete} of {total} complete")
    
    # Simulation loop
    # NOTE: Using time.sleep() blocks the Streamlit thread, which is a known
    # limitation. In production, consider using st.fragment (Streamlit 1.33+)
    # for partial reruns, or async patterns with custom components.
    if st.session_state.running:
        if st.session_state.index < len(INTERVIEW_SCRIPT):
            send_msg()
            poll_analysis()
            delay = random.uniform(MIN_DELAY_SECONDS, MAX_DELAY_SECONDS)
            time.sleep(delay)
            st.rerun()
        else:
            stop_sim()
            st.rerun()
    else:
        poll_analysis()
        time.sleep(IDLE_POLL_DELAY_SECONDS)
        st.rerun()


if __name__ == "__main__":
    main()
