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
        except (Exception, asyncio.CancelledError) as exc:  # pragma: no cover
            # Capture asyncio.run failures (including CancelledError) so the
            # caller thread can re-raise them after join().
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


def user_holds_permission(db, current_user, permission_name: str) -> bool:
    """Whether one of a user's roles grants ``permission_name``.

    This is the single resolver for "which roles does this user hold in
    their account". ``preloop.api.auth.permissions.has_permission`` (the
    fallback used when no RBAC plugin overlay is installed) delegates here,
    so both checks always agree. The kill switch keeps its own copy because
    its tests patch that module's plugin symbols.

    A role is held when it is assigned to the user directly, or to a team in
    the user's account that the user belongs to. Roles scoped to another
    account never count. Data driven on the seeded role/permission matrix,
    with the ``owner`` system role treated as all-powerful (the
    implicit-owner convention the RBAC layer already follows). A custom role
    that merely happens to be named ``owner`` gets no special treatment.

    Resolved in one SQL round trip because the fallback runs on every
    decorated request.
    """
    from sqlalchemy import and_, or_, select, true, union

    from preloop.models.models.permission import (
        Permission,
        Role,
        RolePermission,
        TeamRole,
        UserRole,
    )
    from preloop.models.models.team import Team, TeamMembership

    user_id = current_user.id
    account_id = current_user.account_id

    direct_role_ids = select(UserRole.role_id).where(UserRole.user_id == user_id)
    team_role_ids = (
        select(TeamRole.role_id)
        .join(TeamMembership, TeamMembership.team_id == TeamRole.team_id)
        .join(Team, Team.id == TeamRole.team_id)
        .where(TeamMembership.user_id == user_id, Team.account_id == account_id)
    )
    role_grants_permission = (
        select(RolePermission.id)
        .join(Permission, Permission.id == RolePermission.permission_id)
        .where(
            RolePermission.role_id == Role.id,
            Permission.name == permission_name,
        )
        .exists()
    )
    held_role = select(Role.id).where(
        Role.id.in_(union(direct_role_ids, team_role_ids)),
        or_(Role.account_id.is_(None), Role.account_id == account_id),
        or_(
            and_(Role.name == "owner", Role.is_system_role == true()),
            role_grants_permission,
        ),
    )
    return bool(db.scalar(select(held_role.exists())))


def ensure_permission_in_oss(db, current_user, permission_name: str) -> None:
    """Enforce ``permission_name`` on builds where the RBAC plugin is absent.

    :func:`require_permission` is a no-op in OSS builds, which is fine for
    reads but not for the handful of controls that steer or stop a running
    agent. This is the OSS half of those: EE returns immediately because the
    decorator already enforced the permission, and OSS falls back to the
    seeded role matrix, with the superuser and the account's primary user
    always allowed so nobody can be locked out of their own account.

    Raises:
        HTTPException: 403 when the user does not hold the permission.
    """
    from preloop.models.crud import crud_account

    if _plugin_require_permission is not None and _rbac_checks_enabled():
        return
    if getattr(current_user, "is_superuser", False):
        return
    account = crud_account.get(db, id=current_user.account_id)
    if account is not None and str(account.primary_user_id) == str(current_user.id):
        return
    if user_holds_permission(db, current_user, permission_name):
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Insufficient permissions. Required: {permission_name}",
    )
