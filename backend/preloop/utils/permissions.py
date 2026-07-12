"""Permission utilities with OSS fallback.

When the proprietary RBAC plugin is unavailable, this module exposes a no-op
decorator. When it is available, the exported decorator preserves the wrapped
function's sync/async nature so FastAPI can keep dispatching sync handlers via
its threadpool.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import threading

from fastapi import HTTPException, status

try:
    from preloop.plugins.proprietary.rbac.permissions import (
        require_permission as _plugin_require_permission,
    )
except ImportError:
    # Covers ModuleNotFoundError and broken/partial RBAC dependency imports.
    _plugin_require_permission = None

_MISSING_DEPS_DETAIL = "Permission check requires current_user and db dependencies"


def _rbac_checks_enabled() -> bool:
    """Return True when permission enforcement should run.

    Honors both the process env flag and the in-memory settings singleton
    (tests often toggle env without recreating Settings).
    """
    import os

    from preloop.config import settings

    if settings.disable_rbac:
        return False
    return os.getenv("DISABLE_RBAC", "false").lower() != "true"


def _ensure_permission_dependencies(**kwargs: object) -> None:
    """Fail closed when the decorated endpoint lacks auth dependencies."""
    if "current_user" not in kwargs or "db" not in kwargs:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=_MISSING_DEPS_DETAIL,
        )


def _run_awaitable_sync(awaitable):
    """Run an awaitable from sync code, even if this thread already has a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, object] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - re-raised below
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def require_permission(permission_name: str):
    """Return a decorator that preserves sync/async behavior."""

    def decorator(func):
        if _plugin_require_permission is None:
            return func

        plugin_wrapped = _plugin_require_permission(permission_name)(func)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args, **kwargs):
                # Only fail-closed on missing deps when RBAC is actually on —
                # otherwise endpoints that omit unused ``db`` break under the
                # test-suite DISABLE_RBAC default.
                if _rbac_checks_enabled():
                    _ensure_permission_dependencies(**kwargs)
                return await plugin_wrapped(*args, **kwargs)

            return async_wrapper

        @functools.wraps(func)
        def sync_wrapper(*args, **kwargs):
            if _rbac_checks_enabled():
                _ensure_permission_dependencies(**kwargs)
            result = plugin_wrapped(*args, **kwargs)
            if inspect.isawaitable(result):
                return _run_awaitable_sync(result)
            return result

        return sync_wrapper

    return decorator
