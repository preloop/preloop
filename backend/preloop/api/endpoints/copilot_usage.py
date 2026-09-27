"""GitHub Copilot usage import endpoints (Cost page, Copilot section).

These rows are imported from GitHub and are never gateway usage: they do not
change gateway totals, budgets or ingestion quota.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.api.loop_safety import run_db_off_loop
from preloop.models import models
from preloop.models.crud import crud_copilot_import_connection
from preloop.models.db.session import get_db_session
from preloop.schemas.copilot_usage import (
    CopilotConnectionResponse,
    CopilotConnectionUpsert,
    CopilotSyncResponse,
    CopilotUsageSummaryResponse,
)
from preloop.services.copilot_usage_import import (
    COPILOT_IMPORT_SECRET_KIND,
    build_copilot_summary,
    connection_payload,
)
from preloop.services.secret_service import get_secret_service
from preloop.sync.services.event_bus import event_bus_service
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cost/copilot", tags=["Cost Analytics"])

#: Default window when the caller does not pass one.
DEFAULT_WINDOW_DAYS = 30


@router.get("", response_model=CopilotUsageSummaryResponse)
@require_permission("view_cost")
def get_copilot_usage(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotUsageSummaryResponse:
    """Return imported Copilot seats, premium-request spend and model mix."""
    account = get_account_or_404(db, current_user)
    end = end_date or datetime.now(UTC)
    start = start_date or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start >= end:
        raise HTTPException(
            status_code=422, detail="start_date must be before end_date"
        )
    return CopilotUsageSummaryResponse(
        **build_copilot_summary(db, account_id=str(account.id), start=start, end=end)
    )


@router.put("/connection", response_model=CopilotConnectionResponse)
@require_permission("manage_budgets")
def upsert_copilot_connection(
    payload: CopilotConnectionUpsert,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotConnectionResponse:
    """Create or update the Copilot connection and its operator seat price."""
    account = get_account_or_404(db, current_user)
    secrets = get_secret_service()
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account.id
    )
    if connection is None and not payload.token:
        raise HTTPException(
            status_code=422, detail="A GitHub token is required to connect."
        )

    secret_id = connection.secret_reference_id if connection else None
    if payload.token:
        secret_id = secrets.create_local_secret_reference(
            db,
            account_id=account.id,
            name="GitHub Copilot organization token",
            secret_kind=COPILOT_IMPORT_SECRET_KIND,
            secret_value=payload.token,
            existing_secret_id=secret_id,
        ).id

    enterprise_secret_id = (
        connection.enterprise_secret_reference_id if connection else None
    )
    stale_enterprise_secret = None
    if payload.enterprise_token:
        enterprise_secret_id = secrets.create_local_secret_reference(
            db,
            account_id=account.id,
            name="GitHub Copilot enterprise billing token",
            secret_kind=COPILOT_IMPORT_SECRET_KIND,
            secret_value=payload.enterprise_token,
            existing_secret_id=enterprise_secret_id,
        ).id
    elif payload.clear_enterprise_token:
        stale_enterprise_secret = enterprise_secret_id
        enterprise_secret_id = None

    values = {
        "organization": payload.organization,
        "enterprise": payload.enterprise,
        "secret_reference_id": secret_id,
        "enterprise_secret_reference_id": enterprise_secret_id,
        "seat_price_monthly": payload.seat_price_monthly,
        "is_active": payload.is_active,
    }
    if connection is None:
        connection = crud_copilot_import_connection.create(
            db, obj_in={"account_id": account.id, **values}
        )
    else:
        if connection.organization != payload.organization:
            # A different organization starts its own history.
            values["last_synced_day"] = None
        connection = crud_copilot_import_connection.update(
            db, db_obj=connection, obj_in=values
        )
    if stale_enterprise_secret is not None:
        _delete_secret(db, stale_enterprise_secret, account.id)
    return CopilotConnectionResponse(**connection_payload(connection))


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("manage_budgets")
def delete_copilot_connection(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Response:
    """Remove the connection and its tokens; imported history is kept."""
    account = get_account_or_404(db, current_user)
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account.id
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="No Copilot connection")
    secret_ids = [
        connection.secret_reference_id,
        connection.enterprise_secret_reference_id,
    ]
    crud_copilot_import_connection.delete(db, id=connection.id)
    for secret_id in secret_ids:
        if secret_id is not None:
            _delete_secret(db, secret_id, account.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/connection/sync",
    response_model=CopilotSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("manage_budgets")
async def sync_copilot_connection(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotSyncResponse:
    """Queue an import for this account now (idempotent for the same day)."""

    def load() -> Optional[str]:
        account = get_account_or_404(db, current_user)
        connection = crud_copilot_import_connection.get_for_account(
            db, account_id=account.id
        )
        return str(account.id) if connection else None

    account_id = await run_db_off_loop(load)
    if account_id is None:
        raise HTTPException(status_code=404, detail="No Copilot connection")
    try:
        ack = await event_bus_service.publish_task(
            "ingest_copilot_usage", account_id=account_id
        )
    except Exception as exc:
        logger.exception("Failed to queue Copilot usage import")
        raise HTTPException(
            status_code=503, detail="Could not queue the Copilot import"
        ) from exc
    if ack is None:
        # The task bus was unreachable; say so instead of claiming a sync.
        raise HTTPException(
            status_code=503, detail="Could not queue the Copilot import"
        )
    return CopilotSyncResponse()


def _delete_secret(db: Session, secret_id: object, account_id: object) -> None:
    from preloop.models.crud import crud_secret_reference

    secret = crud_secret_reference.get_for_account(
        db, secret_id=str(secret_id), account_id=str(account_id)
    )
    if secret is not None:
        crud_secret_reference.delete(db, id=secret.id)
