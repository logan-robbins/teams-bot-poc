"""
Variant plugin registry.
"""

from __future__ import annotations

from variants.base import VariantPlugin
from variants.behavioral import BehavioralVariantPlugin
from variants.default import DefaultVariantPlugin


def _build_registry() -> dict[str, VariantPlugin]:
    plugins: tuple[VariantPlugin, ...] = (
        DefaultVariantPlugin(),
        BehavioralVariantPlugin(),
    )
    return {plugin.variant_id: plugin for plugin in plugins}


_REGISTRY = _build_registry()


def available_variants() -> tuple[str, ...]:
    """Return all supported variant IDs."""
    return tuple(sorted(_REGISTRY.keys()))


def load_variant(variant_id: str) -> VariantPlugin:
    """Load a variant plugin by ID."""
    normalized = (variant_id or "").strip().lower()
    if not normalized:
        raise ValueError("Variant id is empty. Set VARIANT_ID or pass --variant.")

    plugin = _REGISTRY.get(normalized)
    if plugin is None:
        supported = ", ".join(available_variants())
        raise ValueError(
            f"Unknown variant '{variant_id}'. Supported variants: {supported}."
        )
    return plugin
