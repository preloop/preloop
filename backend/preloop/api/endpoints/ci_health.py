"""The console's CI health card for a flow that runs ``ci_health_report``."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints.flow_state import _flow
from preloop.models import models
from preloop.models.crud import crud_flow_execution
from preloop.models.crud import flow_state as crud_flow_state
from preloop.models.db.session import get_db_session as get_db
from preloop.schemas.ci_health import CiHealthView
from preloop.services.ci_health import ledger as ledger_mod
from preloop.services.ci_health.value import health_view
from preloop.utils.permissions import require_permission

router = APIRouter()


@router.get("/flows/{flow_id}/ci-health", response_model=CiHealthView)
@require_permission("view_flows")
def get_flow_ci_health(
    *,
    db: Session = Depends(get_db),
    flow_id: uuid.UUID,
    days: int = Query(default=7, ge=1, le=30),
    current_user: models.User = Depends(get_current_active_user),
) -> CiHealthView:
    """Trend, top flakes and the value summary of one flow.

    404 when the flow has no CI health ledger yet (it never ran the
    collector), so the console shows no card rather than an empty one.
    """
    flow = _flow(db, current_user, flow_id)
    row = crud_flow_state.get_state(
        db, account_id=flow.account_id, flow_id=flow.id, key=ledger_mod.LEDGER_KEY
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No CI health data for this flow")
    now = datetime.now(UTC)
    outcomes = crud_flow_execution.list_outcomes(
        db,
        flow_id=flow.id,
        account_id=flow.account_id,
        since=(now - timedelta(days=2 * days)).replace(tzinfo=None),
        limit=2000,
    )
    return CiHealthView(**health_view(row.value, outcomes, now=now, days=days))
