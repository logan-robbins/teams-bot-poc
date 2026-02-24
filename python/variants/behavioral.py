"""
Behavioral variant plugin.
"""

from __future__ import annotations

from interview_agent.models import AnalysisItem
from variants.base import BaseVariantPlugin, ChecklistItem, VariantUiConfig
from variants.shared_content import DEFAULT_INTERVIEW_SCRIPT


class BehavioralVariantPlugin(BaseVariantPlugin):
    """Behavioral-focused coaching and UI cues."""

    variant_id = "behavioral"
    display_name = "Behavioral Focus"
    ui = VariantUiConfig(
        page_title="Talestral Behavioral Interview Coach",
        page_icon="🧭",
        header_title="🧭 Talestral Behavioral Interview Coach",
        candidate_name="Jordan Lee",
        meeting_url="https://teams.microsoft.com/l/meetup-join/simulated-behavioral",
        interview_script=DEFAULT_INTERVIEW_SCRIPT,
        checklist_items=(
            ChecklistItem("rapport", "Rapport", ("good morning", "thanks", "introduce")),
            ChecklistItem("ownership", "Ownership", ("i redesigned", "i led", "i start")),
            ChecklistItem("collaboration", "Collaboration", ("team", "alignment", "review")),
            ChecklistItem("decision_making", "Decision Making", ("tradeoff", "chose", "balancing")),
            ChecklistItem("communication", "Communication", ("communicate", "status", "questions")),
            ChecklistItem("close", "Wrap-Up", ("success", "technical debt", "questions for me")),
        ),
    )

    def build_analysis_context(
        self,
        base_context: dict[str, object],
        event: object,
    ) -> dict[str, object]:
        enriched = dict(base_context)
        enriched["interview_mode"] = "behavioral"
        enriched["analysis_focus"] = (
            "Prioritize ownership, collaboration, and communication signal."
        )
        return enriched

    def transform_analysis_item(self, analysis_item: AnalysisItem) -> AnalysisItem:
        suggestions = list(analysis_item.follow_up_suggestions)
        if len(suggestions) < 2:
            suggestions.append(
                "Ask for one specific example that proves ownership and collaboration."
            )
        return analysis_item.model_copy(update={"follow_up_suggestions": suggestions})
