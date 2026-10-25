"""API endpoints for querying audit logs."""

import inspect
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, List, Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_audit_log
from preloop.models.db.session import get_db_session as get_db
from preloop.models import models

from preloop.utils.permissions import require_permission

User = models.User

try:
    from preloop.api.heavy_routes import heavy_route
except ModuleNotFoundError as missing:
    # Only a core without preloop/preloop#1653 falls back to the shared pool;
    # any other import failure inside heavy_routes is a real error.
    if missing.name != "preloop.api.heavy_routes":
        raise
    logging.getLogger(__name__).info(
        "preloop.api.heavy_routes unavailable; audit aggregates use the shared pool"
    )

    def heavy_route(func: Callable[..., Any]) -> Callable[..., Any]:  # type: ignore[no-redef]
        return func


logger = logging.getLogger(__name__)
router = APIRouter()


def _audit_query_kwargs(method: Callable[..., Any], **kwargs: Any) -> dict[str, Any]:
    """Drop ``consent_ref`` when this core build cannot filter on it.

    The audit API grew the filter before every deployed core accepted the
    argument. Passing it on the unfiltered timeline then raises ``TypeError``
    and the console shows a 500.
    """
    if "consent_ref" not in kwargs:
        return kwargs
    if "consent_ref" in inspect.signature(method).parameters:
        return kwargs
    if kwargs["consent_ref"] is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Consent reference filtering is not available on this server",
        )
    return {key: value for key, value in kwargs.items() if key != "consent_ref"}


def _core_matches_any_outcome() -> bool:
    """Whether this core build accepts ``outcome_match`` (OR over outcomes)."""
    return (
        "outcome_match"
        in inspect.signature(crud_audit_log.get_grouped_by_correlation).parameters
    )


def _outcome_match_kwargs(
    outcome_match: str, outcomes: Optional[List[str]]
) -> dict[str, Any]:
    """``outcome_match`` for the core, or nothing when it cannot take it.

    Asking for any of several outcomes on a core that only matches all of
    them would answer a different question, so that is refused instead.
    """
    if _core_matches_any_outcome():
        return {"outcome_match": outcome_match}
    if outcome_match == "any" and outcomes and len(outcomes) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Matching any of several outcomes is not available on this server",
        )
    return {}


def _count_limit_kwargs(count_limit: Optional[int]) -> dict[str, Any]:
    """``count_limit`` for the core, or nothing when it cannot take it.

    A core without it counts exactly: slower, but the same answer, and
    ``total_capped`` then stays false because the total is exact.
    """
    if count_limit is None:
        return {}
    if (
        "count_limit"
        in inspect.signature(crud_audit_log.get_grouped_by_correlation).parameters
    ):
        return {"count_limit": count_limit}
    return {}


# The Audit page stops counting at this many groups and shows "10K+" (#1592).
# The overview used to count every group in the window. On a long trail that
# exact total is the cold request.
OVERVIEW_COUNT_LIMIT = 10_000


def _apply_count_cap(
    total: int, *, count_limit: Optional[int], capped_count: bool
) -> tuple[int, bool]:
    """Collapse a core total above ``count_limit`` to the cap.

    Args:
        total: The total the core returned.
        count_limit: The cap that was requested, or None when the caller
            did not ask for one.
        capped_count: Whether the core was asked to stop. An older core
            counts exactly, and that exact total is kept.

    Returns:
        The total to report, and whether it is a lower bound.
    """
    if capped_count and count_limit is not None and total > count_limit:
        return count_limit, True
    return total, False


# Response models
class AuditLogResponse(BaseModel):
    """Response model for audit log entries."""

    id: UUID
    account_id: UUID
    user_id: Optional[UUID]
    action: str
    resource_type: Optional[str]
    resource_id: Optional[str]
    status: str
    ip_address: Optional[str]
    user_agent: Optional[str]
    details: Optional[dict]
    timestamp: datetime

    class Config:
        from_attributes = True


class AuditSubEventResponse(BaseModel):
    """Response model for a sub-event in a grouped timeline."""

    id: UUID
    action: str
    status: str
    details: Optional[dict]
    timestamp: datetime

    class Config:
        from_attributes = True


class AuditGroupResponse(BaseModel):
    """Response model for a grouped timeline entry."""

    correlation_id: Optional[str]
    primary_event: AuditLogResponse
    sub_events: List[AuditSubEventResponse] = []
    outcome: str


class AuditGroupedListResponse(BaseModel):
    """Response model for the grouped timeline."""

    groups: List[AuditGroupResponse]
    total: int
    skip: int
    limit: int
    total_capped: bool = Field(
        False,
        description=(
            "True when count_limit was given and more groups match: total is "
            "then count_limit, a lower bound"
        ),
    )


class AuditStatsResponse(BaseModel):
    """Response model for audit statistics."""

    action_stats: List[dict] = Field(
        description="Statistics by action and status",
        example=[{"action": "permission_check", "status": "denied", "count": 15}],
    )
    user_activity: List[dict] = Field(
        description="Top active users",
        example=[
            {"username": "admin", "email": "admin@example.com", "action_count": 150}
        ],
    )
    total_count: int = Field(description="Total number of audit logs in period")
    period_days: int = Field(description="Number of days analyzed")


class AuditLogsListResponse(BaseModel):
    """Response model for list of audit logs."""

    logs: List[AuditLogResponse]
    total: int
    skip: int
    limit: int


# API Endpoints


class AuditActorResponse(BaseModel):
    """Account-scoped display identity, without user-management fields."""

    id: UUID
    username: str
    full_name: Optional[str] = None


@router.get(
    "/audit-logs/actors", response_model=list[AuditActorResponse], tags=["Audit Logs"]
)
@require_permission("view_audit_logs")
def list_audit_actors(
    skip: int = Query(0, ge=0),
    limit: int = Query(1000, ge=1, le=1000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[AuditActorResponse]:
    """List display identities for the current account's audit filters."""
    from preloop.models.crud import crud_user

    return [
        AuditActorResponse(id=user.id, username=user.username, full_name=user.full_name)
        for user in crud_user.get_by_account(
            db, account_id=str(current_user.account_id), skip=skip, limit=limit
        )
    ]


@router.get(
    "/audit-logs",
    response_model=AuditLogsListResponse,
    tags=["Audit Logs"],
    summary="List audit logs with filters",
)
@require_permission("view_audit_logs")
def list_audit_logs(
    action: Optional[str] = Query(
        None, description="Filter by action (e.g., 'permission_check', 'role_assigned')"
    ),
    status_filter: Optional[str] = Query(
        None, description="Filter by status (e.g., 'success', 'denied', 'failure')"
    ),
    resource_type: Optional[str] = Query(
        None, description="Filter by resource type (e.g., 'issue', 'user', 'team')"
    ),
    start_date: Optional[datetime] = Query(
        None, description="Filter by start date (ISO 8601 format)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter by end date (ISO 8601 format)"
    ),
    consent_ref: Optional[str] = Query(
        None,
        min_length=1,
        max_length=512,
        description="Exact delegated grant consent reference",
    ),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditLogsListResponse:
    """List audit logs with optional filters.

    This endpoint allows querying audit logs for the current user's account
    with various filter options including action type, status, resource type,
    and date range.

    Requires the 'view_audit_logs' permission.
    """
    try:
        # Get filtered audit logs
        logs = crud_audit_log.get_by_account(
            db,
            **_audit_query_kwargs(
                crud_audit_log.get_by_account,
                account_id=current_user.account_id,
                skip=skip,
                limit=limit,
                action=action,
                status=status_filter,
                resource_type=resource_type,
                start_date=start_date,
                end_date=end_date,
                consent_ref=consent_ref,
            ),
        )

        # Get total count
        total = crud_audit_log.count_by_account(
            db,
            **_audit_query_kwargs(
                crud_audit_log.count_by_account,
                account_id=current_user.account_id,
                action=action,
                status=status_filter,
                resource_type=resource_type,
                start_date=start_date,
                end_date=end_date,
                consent_ref=consent_ref,
            ),
        )

        return AuditLogsListResponse(
            logs=[AuditLogResponse.model_validate(log) for log in logs],
            total=total,
            skip=skip,
            limit=limit,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing audit logs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve audit logs",
        )


@router.get(
    "/audit-logs/grouped",
    response_model=AuditGroupedListResponse,
    tags=["Audit Logs"],
    summary="Get audit logs grouped by correlation for unified timeline",
)
@heavy_route
@require_permission("view_audit_logs")
def list_grouped_audit_logs(
    event_type: Optional[List[str]] = Query(
        None,
        description="Filter by event type(s): 'tool_call', 'model_gateway_request', 'config:mcp_server', 'config:tool_rule', etc.",
    ),
    outcome: Optional[List[str]] = Query(
        None,
        description=(
            "Filter by outcome(s) (allow, deny, require_approval, approved, "
            "declined, failed, budget_denied, upstream_error, error)"
        ),
    ),
    outcome_match: Annotated[
        Literal["all", "any"],
        Query(
            description=(
                "With several outcomes: 'all' (default) keeps a row whose chain "
                "carries every one, 'any' a row that carries at least one"
            ),
        ),
    ] = "all",
    tool_name: Optional[str] = Query(
        None, description="Filter by tool name (substring match)"
    ),
    start_date: Optional[datetime] = Query(
        None, description="Filter by start date (ISO 8601)"
    ),
    end_date: Optional[datetime] = Query(
        None, description="Filter by end date (ISO 8601)"
    ),
    min_cost: Optional[float] = Query(
        None, ge=0, description="Minimum estimated gateway spend (USD)"
    ),
    max_cost: Optional[float] = Query(
        None, ge=0, description="Maximum estimated gateway spend (USD)"
    ),
    consent_ref: Optional[str] = Query(
        None,
        min_length=1,
        max_length=512,
        description="Exact delegated grant consent reference",
    ),
    skip: int = Query(0, ge=0, description="Number of groups to skip"),
    limit: int = Query(50, ge=1, le=200, description="Maximum number of groups"),
    count_limit: Annotated[
        Optional[int],
        Query(
            ge=1,
            le=1_000_000,
            description=(
                "Stop counting the total past this many groups (total_capped "
                "says so). The exact total reads the whole matching trail."
            ),
        ),
    ] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditGroupedListResponse:
    """Get audit logs grouped by correlation_id for the unified timeline.

    Returns tool call events as primary rows with their correlated sub-events
    (policy decisions, approval lifecycle) grouped together. Non-tool events
    are returned as standalone groups.
    """
    count_kwargs = _count_limit_kwargs(count_limit)
    try:
        groups, total = crud_audit_log.get_grouped_by_correlation(
            db,
            **_audit_query_kwargs(
                crud_audit_log.get_grouped_by_correlation,
                account_id=current_user.account_id,
                skip=skip,
                limit=limit,
                event_type_filter=event_type,
                outcome_filter=outcome,
                **_outcome_match_kwargs(outcome_match, outcome),
                tool_name_filter=tool_name,
                start_date=start_date,
                end_date=end_date,
                consent_ref=consent_ref,
                min_cost=min_cost,
                max_cost=max_cost,
                **count_kwargs,
            ),
        )
        # Only a core that took the limit stopped counting; an exact total
        # from an older core is reported as it is.
        total, total_capped = _apply_count_cap(
            total, count_limit=count_limit, capped_count=bool(count_kwargs)
        )

        return AuditGroupedListResponse(
            groups=[
                AuditGroupResponse(
                    correlation_id=g["correlation_id"],
                    primary_event=AuditLogResponse.model_validate(g["primary_event"]),
                    sub_events=[
                        AuditSubEventResponse.model_validate(s) for s in g["sub_events"]
                    ],
                    outcome=g["outcome"],
                )
                for g in groups
            ],
            total=total,
            skip=skip,
            limit=limit,
            total_capped=total_capped,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error listing grouped audit logs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve grouped audit logs",
        )


class AuditOverviewResponse(BaseModel):
    """Everything the console Overview reads from the audit trail, in one call."""

    events_total: int = Field(
        description=(
            "Timeline groups since start_date, counted up to 10000. "
            "events_total_capped means more groups match."
        )
    )
    events_total_capped: bool = Field(
        False,
        description=(
            "True when events_total stopped at 10000 groups and more match, "
            "the same cap the Audit page reports as 10K+."
        ),
    )
    tool_calls_total: int = Field(
        description="tool_call events since start_date (every one when absent)"
    )
    tool_calls_failed: int = Field(
        description="tool_call events with status failed since start_date"
    )
    exceptions: AuditGroupedListResponse = Field(
        description=(
            "Most recent groups since start_date whose chain failed or was "
            "denied by a policy or a budget (any of these outcomes), newest "
            "first, with their total."
        )
    )


def _grouped_response(
    groups: List[dict[str, Any]],
    total: int,
    limit: int,
    total_capped: bool = False,
) -> AuditGroupedListResponse:
    return AuditGroupedListResponse(
        groups=[
            AuditGroupResponse(
                correlation_id=g["correlation_id"],
                primary_event=AuditLogResponse.model_validate(g["primary_event"]),
                sub_events=[
                    AuditSubEventResponse.model_validate(s) for s in g["sub_events"]
                ],
                outcome=g["outcome"],
            )
            for g in groups
        ],
        total=total,
        skip=0,
        limit=limit,
        total_capped=total_capped,
    )


# The outcomes the Overview "Failures and denials" card lists: tool calls
# that failed, and calls or model requests a policy or a budget blocked. A
# gateway ``error`` is left to the Gateway failures card beside it.
_EXCEPTION_OUTCOMES = ("failed", "upstream_error", "deny", "budget_denied")
# What a core without ``outcome_match`` is asked, one outcome at a time.
# ``deny`` is already a filter on that core. ``upstream_error`` is not:
# those calls are still reported as ``allow`` until the core bump.
_LEGACY_EXCEPTION_OUTCOMES = ("failed", "deny", "budget_denied")


def _legacy_exceptions(
    grouped: Callable[..., tuple[List[dict[str, Any]], int]],
    limit: int,
    start_date: Optional[datetime],
    count_kwargs: dict[str, Any],
) -> AuditGroupedListResponse:
    """Exceptions on a core whose outcome filter only matches all outcomes.

    Each outcome is asked on its own and the pages are merged. A chain that
    carries both is listed once; past ``limit`` it may be counted twice, so
    the total is then an upper bound. Each read uses the same count cap as
    the trail total.
    """
    exceptions: dict[Any, dict[str, Any]] = {}
    exceptions_total = 0
    listed_twice = 0
    for outcome in _LEGACY_EXCEPTION_OUTCOMES:
        groups, total = grouped(
            limit=limit,
            outcome_filter=[outcome],
            start_date=start_date,
            **count_kwargs,
        )
        exceptions_total += total
        for group in groups:
            if group["primary_event"].id in exceptions:
                listed_twice += 1
            else:
                exceptions[group["primary_event"].id] = group
    newest = sorted(
        exceptions.values(),
        key=lambda group: group["primary_event"].timestamp,
        reverse=True,
    )[:limit]
    total, capped = _apply_count_cap(
        exceptions_total - listed_twice,
        count_limit=OVERVIEW_COUNT_LIMIT,
        capped_count=bool(count_kwargs),
    )
    return _grouped_response(newest, total, limit, capped)


# The Overview asks for the same figures on every load and on every range
# switch. They are cached per worker for a minute, so a burst of loads (or a
# few people on the same account) costs one read, not one each.
OVERVIEW_CACHE_TTL_SECONDS = 60.0
_OVERVIEW_CACHE_MAX_ENTRIES = 512
# How long a request waits for another one computing the same figures
# before it reads them itself.
_OVERVIEW_WAIT_SECONDS = 30.0
_OverviewKey = tuple[str, Optional[str], int]
_overview_cache: dict[_OverviewKey, tuple[float, Any]] = {}
# Keys being computed right now, each with the event its waiters block on.
# An entry lives exactly as long as its computation (single flight).
_overview_inflight: dict[_OverviewKey, threading.Event] = {}
_overview_cache_lock = threading.Lock()


def clear_overview_cache() -> None:
    """Forget every cached overview (tests)."""
    with _overview_cache_lock:
        _overview_cache.clear()


def _cached_overview_locked(key: _OverviewKey) -> Optional[Any]:
    hit = _overview_cache.get(key)
    if hit is not None and hit[0] > time.monotonic():
        return hit[1]
    return None


def _store_overview_locked(key: _OverviewKey, value: Any) -> None:
    now = time.monotonic()
    if (
        key not in _overview_cache
        and len(_overview_cache) >= _OVERVIEW_CACHE_MAX_ENTRIES
    ):
        for stale in [k for k, (exp, _) in _overview_cache.items() if exp <= now]:
            del _overview_cache[stale]
        if len(_overview_cache) >= _OVERVIEW_CACHE_MAX_ENTRIES:
            del _overview_cache[
                min(_overview_cache, key=lambda k: _overview_cache[k][0])
            ]
    _overview_cache[key] = (now + OVERVIEW_CACHE_TTL_SECONDS, value)


def _single_flight_overview(key: _OverviewKey, build: Callable[[], Any]) -> Any:
    """The cached figures for ``key``, or ``build()`` run once for everyone.

    Concurrent misses on the same key wait for the first request instead of
    each running the reads. If that request fails or takes longer than
    ``_OVERVIEW_WAIT_SECONDS``, one of the waiters takes over the single
    flight and the rest wait for it in turn.
    """
    while True:
        with _overview_cache_lock:
            cached = _cached_overview_locked(key)
            if cached is not None:
                return cached
            running = _overview_inflight.get(key)
            if running is None:
                running = _overview_inflight[key] = threading.Event()
                break
        running.wait(_OVERVIEW_WAIT_SECONDS)
        with _overview_cache_lock:
            if _overview_inflight.get(key) is running:
                # The leader is still busy past the wait: stop waiting on
                # it and run the next build under a fresh flight.
                del _overview_inflight[key]
    try:
        value = build()
        with _overview_cache_lock:
            _store_overview_locked(key, value)
        return value
    finally:
        with _overview_cache_lock:
            if _overview_inflight.get(key) is running:
                del _overview_inflight[key]
        running.set()


@router.get(
    "/audit-logs/overview",
    response_model=AuditOverviewResponse,
    tags=["Audit Logs"],
    summary="Audit figures and exceptions for the console Overview",
)
@heavy_route
@require_permission("view_audit_logs")
def get_audit_overview(
    start_date: Optional[datetime] = Query(
        None, description="Window start for every figure and the exceptions (ISO 8601)"
    ),
    exceptions_limit: int = Query(
        12, ge=1, le=50, description="Maximum number of exception groups"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditOverviewResponse:
    """The four audit reads the Overview made separately, in one request.

    Every figure covers the same window, ``start_date`` onwards (#1592): the
    Overview is a "this range" page, and the all-time tool call totals were
    the slowest part of the request on a long trail.

    ``events_total`` is the ``total`` the grouped timeline reports for the
    window, counted up to ``OVERVIEW_COUNT_LIMIT`` so it agrees with the
    Audit page. Past that, ``events_total_capped`` is set and the total is
    the cap. ``tool_calls_total`` and
    ``tool_calls_failed`` are plain counts of ``tool_call`` rows (all, and
    those with status ``failed``) in the window. ``tool_calls_total`` equals
    the grouped total for ``event_type=tool_call``; ``tool_calls_failed`` is
    exact, where the grouped outcome filter only looks at the newest 5000
    calls. ``exceptions`` lists the groups in the window that failed or were
    denied by a policy or a budget (any of those outcomes, not all).

    The response is cached per worker for ``OVERVIEW_CACHE_TTL_SECONDS``.
    """
    key: _OverviewKey = (
        str(current_user.account_id),
        start_date.isoformat() if start_date else None,
        exceptions_limit,
    )
    return _single_flight_overview(
        key,
        lambda: _build_audit_overview(
            db, current_user.account_id, start_date, exceptions_limit
        ),
    )


def _build_audit_overview(
    db: Session,
    account_id: Any,
    start_date: Optional[datetime],
    exceptions_limit: int,
) -> AuditOverviewResponse:
    def grouped(**filters: Any) -> tuple[List[dict[str, Any]], int]:
        return crud_audit_log.get_grouped_by_correlation(
            db, account_id=account_id, skip=0, **filters
        )

    def tool_calls(**filters: Any) -> int:
        return (
            crud_audit_log.count_by_account(
                db,
                account_id=account_id,
                action="tool_call",
                start_date=start_date,
                **filters,
            )
            or 0
        )

    # Same cap as the Audit page. Without it the trail total is an exact
    # grouped count of the whole window, which is the cold overview (#1592).
    count_kwargs = _count_limit_kwargs(OVERVIEW_COUNT_LIMIT)
    try:
        _, events_total = grouped(limit=1, start_date=start_date, **count_kwargs)
        events_total, events_total_capped = _apply_count_cap(
            events_total,
            count_limit=OVERVIEW_COUNT_LIMIT,
            capped_count=bool(count_kwargs),
        )
        tool_calls_total = tool_calls()
        tool_calls_failed = tool_calls(status="failed")
        if _core_matches_any_outcome():
            # One grouped read: failed or denied, in the same window.
            # The page is ``exceptions_limit`` groups. ``count_limit`` is
            # the trail cap, so a total past it is a lower bound. The rows
            # are the core's recent-primary outcome window, the same one
            # the Audit page reads, which keeps the card equal to it.
            groups, exceptions_total = grouped(
                limit=exceptions_limit,
                outcome_filter=list(_EXCEPTION_OUTCOMES),
                outcome_match="any",
                start_date=start_date,
                **count_kwargs,
            )
            exceptions_total, exceptions_capped = _apply_count_cap(
                exceptions_total,
                count_limit=OVERVIEW_COUNT_LIMIT,
                capped_count=bool(count_kwargs),
            )
            exceptions = _grouped_response(
                groups, exceptions_total, exceptions_limit, exceptions_capped
            )
        else:
            exceptions = _legacy_exceptions(
                grouped, exceptions_limit, start_date, count_kwargs
            )
        return AuditOverviewResponse(
            events_total=events_total,
            events_total_capped=events_total_capped,
            tool_calls_total=tool_calls_total,
            tool_calls_failed=tool_calls_failed,
            exceptions=exceptions,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error building the audit overview: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve the audit overview",
        )


@router.get(
    "/audit-logs/stats",
    response_model=AuditStatsResponse,
    tags=["Audit Logs"],
    summary="Get audit log statistics",
)
@heavy_route
@require_permission("view_audit_logs")
def get_audit_stats(
    days: int = Query(
        30, ge=1, le=365, description="Number of days to analyze (default 30)"
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditStatsResponse:
    """Get statistics about audit events.

    Returns aggregated statistics including action counts by type and status,
    top active users, and total log counts for the specified time period.

    Requires the 'view_audit_logs' permission.
    """
    try:
        # Get action statistics
        action_stats = crud_audit_log.get_action_stats(
            db, account_id=current_user.account_id, days=days
        )

        # Get user activity
        user_activity = crud_audit_log.get_user_activity(
            db, account_id=current_user.account_id, days=days, limit=10
        )

        # Get total count for the period
        start_date = datetime.now(timezone.utc) - timedelta(days=days)
        total_count = crud_audit_log.count_by_account(
            db, account_id=current_user.account_id, start_date=start_date
        )

        return AuditStatsResponse(
            action_stats=action_stats,
            user_activity=user_activity,
            total_count=total_count,
            period_days=days,
        )

    except Exception as e:
        logger.error(f"Error getting audit statistics: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve audit statistics",
        )


@router.get(
    "/audit-logs/user/{user_id}",
    response_model=AuditLogsListResponse,
    tags=["Audit Logs"],
    summary="Get audit logs for a specific user",
)
@require_permission("view_audit_logs")
def get_user_audit_logs(
    user_id: UUID,
    days: int = Query(
        30, ge=1, le=365, description="Number of days to look back (default 30)"
    ),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditLogsListResponse:
    """Get audit logs for a specific user.

    Returns all audit logs associated with the specified user within the
    given time period. This is useful for investigating user activity and
    tracking security-relevant actions.

    Requires the 'view_audit_logs' permission.
    """
    try:
        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        # Get user's audit logs
        logs = crud_audit_log.get_by_user(
            db,
            user_id=user_id,
            account_id=current_user.account_id,
            skip=skip,
            limit=limit,
            days=days,
            start_date=start_date,
        )

        # Count total logs for this user
        total = crud_audit_log.count_by_account(
            db,
            account_id=current_user.account_id,
            user_id=user_id,
            start_date=start_date,
            end_date=None,
        )

        return AuditLogsListResponse(
            logs=[AuditLogResponse.model_validate(log) for log in logs],
            total=total,
            skip=skip,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"Error getting user audit logs: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve user audit logs",
        )


@router.get(
    "/audit-logs/denials",
    response_model=AuditLogsListResponse,
    tags=["Audit Logs"],
    summary="Get recent permission denial events",
)
@require_permission("view_audit_logs")
def get_permission_denials(
    days: int = Query(7, ge=1, le=90, description="Number of days to look back"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum number of records"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditLogsListResponse:
    """Get recent permission denial events.

    Returns all permission check events that resulted in denial within the
    specified time period. This is useful for security auditing and
    identifying potential unauthorized access attempts.

    Requires the 'view_audit_logs' permission.
    """
    try:
        # Get permission denial logs
        logs = crud_audit_log.get_permission_denials(
            db, account_id=current_user.account_id, days=days, limit=limit
        )

        return AuditLogsListResponse(
            logs=[AuditLogResponse.model_validate(log) for log in logs],
            total=len(logs),
            skip=0,
            limit=limit,
        )

    except Exception as e:
        logger.error(f"Error getting permission denials: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve permission denials",
        )


@router.get(
    "/audit-logs/{entry_id}", response_model=AuditLogResponse, tags=["Audit Logs"]
)
@require_permission("view_audit_logs")
def get_audit_entry(
    entry_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> AuditLogResponse:
    """Fetch one account-owned entry so a console deep link can focus it."""
    row = crud_audit_log.get(db, id=entry_id, account_id=str(current_user.account_id))
    if row is None:
        raise HTTPException(status_code=404, detail="Audit entry not found")
    return AuditLogResponse.model_validate(row)
