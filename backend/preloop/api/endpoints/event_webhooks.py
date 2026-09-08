"""Outbound event webhooks: endpoint CRUD, test send, deliveries, replay.

Distinct from :mod:`preloop.api.endpoints.webhooks`, which receives inbound
tracker webhooks, and from the inbound flow trigger documented in
``docs/webhook-triggers.md``. This module is the other direction: Preloop
POSTing signed governance events to a customer's SIEM or GRC platform.

The delivery log is the operational surface: an integrator who is not
receiving events needs to see whether the event was queued, how many attempts
it took and what the receiver answered, without asking for a server log.

Permissions reuse ``view_policies`` / ``manage_policies``. A webhook endpoint
is a governance egress control and sits beside policies in the console; a
brand new permission would need seeding in the RBAC role matrix, which is not
part of this change.
"""

from __future__ import annotations

import logging
from typing import Annotated, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User
from preloop.models.models.webhook_endpoint import (
    DELIVERY_DEAD,
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.schemas.webhook_endpoint import (
    WebhookCatalogue,
    WebhookDeliveryRead,
    WebhookEndpointCreate,
    WebhookEndpointCreated,
    WebhookEndpointRead,
    WebhookEndpointUpdate,
    WebhookEventTypeInfo,
    WebhookReplayResult,
    WebhookTestSendResult,
)
from preloop.services.event_webhooks import outbox
from preloop.services.event_webhooks.events import (
    ENVELOPE_VERSION,
    EVENT_TEST,
    EVENT_TYPE_DESCRIPTIONS,
    EVENT_TYPES_V1,
)
from preloop.services.event_webhooks.targets import blocked_target_reason
from preloop.services.event_webhooks.signing import (
    DEFAULT_TOLERANCE_SECONDS,
    SIGNATURE_HEADER,
    generate_secret,
    secret_hint,
)
from preloop.utils.encryption import encrypt_value
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/event-webhooks", tags=["Outbound Webhooks"])

VIEW_PERMISSION = "view_policies"
MANAGE_PERMISSION = "manage_policies"

# An account cannot register an unbounded number of endpoints: every event
# fans out to all of them, so the ceiling is a load control, not policy.
MAX_ENDPOINTS_PER_ACCOUNT = 20


def _to_read(endpoint: WebhookEndpoint) -> WebhookEndpointRead:
    """Render an endpoint row for the API, without its secret."""
    return WebhookEndpointRead(
        id=endpoint.id,
        url=endpoint.url,
        description=endpoint.description,
        event_types=list(endpoint.event_types or []),
        active=bool(endpoint.active),
        source=endpoint.source,
        secret_hint=endpoint.secret_hint,
        created_by_user_id=endpoint.created_by_user_id,
        consecutive_failures=endpoint.consecutive_failures or 0,
        circuit_open=endpoint.circuit_opened_at is not None,
        last_delivery_status=endpoint.last_delivery_status,
        last_delivery_at=endpoint.last_delivery_at,
        last_response_code=endpoint.last_response_code,
        last_error=endpoint.last_error,
        created_at=getattr(endpoint, "created_at", None),
    )


def _get_owned(db: Session, account_id, endpoint_id: UUID) -> WebhookEndpoint:
    """Fetch an endpoint or 404. Scoping to the account is mandatory."""
    endpoint = db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id,
            WebhookEndpoint.account_id == account_id,
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Webhook endpoint not found"
        )
    return endpoint


def _reject_blocked_target(url: str) -> None:
    """Refuse an internal target when the deployment blocks them."""
    reason = blocked_target_reason(url)
    if reason:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This deployment refuses webhook targets in {reason} address "
                "space. Use a publicly routable URL."
            ),
        )


def _reject_shim_edit(endpoint: WebhookEndpoint, verb: str) -> None:
    """Shim rows mirror an approval workflow; they are edited over there."""
    if endpoint.source != SOURCE_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This endpoint is managed by an approval workflow. "
                f"{verb} the workflow's webhook_url instead."
            ),
        )


@router.get("/catalogue", response_model=WebhookCatalogue)
@require_permission(VIEW_PERMISSION)
async def get_webhook_catalogue(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Return the subscribable events and the delivery contract."""
    return WebhookCatalogue(
        version=ENVELOPE_VERSION,
        event_types=[
            WebhookEventTypeInfo(
                name=name, description=EVENT_TYPE_DESCRIPTIONS.get(name, "")
            )
            for name in EVENT_TYPES_V1
        ],
        signature_header=SIGNATURE_HEADER,
        tolerance_seconds=DEFAULT_TOLERANCE_SECONDS,
        max_attempts=outbox.MAX_ATTEMPTS,
        retry_delays_seconds=list(outbox.RETRY_BASE_DELAYS),
    )


@router.get("/endpoints", response_model=List[WebhookEndpointRead])
@require_permission(VIEW_PERMISSION)
async def list_webhook_endpoints(
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List this account's webhook endpoints, newest first.

    Includes the read-only rows the approval-workflow shim owns, so an
    operator can see why a legacy approval webhook is failing.
    """
    rows = (
        db.execute(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.account_id == account.id)
            .order_by(WebhookEndpoint.created_at.desc())
        )
        .scalars()
        .all()
    )
    return [_to_read(row) for row in rows]


@router.post(
    "/endpoints",
    response_model=WebhookEndpointCreated,
    status_code=status.HTTP_201_CREATED,
)
@require_permission(MANAGE_PERMISSION)
async def create_webhook_endpoint(
    payload: WebhookEndpointCreate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Create an endpoint and return its signing secret, once."""
    _reject_blocked_target(payload.url)
    existing = (
        db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.account_id == account.id,
                WebhookEndpoint.source == SOURCE_ACCOUNT,
            )
        )
        .scalars()
        .all()
    )
    if len(existing) >= MAX_ENDPOINTS_PER_ACCOUNT:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"An account may register at most {MAX_ENDPOINTS_PER_ACCOUNT} "
                "webhook endpoints"
            ),
        )

    secret = generate_secret()
    endpoint = WebhookEndpoint(
        account_id=account.id,
        url=payload.url,
        description=payload.description,
        event_types=payload.event_types,
        active=payload.active,
        source=SOURCE_ACCOUNT,
        secret_encrypted=encrypt_value(secret),
        secret_hint=secret_hint(secret),
        created_by_user_id=current_user.id,
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)

    return WebhookEndpointCreated(**_to_read(endpoint).model_dump(), secret=secret)


@router.patch("/endpoints/{endpoint_id}", response_model=WebhookEndpointRead)
@require_permission(MANAGE_PERMISSION)
async def update_webhook_endpoint(
    endpoint_id: UUID,
    payload: WebhookEndpointUpdate,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Update an endpoint's URL, filter, description or active flag."""
    endpoint = _get_owned(db, account.id, endpoint_id)
    _reject_shim_edit(endpoint, "Edit")

    fields = payload.model_dump(exclude_unset=True)
    if fields.get("url"):
        _reject_blocked_target(fields["url"])
    for name, value in fields.items():
        setattr(endpoint, name, value)
    if "url" in fields or fields.get("active") is True:
        # An operator changing the URL or re-enabling the endpoint is fixing
        # something. Close the breaker so the fix is tried on the next tick.
        endpoint.consecutive_failures = 0
        endpoint.circuit_opened_at = None
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    return _to_read(endpoint)


@router.delete("/endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission(MANAGE_PERMISSION)
async def delete_webhook_endpoint(
    endpoint_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Delete an endpoint and its delivery history."""
    endpoint = _get_owned(db, account.id, endpoint_id)
    _reject_shim_edit(endpoint, "Clear")
    db.delete(endpoint)
    db.commit()
    return None


@router.post("/endpoints/{endpoint_id}/test", response_model=WebhookTestSendResult)
@require_permission(MANAGE_PERMISSION)
async def test_webhook_endpoint(
    endpoint_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Queue a ``webhook.test`` event for one endpoint.

    Queued, not sent: the response says the event is on its way, and the
    deliveries list says what the receiver actually answered. Reporting the
    outcome synchronously would mean putting an HTTP call back on the request
    path, which is the thing this change removed.
    """
    endpoint = _get_owned(db, account.id, endpoint_id)
    result = outbox.enqueue_event(
        db,
        account_id=account.id,
        event_type=EVENT_TEST,
        data={
            "message": "Test event from Preloop.",
            "endpoint_id": str(endpoint.id),
            "requested_by_user_id": str(current_user.id),
        },
        endpoints=[endpoint],
    )
    db.commit()
    if result.skipped_queue_full:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="The delivery queue for this account is full",
        )
    return WebhookTestSendResult(
        event_id=result.event_id,
        delivery_ids=result.delivery_ids,
        queued=len(result.delivery_ids),
    )


@router.get("/deliveries/dead-letter", response_model=List[WebhookDeliveryRead])
@require_permission(VIEW_PERMISSION)
async def list_dead_letter_deliveries(
    account: Annotated[Account, Depends(get_account_for_user)],
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List deliveries that exhausted their retries."""
    rows = (
        db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.account_id == account.id,
                WebhookDelivery.status == DELIVERY_DEAD,
            )
            .order_by(WebhookDelivery.updated_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    return [WebhookDeliveryRead.model_validate(row) for row in rows]


@router.get("/deliveries", response_model=List[WebhookDeliveryRead])
@require_permission(VIEW_PERMISSION)
async def list_webhook_deliveries(
    account: Annotated[Account, Depends(get_account_for_user)],
    endpoint_id: Optional[UUID] = Query(None),
    delivery_status: Optional[str] = Query(
        None, description="pending, delivered or dead"
    ),
    limit: int = Query(50, ge=1, le=200),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """List recent deliveries, newest first.

    Dead-lettered rows are visible here rather than only in a server log:
    they are the ones an operator needs to find and replay.
    """
    query = select(WebhookDelivery).where(WebhookDelivery.account_id == account.id)
    if endpoint_id is not None:
        query = query.where(WebhookDelivery.endpoint_id == endpoint_id)
    if delivery_status:
        query = query.where(WebhookDelivery.status == delivery_status)
    rows = (
        db.execute(query.order_by(WebhookDelivery.created_at.desc()).limit(limit))
        .scalars()
        .all()
    )
    return [WebhookDeliveryRead.model_validate(row) for row in rows]


@router.post("/deliveries/{event_id}/replay", response_model=WebhookReplayResult)
@require_permission(MANAGE_PERMISSION)
async def replay_webhook_event(
    event_id: UUID,
    account: Annotated[Account, Depends(get_account_for_user)],
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
):
    """Re-queue one event id to every endpoint that already received it.

    The original rows are left alone: a replay is a new generation, so a
    dead-lettered attempt stays on the record.
    """
    delivery_ids = outbox.replay_event(db, account_id=account.id, event_id=event_id)
    if not delivery_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No deliveries found for that event id",
        )
    db.commit()
    return WebhookReplayResult(
        event_id=event_id, delivery_ids=delivery_ids, queued=len(delivery_ids)
    )
