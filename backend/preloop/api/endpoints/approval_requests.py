"""API endpoints for approval requests."""

import logging
import os
import uuid
from datetime import datetime
from typing import AsyncGenerator, Optional, Union

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.services.approval_attribution import (
    attach_attribution,
    attributed,
    attributed_async,
)
from preloop.services.approval_service import ApprovalService
from preloop.models.crud import crud_approval_event, crud_approval_request
from preloop.models.db.session import get_async_db_session, get_db_session
from preloop.models.models import ApprovalRequest
from preloop.models.models.user import User
from preloop.models.schemas.approval_request import (
    ApprovalBatchDecision,
    ApprovalBatchItemResult,
    ApprovalBatchResponse,
    ApprovalRequestResponse,
    ApprovalDecision,
    ApprovalEventResponse,
)
from preloop.utils.permissions import require_permission

router = APIRouter(
    prefix="/approval-requests",
    tags=["approval_requests"],
)

logger = logging.getLogger(__name__)

#: Channel label recorded on the timeline for decisions made through the
#: authenticated API (web console and mobile app sessions).
AUTHENTICATED_DECISION_CHANNEL = "console"


async def _async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async session for handlers that must not block the event loop.

    ``get_db_session`` on an ``async def`` route is a latent liveness risk:
    the first sync query waits on the loop. Bulk decide uses this instead.
    """
    async with get_async_db_session() as session:
        yield session


def _pending_request_has_expired(approval_request: ApprovalRequest) -> bool:
    """True when a still-pending row is past ``expires_at``.

    Matches ``ApprovalService._reject_if_not_actionable``: naive UTC, so a
    request the operator is staring at can expire without a status write.
    """
    expires_at = approval_request.expires_at
    if expires_at is None:
        return False
    if getattr(expires_at, "tzinfo", None) is not None:
        expires_at = expires_at.replace(tzinfo=None)
    return datetime.utcnow() > expires_at


def _record_viewed_event(
    db: Session, approval_request: ApprovalRequest, actor_id: Union[uuid.UUID, None]
) -> None:
    """Append one ``viewed`` timeline entry per viewer (best-effort).

    Deduped so refreshing the page does not flood the history; anonymous
    (token) views are recorded by the public endpoint instead.
    """
    try:
        already_viewed = crud_approval_event.has_event(
            db,
            approval_request_id=approval_request.id,
            event_type="viewed",
            actor_id=actor_id,
        )
        if not already_viewed:
            crud_approval_event.record(
                db,
                approval_request_id=approval_request.id,
                account_id=approval_request.account_id,
                event_type="viewed",
                detail="Approval request opened",
                actor_id=actor_id,
            )
    except Exception:
        db.rollback()
        # View tracking must never break reading the request.
        logger.debug(
            "Failed to record viewed event for approval %s",
            approval_request.id,
            exc_info=True,
        )


@router.get("/{request_id}", response_model=ApprovalRequestResponse)
@require_permission("view_approvals")
def get_approval_request(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalRequestResponse:
    """Get an approval request by ID.

    Args:
        request_id: Approval request ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Approval request

    Raises:
        HTTPException: If request not found or unauthorized
    """
    # Use CRUD layer with account_id filtering
    approval_request = crud_approval_request.get(
        db, id=str(request_id), account_id=current_user.account_id
    )

    if not approval_request:
        raise HTTPException(status_code=404, detail="Approval request not found")

    # Track who opened the request (one timeline entry per viewer).
    _record_viewed_event(db, approval_request, current_user.id)

    # Name the agent, key, session and flow run instead of leaving the detail
    # page with four bare ids (the "Agent: AI agent" report), then convert
    # while the request session is still open so serialization cannot hit a
    # detached instance.
    return ApprovalRequestResponse.model_validate(attributed(db, approval_request))


@router.get("/{request_id}/history", response_model=list[ApprovalEventResponse])
@require_permission("view_approvals")
def get_approval_request_history(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[ApprovalEventResponse]:
    """Get the workflow-history timeline of an approval request.

    Returns every lifecycle event in order: request creation, per-channel
    notification fan-outs, opens, votes (with actor), escalations, and the
    final resolution or expiry.

    Args:
        request_id: Approval request ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        Timeline events ordered by timestamp

    Raises:
        HTTPException: If request not found or unauthorized
    """
    approval_request = crud_approval_request.get(
        db, id=str(request_id), account_id=current_user.account_id
    )
    if not approval_request:
        raise HTTPException(status_code=404, detail="Approval request not found")

    events = crud_approval_event.get_by_request(
        db, approval_request_id=approval_request.id
    )

    # Resolve actor identities in one query so the timeline can show WHO
    # voted without exposing a bare UUID.
    actor_ids = {event.actor_id for event in events if event.actor_id is not None}
    actors: dict = {}
    if actor_ids:
        actors = {
            user.id: user
            for user in db.query(User).filter(User.id.in_(actor_ids)).all()
        }

    response: list[ApprovalEventResponse] = []
    for event in events:
        actor = actors.get(event.actor_id) if event.actor_id else None
        response.append(
            ApprovalEventResponse(
                id=event.id,
                event_type=event.event_type,
                detail=event.detail,
                comment=event.comment,
                actor_id=event.actor_id,
                actor_email=(actor.email or actor.username) if actor else None,
                timestamp=event.timestamp,
            )
        )
    return response


@router.get("", response_model=list[ApprovalRequestResponse])
@require_permission("view_approvals")
def list_approval_requests(
    status: Optional[str] = Query(None, description="Filter by status"),
    execution_id: Optional[str] = Query(None, description="Filter by execution ID"),
    limit: int = Query(50, le=100, description="Maximum number of results"),
    skip: int = Query(0, description="Number of results to skip"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[ApprovalRequestResponse]:
    """List approval requests for the current account.

    Args:
        status: Filter by status (pending, approved, declined, etc.)
        execution_id: Filter by execution ID
        limit: Maximum number of results
        skip: Number of results to skip
        current_user: Current authenticated user

    Returns:
        List of approval requests
    """
    # Use CRUD layer to get approval requests with filters
    rows = crud_approval_request.get_multi_by_account(
        db,
        account_id=current_user.account_id,
        execution_id=execution_id,
        status=status,
        skip=skip,
        limit=limit,
    )
    # One batched pass for the page, not four lookups per row, then convert
    # every row while the request session is still open.
    return [
        ApprovalRequestResponse.model_validate(row)
        for row in attach_attribution(db, rows)
    ]


@router.post("/{request_id}/approve", response_model=ApprovalRequestResponse)
@require_permission("decide_approvals")
async def approve_request(
    request_id: uuid.UUID,
    decision: ApprovalDecision,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    # Required by @require_permission (fail-closed checks kwargs["db"]).
    # Handler body uses get_async_db_session() for ApprovalService work.
    db: Session = Depends(get_db_session),
) -> ApprovalRequestResponse:
    """Approve an approval request.

    Args:
        request_id: Approval request ID
        decision: Approval decision with optional comment
        request: HTTP request
        current_user: Current authenticated user

    Returns:
        Updated approval request

    Raises:
        HTTPException: If request not found or unauthorized
    """
    _ = db  # Injected for @require_permission; not used by handler body.
    # Get base URL from request
    base_url = os.getenv("PRELOOP_URL", str(request.base_url).rstrip("/"))

    async with get_async_db_session() as async_db:
        approval_service = ApprovalService(async_db, base_url)

        # Get approval request
        approval_request = await approval_service.get_approval_request(request_id)
        if not approval_request:
            raise HTTPException(status_code=404, detail="Approval request not found")

        # Check authorization
        if approval_request.account_id != current_user.account_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to approve this request"
            )

        # Check if already resolved
        if approval_request.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Request already {approval_request.status}",
            )

        # Approve (pass user_id for quorum tracking)
        updated = await approval_service.approve_request(
            request_id,
            decision.effective_comment,
            user_id=current_user.id,
            channel=AUTHENTICATED_DECISION_CHANNEL,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to approve request")

        # Name the agent, key, session and flow run on the async session the
        # handler already holds (the sync request session would block the
        # event loop), then convert while the write session is still open to
        # avoid DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(
            await attributed_async(async_db, updated)
        )


@router.post("/{request_id}/decline", response_model=ApprovalRequestResponse)
@require_permission("decide_approvals")
async def decline_request(
    request_id: uuid.UUID,
    decision: ApprovalDecision,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    # Required by @require_permission (fail-closed checks kwargs["db"]).
    # Handler body uses get_async_db_session() for ApprovalService work.
    db: Session = Depends(get_db_session),
) -> ApprovalRequestResponse:
    """Decline an approval request.

    Args:
        request_id: Approval request ID
        decision: Approval decision with optional comment
        request: HTTP request
        current_user: Current authenticated user

    Returns:
        Updated approval request

    Raises:
        HTTPException: If request not found or unauthorized
    """
    _ = db  # Injected for @require_permission; not used by handler body.
    # Get base URL from request
    base_url = os.getenv("PRELOOP_URL", str(request.base_url).rstrip("/"))

    async with get_async_db_session() as async_db:
        approval_service = ApprovalService(async_db, base_url)

        # Get approval request
        approval_request = await approval_service.get_approval_request(request_id)
        if not approval_request:
            raise HTTPException(status_code=404, detail="Approval request not found")

        # Check authorization
        if approval_request.account_id != current_user.account_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to decline this request"
            )

        # Check if already resolved
        if approval_request.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Request already {approval_request.status}",
            )

        # Decline (pass user_id for quorum tracking)
        updated = await approval_service.decline_request(
            request_id,
            decision.effective_comment,
            user_id=current_user.id,
            channel=AUTHENTICATED_DECISION_CHANNEL,
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to decline request")

        # Name the agent, key, session and flow run on the async session the
        # handler already holds (the sync request session would block the
        # event loop), then convert while the write session is still open to
        # avoid DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(
            await attributed_async(async_db, updated)
        )


@router.post("/{request_id}/decide", response_model=ApprovalRequestResponse)
@require_permission("decide_approvals")
async def decide_request(
    request_id: uuid.UUID,
    decision: ApprovalDecision,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    # Required by @require_permission (fail-closed checks kwargs["db"]).
    # Handler body uses get_async_db_session() for ApprovalService work.
    db: Session = Depends(get_db_session),
) -> ApprovalRequestResponse:
    """Approve or decline an approval request based on decision.approved.

    This is a convenience endpoint that calls approve or decline based on
    the decision.approved boolean.

    Args:
        request_id: Approval request ID
        decision: Approval decision with approved flag and optional comment
        request: HTTP request
        current_user: Current authenticated user

    Returns:
        Updated approval request

    Raises:
        HTTPException: If request not found or unauthorized
    """
    _ = db  # Injected for @require_permission; not used by handler body.
    # Get base URL from request
    base_url = os.getenv("PRELOOP_URL", str(request.base_url).rstrip("/"))

    async with get_async_db_session() as async_db:
        approval_service = ApprovalService(async_db, base_url)

        # Get approval request
        approval_request = await approval_service.get_approval_request(request_id)
        if not approval_request:
            raise HTTPException(status_code=404, detail="Approval request not found")

        # Check authorization
        if approval_request.account_id != current_user.account_id:
            raise HTTPException(
                status_code=403, detail="Not authorized to decide on this request"
            )

        # Check if already resolved
        if approval_request.status != "pending":
            raise HTTPException(
                status_code=400,
                detail=f"Request already {approval_request.status}",
            )

        # Approve or decline based on decision (pass user_id for quorum tracking)
        if decision.approved:
            updated = await approval_service.approve_request(
                request_id,
                decision.effective_comment,
                user_id=current_user.id,
                channel=AUTHENTICATED_DECISION_CHANNEL,
            )
        else:
            updated = await approval_service.decline_request(
                request_id,
                decision.effective_comment,
                user_id=current_user.id,
                channel=AUTHENTICATED_DECISION_CHANNEL,
            )

        if not updated:
            raise HTTPException(status_code=500, detail="Failed to process decision")

        # Name the agent, key, session and flow run on the async session the
        # handler already holds (the sync request session would block the
        # event loop), then convert while the write session is still open to
        # avoid DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(
            await attributed_async(async_db, updated)
        )


@router.post("/decide-batch", response_model=ApprovalBatchResponse)
@require_permission("decide_approvals")
async def decide_requests_batch(
    decision: ApprovalBatchDecision,
    request: Request,
    current_user: User = Depends(get_current_active_user),
    # Required by @require_permission (fail-closed checks kwargs["db"]).
    # Async session: a sync Session here would grow the event-loop pool-wait
    # surface (see test_async_sync_session_route_count_does_not_grow).
    db: AsyncSession = Depends(_async_db_session),
) -> ApprovalBatchResponse:
    """Approve or decline several requests with one decision.

    An operator clearing an inbox picks the rows first and decides once. Doing
    that as N round trips means N approvals racing for the same expiry window
    and N chances for the page to lose track of which ones landed, so the
    console sends the whole selection here.

    The batch never fails as a whole: each request is decided on its own and
    reported on its own, so an id that expired while the operator was reading
    costs that row and nothing else.

    Args:
        decision: The ids to decide, the decision, and an optional comment
        request: HTTP request
        current_user: Current authenticated user
        db: Async session used for the permission check and the decisions

    Returns:
        One result per requested id, in the order they were sent
    """
    base_url = os.getenv("PRELOOP_URL", str(request.base_url).rstrip("/"))

    results: list[ApprovalBatchItemResult] = []
    approval_service = ApprovalService(db, base_url)
    expected_status = "approved" if decision.approved else "declined"

    # Sequential on purpose. Each decision writes the request, appends
    # timeline events and broadcasts, and several of those running at once
    # against one session is how a batch turns into a deadlock.
    for request_id in decision.unique_ids:
        approval_request = await approval_service.get_approval_request(request_id)
        if not approval_request:
            results.append(
                ApprovalBatchItemResult(
                    id=request_id, ok=False, error="Approval request not found"
                )
            )
            continue
        if approval_request.account_id != current_user.account_id:
            # Same message as "not found" on purpose: a caller must not be
            # able to probe another account's request ids.
            results.append(
                ApprovalBatchItemResult(
                    id=request_id, ok=False, error="Approval request not found"
                )
            )
            continue
        if approval_request.status != "pending":
            results.append(
                ApprovalBatchItemResult(
                    id=request_id,
                    ok=False,
                    status=approval_request.status,
                    error=f"Request already {approval_request.status}",
                )
            )
            continue
        if _pending_request_has_expired(approval_request):
            # Still pending in the DB, but the window has closed. Do not
            # call approve/decline: those would expire the row and this
            # handler used to report that as ok=True.
            results.append(
                ApprovalBatchItemResult(
                    id=request_id,
                    ok=False,
                    status="expired",
                    error="Request already expired",
                )
            )
            continue

        try:
            if decision.approved:
                updated = await approval_service.approve_request(
                    request_id,
                    decision.comment,
                    user_id=current_user.id,
                    channel=AUTHENTICATED_DECISION_CHANNEL,
                )
            else:
                updated = await approval_service.decline_request(
                    request_id,
                    decision.comment,
                    user_id=current_user.id,
                    channel=AUTHENTICATED_DECISION_CHANNEL,
                )
        except Exception as error:  # noqa: BLE001 - one bad id, not the batch
            await db.rollback()
            logger.warning(
                "Batch decision failed for approval %s: %s",
                request_id,
                error,
                exc_info=True,
            )
            results.append(
                ApprovalBatchItemResult(
                    id=request_id, ok=False, error="Failed to process decision"
                )
            )
            continue

        if not updated:
            results.append(
                ApprovalBatchItemResult(
                    id=request_id, ok=False, error="Failed to process decision"
                )
            )
            continue

        updated_status = getattr(updated, "status", None)
        if updated_status != expected_status:
            results.append(
                ApprovalBatchItemResult(
                    id=request_id,
                    ok=False,
                    status=updated_status,
                    error=f"Request {updated_status}",
                )
            )
            continue

        results.append(
            ApprovalBatchItemResult(id=request_id, ok=True, status=updated_status)
        )

    return ApprovalBatchResponse(results=results)
