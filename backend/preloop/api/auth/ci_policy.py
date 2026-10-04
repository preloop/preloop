"""Inventory and exact handler matching for restricted HTTP policies."""

from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import Any

from starlette.routing import Match, Mount
from starlette.types import Scope

from preloop.schemas.ci_principal import CiAction


def effective_routes(app: Any) -> Iterator[Any]:
    """Include lazy FastAPI routers as well as ordinary Starlette routes."""
    for route in app.routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield route


def route_description(route: Any) -> tuple[str, set[str], Any]:
    """Read the effective path/method/endpoint across FastAPI versions."""
    original = getattr(route, "original_route", route)
    concrete = getattr(route, "starlette_route", None) or original
    path = getattr(route, "path", "") or getattr(concrete, "path", "")
    methods = getattr(route, "methods", None) or getattr(concrete, "methods", None)
    if not methods:
        methods = {"MOUNT"} if hasattr(concrete, "routes") else {"WEBSOCKET"}
    endpoint = getattr(route, "endpoint", None) or getattr(original, "endpoint", None)
    return path, set(methods), endpoint


def restricted_route_inventory(app: Any, prefix: str = "") -> dict[str, str]:
    """Every operation has an explicit deny or supported action classification."""
    inventory: dict[str, str] = {}
    for route in effective_routes(app):
        path, methods, endpoint = route_description(route)
        action = getattr(endpoint, "_ci_action", None)
        policy = action.value if isinstance(action, CiAction) else "deny"
        for method in methods:
            name = f"{method} {prefix}{path}"
            if name in inventory:
                raise ValueError(f"Ambiguous restricted route policy: {name}")
            inventory[name] = policy
        original = getattr(route, "original_route", route)
        concrete = getattr(route, "starlette_route", None) or original
        children = None
        mounted = concrete
        seen: set[int] = set()
        while mounted is not None and id(mounted) not in seen:
            seen.add(id(mounted))
            children = getattr(mounted, "routes", None)
            if children:
                break
            mounted = getattr(mounted, "app", None)
        if children:
            nested = restricted_route_inventory(
                SimpleNamespace(routes=children), prefix=prefix + path
            )
            overlap = set(inventory) & set(nested)
            if overlap:
                raise ValueError(
                    f"Ambiguous mounted restricted policies: {sorted(overlap)}"
                )
            inventory.update(nested)
    return inventory


def matched_machine_handler(scope: Scope, action: CiAction) -> bool:
    """Only the router's first matching handler can opt into machine authority.

    Pattern overlap or a newly added alias must not send a machine context to
    an unrelated/public handler merely because its URL matches an allow rule.
    """
    app = scope.get("app")
    if app is None:
        return False
    for route in effective_routes(app):
        match, _ = route.matches(scope)
        if match == Match.FULL:
            _, _, endpoint = route_description(route)
            return getattr(endpoint, "_ci_action", None) == action
    return False


def validate_machine_policies(
    app: Any, policies: Mapping[tuple[str, str], CiAction]
) -> None:
    """Reject mounted allow rules until nested dispatch is explicitly supported."""
    if not policies:
        return
    seen: set[int] = set()
    while app is not None and id(app) not in seen:
        seen.add(id(app))
        if hasattr(app, "routes"):
            break
        app = getattr(app, "app", None)
    if app is None or not hasattr(app, "routes"):
        raise ValueError("Restricted CI policies require an inspectable router")
    for method, path in policies:
        scope: Scope = {
            "type": "http",
            "method": method,
            "path": path,
            "root_path": "",
        }
        for route in effective_routes(app):
            original = getattr(route, "original_route", route)
            concrete = getattr(route, "starlette_route", None) or original
            if isinstance(concrete, Mount):
                match, _ = concrete.matches(scope)
                if match == Match.FULL:
                    raise ValueError(
                        f"Mounted restricted CI operations are unsupported: "
                        f"{method} {path}"
                    )
