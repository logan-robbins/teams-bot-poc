"""Tests for product spec loading and route orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legionmeet_platform import load_product_spec
from legionmeet_platform.routes import build_route_orchestrator
from legionmeet_platform.spec_models import ProductSpec


def test_load_product_spec_from_explicit_path() -> None:
    """Explicit product spec path is required and loads successfully."""
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "legionmeet_platform"
        / "specs"
        / "talestral.json"
    ).resolve()
    spec, loaded_path = load_product_spec(str(spec_path))

    assert spec.product_id == "talestral"
    assert loaded_path == spec_path
    assert len(spec.checklist.items) > 0


def test_load_prd_pro_spec_from_explicit_path() -> None:
    """PRD Pro spec loads from explicit path."""
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "legionmeet_platform"
        / "specs"
        / "prd-pro.json"
    ).resolve()
    spec, loaded_path = load_product_spec(str(spec_path))

    assert spec.product_id == "prd-pro"
    assert spec.display_name == "PRD Pro"
    assert loaded_path == spec_path


def test_spec_path_is_required(monkeypatch) -> None:
    """Spec path must be provided via arg or PRODUCT_SPEC_PATH."""
    monkeypatch.delenv("PRODUCT_SPEC_PATH", raising=False)
    with pytest.raises(RuntimeError, match="required"):
        load_product_spec()


def test_product_spec_requires_non_empty_checklist(tmp_path) -> None:
    """Spec validation fails fast when checklist is missing required items."""
    invalid_spec = {
        "product_id": "broken",
        "display_name": "Broken",
        "agent": {
            "prompt_template": None,
            "tools": ["none"],
            "model": None,
            "reasoning_effort": None,
        },
        "checklist": {"items": []},
        "outputs": {
            "routes": [
                {
                    "id": "ui",
                    "type": "ui_stream",
                    "enabled": True,
                    "headers": {},
                    "timeout_seconds": 5.0,
                }
            ]
        },
        "ui": {
            "template": "talestral",
            "page_title": "Test",
            "page_icon": "T",
            "header_title": "Test",
            "candidate_name": "Test",
            "meeting_url": "https://teams.microsoft.com/l/meetup-join/test",
            "interview_script": [["speaker_0", "hello"]],
        },
    }
    spec_file = tmp_path / "invalid.json"
    spec_file.write_text(json.dumps(invalid_spec), encoding="utf-8")

    with pytest.raises(RuntimeError, match="checklist"):
        load_product_spec(str(spec_file))


def test_unimplemented_route_type_fails_fast() -> None:
    """Enabled teams routes are declared but intentionally not implemented yet."""
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "legionmeet_platform"
        / "specs"
        / "talestral.json"
    ).resolve()
    spec, _ = load_product_spec(str(spec_path))
    raw = spec.model_dump()
    routes = list(raw["outputs"]["routes"])
    routes[0] = {
        "id": "teams-dm",
        "type": "teams_dm",
        "enabled": True,
        "headers": {},
        "timeout_seconds": 5.0,
    }
    raw["outputs"]["routes"] = routes
    parsed = ProductSpec.model_validate(raw)

    with pytest.raises(RuntimeError, match="unimplemented"):
        build_route_orchestrator(parsed)
