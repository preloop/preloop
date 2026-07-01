"""Helpers for inspecting FastAPI route trees across versions."""

from __future__ import annotations

from typing import Iterable


def collect_route_paths(routes: Iterable[object]) -> set[str]:
    """Collect HTTP route paths from a FastAPI/Starlette route tree.

    FastAPI >= 0.137 nests included routers as ``_IncludedRouter`` objects
    that do not expose ``.path``. Prefer ``iter_route_contexts`` when
    available and fall back to recursive traversal for older versions.
    """
    try:
        from fastapi.routing import iter_route_contexts
    except ImportError:  # pragma: no cover - FastAPI < 0.137.2
        iter_route_contexts = None  # type: ignore[assignment,misc]

    if iter_route_contexts is not None:
        return {context.path for context in iter_route_contexts(routes)}

    return _collect_route_paths_legacy(routes)


def _collect_route_paths_legacy(
    routes: Iterable[object], *, prefix: str = ""
) -> set[str]:
    """Recursively collect route paths from a flat/nested route list."""
    paths: set[str] = set()
    for route in routes:
        route_path = getattr(route, "path", None)
        if route_path is not None:
            paths.add(f"{prefix}{route_path}".replace("//", "/"))
        nested = getattr(route, "routes", None)
        if nested:
            nested_prefix = prefix
            if route_path is not None:
                nested_prefix = f"{prefix}{route_path}".rstrip("/")
            paths.update(_collect_route_paths_legacy(nested, prefix=nested_prefix))
    return paths
