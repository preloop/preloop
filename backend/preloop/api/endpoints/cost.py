"""Open-source cost analytics endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models.crud import crud_account
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.cost_analytics import CostAnalyticsSummaryResponse
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/cost", tags=["Cost Analytics"])


def _get_account_or_404(db: Session, current_user: User) -> Any:
    account = crud_account.get(db=db, id=current_user.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Account not found")
    return account


@router.get("/summary", response_model=CostAnalyticsSummaryResponse)
@require_permission("view_cost")
def get_cost_summary(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    runtime_principal_id: Optional[str] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> CostAnalyticsSummaryResponse:
    """Return the OSS cost overview using gateway usage and pricing metadata."""
    account = _get_account_or_404(db, current_user)
    summary = ModelGatewayUsageService(db).get_account_summary(
        account=account,
        start_date=start_date,
        end_date=end_date,
        runtime_principal_id=runtime_principal_id,
    )
    return CostAnalyticsSummaryResponse(
        period_start=summary.period_start,
        period_end=summary.period_end,
        total_requests=summary.total_requests,
        successful_requests=summary.successful_requests,
        failed_requests=summary.failed_requests,
        token_usage=summary.token_usage,
        estimated_cost=summary.estimated_cost,
        budget=summary.budget,
        requests_by_day=summary.requests_by_day,
        usage_by_model=summary.usage_by_model,
        usage_by_flow=summary.usage_by_flow,
        usage_by_session=summary.usage_by_session,
        usage_by_tool=summary.usage_by_tool,
    )
