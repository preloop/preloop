"""Attach the authenticated principal and outcome hints to a request's scope.

Authentication code calls ``attribute_principal`` as soon as it has
identified a credential, before any route-scope or permission denial, so a
403 is still attributed. ``ApiUsageMiddleware`` reads the result after the
response. Values live in ``scope["state"]``, which the middleware, the CI
middleware and route handlers share.
"""

from __future__ import annotations

from typing import Any, Optional
from uuid import UUID

from preloop.services.api_traffic_recorder import ANONYMOUS, TrafficPrincipal

_PRINCIPAL_KEY = "traffic_principal"
_REASON_KEY = "traffic_reason_code"
_EMPTY_AFTER_CREATE_KEY = "traffic_empty_after_create"


def _state(target: Any) -> Optional[dict[str, Any]]:
    """Return the shared state dict of a Request or an ASGI scope."""
    if target is None:
        return None
    scope = getattr(target, "scope", target)
    if not isinstance(scope, dict):
        return None
    state = scope.setdefault("state", {})
    return state if isinstance(state, dict) else None


def _as_uuid(value: Any) -> Optional[UUID]:
    """Coerce an id to UUID, or None when it is not one."""
    if value is None or isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (TypeError, ValueError):
        return None


def attribute_principal(
    target: Any, *, kind: str, principal_id: Any, account_id: Any
) -> None:
    """Record who made the request; the first attribution wins.

    Args:
        target: The Request or ASGI scope.
        kind: ``session``, ``api_key``, ``runtime_key`` or ``ci_identity``.
        principal_id: User, key or CI principal id.
        account_id: The principal's account.
    """
    state = _state(target)
    if state is None or _PRINCIPAL_KEY in state:
        return
    state[_PRINCIPAL_KEY] = TrafficPrincipal(
        kind=kind,
        principal_id=_as_uuid(principal_id),
        account_id=_as_uuid(account_id),
    )


def attribute_api_key(target: Any, api_key: Any) -> None:
    """Record an API key principal, telling runtime keys from account keys."""
    context = getattr(api_key, "context_data", None)
    bound = isinstance(context, dict) and any(
        context.get(name)
        for name in ("flow_execution_id", "runtime_session_id", "managed_agent_id")
    )
    runtime = bound or (
        getattr(api_key, "credential_type", None) == "restricted_runtime"
    )
    attribute_principal(
        target,
        kind="runtime_key" if runtime else "api_key",
        principal_id=getattr(api_key, "id", None),
        account_id=getattr(api_key, "account_id", None),
    )


def traffic_principal(target: Any) -> TrafficPrincipal:
    """Return the recorded principal, or the anonymous one."""
    state = _state(target)
    value = state.get(_PRINCIPAL_KEY) if state else None
    return value if isinstance(value, TrafficPrincipal) else ANONYMOUS


def set_reason_code(target: Any, reason_code: str) -> None:
    """Record a short first-party code explaining the response."""
    state = _state(target)
    if state is not None:
        state[_REASON_KEY] = reason_code


def reason_code(target: Any) -> Optional[str]:
    """Return the recorded reason code, if any."""
    state = _state(target)
    value = state.get(_REASON_KEY) if state else None
    return value if isinstance(value, str) else None


def mark_empty_after_create(target: Any) -> None:
    """Flag a list response that was empty right after a create in scope."""
    state = _state(target)
    if state is not None:
        state[_EMPTY_AFTER_CREATE_KEY] = True


def is_empty_after_create(target: Any) -> bool:
    """Return whether the response was flagged as empty after a create."""
    state = _state(target)
    return bool(state and state.get(_EMPTY_AFTER_CREATE_KEY))
