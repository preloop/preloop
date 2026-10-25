"""Permission utilities with basic OSS enforcement.

When the proprietary RBAC plugin is unavailable, this module enforces the
seeded account role permissions. When it is available, the exported decorator
preserves the wrapped function's sync/async nature so FastAPI can dispatch
sync handlers via its threadpool.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import os
import threading
from contextvars import ContextVar, copy_context
from typing import TYPE_CHECKING, Any, Callable

from fastapi import HTTPException, status

from preloop.schemas.ci_principal import CiAction

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from preloop.models.models.user import User

try:
    if os.getenv("DISABLE_PROPRIETARY_PLUGINS", "false").lower() in {
        "true",
        "1",
        "yes",
    }:
        raise ImportError("Proprietary plugins disabled")
    from preloop.plugins.proprietary.rbac.permissions import (
        require_permission as _plugin_require_permission,
    )
except ImportError:
    # Covers ModuleNotFoundError and broken/partial RBAC dependency imports.
    _plugin_require_permission = None

# Compatibility extensions must leave denied-search collection to core.
AUDITS_DENIED_SESSION_SEARCH = True

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

    thread = threading.Thread(target=copy_context().run, args=(runner,), daemon=True)
    thread.start()
    thread.join()

    if "error" in result:
        raise result["error"]
    return result.get("value")


def _check_authorizer(permission_name: str, kwargs: dict) -> None:
    """Ask the registered authorizer (account hook H4) about an endpoint.

    The action is the permission name and there is no resource: this is the
    endpoint-level gate. Only a deny changes anything (403).
    """
    from preloop.plugins.account_hooks import AuthorizationContext, authorize

    user = kwargs.get("current_user")
    ctx = AuthorizationContext(
        account_id=getattr(user, "account_id", None),
        db=kwargs.get("db"),
        user=user,
    )
    decision = authorize(ctx, permission_name, None)
    if not decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=decision.reason or f"Permission denied: {permission_name}",
        )


def _with_authorizer(func, permission_name: str):
    """Consult the H4 authorizer before ``func``, keeping sync/async shape.

    Plugins register the authorizer at startup, after endpoints are
    decorated, so the registry is read on every call. With nothing
    registered the call goes straight through.
    """
    from preloop.plugins.account_hooks import get_authorizer

    if asyncio.iscoroutinefunction(func):

        @functools.wraps(func)
        async def authorized_async(*args, **kwargs):
            if get_authorizer() is not None:
                from preloop.api.loop_safety import run_db_off_loop

                await run_db_off_loop(
                    lambda: _check_authorizer(permission_name, kwargs)
                )
            return await func(*args, **kwargs)

        return authorized_async

    @functools.wraps(func)
    def authorized_sync(*args, **kwargs):
        if get_authorizer() is not None:
            _check_authorizer(permission_name, kwargs)
        return func(*args, **kwargs)

    return authorized_sync


def _with_core_permission(
    func: Callable[..., Any], permission_name: str
) -> Callable[..., Any]:
    """Enforce basic account permissions when the advanced RBAC overlay is absent."""
    signature = inspect.signature(func)

    def check(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if not _rbac_checks_enabled():
            return
        bound = signature.bind_partial(*args, **kwargs)
        dependencies = dict(bound.arguments)
        _ensure_permission_dependencies(**dependencies)
        user = dependencies["current_user"]
        if not getattr(user, "is_active", False):
            raise HTTPException(403, "Inactive membership")
        ensure_permission_in_oss(dependencies["db"], user, permission_name)

    if inspect.iscoroutinefunction(func):

        @functools.wraps(func)
        async def asynchronous(*args: Any, **kwargs: Any) -> Any:
            from preloop.api.loop_safety import run_db_off_loop

            await run_db_off_loop(lambda: check(args, kwargs))
            return await func(*args, **kwargs)

        return asynchronous

    @functools.wraps(func)
    def synchronous(*args: Any, **kwargs: Any) -> Any:
        check(args, kwargs)
        return func(*args, **kwargs)

    return synchronous


def _require_human_permission(permission_name: str):
    """Return a decorator that preserves sync/async behavior.

    Core role checks run before the account authorizer (H4). With the RBAC
    overlay, its advanced checks replace the core check before the authorizer
    and endpoint run.
    """

    def decorator(func):
        if _plugin_require_permission is None:
            return _with_core_permission(
                _with_authorizer(func, permission_name), permission_name
            )

        plugin_wrapped = _plugin_require_permission(permission_name)(
            _with_authorizer(func, permission_name)
        )

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


def require_permission(
    permission_name: str, *, ci_action: CiAction | None = None
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Check human permissions or an explicit independent machine operation.

    Restricted actors never enter the human plugin/owner path. Unmarked
    handlers deny machines even with RBAC disabled or an extension permitting
    everything. A marked handler still needs fresh core and EE machine checks.
    """
    from sqlalchemy.orm import Session

    from preloop.models import crud
    from preloop.models.crud.ci_principal import CiAuthorizationContext

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        signature = inspect.signature(func)
        # One mutable marker per call, shared with copied worker/async contexts.
        # A 403 raised after entry is already owned by the search audit wrapper.
        entered: ContextVar[list[bool]] = ContextVar("permission_handler_entered")

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def enter_handler(*args: Any, **kwargs: Any) -> Any:
                entered.get()[0] = True
                return await func(*args, **kwargs)
        else:

            @functools.wraps(func)
            def enter_handler(*args: Any, **kwargs: Any) -> Any:
                entered.get()[0] = True
                return func(*args, **kwargs)

        human_handler = _require_human_permission(permission_name)(enter_handler)

        def record_refusal(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
            if entered.get()[0] or func.__name__ != "search_runtime_sessions":
                return
            from preloop.services.session_search_audit import (
                record_denied_endpoint_search,
            )

            bound = signature.bind_partial(*args, **kwargs)
            record_denied_endpoint_search(
                permission=permission_name, kwargs=dict(bound.arguments)
            )

        def machine_call(
            args: tuple[Any, ...], kwargs: dict[str, Any]
        ) -> inspect.BoundArguments | None:
            bound = signature.bind_partial(*args, **kwargs)
            actor = bound.arguments.get("current_user", kwargs.get("current_user"))
            if not isinstance(actor, CiAuthorizationContext):
                return None
            db = bound.arguments.get("db", kwargs.get("db"))
            if not isinstance(ci_action, CiAction) or not isinstance(db, Session):
                raise HTTPException(403, "Restricted CI authorization denied")
            try:
                fresh = crud.crud_ci_principal.authorize(
                    db, context=actor, action=ci_action
                )
                if "current_user" in bound.arguments:
                    bound.arguments["current_user"] = fresh
                else:
                    for name, parameter in signature.parameters.items():
                        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
                            bound.arguments[name]["current_user"] = fresh
            except PermissionError:
                raise HTTPException(403, "Restricted CI authorization denied") from None
            return bound

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def wrapped(*args: Any, **kwargs: Any) -> Any:
                from preloop.api.loop_safety import run_db_off_loop

                token = entered.set([False])
                try:
                    bound = signature.bind_partial(*args, **kwargs)
                    checked = None
                    if isinstance(
                        bound.arguments.get("current_user", kwargs.get("current_user")),
                        CiAuthorizationContext,
                    ):
                        checked = await run_db_off_loop(
                            lambda: machine_call(args, kwargs)
                        )
                    if checked is not None:
                        entered.get()[0] = True
                        return await func(*checked.args, **checked.kwargs)
                    return await human_handler(*args, **kwargs)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        await run_db_off_loop(lambda: record_refusal(args, kwargs))
                    raise
                finally:
                    entered.reset(token)
        else:

            @functools.wraps(func)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                token = entered.set([False])
                try:
                    checked = machine_call(args, kwargs)
                    if checked is not None:
                        entered.get()[0] = True
                        return func(*checked.args, **checked.kwargs)
                    return human_handler(*args, **kwargs)
                except HTTPException as exc:
                    if exc.status_code == 403:
                        record_refusal(args, kwargs)
                    raise
                finally:
                    entered.reset(token)

        # Inventory only enables policies on handlers that explicitly opt in.
        wrapped._ci_action = ci_action
        return wrapped

    return decorator


def _held_role_ids(user_id: object, account_id: object) -> Any:
    """Role ids assigned directly or through a team in the user's account.

    Roles granted by a team in another account are excluded here. Callers
    still drop roles whose own ``account_id`` belongs to a different account,
    so a system role (``account_id`` is null) can be held and a custom role
    from elsewhere cannot.
    """
    from sqlalchemy import select, union

    from preloop.models.models.permission import TeamRole, UserRole
    from preloop.models.models.team import Team, TeamMembership

    direct_role_ids = select(UserRole.role_id).where(UserRole.user_id == user_id)
    team_role_ids = (
        select(TeamRole.role_id)
        .join(TeamMembership, TeamMembership.team_id == TeamRole.team_id)
        .join(Team, Team.id == TeamRole.team_id)
        .where(TeamMembership.user_id == user_id, Team.account_id == account_id)
    )
    return union(direct_role_ids, team_role_ids)


def _scoped_held_roles(user_id: object, account_id: object) -> Any:
    """Roles the user holds that belong to their account.

    System roles (``account_id`` is null) count. A custom role scoped to
    another account does not. Shared by the boolean check and the listing
    so the two cannot drift.
    """
    from sqlalchemy import and_, or_

    from preloop.models.models.permission import Role

    return and_(
        Role.id.in_(_held_role_ids(user_id, account_id)),
        or_(Role.account_id.is_(None), Role.account_id == account_id),
    )


def user_holds_permission(db, current_user, permission_name: str) -> bool:
    """Whether one of a user's roles grants ``permission_name``.

    This is the single resolver for "which roles does this user hold in
    their account". ``preloop.api.auth.permissions.has_permission`` (the
    fallback used when no RBAC plugin overlay is installed) delegates here,
    and ``get_user_permissions`` lists names from the same role set via
    :func:`user_permission_names`. The kill switch keeps its own copy because
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
    from sqlalchemy import and_, or_, select, true

    from preloop.models.models.permission import Permission, Role, RolePermission

    user_id = current_user.id
    account_id = current_user.account_id

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
        _scoped_held_roles(user_id, account_id),
        or_(
            and_(Role.name == "owner", Role.is_system_role == true()),
            role_grants_permission,
        ),
    )
    return bool(db.scalar(select(held_role.exists())))


def user_permission_names(db: Session, current_user: User) -> list[str]:
    """Permission names granted by roles the user holds in their account.

    Same role set as :func:`user_holds_permission`: direct assignments and
    roles granted through teams in the user's account. Roles scoped to
    another account never count. Holding the system ``owner`` role expands
    to every permission name. A custom role that is merely named ``owner``
    does not.

    Args:
        db: Database session.
        current_user: User whose permissions are listed.

    Returns:
        Permission names. Order is not significant.
    """
    from sqlalchemy import or_, select, true

    from preloop.models.models.permission import Permission, Role, RolePermission

    user_id = current_user.id
    account_id = current_user.account_id

    system_owner_held = (
        select(Role.id)
        .where(
            _scoped_held_roles(user_id, account_id),
            Role.name == "owner",
            Role.is_system_role == true(),
        )
        .exists()
    )
    granted_permission_ids = (
        select(RolePermission.permission_id)
        .join(Role, Role.id == RolePermission.role_id)
        .where(_scoped_held_roles(user_id, account_id))
    )
    names = select(Permission.name).where(
        or_(system_owner_held, Permission.id.in_(granted_permission_ids))
    )
    return list(db.scalars(names).all())


def ensure_permission_in_oss(db, current_user, permission_name: str) -> None:
    """Enforce ``permission_name`` on builds where the RBAC plugin is absent.

    The core decorator and controls that steer or stop a running agent use
    this fallback. EE returns immediately because its decorator already
    enforced the permission, and OSS falls back to the
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
