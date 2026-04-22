"""
Alfred variant — passive meeting assistant with chat read/send.

Alfred reads the unified meeting timeline (transcript turns + chat messages)
and on each tick emits one of three actions: SILENT, SEND, or ASK. SILENT
means note-taking only; SEND/ASK post a message to the meeting chat via
the bot's proactive-messaging endpoint.

The persona, prompt, and decision rules are driven by the product spec
(see legionmeet_platform/specs/alfred.yaml), not hardcoded here.
"""

from __future__ import annotations

from typing import Any

from meeting_agent.models import AnalysisItem, ChatMessage, MeetingEvent, TranscriptEvent
from variants.base import BaseVariantPlugin, ChecklistItem, VariantUiConfig


class AlfredVariantPlugin(BaseVariantPlugin):
    """Passive assistant + chat I/O."""

    variant_id = "alfred"
    display_name = "Alfred — Meeting Assistant"
    ui = VariantUiConfig(
        page_title="Alfred — Meeting Assistant",
        page_icon="🦉",
        header_title="🦉 Alfred",
        candidate_name="",
        meeting_url="",
        interview_script=tuple(),
        checklist_items=(
            ChecklistItem("agenda", "Agenda established", ("agenda", "goal", "objective")),
            ChecklistItem("decisions", "Decisions captured", ("decided", "agreed", "conclusion")),
            ChecklistItem("actions", "Action items captured", ("action item", "will do", "by next", "follow up")),
            ChecklistItem("next_steps", "Next steps defined", ("next steps", "next meeting", "follow-up")),
        ),
    )

    def build_analysis_context(
        self,
        base_context: dict[str, Any],
        event: TranscriptEvent | ChatMessage | MeetingEvent,
    ) -> dict[str, Any]:
        enriched = dict(base_context)
        enriched["assistant_mode"] = "alfred"
        enriched["action_menu"] = ["SILENT", "SEND", "ASK"]
        enriched["bias_toward_silence"] = True
        if isinstance(event, MeetingEvent):
            enriched["trigger_kind"] = "chat" if event.kind == "chat" else "speech"
        else:
            enriched["trigger_kind"] = "chat" if isinstance(event, ChatMessage) else "speech"
        return enriched

    def transform_analysis_item(self, analysis_item: AnalysisItem) -> AnalysisItem:
        # Alfred populates alfred_action directly in the agent layer; nothing
        # to transform here, but returning the item allows future augmentation
        # (e.g., rate-limit-aware downgrades from SEND to SILENT).
        return analysis_item
