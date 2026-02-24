"""Output routing package."""

from legionmeet_platform.routes.base import RouteDispatchResult
from legionmeet_platform.routes.router import RouteOrchestrator, build_route_orchestrator

__all__ = [
    "RouteDispatchResult",
    "RouteOrchestrator",
    "build_route_orchestrator",
]
