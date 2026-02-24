"""Route orchestrator for product-configured outputs."""

from __future__ import annotations

from typing import Any

from legionmeet_platform.routes.base import OutputRoute, RouteDispatchResult
from legionmeet_platform.routes.ui_stream import UiStreamRoute
from legionmeet_platform.routes.webhook import WebhookRoute
from legionmeet_platform.spec_models import OutputRouteType, ProductSpec


class RouteOrchestrator:
    """Dispatches payloads to all enabled output routes."""

    def __init__(self, routes: tuple[OutputRoute, ...]) -> None:
        self._routes = routes

    @property
    def route_count(self) -> int:
        return len(self._routes)

    async def dispatch_all(self, payload: dict[str, Any]) -> list[RouteDispatchResult]:
        results: list[RouteDispatchResult] = []
        for route in self._routes:
            results.append(await route.dispatch(payload))
        return results


def build_route_orchestrator(spec: ProductSpec) -> RouteOrchestrator:
    """Create route instances from a validated product spec."""
    routes: list[OutputRoute] = []

    for route in spec.outputs.routes:
        if not route.enabled:
            continue

        if route.type == OutputRouteType.UI_STREAM:
            routes.append(UiStreamRoute(route.id))
            continue

        if route.type == OutputRouteType.WEBHOOK:
            if not route.url:
                raise RuntimeError(
                    f"Route '{route.id}' is webhook but has no URL configured."
                )
            routes.append(
                WebhookRoute(
                    route_id=route.id,
                    url=route.url,
                    headers=route.headers,
                    timeout_seconds=route.timeout_seconds,
                )
            )
            continue

        if route.type in {OutputRouteType.TEAMS_CHAT, OutputRouteType.TEAMS_DM}:
            raise RuntimeError(
                f"Route '{route.id}' uses unimplemented type '{route.type.value}'."
            )

        raise RuntimeError(f"Unsupported route type '{route.type.value}'.")

    if not routes:
        raise RuntimeError("No enabled output routes configured in product spec.")

    return RouteOrchestrator(tuple(routes))
