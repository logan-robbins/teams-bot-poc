"""Checklist state manager for sink-owned checklist lifecycle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


class ChecklistStatus(str, Enum):
    """Checklist item status."""

    PENDING = "pending"
    ANALYZING = "analyzing"
    COMPLETE = "complete"


@dataclass(frozen=True)
class ChecklistDefinition:
    """Checklist definition for one item."""

    id: str
    label: str
    keywords: tuple[str, ...]


@dataclass
class ChecklistItemState:
    """Mutable state for one checklist item."""

    status: ChecklistStatus = ChecklistStatus.PENDING
    updated_at: str | None = None
    reason: str | None = None
    source: str | None = None


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class ChecklistStateManager:
    """Manages required checklist state for a session."""

    def __init__(self, definitions: Iterable[ChecklistDefinition]) -> None:
        self._definitions = tuple(definitions)
        if not self._definitions:
            raise RuntimeError("Checklist must contain at least one item.")

        self._state = {
            item.id: ChecklistItemState()
            for item in self._definitions
        }
        self._id_by_label = {item.label.strip().lower(): item.id for item in self._definitions}

    def reset(self) -> None:
        """Reset all checklist items to pending."""
        self._state = {item.id: ChecklistItemState() for item in self._definitions}

    def _resolve_item_id(self, item: str) -> str | None:
        normalized = (item or "").strip()
        if not normalized:
            return None
        if normalized in self._state:
            return normalized
        return self._id_by_label.get(normalized.lower())

    def update(
        self,
        item: str,
        status: ChecklistStatus | str,
        reason: str,
        source: str,
    ) -> bool:
        """Update one checklist item by id or label."""
        item_id = self._resolve_item_id(item)
        if item_id is None:
            return False

        try:
            next_status = ChecklistStatus(status)
        except ValueError:
            return False

        current = self._state[item_id]
        if current.status == next_status:
            return False

        current.status = next_status
        current.updated_at = _now_utc()
        current.reason = reason
        current.source = source
        return True

    def apply_talestral_heuristic(self, text: str, speaker_role: str) -> bool:
        """Apply current baseline checklist behavior from transcript text."""
        if not text:
            return False

        text_lower = text.lower()
        for item in self._definitions:
            if any(keyword.lower() in text_lower for keyword in item.keywords):
                item_state = self._state[item.id]
                if item_state.status == ChecklistStatus.PENDING:
                    return self.update(
                        item=item.id,
                        status=ChecklistStatus.ANALYZING,
                        reason="Keyword hit while topic is being discussed",
                        source="heuristic",
                    )
                if (
                    item_state.status == ChecklistStatus.ANALYZING
                    and speaker_role == "candidate"
                ):
                    return self.update(
                        item=item.id,
                        status=ChecklistStatus.COMPLETE,
                        reason="Candidate response completed active topic",
                        source="heuristic",
                    )
                return False
        return False

    def snapshot(self) -> list[dict[str, str | None]]:
        """Return ordered checklist snapshot for APIs/routes/output."""
        rows: list[dict[str, str | None]] = []
        for item in self._definitions:
            state = self._state[item.id]
            rows.append(
                {
                    "id": item.id,
                    "label": item.label,
                    "status": state.status.value,
                    "updated_at": state.updated_at,
                    "reason": state.reason,
                    "source": state.source,
                }
            )
        return rows
