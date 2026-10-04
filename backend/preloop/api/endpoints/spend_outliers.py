"""Spend outlier alerts for the console Attention page (#960).

Findings are produced by the scheduled evaluation in
:mod:`preloop.services.spend_outliers`. These endpoints list the open ones
and read or write the account's thresholds. Dismiss and restore go through
the existing ``/attention/dismissals`` endpoints.
"""

from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models import models
from preloop.models.crud import crud_spend_outlier_settings
from preloop.models.db.session import get_db_session
from preloop.schemas.spend_outliers import (
    SpendOutlierFindingResponse,
    SpendOutlierListResponse,
    SpendOutlierSettingsPayload,
    SpendOutlierSettingsResponse,
)
from preloop.services.spend_outliers import SpendOutlierConfig, list_open_findings
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

router = APIRouter(prefix="/attention/spend-outliers", tags=["Attention"])


def _settings_response(
    row: models.SpendOutlierSettings | None,
) -> SpendOutlierSettingsResponse:
    config = SpendOutlierConfig.from_row(row)
    return SpendOutlierSettingsResponse(
        daily_multiple=config.daily_multiple,
        min_history_days=config.min_history_days,
        top_tier_model_prefixes=list(config.top_tier_model_prefixes),
        top_tier_share=config.top_tier_share,
        session_cost_threshold_usd=config.session_cost_threshold_usd,
        configured=row is not None,
    )


@router.get("", response_model=SpendOutlierListResponse)
@require_permission("view_cost")
def list_spend_outliers(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Open spend outliers: the latest finding per rule and user (or session).

    The console applies ``/attention/dismissals`` to these like any other
    attention item.
    """
    ensure_permission_in_oss(db, current_user, "view_cost")
    items = [
        SpendOutlierFindingResponse(**finding)
        for finding in list_open_findings(db, current_user.account_id)
    ]
    return SpendOutlierListResponse(items=items, total=len(items))


@router.get("/settings", response_model=SpendOutlierSettingsResponse)
@require_permission("view_cost")
def get_spend_outlier_settings(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """The account's spend outlier thresholds (defaults when never set)."""
    ensure_permission_in_oss(db, current_user, "view_cost")
    return _settings_response(
        crud_spend_outlier_settings.get_for_account(
            db, account_id=current_user.account_id
        )
    )


@router.put("/settings", response_model=SpendOutlierSettingsResponse)
@require_permission("manage_budgets")
def update_spend_outlier_settings(
    payload: SpendOutlierSettingsPayload,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Any:
    """Replace the account's spend outlier thresholds.

    Writing takes ``manage_budgets``, the same permission as budget limits.
    Saving here changes nothing by itself. Findings already recorded stand
    until the daily pass next judges their day: for accounts without an
    imported-spend replay that never happens, while an account with an
    active Copilot import has its 28 most recent days re-judged with the new
    thresholds on the next pass, which can update or supersede a recent
    finding (see :mod:`preloop.services.spend_outliers`).
    """
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    row = crud_spend_outlier_settings.upsert(
        db,
        account_id=current_user.account_id,
        values=payload.model_dump(),
    )
    return _settings_response(row)
