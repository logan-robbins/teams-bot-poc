"""Tests for product spec loading and route orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from batcave_platform import load_product_spec
from batcave_platform.routes import build_route_orchestrator
from batcave_platform.spec_models import ProductSpec


ALFRED_SPEC = (
    Path(__file__).resolve().parent.parent
    / "batcave_platform"
    / "specs"
    / "alfred.yaml"
).resolve()


def test_load_alfred_spec_from_explicit_path() -> None:
    """Alfred YAML spec loads via explicit path."""
    spec, loaded_path = load_product_spec(str(ALFRED_SPEC))

    assert spec.product_id == "alfred"
    assert "Alfred" in spec.display_name
    assert loaded_path == ALFRED_SPEC
    assert len(spec.checklist.items) > 0


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
            "template": "alfred",
            "page_title": "Test",
            "page_icon": "T",
            "header_title": "Test",
        },
    }
    spec_file = tmp_path / "invalid.json"
    spec_file.write_text(json.dumps(invalid_spec), encoding="utf-8")

    with pytest.raises(RuntimeError, match="checklist"):
        load_product_spec(str(spec_file))


def test_yaml_spec_loads_equivalently(tmp_path) -> None:
    """YAML and JSON specs parse to the same ProductSpec shape."""
    import yaml
    raw = yaml.safe_load(ALFRED_SPEC.read_text(encoding="utf-8"))
    json_path = tmp_path / "alfred.json"
    json_path.write_text(json.dumps(raw), encoding="utf-8")

    yaml_spec, _ = load_product_spec(str(ALFRED_SPEC))
    json_spec, _ = load_product_spec(str(json_path))

    assert yaml_spec.model_dump() == json_spec.model_dump()


def test_teams_dm_route_is_unimplemented() -> None:
    """teams_dm is declared but intentionally not implemented yet."""
    spec, _ = load_product_spec(str(ALFRED_SPEC))
    raw = spec.model_dump()
    raw["outputs"]["routes"] = [
        {
            "id": "teams-dm",
            "type": "teams_dm",
            "enabled": True,
            "headers": {},
            "timeout_seconds": 5.0,
            "max_rps": 4.0,
        }
    ]
    parsed = ProductSpec.model_validate(raw)

    with pytest.raises(RuntimeError, match="unimplemented"):
        build_route_orchestrator(parsed)


def test_teams_chat_route_requires_url_when_enabled() -> None:
    """teams_chat routes must declare a URL when enabled."""
    spec, _ = load_product_spec(str(ALFRED_SPEC))
    raw = spec.model_dump()
    raw["outputs"]["routes"] = [
        {
            "id": "teams-chat-broken",
            "type": "teams_chat",
            "enabled": True,
            "url": None,
            "headers": {},
            "timeout_seconds": 5.0,
            "max_rps": 4.0,
        }
    ]

    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ProductSpec.model_validate(raw)
