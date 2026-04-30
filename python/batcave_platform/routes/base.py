"""Output route interfaces and shared types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class RouteDispatchResult:
    """Result of attempting one route dispatch."""

    route_id: str
    route_type: str
    ok: bool
    detail: str | None = None


class OutputRoute(Protocol):
    """Interface for sink output routes."""

    route_id: str
    route_type: str

    async def dispatch(self, payload: dict[str, Any]) -> RouteDispatchResult:
        """Dispatch one payload to this route."""
