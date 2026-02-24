"""Load and validate LegionMeet product specs."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from legionmeet_platform.spec_models import ProductSpec


PLATFORM_NAME = "LegionMeet"


def resolve_spec_path(spec_path: str | None = None) -> Path:
    """Resolve explicit path or PRODUCT_SPEC_PATH. Spec is required."""
    raw_path = (spec_path or os.environ.get("PRODUCT_SPEC_PATH") or "").strip()
    if not raw_path:
        raise RuntimeError(
            "Product spec path is required. Set PRODUCT_SPEC_PATH or pass --product-spec."
        )
    return Path(raw_path).expanduser()


def load_product_spec(spec_path: str | None = None) -> tuple[ProductSpec, Path]:
    """Load product spec JSON from disk with strict validation."""
    resolved_path = resolve_spec_path(spec_path).resolve()
    if not resolved_path.exists():
        raise RuntimeError(
            f"Product spec file not found at '{resolved_path}'. "
            "Set PRODUCT_SPEC_PATH or provide a valid --product-spec path."
        )

    try:
        with open(resolved_path, "r", encoding="utf-8") as spec_file:
            raw_spec = json.load(spec_file)
    except OSError as exc:
        raise RuntimeError(
            f"Failed to read product spec '{resolved_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Product spec at '{resolved_path}' is not valid JSON: {exc}"
        ) from exc

    try:
        return ProductSpec.model_validate(raw_spec), resolved_path
    except ValidationError as exc:
        raise RuntimeError(
            f"Product spec validation failed for '{resolved_path}': {exc}"
        ) from exc
