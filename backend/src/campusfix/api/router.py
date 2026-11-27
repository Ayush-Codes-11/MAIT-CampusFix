"""URL router and route matching for MAIT CampusFix HTTP API."""

from __future__ import annotations

import re
from typing import Any, Callable, NamedTuple


class RouteMatch(NamedTuple):
    handler: Callable[..., Any]
    path_params: dict[str, str]


class Router:
    """Lightweight, regex-based HTTP route table for API Gateway HTTP API v2."""

    def __init__(self) -> None:
        self._routes: list[tuple[str, re.Pattern[str], Callable[..., Any]]] = []

    def add_route(self, method: str, path_pattern: str, handler: Callable[..., Any]) -> None:
        """Register a route with an HTTP method and path pattern containing {params}."""
        # Convert {param} placeholders to named regex groups
        regex_pattern = re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", r"(?P<\1>[^/]+)", path_pattern)
        compiled = re.compile(f"^{regex_pattern}$")
        self._routes.append((method.upper(), compiled, handler))

    def match(self, method: str, path: str) -> RouteMatch | None:
        """Match an incoming HTTP method and path against registered routes."""
        normalized_method = method.upper()
        # Strip trailing slash if present (except root '/')
        normalized_path = path.rstrip("/") if len(path) > 1 and path.endswith("/") else path

        for route_method, pattern, handler in self._routes:
            if route_method == normalized_method:
                match = pattern.match(normalized_path)
                if match:
                    return RouteMatch(handler=handler, path_params=match.groupdict())
        return None
