"""Load and validate Batcave product specs (JSON or YAML)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from batcave_platform.spec_models import ProductSpec


PLATFORM_NAME = "Batcave"


def resolve_spec_path(spec_path: str | None = None) -> Path:
    """Resolve explicit path or PRODUCT_SPEC_PATH. Spec is required."""
    raw_path = (spec_path or os.environ.get("PRODUCT_SPEC_PATH") or "").strip()
    if not raw_path:
        raise RuntimeError(
            "Product spec path is required. Set PRODUCT_SPEC_PATH or pass --product-spec."
        )
    return Path(raw_path).expanduser()


def _parse_spec_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Failed to read product spec '{path}': {exc}") from exc

    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                "YAML spec requested but PyYAML is not installed. "
                "Add 'pyyaml' to requirements or use a .json spec."
            ) from exc
        try:
            return yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            raise RuntimeError(
                f"Product spec at '{path}' is not valid YAML: {exc}"
            ) from exc

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Product spec at '{path}' is not valid JSON: {exc}"
        ) from exc


def load_product_spec(spec_path: str | None = None) -> tuple[ProductSpec, Path]:
    """Load product spec from disk (JSON or YAML) with strict validation."""
    resolved_path = resolve_spec_path(spec_path).resolve()
    if not resolved_path.exists():
        raise RuntimeError(
            f"Product spec file not found at '{resolved_path}'. "
            "Set PRODUCT_SPEC_PATH or provide a valid --product-spec path."
        )

    raw_spec = _parse_spec_file(resolved_path)

    try:
        return ProductSpec.model_validate(raw_spec), resolved_path
    except ValidationError as exc:
        raise RuntimeError(
            f"Product spec validation failed for '{resolved_path}': {exc}"
        ) from exc
