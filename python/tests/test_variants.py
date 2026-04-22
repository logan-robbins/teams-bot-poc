"""Tests for the variant plugin registry."""

from __future__ import annotations

import pytest

from meeting_agent.models import AnalysisItem, AlfredAction, ChatMessage, TranscriptEvent
from variants import available_variants, load_variant


def test_alfred_is_the_only_registered_variant() -> None:
    variants = available_variants()
    assert variants == ("alfred",)


def test_load_unknown_variant_fails_fast() -> None:
    with pytest.raises(ValueError, match="Unknown variant"):
        load_variant("does-not-exist")


def test_alfred_context_marks_trigger_kind_for_speech() -> None:
    alfred = load_variant("alfred")
    event = TranscriptEvent(event_type="final", timestamp_utc="2026-04-22T16:00:00Z")
    ctx = alfred.build_analysis_context({}, event)
    assert ctx["trigger_kind"] == "speech"
    assert ctx["assistant_mode"] == "alfred"
    assert "SILENT" in ctx["action_menu"]
    assert ctx["bias_toward_silence"] is True


def test_alfred_context_marks_trigger_kind_for_chat() -> None:
    alfred = load_variant("alfred")
    message = ChatMessage(
        chat_thread_id="19:m@thread.v2",
        message_id="m1",
        timestamp_utc="2026-04-22T16:00:00Z",
        text="hi",
    )
    ctx = alfred.build_analysis_context({}, message)
    assert ctx["trigger_kind"] == "chat"


def test_alfred_transform_passes_through() -> None:
    alfred = load_variant("alfred")
    item = AnalysisItem(
        response_id="r1",
        response_text="Sample response",
        alfred_action=AlfredAction(
            action="SILENT",
            rationale="not enough signal yet",
            running_summary="",
            topics=[],
        ),
    )
    transformed = alfred.transform_analysis_item(item)
    assert transformed.alfred_action is not None
    assert transformed.alfred_action.action == "SILENT"
