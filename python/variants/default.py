"""
Default variant plugin.
"""

from __future__ import annotations

from variants.base import BaseVariantPlugin, ChecklistItem, VariantUiConfig
from variants.shared_content import DEFAULT_INTERVIEW_SCRIPT


class DefaultVariantPlugin(BaseVariantPlugin):
    """Balanced general interview analysis + UI."""

    variant_id = "default"
    display_name = "Default"
    ui = VariantUiConfig(
        page_title="Talestral Interview Agent",
        page_icon="🎯",
        header_title="🎯 Talestral Interview Agent",
        candidate_name="Sarah Chen",
        meeting_url="https://teams.microsoft.com/l/meetup-join/simulated-default",
        interview_script=DEFAULT_INTERVIEW_SCRIPT,
        checklist_items=(
            ChecklistItem("intro", "Intro", ("good morning", "introduce", "background")),
            ChecklistItem("experience", "Experience", ("experience", "project", "scale")),
            ChecklistItem("tradeoffs", "Tradeoffs", ("tradeoff", "consistency", "throughput")),
            ChecklistItem("debugging", "Debugging", ("incident", "debug", "telemetry")),
            ChecklistItem("quality", "Code Quality", ("tests", "typing", "review")),
            ChecklistItem("close", "Closing", ("questions for me", "success", "technical debt")),
        ),
    )
