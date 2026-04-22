"""Tests for the Alfred agent tools."""

from __future__ import annotations

import pytest

from meeting_agent.session import InterviewSessionManager
from meeting_agent.tools import AlfredAgentContext, send_to_meeting_chat_impl


def _make_context(
    *, muted: bool = False, capture_ref: bool = True, send_url: str | None = None
) -> AlfredAgentContext:
    sm = InterviewSessionManager()
    session = sm.start_session(
        "Weekly staff sync",
        "https://teams.microsoft.com/l/meetup-join/t",
    )
    session.alfred_muted = muted
    if capture_ref:
        session.conversation_reference_id = "conv-ref-123"
    return AlfredAgentContext(session_manager=sm, send_chat_url=send_url)


@pytest.mark.asyncio
async def test_tool_rejects_empty_text() -> None:
    ctx = _make_context()
    result = await send_to_meeting_chat_impl(ctx, text="   ")
    assert result.ok is False
    assert result.reason == "empty_text"


@pytest.mark.asyncio
async def test_tool_refuses_when_muted() -> None:
    ctx = _make_context(muted=True)
    result = await send_to_meeting_chat_impl(ctx, text="I have something useful.")
    assert result.ok is False
    assert result.reason == "muted"


@pytest.mark.asyncio
async def test_tool_refuses_without_conversation_reference() -> None:
    ctx = _make_context(capture_ref=False)
    result = await send_to_meeting_chat_impl(ctx, text="hello there")
    assert result.ok is False
    assert result.reason == "no_conversation_reference"


@pytest.mark.asyncio
async def test_tool_dry_runs_when_no_send_url_and_logs_intent() -> None:
    ctx = _make_context()
    result = await send_to_meeting_chat_impl(ctx, text="Noted.")
    assert result.ok is True
    assert result.posted_at is not None
    assert result.message_id is not None

    session = ctx.session_manager.session
    assert session is not None
    # Intent logged for echo suppression.
    assert len(session.outbound_chat_intents) == 1
    assert session.outbound_chat_intents[0].text == "Noted."
    # Alfred's own utterance appended into the canonical ledger.
    assert any(ev.from_bot and ev.source == "alfred" for ev in session.meeting_events)
    # Tool record captured.
    assert len(ctx.tool_records) == 1
    assert ctx.tool_records[0].tool_name == "send_to_meeting_chat"
    assert ctx.tool_records[0].ok is True


@pytest.mark.asyncio
async def test_question_kind_auto_appends_question_mark() -> None:
    ctx = _make_context()
    result = await send_to_meeting_chat_impl(ctx, text="who owns this", kind="question")
    assert result.ok is True
    session = ctx.session_manager.session
    assert session is not None
    last = session.meeting_events[-1]
    assert last.text.endswith("?")
    assert last.from_bot is True
