#!/usr/bin/env python3
"""
Alfred — note-taking UI for the LegionMeet sink.

Two-column workspace:
  - Left 60%: unified meeting timeline (speech + chat + Alfred sends), ordered
    by timestamp. Each entry is a card with speaker, role badge, and modality
    icon.
  - Right 40%: notebook panel (running summary, running notes, topics)
    plus session controls.

Polls /session/status every 0.5s; /session/analysis on a slower cadence.
Compose box posts into the meeting chat on behalf of a human user.

Usage:
    VARIANT_ID=alfred PRODUCT_SPEC_PATH=legionmeet_platform/specs/alfred.yaml \\
        streamlit run streamlit_ui.py
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

import httpx
import streamlit as st

from legionmeet_platform import load_product_spec

logger: logging.Logger = logging.getLogger(__name__)

# =============================================================================
# Configuration
# =============================================================================

PRODUCT_SPEC, PRODUCT_SPEC_PATH = load_product_spec()
INSTANCE_ID: Final[str] = os.environ.get("INSTANCE_ID", PRODUCT_SPEC.product_id)
SINK_URL: Final[str] = os.environ.get("SINK_URL", "http://127.0.0.1:8765")

HTTP_TIMEOUT: Final[float] = 5.0
STATUS_POLL_SECONDS: Final[float] = 0.5
ANALYSIS_POLL_SECONDS: Final[float] = 2.0

ROLE_BADGES: Final[dict[str, str]] = {
    "bot": "🤖 Alfred",
    "candidate": "👤 Subject",
    "interviewer": "🎤 Host",
    "participant": "👥 Participant",
    "unknown": "❓",
}

KIND_ICON: Final[dict[str, str]] = {
    "speech": "🎙️",
    "chat": "💬",
}

# =============================================================================
# Page shell
# =============================================================================

st.set_page_config(
    page_title=PRODUCT_SPEC.ui.page_title,
    page_icon=PRODUCT_SPEC.ui.page_icon,
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown(
    """
    <style>
    #MainMenu, footer, .stDeployButton { visibility: hidden; }
    .block-container { padding-top: 1rem; padding-bottom: 1rem; max-width: 1600px; }
    .alfred-card {
        background: #fff; border: 1px solid #E2E8F0; border-radius: 10px;
        padding: 12px 14px; margin-bottom: 8px;
    }
    .alfred-card.chat { background: #F8FAFC; }
    .alfred-card.bot  { background: #FFFBEB; border-color: #FDE68A; }
    .alfred-card .meta { color: #64748B; font-size: 12px; margin-bottom: 4px; }
    .alfred-card .text { font-size: 15px; line-height: 1.45; color: #0F172A; }
    .alfred-topic-chip {
        display: inline-block; padding: 3px 10px; margin: 2px 4px 2px 0;
        background: #EEF2FF; color: #3730A3; border-radius: 999px; font-size: 12px;
    }
    .alfred-status {
        display: inline-block; padding: 4px 10px; border-radius: 999px;
        font-size: 12px; font-weight: 600;
    }
    .alfred-status.listening { background: #DCFCE7; color: #166534; }
    .alfred-status.muted     { background: #FEE2E2; color: #991B1B; }
    .alfred-status.inactive  { background: #E2E8F0; color: #475569; }
    .alfred-card.bot .meta { color: #92400E; }
    </style>
    """,
    unsafe_allow_html=True,
)

# =============================================================================
# Session state
# =============================================================================


def _init_state() -> None:
    st.session_state.setdefault("alfred_muted", False)
    st.session_state.setdefault("last_status", None)
    st.session_state.setdefault("last_analysis", None)
    st.session_state.setdefault("compose_draft", "")
    st.session_state.setdefault("candidate_name", "")
    st.session_state.setdefault(
        "meeting_url",
        "https://teams.microsoft.com/l/meetup-join/",
    )


_init_state()

# =============================================================================
# Sink client
# =============================================================================


def _sink_get(path: str, timeout: float = HTTP_TIMEOUT) -> dict[str, Any] | None:
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(f"{SINK_URL}{path}")
        if resp.status_code == 200:
            return resp.json()
        logger.warning("GET %s -> %s", path, resp.status_code)
    except httpx.HTTPError as exc:
        logger.debug("GET %s failed: %s", path, exc)
    return None


def _sink_post(path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT) as client:
            resp = client.post(f"{SINK_URL}{path}", json=body)
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, None
    except httpx.HTTPError as exc:
        logger.warning("POST %s failed: %s", path, exc)
        return 0, None


def fetch_status() -> dict[str, Any] | None:
    return _sink_get("/session/status")


def fetch_analysis() -> dict[str, Any] | None:
    return _sink_get("/session/analysis")


def start_session(candidate_name: str, meeting_url: str) -> tuple[int, dict[str, Any] | None]:
    return _sink_post(
        "/session/start",
        {
            "candidate_name": candidate_name or "Meeting",
            "meeting_url": meeting_url or "https://teams.microsoft.com/l/meetup-join/",
            "product_id": PRODUCT_SPEC.product_id,
        },
    )


def end_session() -> tuple[int, dict[str, Any] | None]:
    return _sink_post("/session/end", {})


def post_chat(text: str, as_alfred: bool) -> tuple[int, dict[str, Any] | None]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    msg_id = f"ui_{uuid.uuid4().hex[:10]}"
    return _sink_post(
        "/chat",
        {
            "event_type": "chat_created",
            "chat_thread_id": "ui-simulated-thread",
            "message_id": msg_id,
            "text": text,
            "sender_display_name": "Alfred" if as_alfred else "You",
            "sender_id": "bot-alfred" if as_alfred else "ui-human",
            "timestamp_utc": now,
            "from_bot": as_alfred,
        },
    )


def post_simulated_speech(speaker_id: str, text: str) -> tuple[int, dict[str, Any] | None]:
    return _sink_post(
        "/transcript",
        {
            "event_type": "final",
            "text": text,
            "timestamp_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "speaker_id": speaker_id,
            "confidence": 0.95,
        },
    )


# =============================================================================
# Rendering helpers
# =============================================================================


def render_header(status: dict[str, Any] | None) -> None:
    session = (status or {}).get("session") or {}
    active = bool(session.get("active"))
    muted = bool(st.session_state.get("alfred_muted"))
    badge_class = "listening" if active and not muted else ("muted" if muted else "inactive")
    badge_text = "LISTENING" if active and not muted else ("MUTED" if muted else "IDLE")

    cols = st.columns([6, 2, 1])
    with cols[0]:
        title = PRODUCT_SPEC.ui.header_title or "🦉 Alfred"
        st.markdown(f"### {title}")
        subtitle = session.get("candidate_name") or "No active meeting"
        url = session.get("meeting_url") or ""
        if url:
            st.caption(f"**{subtitle}** · {url}")
        else:
            st.caption(subtitle)
    with cols[1]:
        st.markdown(
            f"<div style='text-align:right;padding-top:14px;'>"
            f"<span class='alfred-status {badge_class}'>{badge_text}</span></div>",
            unsafe_allow_html=True,
        )
    with cols[2]:
        if active:
            if st.button("End", use_container_width=True):
                end_session()
                st.rerun()


def render_timeline(status: dict[str, Any] | None) -> None:
    session = (status or {}).get("session") or {}
    history = session.get("meeting_history") or []

    st.markdown("#### Timeline")
    if not history:
        st.info("No activity yet. Start a session and speak or chat in the meeting.")
        return

    for entry in history[-80:]:
        kind = entry.get("kind") or "speech"
        role = entry.get("role") or "unknown"
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        display_name = (
            entry.get("display_name")
            or entry.get("speaker_id")
            or ROLE_BADGES.get(role, "?")
        )
        ts = entry.get("timestamp_utc") or ""
        clock = ts[11:19] if len(ts) >= 19 else ts
        from_bot = bool(entry.get("from_bot"))

        css_class = "alfred-card"
        if kind == "chat":
            css_class += " chat"
        if from_bot:
            css_class += " bot"

        icon = KIND_ICON.get(kind, "•")
        badge = ROLE_BADGES.get(role, role)
        meta = f"{icon} {badge} · {display_name} · {clock}"
        safe_text = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("\n", "<br>")
        )
        st.markdown(
            f"<div class='{css_class}'>"
            f"<div class='meta'>{meta}</div>"
            f"<div class='text'>{safe_text}</div>"
            f"</div>",
            unsafe_allow_html=True,
        )


def _collect_alfred_notes(analysis: dict[str, Any] | None) -> tuple[str, list[str], list[str]]:
    """Extract (running_summary, notes, topics) from the session analysis payload."""
    if not analysis:
        return "", [], []
    body = (analysis or {}).get("analysis") or {}
    running_summary = body.get("running_summary") or ""
    topics = list(body.get("topics") or [])

    notes: list[str] = []
    for item in body.get("analysis_items") or []:
        action = (item or {}).get("alfred_action") or {}
        for note in action.get("notes") or []:
            if note and note not in notes:
                notes.append(note)
        # Fall back to legacy key_points when alfred_action isn't populated yet.
        if not action:
            for kp in (item or {}).get("key_points") or []:
                if kp and kp not in notes:
                    notes.append(kp)

    return running_summary, notes, topics


def render_notebook(
    status: dict[str, Any] | None,
    analysis: dict[str, Any] | None,
) -> None:
    session = (status or {}).get("session") or {}
    checklist = session.get("checklist") or []
    running_summary, notes, topics = _collect_alfred_notes(analysis)

    st.markdown("#### Summary")
    if running_summary:
        st.markdown(running_summary)
    else:
        st.caption("Alfred will start summarising once speech or chat arrives.")

    st.markdown("#### Notes")
    if notes:
        for i, note in enumerate(notes[-30:]):
            st.markdown(f"- {note}")
    else:
        st.caption("No notes yet.")

    st.markdown("#### Topics")
    if topics:
        chips = " ".join(
            f"<span class='alfred-topic-chip'>{t}</span>" for t in topics[:16]
        )
        st.markdown(chips, unsafe_allow_html=True)
    else:
        st.caption("Topics will appear as the conversation unfolds.")

    if checklist:
        st.markdown("#### Progress")
        for item in checklist:
            status_value = item.get("status") or "pending"
            mark = {
                "complete": "✅",
                "analyzing": "🟡",
                "pending": "⬜",
            }.get(status_value, "⬜")
            st.markdown(f"{mark} {item.get('label') or item.get('id')}")


def render_controls(status: dict[str, Any] | None) -> None:
    session = (status or {}).get("session") or {}
    active = bool(session.get("active"))

    with st.expander("Session", expanded=not active):
        c1, c2 = st.columns([3, 3])
        with c1:
            st.session_state.candidate_name = st.text_input(
                "Meeting label",
                value=st.session_state.candidate_name,
                placeholder="Weekly staff sync",
            )
        with c2:
            st.session_state.meeting_url = st.text_input(
                "Meeting URL",
                value=st.session_state.meeting_url,
            )
        if active:
            st.caption(f"session_id: `{session.get('session_id')}`")
        else:
            if st.button("Start session", type="primary"):
                code, _ = start_session(
                    st.session_state.candidate_name,
                    st.session_state.meeting_url,
                )
                if code == 200:
                    st.rerun()
                else:
                    st.error(f"Start failed (HTTP {code})")

    mute_col, _ = st.columns([2, 3])
    with mute_col:
        st.session_state.alfred_muted = st.toggle(
            "Mute Alfred (never post to chat)",
            value=bool(st.session_state.alfred_muted),
        )

    st.markdown("#### Compose")
    st.session_state.compose_draft = st.text_area(
        "Message",
        value=st.session_state.compose_draft,
        height=80,
        placeholder="Type to send into the meeting chat.",
        label_visibility="collapsed",
    )
    send_cols = st.columns([1, 1, 3])
    with send_cols[0]:
        if st.button("Send as you", disabled=not active):
            text = st.session_state.compose_draft.strip()
            if text:
                code, _ = post_chat(text, as_alfred=False)
                if code == 200:
                    st.session_state.compose_draft = ""
                    st.rerun()
    with send_cols[1]:
        disabled = (not active) or bool(st.session_state.alfred_muted)
        if st.button("Send as Alfred", disabled=disabled):
            text = st.session_state.compose_draft.strip()
            if text:
                code, _ = post_chat(text, as_alfred=True)
                if code == 200:
                    st.session_state.compose_draft = ""
                    st.rerun()

    with st.expander("Simulate speech (demo)"):
        sim_speaker = st.selectbox(
            "Speaker",
            ["speaker_0", "speaker_1", "speaker_2"],
            index=0,
        )
        sim_text = st.text_input("Speech", value="", key="sim_speech_text")
        if st.button("Post simulated speech", disabled=not active):
            if sim_text.strip():
                post_simulated_speech(sim_speaker, sim_text.strip())
                st.session_state.sim_speech_text = ""
                st.rerun()


# =============================================================================
# Main render
# =============================================================================


@st.fragment(run_every=STATUS_POLL_SECONDS)
def _timeline_fragment() -> None:
    status = fetch_status()
    st.session_state.last_status = status
    render_timeline(status)


@st.fragment(run_every=ANALYSIS_POLL_SECONDS)
def _notebook_fragment() -> None:
    status = st.session_state.get("last_status") or fetch_status()
    analysis = fetch_analysis()
    st.session_state.last_analysis = analysis
    render_notebook(status, analysis)


def main() -> None:
    status = st.session_state.get("last_status") or fetch_status()
    render_header(status)

    left, right = st.columns([6, 4], gap="large")
    with left:
        _timeline_fragment()
    with right:
        _notebook_fragment()
        st.divider()
        render_controls(status)

    st.caption(
        f"sink: {SINK_URL} · variant: {os.environ.get('VARIANT_ID', 'alfred')} · "
        f"instance: {INSTANCE_ID} · spec: {PRODUCT_SPEC.product_id}"
    )


if __name__ == "__main__" or True:
    main()
