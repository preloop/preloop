"""GitHub Copilot usage import endpoints (Cost page, Copilot section).

These rows are imported from GitHub and are never gateway usage: they do not
change gateway totals, budgets or ingestion quota.

The ``/mappings`` routes (#1061) let an operator say which GitHub login is
which Preloop user, so the spend outlier rules can evaluate imported
premium-request spend per developer. ``/spend-coverage`` reports how much of
the stored spend those mappings cover and why the rest is left out.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Optional

from anyio import from_thread

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models import models
from preloop.models.crud import crud_copilot_import_connection
from preloop.models.db.session import get_db_session
from preloop.schemas.copilot_usage import (
    CopilotConnectionResponse,
    CopilotConnectionUpsert,
    CopilotSpendCoverageResponse,
    CopilotSyncResponse,
    CopilotUsageSummaryResponse,
    CopilotUserMappingListResponse,
    CopilotUserMappingResponse,
    CopilotUserMappingUpsert,
)
from preloop.services.copilot_spend_source import (
    CopilotMappingError,
    delete_user_mapping,
    list_user_mappings,
    mapping_payload,
    spend_coverage,
    upsert_user_mapping,
)
from preloop.services.copilot_usage_import import (
    COPILOT_IMPORT_SECRET_KIND,
    build_copilot_summary,
    connection_payload,
)
from preloop.services.secret_service import get_secret_service
from preloop.sync.services.event_bus import event_bus_service
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/cost/copilot", tags=["Cost Analytics"])

#: Default window when the caller does not pass one.
DEFAULT_WINDOW_DAYS = 30
#: Default coverage window: the replay horizon of the spend outlier pass.
COVERAGE_WINDOW_DAYS = 28


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
    }
    if payload.is_active is not None:
        values["is_active"] = payload.is_active
    elif connection is None:
        values["is_active"] = True
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
def sync_copilot_connection(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotSyncResponse:
    """Queue an import for this account now (idempotent for the same day).

    A plain ``def`` handler runs on the threadpool, so the database lookup
    never blocks the event loop; only the publish hops back onto the loop.
    """
    account = get_account_or_404(db, current_user)
    connection = crud_copilot_import_connection.get_for_account(
        db, account_id=account.id
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="No Copilot connection")
    if not connection.is_active:
        # The worker skips paused connections, so queuing would report a
        # sync that never runs.
        raise HTTPException(
            status_code=409,
            detail="The Copilot connection is paused. Resume it to import.",
        )
    account_id = str(account.id)

    async def publish() -> object:
        return await event_bus_service.publish_task(
            "ingest_copilot_usage", account_id=account_id
        )

    try:
        ack = from_thread.run(publish)
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


# ---------------------------------------------------------------------------
# Login to user mappings (#1061)
# ---------------------------------------------------------------------------


def _mapping_http_error(exc: CopilotMappingError) -> HTTPException:
    """The status a refused mapping write gets.

    A missing connection and an unusable target are both 404: the message
    for a target never says whether the id exists in another account.
    """
    if exc.reason == "invalid_login":
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=404, detail=str(exc))


@router.get("/mappings", response_model=CopilotUserMappingListResponse)
@require_permission("view_cost")
def list_copilot_user_mappings(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotUserMappingListResponse:
    """GitHub login to user mappings for the connected organization."""
    ensure_permission_in_oss(db, current_user, "view_cost")
    account = get_account_or_404(db, current_user)
    listing = list_user_mappings(db, account_id=account.id)
    items = [CopilotUserMappingResponse(**item) for item in listing["items"]]
    return CopilotUserMappingListResponse(
        organization=listing["organization"], items=items, total=len(items)
    )


@router.put("/mappings", response_model=CopilotUserMappingResponse)
@require_permission("manage_budgets")
def upsert_copilot_user_mapping(
    payload: CopilotUserMappingUpsert,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotUserMappingResponse:
    """Map a login to an active user of this account; repeats update the one row.

    Needs an active Copilot connection (404 otherwise). A user id that is not
    an active user of this account is a 404 with one fixed message, so the
    response never reveals whether the id belongs to someone else.
    """
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    try:
        mapping, user_name = upsert_user_mapping(
            db,
            account_id=account.id,
            github_login=payload.github_login,
            user_id=payload.user_id,
        )
    except CopilotMappingError as exc:
        raise _mapping_http_error(exc) from exc
    return CopilotUserMappingResponse(**mapping_payload(mapping, user_name))


@router.delete("/mappings/{github_login}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("manage_budgets")
def delete_copilot_user_mapping(
    github_login: str,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Response:
    """Remove one login's mapping (any letter case)."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    try:
        removed = delete_user_mapping(
            db, account_id=account.id, github_login=github_login
        )
    except CopilotMappingError as exc:
        raise _mapping_http_error(exc) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="No mapping for that login")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/spend-coverage", response_model=CopilotSpendCoverageResponse)
@require_permission("view_cost")
def get_copilot_spend_coverage(
    start_day: Optional[date] = Query(None),
    end_day: Optional[date] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CopilotSpendCoverageResponse:
    """Row counts per outcome for the spend outlier rules, UTC days inclusive.

    Defaults to the 28 completed days ending yesterday, which is the window
    the daily pass replays.
    """
    ensure_permission_in_oss(db, current_user, "view_cost")
    account = get_account_or_404(db, current_user)
    end = end_day or (datetime.now(UTC).date() - timedelta(days=1))
    start = start_day or (end - timedelta(days=COVERAGE_WINDOW_DAYS - 1))
    if start > end:
        raise HTTPException(
            status_code=422, detail="start_day must not be after end_day"
        )
    return CopilotSpendCoverageResponse(
        **spend_coverage(db, account_id=account.id, start_day=start, end_day=end)
    )
