"""Operator read: one account's failures and anomalies over a time window."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_account
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.services.account_health import (
    AccountHealthWindowError,
    build_account_health,
    resolve_window,
)

router = APIRouter()

#: ``context_data`` keys that bind a key to one run, session or agent.
_RUN_BINDINGS = ("flow_execution_id", "runtime_session_id", "managed_agent_id")


class FailedExecutionCategory(BaseModel):
    """Failed executions of one failure category."""

    category: str
    count: int
    first_execution_id: Optional[UUID] = None
    first_at: Optional[datetime] = None


class HollowSuccessSignal(BaseModel):
    """Successful executions that did no visible work of one kind."""

    count: int
    sample_execution_ids: list[UUID] = Field(default_factory=list)


class HollowSuccesses(BaseModel):
    """Successful executions without tool calls or without model requests."""

    zero_tool_calls: HollowSuccessSignal
    zero_model_requests: HollowSuccessSignal


class RouteProblem(BaseModel):
    """A route that answered 4xx or 5xx repeatedly."""

    method: str
    route_template: str
    status_class: int
    count: int
    first_at: datetime
    last_at: datetime


class ErrorResponseGroup(BaseModel):
    """Individually recorded error responses with the same shape."""

    method: str
    route_template: str
    status_code: int
    principal_kind: str
    reason_code: Optional[str] = None
    count: int
    first_at: datetime
    last_at: datetime


class EmptyAfterCreate(BaseModel):
    """A list route that came back empty right after a create in its scope."""

    method: str
    route_template: str
    count: int
    first_at: datetime
    last_at: datetime


class GatewayUpstreamError(BaseModel):
    """Gateway requests that failed upstream, per provider and error class."""

    provider: str
    error_class: str
    count: int
    first_at: datetime
    last_at: datetime


class AccountHealthResponse(BaseModel):
    """Failures and anomalies of one account in a window (no content)."""

    account_id: UUID
    since: datetime
    until: datetime
    failed_executions: list[FailedExecutionCategory]
    hollow_successes: HollowSuccesses
    route_problems: list[RouteProblem]
    error_responses: list[ErrorResponseGroup]
    empty_after_create: list[EmptyAfterCreate]
    gateway_upstream_errors: list[GatewayUpstreamError]


def require_platform_operator(
    current_user: User = Depends(get_current_active_user),
) -> User:
    """Allow installation superusers on a session or an account API key.

    Keys bound to a flow execution, runtime session or managed agent act for
    that run, not for the operator, so they are refused even when their
    owner is a superuser. Restricted CI keys never reach this route.

    Raises:
        HTTPException: 403 for anyone else.
    """
    if not getattr(current_user, "is_superuser", False):
        raise HTTPException(status_code=403, detail="Platform operator access required")
    api_key: Any = getattr(current_user, "_auth_api_key", None)
    if api_key is not None:
        context = getattr(api_key, "context_data", None) or {}
        bound = isinstance(context, dict) and any(
            context.get(name) for name in _RUN_BINDINGS
        )
        if bound or getattr(api_key, "credential_type", "legacy") != "legacy":
            raise HTTPException(
                status_code=403, detail="Platform operator access required"
            )
    return current_user


@router.get(
    "/operator/accounts/{account_id}/health",
    response_model=AccountHealthResponse,
    tags=["Operator"],
)
def get_account_health(
    account_id: UUID,
    since: Optional[datetime] = Query(
        None, description="Window start (default: 24 hours before until)."
    ),
    until: Optional[datetime] = Query(None, description="Window end (default: now)."),
    db: Session = Depends(get_db_session),
    _operator: User = Depends(require_platform_operator),
) -> dict[str, Any]:
    """Failures and anomalies of one account, for platform operators.

    Returns failed executions per category with the first of each,
    successful executions with zero tool calls or zero model requests, API
    routes with repeated 4xx or 5xx, list responses that came back empty
    right after a create, and gateway upstream errors per provider. Only
    categories, counts, ids, route templates and first-party codes are
    returned. The window is at most 31 days.

    Raises:
        HTTPException: 403 for non-operators, 404 for an unknown account,
            422 for an invalid window.
    """
    try:
        start, end = resolve_window(since, until)
    except AccountHealthWindowError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if crud_account.get(db, id=account_id) is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return build_account_health(db, account_id=account_id, since=start, until=end)
