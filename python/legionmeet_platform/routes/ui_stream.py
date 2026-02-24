"""UI stream route placeholder.

The Streamlit UI reads sink-owned analysis artifacts and session status,
so this route acts as an explicit success marker in the route pipeline.
"""

from __future__ import annotations

from typing import Any

from legionmeet_platform.routes.base import RouteDispatchResult


class UiStreamRoute:
    """No-op route used to represent UI stream availability."""

    route_type = "ui_stream"

    def __init__(self, route_id: str) -> None:
        self.route_id = route_id

    async def dispatch(self, payload: dict[str, Any]) -> RouteDispatchResult:
        return RouteDispatchResult(
            route_id=self.route_id,
            route_type=self.route_type,
            ok=True,
            detail="Rendered via sink-owned output artifacts",
        )
