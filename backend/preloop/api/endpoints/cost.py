"""Open-source cost analytics endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.models.crud import crud_account, crud_api_usage
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.cost_analytics import (
    CostAnalyticsSummaryResponse,
    CostHealthResponse,
    ImportedUsageByModel,
    ImportedUsageSummary,
    PriceCatalogInfo,
)
from preloop.services.gateway_accounting_check import run_accounting_checks
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.utils.permissions import require_permission

router = APIRouter(prefix="/cost", tags=["Cost Analytics"])


def _price_catalog_info() -> Optional[PriceCatalogInfo]:
    """Return vendored price-catalog provenance for the staleness indicator."""
    try:
        from preloop.services.model_price_catalog import catalog_metadata

        meta = catalog_metadata()
        return PriceCatalogInfo(**meta) if meta else None
    except Exception:  # noqa: BLE001 - purely informational
        return None


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
    exclude_retries: bool = Query(
        False,
        description=(
            "Exclude rows marked as retries of an identical earlier request. "
            "Retries consume real provider tokens, so they count by default."
        ),
    ),
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
        include_breakdown=True,
        exclude_retries=exclude_retries,
    )
    # Imported (observed) spend is reported as a SEPARATE block, using the
    # summary's normalized window: it is never added into estimated_cost or
    # the budget figures (issue #123 — imported and gateway-metered spend
    # must not silently mix).
    imported_totals = crud_api_usage.get_imported_usage_summary(
        db,
        account_id=str(account.id),
        start_date=summary.period_start,
        end_date=summary.period_end,
        runtime_principal_id=runtime_principal_id,
    )
    imported_usage = None
    if imported_totals["event_count"]:
        imported_usage = ImportedUsageSummary(
            event_count=imported_totals["event_count"],
            total_tokens=imported_totals["total_tokens"],
            imported_cost=imported_totals["imported_cost"],
            usage_by_model=[
                ImportedUsageByModel(**row)
                for row in crud_api_usage.get_imported_usage_by_model(
                    db,
                    account_id=str(account.id),
                    start_date=summary.period_start,
                    end_date=summary.period_end,
                    runtime_principal_id=runtime_principal_id,
                )
            ],
        )
    return CostAnalyticsSummaryResponse(
        period_start=summary.period_start,
        period_end=summary.period_end,
        total_requests=summary.total_requests,
        successful_requests=summary.successful_requests,
        failed_requests=summary.failed_requests,
        token_usage=summary.token_usage,
        estimated_cost=summary.estimated_cost,
        unpriced_requests=summary.unpriced_requests,
        unpriced_tokens=summary.unpriced_tokens,
        price_catalog=_price_catalog_info(),
        budget=summary.budget,
        requests_by_day=summary.requests_by_day,
        usage_by_model=summary.usage_by_model,
        usage_by_flow=summary.usage_by_flow,
        usage_by_session=summary.usage_by_session,
        usage_by_tool=summary.usage_by_tool,
        imported_usage=imported_usage,
    )


@router.get("/health", response_model=CostHealthResponse)
@require_permission("view_cost")
def get_cost_health(
    hours: int = Query(
        24,
        ge=1,
        le=168,
        description="Lookback window in hours (max 168 = 7 days).",
    ),
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> CostHealthResponse:
    """Run the gateway accounting self-check over a lookback window.

    Verifies end-to-end that gateway traffic is recorded, streaming usage is
    captured, requests are priced, usage is provider-reported, and audit
    events are being written — so silent accounting breakage cannot go
    unnoticed.
    """
    account = _get_account_or_404(db, current_user)
    result = run_accounting_checks(db, account_id=str(account.id), window_hours=hours)
    return CostHealthResponse(**result)
