"""
Tests for variant plugin registry and behavior.
"""

from __future__ import annotations

import pytest

from interview_agent.models import AnalysisItem
from variants import available_variants, load_variant


def test_available_variants_contains_required_defaults() -> None:
    variants = available_variants()
    assert "default" in variants
    assert "behavioral" in variants


def test_load_unknown_variant_fails_fast() -> None:
    with pytest.raises(ValueError, match="Unknown variant"):
        load_variant("does-not-exist")


def test_behavioral_variant_enriches_follow_up_suggestions() -> None:
    behavioral = load_variant("behavioral")
    item = AnalysisItem(
        response_id="resp_1",
        response_text="Candidate response",
        relevance_score=0.8,
        clarity_score=0.75,
        follow_up_suggestions=[],
    )

    transformed = behavioral.transform_analysis_item(item)
    assert len(transformed.follow_up_suggestions) >= 1
    assert "ownership" in transformed.follow_up_suggestions[-1].lower()
