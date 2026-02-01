#!/usr/bin/env python3
"""
Streamlit UI for Real-Time Interview Agent Thoughts

Displays the agent's analysis and running assessment in real-time
as the interview progresses.

Usage:
    # Start the transcript sink first:
    uv run python transcript_sink.py

    # In another terminal, start the Streamlit UI:
    uv run streamlit run streamlit_ui.py

    # In a third terminal, run the interview simulation:
    uv run python simulate_interview.py
"""

import streamlit as st
import asyncio
import httpx
import time
from datetime import datetime
from typing import Optional
import json


# =============================================================================
# Configuration
# =============================================================================

SINK_URL = "http://127.0.0.1:8765"
POLL_INTERVAL = 1.0  # seconds


# =============================================================================
# Page Configuration
# =============================================================================

st.set_page_config(
    page_title="Interview Analysis Agent",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =============================================================================
# Custom CSS
# =============================================================================

st.markdown("""
<style>
    .main-header {
        font-size: 2.5rem;
        font-weight: bold;
        color: #1f77b4;
        margin-bottom: 1rem;
    }
    .score-card {
        background-color: #f0f2f6;
        border-radius: 10px;
        padding: 1rem;
        margin: 0.5rem 0;
    }
    .score-high {
        color: #28a745;
        font-weight: bold;
    }
    .score-medium {
        color: #ffc107;
        font-weight: bold;
    }
    .score-low {
        color: #dc3545;
        font-weight: bold;
    }
    .thought-card {
        background-color: #ffffff;
        border-left: 4px solid #1f77b4;
        padding: 1rem;
        margin: 0.5rem 0;
        border-radius: 0 10px 10px 0;
        box-shadow: 0 2px 4px rgba(0,0,0,0.1);
    }
    .assessment-card {
        background-color: #e8f4f8;
        border-radius: 10px;
        padding: 1rem;
        margin: 1rem 0;
    }
    .signal-strong-hire {
        background-color: #d4edda;
        color: #155724;
        padding: 0.5rem 1rem;
        border-radius: 5px;
        font-weight: bold;
    }
    .signal-lean-hire {
        background-color: #cce5ff;
        color: #004085;
        padding: 0.5rem 1rem;
        border-radius: 5px;
        font-weight: bold;
    }
    .signal-lean-no {
        background-color: #fff3cd;
        color: #856404;
        padding: 0.5rem 1rem;
        border-radius: 5px;
        font-weight: bold;
    }
    .signal-strong-no {
        background-color: #f8d7da;
        color: #721c24;
        padding: 0.5rem 1rem;
        border-radius: 5px;
        font-weight: bold;
    }
</style>
""", unsafe_allow_html=True)


# =============================================================================
# Helper Functions
# =============================================================================

def get_score_class(score: float) -> str:
    """Get CSS class based on score value."""
    if score >= 0.8:
        return "score-high"
    elif score >= 0.6:
        return "score-medium"
    else:
        return "score-low"


def get_signal_class(signal: str) -> str:
    """Get CSS class for hire signal."""
    signal_lower = signal.lower()
    if "strong hire" in signal_lower:
        return "signal-strong-hire"
    elif "lean hire" in signal_lower:
        return "signal-lean-hire"
    elif "lean no" in signal_lower:
        return "signal-lean-no"
    elif "strong no" in signal_lower:
        return "signal-strong-no"
    return ""


def format_timestamp(ts: str) -> str:
    """Format ISO timestamp for display."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except:
        return ts


def fetch_session_data() -> Optional[dict]:
    """Fetch current session data from the sink."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{SINK_URL}/session/status")
            if resp.status_code == 200:
                return resp.json()
    except Exception as e:
        st.error(f"Connection error: {e}")
    return None


def fetch_stats() -> Optional[dict]:
    """Fetch current stats from the sink."""
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{SINK_URL}/stats")
            if resp.status_code == 200:
                return resp.json()
    except:
        pass
    return None


def load_analysis_file(session_id: str) -> Optional[dict]:
    """Load analysis JSON file for a session."""
    try:
        from pathlib import Path
        analysis_path = Path(__file__).parent / "output" / f"{session_id}_analysis.json"
        if analysis_path.exists():
            with open(analysis_path, "r") as f:
                return json.load(f)
    except Exception as e:
        st.warning(f"Could not load analysis file: {e}")
    return None


# =============================================================================
# Sidebar
# =============================================================================

def render_sidebar():
    """Render the sidebar with session info and controls."""
    st.sidebar.markdown("## 🎯 Interview Agent")
    st.sidebar.markdown("---")
    
    # Connection status
    stats = fetch_stats()
    if stats:
        st.sidebar.success("✅ Connected to Sink")
        st.sidebar.markdown(f"**Events received:** {stats['stats']['events_received']}")
        st.sidebar.markdown(f"**Final transcripts:** {stats['stats']['final_transcripts']}")
        st.sidebar.markdown(f"**Agent analyses:** {stats['stats']['agent_analyses']}")
        
        if stats.get("agent_available"):
            st.sidebar.success("🤖 Agent Active (GPT-5)")
        else:
            st.sidebar.warning("⚠️ Agent Unavailable")
    else:
        st.sidebar.error("❌ Cannot connect to sink")
        st.sidebar.markdown("Start the sink with:")
        st.sidebar.code("uv run python transcript_sink.py")
    
    st.sidebar.markdown("---")
    
    # Session info
    session_data = fetch_session_data()
    if session_data and session_data.get("session", {}).get("active"):
        session = session_data["session"]
        st.sidebar.markdown("### 📋 Active Session")
        st.sidebar.markdown(f"**Candidate:** {session.get('candidate_name', 'Unknown')}")
        st.sidebar.markdown(f"**Session ID:** `{session.get('session_id', 'N/A')[:20]}...`")
        st.sidebar.markdown(f"**Total Events:** {session.get('total_events', 0)}")
        st.sidebar.markdown(f"**Analyses:** {session.get('analysis_count', 0)}")
        
        # Speaker mappings
        mappings = session.get("speaker_mappings", {})
        if mappings:
            st.sidebar.markdown("**Speakers:**")
            for speaker_id, role in mappings.items():
                icon = "👤" if role == "candidate" else "🎤"
                st.sidebar.markdown(f"  {icon} {speaker_id}: {role}")
    else:
        st.sidebar.markdown("### 📋 No Active Session")
        st.sidebar.markdown("Start a session and run the simulator:")
        st.sidebar.code("uv run python simulate_interview.py")
    
    st.sidebar.markdown("---")
    
    # Auto-refresh toggle
    auto_refresh = st.sidebar.checkbox("🔄 Auto-refresh", value=True)
    refresh_rate = st.sidebar.slider("Refresh interval (s)", 1, 10, 2) if auto_refresh else 2
    
    return auto_refresh, refresh_rate


# =============================================================================
# Main Content
# =============================================================================

def render_running_assessment(assessment: dict):
    """Render the running assessment card."""
    st.markdown("### 📊 Running Assessment")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("#### Competencies")
        st.markdown(f"**Technical:** {assessment.get('technical_competence', 'N/A')}")
        st.markdown(f"**Communication:** {assessment.get('communication', 'N/A')}")
        st.markdown(f"**Problem Solving:** {assessment.get('problem_solving', 'N/A')}")
        st.markdown(f"**Culture Fit:** {assessment.get('culture_fit', 'N/A')}")
    
    with col2:
        # Overall signal
        signal = assessment.get("overall_signal", "Too early to tell")
        signal_class = get_signal_class(signal)
        st.markdown("#### Hiring Signal")
        st.markdown(f'<div class="{signal_class}">{signal}</div>', unsafe_allow_html=True)
        
        # Stats
        st.markdown(f"**Responses Analyzed:** {assessment.get('responses_analyzed', 0)}")
        avg_rel = assessment.get('avg_relevance', 0)
        avg_clar = assessment.get('avg_clarity', 0)
        st.markdown(f"**Avg Relevance:** {avg_rel:.2f}")
        st.markdown(f"**Avg Clarity:** {avg_clar:.2f}")
    
    # Strengths and concerns
    col3, col4 = st.columns(2)
    
    with col3:
        strengths = assessment.get("key_strengths", [])
        if strengths:
            st.markdown("#### ✅ Key Strengths")
            for strength in strengths:
                st.markdown(f"• {strength}")
    
    with col4:
        concerns = assessment.get("areas_of_concern", [])
        if concerns:
            st.markdown("#### ⚠️ Areas of Concern")
            for concern in concerns:
                st.markdown(f"• {concern}")


def render_analysis_item(item: dict, index: int):
    """Render a single analysis item."""
    with st.expander(
        f"Response #{index + 1} - Relevance: {item['relevance_score']:.2f} | Clarity: {item['clarity_score']:.2f}",
        expanded=(index == 0)
    ):
        col1, col2 = st.columns([2, 1])
        
        with col1:
            # Response text
            st.markdown("**Candidate Response:**")
            st.markdown(f"> {item.get('response_text', 'N/A')[:300]}...")
            
            # Question if identified
            question = item.get("question_text")
            if question:
                st.markdown(f"**Responding to:** {question}")
        
        with col2:
            # Scores
            rel_class = get_score_class(item['relevance_score'])
            clar_class = get_score_class(item['clarity_score'])
            
            st.markdown("**Scores:**")
            st.markdown(f'Relevance: <span class="{rel_class}">{item["relevance_score"]:.2f}</span>', unsafe_allow_html=True)
            st.markdown(f'Clarity: <span class="{clar_class}">{item["clarity_score"]:.2f}</span>', unsafe_allow_html=True)
        
        # Key points
        key_points = item.get("key_points", [])
        if key_points:
            st.markdown("**Key Points:**")
            for point in key_points:
                st.markdown(f"• {point}")
        
        # Follow-up suggestions
        follow_ups = item.get("follow_up_suggestions", [])
        if follow_ups:
            st.markdown("**Suggested Follow-ups:**")
            for suggestion in follow_ups:
                st.markdown(f"• {suggestion}")
        
        # Reasoning
        raw_output = item.get("raw_model_output", {})
        reasoning = raw_output.get("reasoning")
        if reasoning:
            st.markdown("**Agent Reasoning:**")
            st.info(reasoning)


def render_analysis_list(analysis_data: dict):
    """Render the list of all analyses."""
    items = analysis_data.get("analysis_items", [])
    
    if not items:
        st.info("No analyses yet. Waiting for candidate responses...")
        return
    
    # Show in reverse order (newest first)
    for i, item in enumerate(reversed(items)):
        render_analysis_item(item, len(items) - i - 1)


def main():
    """Main application."""
    # Header
    st.markdown('<div class="main-header">🎯 Interview Analysis Agent</div>', unsafe_allow_html=True)
    st.markdown("Real-time candidate assessment powered by GPT-5")
    st.markdown("---")
    
    # Sidebar
    auto_refresh, refresh_rate = render_sidebar()
    
    # Main content area
    session_data = fetch_session_data()
    
    if session_data and session_data.get("session", {}).get("active"):
        session = session_data["session"]
        session_id = session.get("session_id")
        
        # Candidate header
        st.markdown(f"## 👤 Candidate: {session.get('candidate_name', 'Unknown')}")
        
        # Load analysis file
        analysis_data = load_analysis_file(session_id) if session_id else None
        
        if analysis_data:
            # Running assessment (from most recent analysis item)
            items = analysis_data.get("analysis_items", [])
            if items:
                latest_item = items[-1]
                raw_output = latest_item.get("raw_model_output", {})
                running_assessment = raw_output.get("running_assessment")
                
                if running_assessment:
                    render_running_assessment(running_assessment)
                    st.markdown("---")
            
            # Overall stats
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric(
                    "Total Responses",
                    analysis_data.get("total_responses_analyzed", 0)
                )
            with col2:
                overall_rel = analysis_data.get("overall_relevance")
                st.metric(
                    "Avg Relevance",
                    f"{overall_rel:.2f}" if overall_rel else "N/A"
                )
            with col3:
                overall_clar = analysis_data.get("overall_clarity")
                st.metric(
                    "Avg Clarity",
                    f"{overall_clar:.2f}" if overall_clar else "N/A"
                )
            
            st.markdown("---")
            
            # Analysis list
            st.markdown("### 📝 Response Analyses")
            render_analysis_list(analysis_data)
        else:
            st.info("Waiting for candidate responses and agent analysis...")
            st.markdown("""
            The agent will analyze each candidate response in real-time, providing:
            - **Relevance Score**: How well they addressed the question
            - **Clarity Score**: How well they communicated
            - **Key Points**: Important takeaways from their response
            - **Follow-up Suggestions**: Questions to probe deeper
            - **Running Assessment**: Overall candidate evaluation
            """)
    
    else:
        # No active session
        st.markdown("## 👋 Welcome to the Interview Analysis Agent")
        st.markdown("""
        This tool provides real-time analysis of candidate responses during interviews.
        
        ### Getting Started
        
        1. **Start the transcript sink:**
        ```bash
        uv run python transcript_sink.py
        ```
        
        2. **Start this Streamlit UI:**
        ```bash
        uv run streamlit run streamlit_ui.py
        ```
        
        3. **Run the interview simulator:**
        ```bash
        uv run python simulate_interview.py
        ```
        
        The agent will analyze candidate responses as they come in, maintaining a running
        assessment of the candidate's technical competence, communication skills, and overall fit.
        """)
        
        # Show demo data
        st.markdown("---")
        st.markdown("### 📊 Demo: What You'll See")
        
        with st.expander("Example Analysis", expanded=True):
            st.markdown("""
            **Candidate Response:**
            > "I have 5 years of Python experience, primarily in backend development. 
            > Most recently, I built a real-time analytics pipeline using FastAPI and Apache Kafka 
            > that processes over 2 million events per day."
            
            **Agent Analysis:**
            - **Relevance Score:** 0.92
            - **Clarity Score:** 0.88
            - **Key Points:**
              • 5 years of Python experience
              • Backend development focus
              • Real-time systems experience (FastAPI, Kafka)
              • Quantifiable impact (2M events/day)
            - **Follow-up Suggestion:** "Can you describe a specific challenge you faced building that pipeline?"
            """)
    
    # Auto-refresh
    if auto_refresh:
        time.sleep(refresh_rate)
        st.rerun()


if __name__ == "__main__":
    main()
