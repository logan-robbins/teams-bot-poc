"""Tests for E4: explicit proactivity policy.

Confirms (a) the policy is rendered into the agent's stable instructions
prefix so it benefits from prompt caching, and (b) the instructions retain
the existing 'strong bias toward silence' line — the rules are exceptions,
not a green light.
"""

from __future__ import annotations

from meeting_agent.agent import AlfredAnalyzer


BASE_INSTRUCTIONS = (
    "You are Alfred. Strong bias toward silence by default.\n"
    "Never interrupt flow. Never narrate."
)


def test_policy_rules_appended_to_instructions() -> None:
    policy = {
        "cooldown_seconds": 45,
        "directly_addressed_bypass": True,
        "rules": [
            {
                "id": "missing_owner",
                "when": "action_item.owner is null",
                "ask": "Who's owning this one?",
            },
            {
                "id": "missing_due",
                "when": "action_item.due is null",
                "ask": "When do we need this by?",
            },
        ],
    }
    composed = AlfredAnalyzer._compose_instructions(BASE_INSTRUCTIONS, policy)

    # Original silence-bias preserved.
    assert "Strong bias toward silence" in composed
    # New section header rendered.
    assert "## When to break silence" in composed
    # Each rule's ask is present.
    assert "Who's owning this one?" in composed
    assert "When do we need this by?" in composed
    # Cooldown message present.
    assert "45" in composed
    assert "directly addresses" in composed.lower() or "alfred" in composed.lower()


def test_policy_with_no_rules_is_no_op() -> None:
    composed = AlfredAnalyzer._compose_instructions(BASE_INSTRUCTIONS, None)
    assert composed == BASE_INSTRUCTIONS

    composed = AlfredAnalyzer._compose_instructions(
        BASE_INSTRUCTIONS, {"cooldown_seconds": 45}
    )
    # No `rules` => skip rendering entirely.
    assert composed == BASE_INSTRUCTIONS


def test_alfred_yaml_loads_intervention_policy() -> None:
    """Smoke-test that the production spec parses with the new field."""
    from pathlib import Path
    from batcave_platform.spec_loader import load_product_spec

    spec_path = (
        Path(__file__).resolve().parent.parent
        / "batcave_platform"
        / "specs"
        / "alfred.yaml"
    )
    spec, _ = load_product_spec(str(spec_path))
    policy = spec.agent.intervention_policy
    assert policy is not None
    assert policy.get("cooldown_seconds") == 45
    rule_ids = {rule.get("id") for rule in policy.get("rules") or []}
    assert {
        "missing_owner",
        "missing_due",
        "implied_decision",
        "unresolved_disagreement",
    }.issubset(rule_ids)
