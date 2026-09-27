"""Cost and cycle time per tracker issue (#958).

The prefix is ``/cost/by-issue`` rather than ``/cost/issues``: the request
middleware counts any POST under a path containing ``/issues`` as an issue
creation, and a rebuild is not one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.issue_cost import (
    IssueCostExecutionRow,
    IssueCostRebuildRequest,
    IssueCostRebuildResponse,
    IssueCostReport,
)
from preloop.services import issue_cost_rollup
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/cost/by-issue", tags=["Cost Analytics"])

#: Widest window one rebuild call scans; split longer backfills.
REBUILD_MAX_WINDOW_DAYS = 92


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _validate_period(start: Optional[datetime], end: Optional[datetime]) -> None:
    # Naive bounds are read as UTC so a mixed pair compares instead of raising.
    if start is not None and end is not None and _utc(end) <= _utc(start):
        raise HTTPException(status_code=422, detail="end_date must be after start_date")


@router.get("", response_model=IssueCostReport)
@require_permission("view_cost")
def list_issue_costs(
    start_date: Optional[datetime] = Query(
        None, description="Issues whose first event is at or after this time."
    ),
    end_date: Optional[datetime] = Query(
        None, description="Issues whose first event is before this time."
    ),
    project_id: Optional[UUID] = Query(None),
    flow_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> IssueCostReport:
    """Issue-level cost and cycle time, with per-project and per-flow sums."""
    account = get_account_or_404(db, current_user)
    _validate_period(start_date, end_date)
    return issue_cost_rollup.build_report(
        db,
        account_id=account.id,
        start=start_date,
        end=end_date,
        project_id=project_id,
        flow_id=flow_id,
    )


@router.get("/export")
@require_permission("view_cost")
def export_issue_costs(
    format: Literal["csv", "json"] = Query("csv"),
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    project_id: Optional[UUID] = Query(None),
    flow_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> Response:
    """Export the issue rows of the current filter as CSV or JSON.

    CSV is the flat issue grain plus one unassigned row. JSON carries the
    contributing execution ids per issue and for the unassigned bucket.
    """
    account = get_account_or_404(db, current_user)
    _validate_period(start_date, end_date)
    report = issue_cost_rollup.build_report(
        db,
        account_id=account.id,
        start=start_date,
        end=end_date,
        project_id=project_id,
        flow_id=flow_id,
        include_execution_ids=format == "json",
    )
    if format == "json":
        return Response(
            content=issue_cost_rollup.report_to_json(report),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="issue-costs.json"'},
        )
    return Response(
        content=issue_cost_rollup.report_to_csv(report),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="issue-costs.csv"'},
    )


# Declared before ``/{rollup_id}/executions`` so "unassigned" is not read as
# a rollup id.
@router.get("/unassigned/executions", response_model=list[IssueCostExecutionRow])
@require_permission("view_cost")
def list_unassigned_issue_cost_executions(
    start_date: Optional[datetime] = Query(
        None, description="Executions that started at or after this time."
    ),
    end_date: Optional[datetime] = Query(
        None, description="Executions that started before this time."
    ),
    project_id: Optional[UUID] = Query(None),
    flow_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> list[IssueCostExecutionRow]:
    """Executions in the unassigned bucket, for the report's drill-down."""
    account = get_account_or_404(db, current_user)
    _validate_period(start_date, end_date)
    return issue_cost_rollup.list_unassigned_executions(
        db,
        account_id=account.id,
        start=start_date,
        end=end_date,
        project_id=project_id,
        flow_id=flow_id,
    )


@router.get("/{rollup_id}/executions", response_model=list[IssueCostExecutionRow])
@require_permission("view_cost")
def list_issue_cost_executions(
    rollup_id: UUID,
    flow_id: Optional[UUID] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> list[IssueCostExecutionRow]:
    """Executions that contributed to one issue row."""
    account = get_account_or_404(db, current_user)
    rows = issue_cost_rollup.list_issue_executions(
        db, account_id=account.id, rollup_id=rollup_id, flow_id=flow_id
    )
    if rows is None:
        raise HTTPException(status_code=404, detail="Issue cost row not found")
    return rows


@router.post("/rebuild", response_model=IssueCostRebuildResponse)
@require_permission("manage_budgets")
def rebuild_issue_costs(
    rebuild_in: IssueCostRebuildRequest,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> IssueCostRebuildResponse:
    """Record finished executions of a window that have no issue fact yet.

    Bounded per call; repeat until ``limit_reached`` is false. Executions
    that fail to record are skipped, counted in ``failed`` and examined again
    on the next call.
    """
    account = get_account_or_404(db, current_user)
    window = rebuild_in.end_date - rebuild_in.start_date
    if window > timedelta(days=REBUILD_MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Window exceeds {REBUILD_MAX_WINDOW_DAYS} days; split the "
                "rebuild into smaller ranges"
            ),
        )
    examined, recorded, failed = issue_cost_rollup.rebuild(
        db,
        account_id=account.id,
        start=rebuild_in.start_date,
        end=rebuild_in.end_date,
    )
    db.commit()
    return IssueCostRebuildResponse(
        recorded=recorded,
        failed=failed,
        limit_reached=examined >= issue_cost_rollup.MAX_REBUILD_EXECUTIONS,
    )
