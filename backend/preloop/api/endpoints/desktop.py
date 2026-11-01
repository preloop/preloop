"""Brokered live desktop views (#870).

Three sockets meet here:

* ``/desktop/endpoints/ws``: the host's durable control socket
  (``preloop desktop serve``), authenticated with an agent key, runtime
  credential or user API key. Registers the endpoint and heartbeats; receives
  ``desktop.open`` / ``desktop.close``.
* ``/desktop/endpoints/{endpoint}/views/{view_id}``: the host's per-view
  sibling socket carrying raw RFB bytes, authorized by the one-time
  ``view_token``.
* ``/ws/desktop/{view_id}``: the operator's viewer socket (noVNC or the CLI).

The two byte legs only talk through the desktop bus (see
``preloop.services.desktop_broker``), so they may land on different API pods.
The view-only filter runs on the viewer leg, before any byte leaves the API,
and the throughput cap paces the host leg. The protocol is pinned in
``factory`` contract-870 and documented in docs/operations/agent-deployment.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect, WebSocketState

from preloop.api.auth import get_current_active_user
from preloop.api.managed_credentials import is_managed_execution_credential
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_desktop, crud_managed_agent
from preloop.models.crud.desktop import endpoint_is_online
from preloop.services import desktop_broker as broker
from preloop.services.db_executor import run_db_async
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

T = TypeVar("T")
PERMISSION = "control_managed_agent"
VIEW_TOKEN_HEADER = "x-preloop-view-token"


async def _run_db(operation: Callable[[Session], T]) -> T:
    """Short-lived DB session off the loop. Tests pin this to their session."""
    return await run_db_async(operation)


def _now() -> datetime:
    """Naive UTC, matching the DateTime columns."""
    return datetime.now(UTC).replace(tzinfo=None)


def _iso(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat() + "Z"


def _str(value: Any) -> Optional[str]:
    return None if value is None else str(value)


def _error(status_code: int, code: str, message: str, **extra: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **extra},
    )


def _require_enabled() -> None:
    if not settings.desktop_views_enabled:
        raise _error(404, "desktop_views_disabled", "Desktop views are not enabled")


@require_permission(PERMISSION)
def _desktop_permission(*, current_user: models.User, db: Session) -> bool:
    """Same check as ``/agents/{agent_id}/control/*`` (control_managed_agent)."""
    return True


def _is_machine_credential(user: Any) -> bool:
    """Runner execution, agent and runtime-session keys never open views."""
    api_key = getattr(user, "_auth_api_key", None)
    if api_key is None:
        return False
    if is_managed_execution_credential(user):
        return True
    context = api_key.context_data if isinstance(api_key.context_data, dict) else {}
    return bool(context.get("runtime_session_id"))


def _authorize_operator(db: Session, user: models.User) -> None:
    if _is_machine_credential(user):
        raise _error(403, "forbidden", "Agent credentials cannot open desktop views")
    _desktop_permission(current_user=user, db=db)
    ensure_permission_in_oss(db, user, PERMISSION)


# ---------------------------------------------------------------------------
# Serialization


def _view_json(view: models.DesktopView, endpoint: models.DesktopEndpoint) -> dict:
    return {
        "view_id": str(view.id),
        "endpoint_id": endpoint.endpoint_id,
        "managed_agent_id": _str(view.managed_agent_id),
        "runtime_session_id": _str(view.runtime_session_id),
        "user_id": str(view.user_id),
        "state": view.state,
        "mode": view.mode,
        "ws_url": f"/api/v1/ws/desktop/{view.id}",
        "created_at": _iso(view.created_at),
        "live_at": _iso(view.live_at),
        "expires_at": _iso(view.expires_at),
        "closed_at": _iso(view.closed_at),
        "closed_reason": view.closed_reason,
        "control_expires_at": _iso(view.control_expires_at),
    }


def _holder(db: Session, view: models.DesktopView) -> dict:
    user = db.get(models.User, view.user_id)
    return {
        "view_id": str(view.id),
        "user_id": str(view.user_id),
        "username": getattr(user, "username", None),
        "since": _iso(view.created_at),
    }


def _endpoint_json(
    endpoint: models.DesktopEndpoint, now: datetime, active: Optional[dict]
) -> dict:
    return {
        "id": str(endpoint.id),
        "endpoint_id": endpoint.endpoint_id,
        "hostname": endpoint.hostname,
        "display": endpoint.display,
        "managed_agent_id": _str(endpoint.managed_agent_id),
        "runner_id": _str(endpoint.runner_id),
        "state": "online" if endpoint_is_online(endpoint, now) else "offline",
        "last_seen_at": _iso(endpoint.last_seen_at),
        "active_view": active,
    }


def _active_holders(db: Session, endpoints: list[models.DesktopEndpoint]) -> dict:
    """endpoint pk -> holder, in one query for all endpoints."""
    rows = crud_desktop.active_views_with_usernames(
        db, endpoint_pks=[endpoint.id for endpoint in endpoints]
    )
    return {
        view.endpoint_pk: {
            "view_id": str(view.id),
            "user_id": str(view.user_id),
            "username": username,
            "since": _iso(view.created_at),
        }
        for view, username in rows
    }


def _audit_facts(
    view: models.DesktopView, endpoint: models.DesktopEndpoint
) -> broker.AuditFacts:
    return broker.AuditFacts(
        view_id=str(view.id),
        endpoint_id=endpoint.endpoint_id,
        hostname=endpoint.hostname,
        managed_agent_id=_str(view.managed_agent_id),
        runner_id=_str(endpoint.runner_id),
        runtime_session_id=_str(view.runtime_session_id),
        mode=view.mode,
    )


def _audit(
    db: Session,
    view: models.DesktopView,
    action: str,
    details: dict[str, Any],
    *,
    user_id: Any = None,
) -> None:
    crud_audit_log.log_action(
        db,
        account_id=view.account_id,
        user_id=user_id if user_id is not None else view.user_id,
        action=action,
        resource_type="desktop_view",
        resource_id=str(view.id),
        status="success",
        details=details,
    )


# ---------------------------------------------------------------------------
# Closing (shared by REST, both legs, timers and the sweeper)


async def close_view(view_id: Any, reason: str, *, expected_state: str = "") -> bool:
    """Close a view once, audit it, and tell both legs and the host."""

    def work(db: Session) -> Optional[tuple[str, str]]:
        now = _now()
        view = crud_desktop.close_view(
            db,
            view_id=view_id,
            reason=reason,
            now=now,
            expected_state=expected_state or None,
        )
        if view is None:
            return None
        endpoint = crud_desktop.get_endpoint_by_pk(db, view.endpoint_pk)
        if endpoint is not None and view.live_at is not None:
            details = broker.view_ended_details(
                _audit_facts(view, endpoint),
                live_at=view.live_at,
                closed_at=now,
                closed_reason=reason,
            )
            _audit(db, view, "desktop.view.ended", details)
        return str(view.id), str(view.endpoint_pk)

    closed = await _run_db(work)
    if closed is None:
        return False
    view_pk, endpoint_pk = closed
    bus = await broker.get_desktop_bus()
    code = broker.CLOSE_CODE_BY_REASON.get(reason, broker.CLOSE_NORMAL)
    try:
        await broker.publish_json(
            bus,
            broker.view_subject(view_pk, "ctl"),
            {"type": "close", "code": code, "reason": reason},
        )
        await broker.publish_json(
            bus,
            broker.endpoint_cmd_subject(endpoint_pk),
            {"type": "desktop.close", "view_id": view_pk, "reason": reason},
        )
    except Exception:  # noqa: BLE001 - the DB close already happened
        logger.warning("Desktop view %s close notification failed", view_pk)
    return True


async def _expire_pending(view_id: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await close_view(view_id, "desktop_agent_unreachable", expected_state="pending")


_background: set[asyncio.Task[Any]] = set()


def _spawn(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def sweep_once() -> int:
    """Close views no relay will close and mark silent endpoints offline."""

    def work(db: Session) -> list[tuple[Any, str]]:
        now = _now()
        crud_desktop.mark_stale_endpoints_offline(db, now=now)
        return crud_desktop.views_to_sweep(db, now=now)

    closed = 0
    for view_id, reason in await _run_db(work):
        if await close_view(view_id, reason):
            closed += 1
    return closed


_sweeper: Optional[asyncio.Task[Any]] = None
SWEEP_INTERVAL_SECONDS = 30.0


def _ensure_sweeper() -> None:
    global _sweeper
    if _sweeper is not None and not _sweeper.done():
        return

    async def loop() -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
            try:
                await sweep_once()
            except Exception:  # noqa: BLE001 - keep sweeping
                logger.exception("Desktop view sweep failed")

    _sweeper = asyncio.create_task(loop())


# ---------------------------------------------------------------------------
# REST
#
# Routes take no request-scoped sync session: every handler authorizes inside
# _run_db with _authorize_operator, which applies the same require_permission
# check (control_managed_agent) on a short-lived worker session.


class DesktopViewCreate(BaseModel):
    duration_seconds: Optional[int] = Field(default=None, ge=broker.MIN_VIEW_SECONDS)
    runtime_session_id: Optional[uuid.UUID] = None


class DesktopControlRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _resolve_endpoint_for_agent(
    db: Session, user: models.User, agent_id: str
) -> models.DesktopEndpoint:
    agent = crud_managed_agent.get_for_account(
        db, account_id=user.account_id, agent_id=agent_id
    )
    if agent is None:
        raise _error(404, "agent_not_found", "Agent not found")
    now = _now()
    for endpoint in crud_desktop.list_endpoints(
        db, account_id=user.account_id, managed_agent_ids=[agent.id]
    ):
        if endpoint_is_online(endpoint, now):
            return endpoint
    raise _error(409, "desktop_not_available", "No online desktop for this agent")


async def _create_view(
    user: models.User,
    body: DesktopViewCreate,
    resolve: Callable[[Session], models.DesktopEndpoint],
) -> dict:
    max_seconds = settings.desktop_view_max_seconds
    duration = body.duration_seconds or min(broker.DEFAULT_VIEW_SECONDS, max_seconds)
    if duration > max_seconds:
        raise _error(
            422,
            "invalid_duration",
            f"duration_seconds must be at most {max_seconds}",
        )

    def work(db: Session) -> tuple[dict, str, str, str]:
        now = _now()
        endpoint = resolve(db)
        if not endpoint_is_online(endpoint, now):
            raise _error(409, "desktop_not_available", "The desktop is offline")
        if body.runtime_session_id is not None:
            linked = crud_desktop.session_link_filters(
                db,
                account_id=user.account_id,
                runtime_session_id=body.runtime_session_id,
            )
            if linked is None:
                raise _error(404, "runtime_session_not_found", "Session not found")
            linked_pks = {
                row.id
                for row in crud_desktop.list_endpoints(
                    db, account_id=user.account_id, **linked
                )
            }
            if endpoint.id not in linked_pks:
                raise _error(
                    422,
                    "runtime_session_not_linked",
                    "That session is not linked to this desktop",
                )
        recent = crud_desktop.count_recent_views(
            db, user_id=user.id, since=now - timedelta(hours=1)
        )
        if recent >= broker.VIEW_CREATIONS_PER_HOUR:
            raise _error(429, "rate_limited", "Too many desktop views this hour")
        crud_desktop.lock_endpoint(db, pk=endpoint.id)
        active = crud_desktop.active_view(db, endpoint_pk=endpoint.id)
        if active is not None:
            holder = _holder(db, active)
            db.rollback()
            raise _error(
                409,
                "desktop_view_busy",
                f"{holder['username'] or 'Another user'} is viewing this desktop",
                holder=holder,
            )
        token, token_hash = broker.issue_view_token()
        view = crud_desktop.create_view(
            db,
            account_id=user.account_id,
            endpoint_pk=endpoint.id,
            user_id=user.id,
            managed_agent_id=endpoint.managed_agent_id,
            runtime_session_id=body.runtime_session_id,
            state="pending",
            mode="view_only",
            view_token_hash=token_hash,
            token_expires_at=now + timedelta(seconds=broker.VIEW_TOKEN_TTL_SECONDS),
            expires_at=now + timedelta(seconds=duration),
        )
        return _view_json(view, endpoint), str(endpoint.id), token, str(view.id)

    payload, endpoint_pk, token, view_id = await _run_db(work)
    bus = await broker.get_desktop_bus()
    await broker.publish_json(
        bus,
        broker.endpoint_cmd_subject(endpoint_pk),
        {
            "type": "desktop.open",
            "view_id": view_id,
            "view_token": token,
            "expires_at": payload["expires_at"],
            "mode": "view_only",
        },
    )
    _spawn(_expire_pending(view_id, broker.VIEW_TOKEN_TTL_SECONDS))
    _ensure_sweeper()
    return payload


def _own_view(
    db: Session, user: models.User, view_id: str
) -> tuple[models.DesktopView, models.DesktopEndpoint]:
    view = crud_desktop.get_view(db, view_id=view_id, account_id=user.account_id)
    if view is None:
        raise _error(404, "desktop_view_not_found", "Desktop view not found")
    if view.user_id != user.id:
        raise _error(403, "forbidden", "This desktop view belongs to another user")
    endpoint = crud_desktop.get_endpoint_by_pk(db, view.endpoint_pk)
    if endpoint is None:
        raise _error(404, "desktop_view_not_found", "Desktop view not found")
    return view, endpoint


@router.get("/desktop/endpoints")
async def list_desktop_endpoints(
    managed_agent_id: Optional[uuid.UUID] = Query(default=None),
    runner_id: Optional[uuid.UUID] = Query(default=None),
    runtime_session_id: Optional[uuid.UUID] = Query(default=None),
    state: Optional[str] = Query(default=None, pattern="^(online|offline)$"),
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Desktop endpoints in the caller's account, filtered by linkage."""
    _require_enabled()

    def work(db: Session) -> list[dict]:
        _authorize_operator(db, current_user)
        account_id = current_user.account_id
        filters: dict[str, Any] = {}
        if managed_agent_id is not None:
            filters["managed_agent_ids"] = [managed_agent_id]
        if runner_id is not None:
            filters["runner_id"] = runner_id
        if runtime_session_id is not None:
            linked = crud_desktop.session_link_filters(
                db, account_id=account_id, runtime_session_id=runtime_session_id
            )
            if linked is None:
                return []
            filters.setdefault("managed_agent_ids", []).extend(
                linked["managed_agent_ids"]
            )
            filters["api_key_ids"] = linked["api_key_ids"]
        now = _now()
        endpoints = crud_desktop.list_endpoints(db, account_id=account_id, **filters)
        holders = _active_holders(db, endpoints)
        rows = [
            _endpoint_json(endpoint, now, holders.get(endpoint.id))
            for endpoint in endpoints
        ]
        return [row for row in rows if state is None or row["state"] == state]

    return {"endpoints": await _run_db(work)}


@router.post("/desktop/endpoints/{endpoint_id}/views", status_code=201)
async def create_desktop_view(
    endpoint_id: str,
    body: DesktopViewCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Open a view-only desktop view on one endpoint."""
    _require_enabled()

    def resolve(db: Session) -> models.DesktopEndpoint:
        _authorize_operator(db, current_user)
        endpoint = crud_desktop.get_endpoint(
            db, account_id=current_user.account_id, ident=endpoint_id
        )
        if endpoint is None:
            raise _error(404, "desktop_endpoint_not_found", "Desktop not found")
        return endpoint

    return await _create_view(current_user, body, resolve)


@router.post("/agents/{agent_id}/desktop-views", status_code=201)
async def create_agent_desktop_view(
    agent_id: str,
    body: DesktopViewCreate,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Open a view on the most recently seen online desktop of an agent."""
    _require_enabled()

    def resolve(db: Session) -> models.DesktopEndpoint:
        _authorize_operator(db, current_user)
        return _resolve_endpoint_for_agent(db, current_user, agent_id)

    return await _create_view(current_user, body, resolve)


async def _get_view(user: models.User, view_id: str) -> dict:
    _require_enabled()

    def work(db: Session) -> dict:
        _authorize_operator(db, user)
        view, endpoint = _own_view(db, user, view_id)
        return _view_json(view, endpoint)

    return await _run_db(work)


async def _delete_view(user: models.User, view_id: str) -> None:
    _require_enabled()

    def work(db: Session) -> None:
        _authorize_operator(db, user)
        _own_view(db, user, view_id)

    await _run_db(work)
    await close_view(view_id, "user_closed")


async def _grant_control(user: models.User, view_id: str, reason: str) -> dict:
    """OSS v1: self-confirm with a reason (EE adds an approver policy)."""
    _require_enabled()

    def work(db: Session) -> tuple[dict, str]:
        _authorize_operator(db, user)
        view, endpoint = _own_view(db, user, view_id)
        if view.state == "closed":
            raise _error(409, "desktop_view_closed", "The desktop view has ended")
        now = _now()
        until = min(
            view.expires_at, now + timedelta(seconds=broker.CONTROL_MAX_SECONDS)
        )
        facts = _audit_facts(view, endpoint).__dict__
        _audit(db, view, "desktop.control.requested", {**facts, "reason": reason})
        crud_desktop.set_mode(
            db, view_id=view.id, mode="control", control_expires_at=until
        )
        _audit(
            db,
            view,
            "desktop.control.granted",
            {
                **facts,
                "mode": "control",
                "approval": "self_confirmed",
                "reason": reason,
                "control_expires_at": _iso(until),
            },
        )
        db.refresh(view)
        return _view_json(view, endpoint), str(view.id)

    payload, view_pk = await _run_db(work)
    bus = await broker.get_desktop_bus()
    await broker.publish_json(
        bus,
        broker.view_subject(view_pk, "ctl"),
        {
            "type": "mode",
            "mode": "control",
            "control_expires_at": payload["control_expires_at"],
        },
    )
    return payload


async def _release_control(user: models.User, view_id: str, *, reason: str) -> None:
    _require_enabled()

    def work(db: Session) -> Optional[str]:
        _authorize_operator(db, user)
        view, endpoint = _own_view(db, user, view_id)
        if view.mode != "control":
            return None
        crud_desktop.set_mode(
            db, view_id=view.id, mode="view_only", control_expires_at=None
        )
        _audit(
            db,
            view,
            "desktop.control.released",
            {**_audit_facts(view, endpoint).__dict__, "released_by": reason},
        )
        return str(view.id)

    view_pk = await _run_db(work)
    if view_pk is not None:
        bus = await broker.get_desktop_bus()
        await broker.publish_json(
            bus,
            broker.view_subject(view_pk, "ctl"),
            {"type": "mode", "mode": "view_only", "control_expires_at": None},
        )


@router.get("/desktop/views/{view_id}")
async def get_desktop_view(
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """State of one of the caller's desktop views."""
    return await _get_view(current_user, view_id)


@router.get("/agents/{agent_id}/desktop-views/{view_id}")
async def get_agent_desktop_view(
    agent_id: str,
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Agent-scoped alias of ``GET /desktop/views/{view_id}``."""
    return await _get_view(current_user, view_id)


@router.delete("/desktop/views/{view_id}", status_code=204)
async def delete_desktop_view(
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """End a desktop view (idempotent)."""
    await _delete_view(current_user, view_id)


@router.delete("/agents/{agent_id}/desktop-views/{view_id}", status_code=204)
async def delete_agent_desktop_view(
    agent_id: str,
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """Agent-scoped alias of ``DELETE /desktop/views/{view_id}``."""
    await _delete_view(current_user, view_id)


@router.post("/desktop/views/{view_id}/control")
async def request_desktop_control(
    view_id: str,
    body: DesktopControlRequest,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Take control of the desktop (self-confirmed with a reason)."""
    return await _grant_control(current_user, view_id, body.reason)


@router.post("/agents/{agent_id}/desktop-views/{view_id}/control")
async def request_agent_desktop_control(
    agent_id: str,
    view_id: str,
    body: DesktopControlRequest,
    current_user: models.User = Depends(get_current_active_user),
) -> dict:
    """Agent-scoped alias of ``POST /desktop/views/{view_id}/control``."""
    return await _grant_control(current_user, view_id, body.reason)


@router.post("/desktop/views/{view_id}/control/release", status_code=204)
async def release_desktop_control(
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """Return the view to view-only."""
    await _release_control(current_user, view_id, reason="user")


@router.post(
    "/agents/{agent_id}/desktop-views/{view_id}/control/release", status_code=204
)
async def release_agent_desktop_control(
    agent_id: str,
    view_id: str,
    current_user: models.User = Depends(get_current_active_user),
) -> None:
    """Agent-scoped alias of ``POST /desktop/views/{view_id}/control/release``."""
    await _release_control(current_user, view_id, reason="user")


# ---------------------------------------------------------------------------
# WebSocket helpers


def _bearer(websocket: WebSocket) -> Optional[str]:
    header = websocket.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip() or None
    return websocket.query_params.get("token") or None


def _subprotocol(websocket: WebSocket) -> Optional[str]:
    offered = websocket.headers.get("sec-websocket-protocol", "")
    names = [p.strip() for p in offered.split(",") if p.strip()]
    return "binary" if "binary" in names else None


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    try:
        await websocket.close(code=code, reason=reason[:120])
    except (RuntimeError, OSError):
        logger.debug("desktop websocket already closed", exc_info=True)


class _EndpointIdentity(BaseModel):
    account_id: uuid.UUID
    user_id: Optional[uuid.UUID]
    api_key_id: uuid.UUID
    bound_agent_id: Optional[uuid.UUID] = None


def _authenticate_endpoint(db: Session, token: str) -> _EndpointIdentity:
    """Agent key, runtime credential or user API key (not a console JWT)."""
    from preloop.api.auth.jwt import _authenticate_with_api_key
    from preloop.models.crud import crud_api_key

    if not token or "." in token:
        raise _error(401, "unauthorized", "API key required")
    api_key = crud_api_key.get_by_key(db, key=token)
    if api_key is None:
        raise _error(401, "unauthorized", "Invalid API key")
    user = _authenticate_with_api_key(
        db, api_key, allow_stale_runtime_session=True, allow_managed_agent=True
    )
    from preloop.api.auth.key_scopes import enforce_runtime_api_key_scope

    enforce_runtime_api_key_scope(api_key)
    context = api_key.context_data if isinstance(api_key.context_data, dict) else {}
    bound = context.get("managed_agent_id")
    return _EndpointIdentity(
        account_id=api_key.account_id,
        user_id=user.id,
        api_key_id=api_key.id,
        bound_agent_id=uuid.UUID(str(bound)) if bound else None,
    )


class EndpointRegister(BaseModel):
    type: str
    endpoint_id: str = Field(
        min_length=8, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$"
    )
    hostname: str = Field(default="", max_length=255)
    display: str = Field(default=":99", max_length=32)
    vnc_addr: str = Field(default="127.0.0.1:5900", max_length=64)
    managed_agent_id: Optional[uuid.UUID] = None
    runner_id: Optional[uuid.UUID] = None
    version: Optional[str] = Field(default=None, max_length=64)


def _register_endpoint(
    db: Session, identity: _EndpointIdentity, reg: EndpointRegister, connection_id: str
) -> models.DesktopEndpoint:
    agent_id = reg.managed_agent_id or identity.bound_agent_id
    if identity.bound_agent_id and agent_id != identity.bound_agent_id:
        raise _error(403, "forbidden", "This credential is bound to another agent")
    if agent_id is not None and (
        crud_managed_agent.get_for_account(
            db, account_id=identity.account_id, agent_id=agent_id
        )
        is None
    ):
        raise _error(404, "agent_not_found", "Agent not found")
    if reg.runner_id is not None:
        runner = db.get(models.FlowRunner, reg.runner_id)
        if runner is None or runner.account_id != identity.account_id:
            raise _error(404, "runner_not_found", "Runner not found")
    return crud_desktop.register_endpoint(
        db,
        account_id=identity.account_id,
        endpoint_id=reg.endpoint_id,
        hostname=reg.hostname,
        display=reg.display,
        managed_agent_id=agent_id,
        runner_id=reg.runner_id,
        api_key_id=identity.api_key_id,
        connection_id=connection_id,
        now=_now(),
    )


async def _send_error(websocket: WebSocket, code: str, message: str) -> None:
    try:
        await websocket.send_json({"type": "error", "code": code, "message": message})
    except (RuntimeError, OSError, WebSocketDisconnect):
        pass


def _detail_code(exc: HTTPException) -> tuple[str, str]:
    detail = exc.detail
    if isinstance(detail, dict):
        return str(detail.get("code", "error")), str(detail.get("message", ""))
    return "unauthorized", str(detail)


# ---------------------------------------------------------------------------
# Host control socket


@router.websocket("/desktop/endpoints/ws")
async def desktop_endpoint_socket(websocket: WebSocket) -> None:
    """Durable control socket of a host running ``preloop desktop serve``."""
    token = _bearer(websocket)
    await websocket.accept()
    if not settings.desktop_views_enabled:
        await _send_error(websocket, "desktop_views_disabled", "Desktop views are off")
        await _close(websocket, broker.CLOSE_NOT_FOUND, "desktop_views_disabled")
        return
    try:
        identity = await _run_db(lambda db: _authenticate_endpoint(db, token or ""))
    except HTTPException as exc:
        code, message = _detail_code(exc)
        await _send_error(websocket, code, message)
        await _close(websocket, broker.CLOSE_UNAUTHORIZED, code)
        return
    try:
        first = await asyncio.wait_for(websocket.receive_json(), timeout=10)
        reg = EndpointRegister.model_validate(first)
        if reg.type != "desktop.register":
            raise ValueError("expected desktop.register")
    except (ValidationError, ValueError, asyncio.TimeoutError, json.JSONDecodeError):
        await _send_error(websocket, "invalid_register", "Send desktop.register first")
        await _close(websocket, broker.CLOSE_PROTOCOL, "invalid_register")
        return
    except WebSocketDisconnect:
        return

    connection_id = uuid.uuid4().hex

    def register(db: Session) -> dict[str, str]:
        # Scalars only: the row is detached once _run_db closes its session.
        endpoint = _register_endpoint(db, identity, reg, connection_id)
        return {
            "id": str(endpoint.id),
            "endpoint_id": endpoint.endpoint_id,
            "hostname": endpoint.hostname,
        }

    try:
        endpoint = await _run_db(register)
    except HTTPException as exc:
        code, message = _detail_code(exc)
        await _send_error(websocket, code, message)
        await _close(websocket, broker.CLOSE_FORBIDDEN, code)
        return
    endpoint_pk = endpoint["id"]
    bus = await broker.get_desktop_bus()
    subject = broker.endpoint_cmd_subject(endpoint_pk)
    # Evict any older socket of the same endpoint, on any pod.
    await broker.publish_json(
        bus, subject, {"type": "desktop.evict", "connection_id": connection_id}
    )
    evicted = asyncio.Event()

    async def on_command(raw: bytes) -> None:
        message = json.loads(raw)
        if message.get("type") == "desktop.evict":
            if message.get("connection_id") != connection_id:
                evicted.set()
            return
        if message.get("type") in ("desktop.open", "desktop.close"):
            await websocket.send_json(message)

    unsubscribe = await bus.subscribe(subject, on_command)
    _ensure_sweeper()
    logger.info("Desktop endpoint %s online (%s)", endpoint_pk, endpoint["hostname"])
    try:
        await websocket.send_json(
            {
                "type": "desktop.registered",
                "id": endpoint_pk,
                "endpoint_id": endpoint["endpoint_id"],
                "heartbeat_interval_seconds": broker.ENDPOINT_HEARTBEAT_SECONDS,
            }
        )
        evict_wait = asyncio.create_task(evicted.wait())
        try:
            while True:
                receive = asyncio.create_task(websocket.receive_json())
                done, _ = await asyncio.wait(
                    {receive, evict_wait},
                    timeout=broker.ENDPOINT_SILENCE_SECONDS,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if evict_wait in done:
                    receive.cancel()
                    await _close(websocket, broker.CLOSE_EVICTED, "replaced")
                    return
                if not done:
                    receive.cancel()
                    await _close(websocket, 1001, "heartbeat timeout")
                    return
                message = receive.result()
                await _handle_endpoint_message(
                    websocket, message, endpoint_pk, connection_id
                )
        finally:
            evict_wait.cancel()
    except (WebSocketDisconnect, RuntimeError, json.JSONDecodeError):
        pass
    finally:
        await unsubscribe()
        await _run_db(
            lambda db: crud_desktop.mark_endpoint_offline(
                db, pk=endpoint_pk, connection_id=connection_id
            )
        )
        logger.info("Desktop endpoint %s offline", endpoint_pk)


async def _handle_endpoint_message(
    websocket: WebSocket, message: Any, endpoint_pk: str, connection_id: str
) -> None:
    if not isinstance(message, dict):
        return
    kind = message.get("type")
    if kind == "desktop.heartbeat":
        alive = await _run_db(
            lambda db: crud_desktop.touch_endpoint(
                db, pk=endpoint_pk, connection_id=connection_id, now=_now()
            )
        )
        if not alive:
            await _close(websocket, broker.CLOSE_EVICTED, "replaced")
            raise WebSocketDisconnect(broker.CLOSE_EVICTED)
        await websocket.send_json({"type": "desktop.heartbeat_ack"})
    elif kind == "desktop.open_result" and message.get("ok") is False:
        view_id = str(message.get("view_id", ""))
        error = message.get("error")
        reason = error if error in broker.CLOSE_REASONS else "display_unavailable"

        def belongs(db: Session) -> bool:
            view = crud_desktop.get_view(db, view_id=view_id)
            return view is not None and str(view.endpoint_pk) == endpoint_pk

        if await _run_db(belongs):
            await close_view(view_id, reason)


# ---------------------------------------------------------------------------
# Host view socket (RFB bytes from the host)


@router.websocket("/desktop/endpoints/{endpoint_ident}/views/{view_id}")
async def desktop_endpoint_view_socket(
    websocket: WebSocket, endpoint_ident: str, view_id: str
) -> None:
    """Sibling socket the host opens for one view after ``desktop.open``."""
    token = _bearer(websocket)
    view_token = websocket.headers.get(VIEW_TOKEN_HEADER, "")
    await websocket.accept(subprotocol=_subprotocol(websocket))
    if not settings.desktop_views_enabled:
        await _close(websocket, broker.CLOSE_NOT_FOUND, "desktop_views_disabled")
        return

    def consume(db: Session) -> Optional[tuple[str, datetime, bool]]:
        identity = _authenticate_endpoint(db, token or "")
        endpoint = crud_desktop.get_endpoint(
            db, account_id=identity.account_id, ident=endpoint_ident
        )
        if endpoint is None or not view_token:
            return None
        if not crud_desktop.consume_token(
            db,
            view_id=view_id,
            endpoint_pk=endpoint.id,
            token_hash=broker.hash_view_token(view_token),
            now=_now(),
        ):
            return None
        view = crud_desktop.get_view(db, view_id=view_id)
        assert view is not None
        _audit(
            db,
            view,
            "desktop.view.started",
            {
                **_audit_facts(view, endpoint).__dict__,
                "expires_at": _iso(view.expires_at),
            },
        )
        return str(view.id), view.expires_at, view.viewer_attached_at is not None

    try:
        consumed = await _run_db(consume)
    except HTTPException:
        consumed = None
    if consumed is None:
        await _close(websocket, broker.CLOSE_UNAUTHORIZED, "invalid view token")
        return
    view_pk, expires_at, attached_now = consumed
    await _relay_endpoint_leg(websocket, view_pk, expires_at, attached_now)


async def _relay_endpoint_leg(
    websocket: WebSocket, view_pk: str, expires_at: datetime, attached_now: bool
) -> None:
    bus = await broker.get_desktop_bus()
    attached = asyncio.Event()
    peer_closed: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    if attached_now:
        attached.set()

    async def to_host(data: bytes) -> None:
        await websocket.send_bytes(data)

    async def on_ctl(raw: bytes) -> None:
        message = json.loads(raw)
        if message.get("type") == "viewer_attached":
            attached.set()
        elif message.get("type") == "close" and not peer_closed.done():
            peer_closed.set_result(int(message.get("code", broker.CLOSE_NORMAL)))

    unsub_ctl = await bus.subscribe(broker.view_subject(view_pk, "ctl"), on_ctl)
    unsub_bytes = await bus.subscribe(
        broker.view_subject(view_pk, "to_endpoint"), to_host
    )
    if not attached.is_set():
        attached_db = await _run_db(
            lambda db: getattr(
                crud_desktop.get_view(db, view_id=view_pk), "viewer_attached_at", None
            )
        )
        if attached_db is not None:
            attached.set()
    limiter = broker.ThroughputLimiter()

    # Read the host socket from the start so a host that drops before the
    # viewer attaches is noticed; the bounded queue keeps backpressure and
    # holds the host's first RFB bytes until someone is there to read them.
    queue: asyncio.Queue[Optional[bytes]] = asyncio.Queue(maxsize=64)

    async def read_host() -> None:
        try:
            while True:
                message = await websocket.receive()
                data = message.get("bytes")
                if (
                    message["type"] == "websocket.disconnect"
                    or data is None
                    or len(data) > broker.MAX_FRAME_BYTES
                ):
                    return
                await queue.put(data)
        except (WebSocketDisconnect, RuntimeError, OSError):
            return
        finally:
            await queue.put(None)

    async def pump() -> str:
        reader = asyncio.create_task(read_host())
        attach_wait = asyncio.create_task(attached.wait())
        try:
            done, _ = await asyncio.wait(
                {attach_wait, reader},
                timeout=broker.VIEWER_IDLE_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if attach_wait not in done:
                return "agent_disconnected" if reader in done else "idle"
            subject = broker.view_subject(view_pk, "to_viewer")
            while True:
                data = await queue.get()
                if data is None:
                    return "agent_disconnected"
                await limiter.acquire(len(data))
                await broker.publish_bytes(bus, subject, data)
        finally:
            reader.cancel()
            attach_wait.cancel()

    await _run_legs(
        websocket,
        view_pk,
        expires_at,
        pump(),
        peer_closed,
        (unsub_ctl, unsub_bytes),
        error_reason="agent_disconnected",
    )


def _pump_outcome(task: asyncio.Task[str], error_reason: str) -> tuple[str, int]:
    """Close reason and code for a finished pump task."""
    exc = task.exception()
    if isinstance(exc, broker.RfbProtocolError):
        return error_reason, broker.CLOSE_PROTOCOL
    if exc is not None:
        if not isinstance(exc, (WebSocketDisconnect, RuntimeError, OSError)):
            logger.error("Desktop relay failed", exc_info=exc)
        return error_reason, broker.CLOSE_CODE_BY_REASON[error_reason]
    reason = task.result()
    return reason, broker.CLOSE_CODE_BY_REASON[reason]


async def _run_legs(
    websocket: WebSocket,
    view_pk: str,
    expires_at: datetime,
    pump: Any,
    peer_closed: asyncio.Future[int],
    unsubscribes: tuple[Any, ...],
    *,
    error_reason: str,
) -> None:
    """Run one relay leg until its pump ends, the peer closes, or expiry.

    ``error_reason`` is the close reason when this leg's socket fails.
    """
    remaining = max((expires_at - _now()).total_seconds(), 0.0)
    pump_task = asyncio.create_task(pump)
    expiry = asyncio.create_task(asyncio.sleep(remaining))
    try:
        done, _ = await asyncio.wait(
            {pump_task, expiry, peer_closed}, return_when=asyncio.FIRST_COMPLETED
        )
        if peer_closed in done:
            code = peer_closed.result()
        else:
            if pump_task in done:
                reason, code = _pump_outcome(pump_task, error_reason)
            else:
                reason, code = "expired", broker.CLOSE_EXPIRED
            await close_view(view_pk, reason)
        await _close(websocket, code)
    finally:
        for task in (pump_task, expiry):
            task.cancel()
        if not peer_closed.done():
            peer_closed.cancel()
        for unsubscribe in unsubscribes:
            await unsubscribe()


# ---------------------------------------------------------------------------
# Viewer socket


async def _resolve_viewer(token: str) -> Optional[models.User]:
    from preloop.api.auth.jwt import get_user_from_token_if_valid_sync
    from preloop.services.db_executor import detach_user

    def lookup(db: Session) -> Optional[models.User]:
        user = get_user_from_token_if_valid_sync(token, db)
        if user is None or _is_machine_credential(user):
            return None
        from preloop.api.auth.key_scopes import api_key_allowed_on_channel

        if not api_key_allowed_on_channel(
            getattr(user, "_auth_api_key", None), "desktop viewer websocket"
        ):
            return None
        return detach_user(db, user)

    try:
        return await _run_db(lookup)
    except HTTPException:
        return None


@router.websocket("/ws/desktop/{view_id}")
async def desktop_viewer_socket(websocket: WebSocket, view_id: str) -> None:
    """Operator side of a view: raw RFB, view-only filtered server-side."""
    token = _bearer(websocket)
    await websocket.accept(subprotocol=_subprotocol(websocket))
    if not settings.desktop_views_enabled:
        await _close(websocket, broker.CLOSE_NOT_FOUND, "desktop_views_disabled")
        return
    user = await _resolve_viewer(token) if token else None
    if user is None:
        await _close(websocket, broker.CLOSE_UNAUTHORIZED, "authentication required")
        return

    def authorize(db: Session) -> tuple[int, Optional[dict]]:
        try:
            _authorize_operator(db, user)
        except HTTPException:
            return broker.CLOSE_FORBIDDEN, None
        view = crud_desktop.get_view(db, view_id=view_id)
        if view is None:
            return broker.CLOSE_NOT_FOUND, None
        if view.account_id != user.account_id or view.user_id != user.id:
            return broker.CLOSE_FORBIDDEN, None
        if view.state == "closed":
            return broker.CLOSE_NOT_FOUND, None
        return 0, {
            "view_pk": str(view.id),
            "expires_at": view.expires_at,
            "mode": view.mode,
            "control_expires_at": view.control_expires_at,
        }

    code, info = await _run_db(authorize)
    if info is None:
        await _close(websocket, code)
        return
    view_pk = info["view_pk"]
    bus = await broker.get_desktop_bus()
    mode = {"mode": info["mode"], "until": info["control_expires_at"]}
    peer_closed: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def to_viewer(data: bytes) -> None:
        await websocket.send_bytes(data)

    async def on_ctl(raw: bytes) -> None:
        message = json.loads(raw)
        if message.get("type") == "mode":
            until = message.get("control_expires_at")
            mode["mode"] = message.get("mode", "view_only")
            mode["until"] = datetime.fromisoformat(until.rstrip("Z")) if until else None
        elif message.get("type") == "close" and not peer_closed.done():
            peer_closed.set_result(int(message.get("code", broker.CLOSE_NORMAL)))

    unsub_ctl = await bus.subscribe(broker.view_subject(view_pk, "ctl"), on_ctl)
    unsub_bytes = await bus.subscribe(
        broker.view_subject(view_pk, "to_viewer"), to_viewer
    )
    if not await _run_db(
        lambda db: crud_desktop.attach_viewer(db, view_id=view_pk, now=_now())
    ):
        await unsub_ctl()
        await unsub_bytes()
        await _close(websocket, broker.CLOSE_VIEWER_BUSY, "viewer already attached")
        return
    await broker.publish_json(
        bus, broker.view_subject(view_pk, "ctl"), {"type": "viewer_attached"}
    )
    rfb_filter = broker.RfbClientFilter()

    def input_allowed() -> bool:
        if mode["mode"] != "control":
            return False
        until = mode["until"]
        return until is None or _now() < until

    async def pump() -> str:
        subject = broker.view_subject(view_pk, "to_endpoint")
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive(), timeout=broker.VIEWER_IDLE_SECONDS
                )
            except asyncio.TimeoutError:
                return "idle"
            if message["type"] == "websocket.disconnect":
                return "viewer_disconnected"
            data = message.get("bytes")
            if data is None or len(data) > broker.MAX_FRAME_BYTES:
                raise broker.RfbProtocolError("binary frames up to 1 MiB only")
            forward = rfb_filter.feed(data, allow_input=input_allowed())
            if forward:
                await broker.publish_bytes(bus, subject, forward)

    await _run_legs(
        websocket,
        view_pk,
        info["expires_at"],
        pump(),
        peer_closed,
        (unsub_ctl, unsub_bytes),
        error_reason="viewer_disconnected",
    )
