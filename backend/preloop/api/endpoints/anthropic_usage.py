"""Anthropic usage import endpoints (Cost page, Anthropic section, #1413).

Imported Claude Code Analytics rows are never gateway usage: they do not
change gateway totals, budgets or ingestion quota. The Admin API key is
write-only and never appears in a response or a log line.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Optional

from anyio import from_thread
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.models import models
from preloop.models.crud import (
    crud_anthropic_import_connection,
    crud_anthropic_user_mapping,
    crud_secret_reference,
    crud_user,
)
from preloop.models.crud.anthropic_import import canonical_actor
from preloop.models.db.session import get_db_session
from preloop.schemas.anthropic_usage import (
    AnthropicConnectionResponse,
    AnthropicConnectionTestResponse,
    AnthropicConnectionUpsert,
    AnthropicSyncResponse,
    AnthropicUsageSummaryResponse,
    AnthropicUserMappingListResponse,
    AnthropicUserMappingResponse,
    AnthropicUserMappingUpsert,
)
from preloop.services.anthropic_usage_import import (
    ANTHROPIC_IMPORT_SECRET_KIND,
    AnthropicAdminClient,
    AnthropicImportError,
    _default_http_client,
    build_anthropic_summary,
    connection_payload,
    resolve_admin_key,
    test_connection,
)
from preloop.services.secret_service import get_secret_service
from preloop.sync.services.event_bus import event_bus_service
from preloop.utils.permissions import ensure_permission_in_oss, require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/anthropic-usage", tags=["Cost Analytics"])

DEFAULT_WINDOW_DAYS = 30


def _connection_or_404(
    db: Session, account_id: object
) -> models.AnthropicImportConnection:
    connection = crud_anthropic_import_connection.get_for_account(
        db, account_id=account_id
    )
    if connection is None:
        raise HTTPException(status_code=404, detail="No Anthropic connection")
    return connection


@router.get("", response_model=AnthropicUsageSummaryResponse)
@require_permission("view_cost")
def get_anthropic_usage(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicUsageSummaryResponse:
    """Imported Claude Code usage outside the gateway, by actor and model."""
    ensure_permission_in_oss(db, current_user, "view_cost")
    account = get_account_or_404(db, current_user)
    end = end_date or datetime.now(UTC)
    start = start_date or end - timedelta(days=DEFAULT_WINDOW_DAYS)
    if start >= end:
        raise HTTPException(
            status_code=422, detail="start_date must be before end_date"
        )
    return AnthropicUsageSummaryResponse(
        **build_anthropic_summary(db, account_id=str(account.id), start=start, end=end)
    )


@router.put("/connection", response_model=AnthropicConnectionResponse)
@require_permission("manage_budgets")
def upsert_anthropic_connection(
    payload: AnthropicConnectionUpsert,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicConnectionResponse:
    """Create or update the connection; the Admin key is write-only."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = crud_anthropic_import_connection.get_for_account(
        db, account_id=account.id
    )
    if connection is None and not payload.admin_key:
        raise HTTPException(
            status_code=422, detail="An Anthropic Admin API key is required."
        )
    values: dict = {}
    if payload.admin_key:
        key = payload.admin_key.strip()
        values["secret_reference_id"] = (
            get_secret_service()
            .create_local_secret_reference(
                db,
                account_id=account.id,
                name="Anthropic Admin API key (usage import)",
                secret_kind=ANTHROPIC_IMPORT_SECRET_KIND,
                secret_value=key,
                existing_secret_id=(
                    connection.secret_reference_id if connection else None
                ),
            )
            .id
        )
        values["key_hint"] = key[-4:]
    if payload.gateway_key_names is not None:
        values["gateway_key_names"] = payload.gateway_key_names
    if payload.is_active is not None:
        values["is_active"] = payload.is_active
    if connection is None:
        values.setdefault("is_active", True)
        connection = crud_anthropic_import_connection.create(
            db, obj_in={"account_id": account.id, **values}
        )
    elif values:
        connection = crud_anthropic_import_connection.update(
            db, db_obj=connection, obj_in=values
        )
    return AnthropicConnectionResponse(**connection_payload(connection))


@router.delete("/connection", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("manage_budgets")
def delete_anthropic_connection(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Response:
    """Remove the connection and its key; imported history is kept."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = _connection_or_404(db, account.id)
    secret_id = connection.secret_reference_id
    crud_anthropic_import_connection.delete(db, id=connection.id)
    secret = crud_secret_reference.get_for_account(
        db, secret_id=str(secret_id), account_id=str(account.id)
    )
    if secret is not None:
        crud_secret_reference.delete(db, id=secret.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/connection/test", response_model=AnthropicConnectionTestResponse)
@require_permission("manage_budgets")
def test_anthropic_connection(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicConnectionTestResponse:
    """Check the stored key with the cheapest read (one API key listed)."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = _connection_or_404(db, account.id)
    key = resolve_admin_key(db, connection)
    if not key:
        return AnthropicConnectionTestResponse(
            ok=False, error="The Admin API key is missing."
        )
    try:
        with _default_http_client() as http:
            test_connection(AnthropicAdminClient(http, key))
    except AnthropicImportError as exc:
        return AnthropicConnectionTestResponse(ok=False, error=str(exc))
    return AnthropicConnectionTestResponse(ok=True)


@router.post(
    "/sync",
    response_model=AnthropicSyncResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
@require_permission("manage_budgets")
def sync_anthropic_usage(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicSyncResponse:
    """Queue an import for this account now (idempotent for the same days)."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = _connection_or_404(db, account.id)
    if not connection.is_active:
        raise HTTPException(
            status_code=409,
            detail="The Anthropic connection is paused. Resume it to import.",
        )
    account_id = str(account.id)

    async def publish() -> object:
        return await event_bus_service.publish_task(
            "ingest_anthropic_usage", account_id=account_id
        )

    try:
        ack = from_thread.run(publish)
    except Exception as exc:
        logger.exception("Failed to queue Anthropic usage import")
        raise HTTPException(
            status_code=503, detail="Could not queue the Anthropic import"
        ) from exc
    if ack is None:
        raise HTTPException(
            status_code=503, detail="Could not queue the Anthropic import"
        )
    return AnthropicSyncResponse()


# ---------------------------------------------------------------------------
# Actor to user mappings
# ---------------------------------------------------------------------------


def _mapping_response(
    mapping: models.AnthropicUserMapping,
) -> AnthropicUserMappingResponse:
    return AnthropicUserMappingResponse(
        actor=mapping.actor,
        user_id=mapping.user_id,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


@router.get("/mappings", response_model=AnthropicUserMappingListResponse)
@require_permission("view_cost")
def list_anthropic_user_mappings(
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicUserMappingListResponse:
    """Explicit actor to user mappings of this account."""
    ensure_permission_in_oss(db, current_user, "view_cost")
    account = get_account_or_404(db, current_user)
    connection = crud_anthropic_import_connection.get_for_account(
        db, account_id=account.id
    )
    if connection is None:
        return AnthropicUserMappingListResponse()
    items = [
        _mapping_response(m)
        for m in crud_anthropic_user_mapping.list_for_connection(
            db, connection=connection
        )
    ]
    return AnthropicUserMappingListResponse(items=items, total=len(items))


@router.put("/mappings", response_model=AnthropicUserMappingResponse)
@require_permission("manage_budgets")
def upsert_anthropic_user_mapping(
    payload: AnthropicUserMappingUpsert,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> AnthropicUserMappingResponse:
    """Map an actor to an active user of this account; repeats update one row."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = _connection_or_404(db, account.id)
    try:
        actor = canonical_actor(payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    user = crud_user.get(db, id=payload.user_id)
    if user is None or str(user.account_id) != str(account.id) or not user.is_active:
        raise HTTPException(
            status_code=404, detail="No active user with that id in this account."
        )
    mapping = crud_anthropic_user_mapping.upsert(
        db, connection=connection, actor=actor, user_id=user.id
    )
    return _mapping_response(mapping)


@router.delete("/mappings/{actor}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("manage_budgets")
def delete_anthropic_user_mapping(
    actor: str,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> Response:
    """Remove one actor's mapping."""
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    account = get_account_or_404(db, current_user)
    connection = _connection_or_404(db, account.id)
    try:
        removed = crud_anthropic_user_mapping.delete_for_actor(
            db, connection=connection, actor=actor
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="No mapping for that actor")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
