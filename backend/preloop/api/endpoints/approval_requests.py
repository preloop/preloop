"""API endpoints for approval requests."""

import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.services.approval_service import ApprovalService
from preloop.models.crud import crud_approval_request
from preloop.models.db.session import get_async_db_session, get_db_session
from preloop.models.models import ApprovalRequest
from preloop.models.models.user import User
from preloop.models.schemas.approval_request import (
    ApprovalRequestResponse,
    ApprovalDecision,
)
from preloop.utils.permissions import require_permission

router = APIRouter(
    prefix="/approval-requests",
    tags=["approval_requests"],
)


@router.get("/{request_id}", response_model=ApprovalRequestResponse)
@require_permission("view_approvals")
def get_approval_request(
    request_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalRequest:
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

    return approval_request


@router.get("", response_model=list[ApprovalRequestResponse])
@require_permission("view_approvals")
def list_approval_requests(
    status: Optional[str] = Query(None, description="Filter by status"),
    execution_id: Optional[str] = Query(None, description="Filter by execution ID"),
    limit: int = Query(50, le=100, description="Maximum number of results"),
    skip: int = Query(0, description="Number of results to skip"),
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> list[ApprovalRequest]:
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
    return crud_approval_request.get_multi_by_account(
        db,
        account_id=current_user.account_id,
        execution_id=execution_id,
        status=status,
        skip=skip,
        limit=limit,
    )


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
            request_id, decision.effective_comment, user_id=current_user.id
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to approve request")

        # Convert to Pydantic model while session is still open to avoid
        # DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(updated)


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
            request_id, decision.effective_comment, user_id=current_user.id
        )
        if not updated:
            raise HTTPException(status_code=500, detail="Failed to decline request")

        # Convert to Pydantic model while session is still open to avoid
        # DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(updated)


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
                request_id, decision.effective_comment, user_id=current_user.id
            )
        else:
            updated = await approval_service.decline_request(
                request_id, decision.effective_comment, user_id=current_user.id
            )

        if not updated:
            raise HTTPException(status_code=500, detail="Failed to process decision")

        # Convert to Pydantic model while session is still open to avoid
        # DetachedInstanceError during response serialization.
        return ApprovalRequestResponse.model_validate(updated)
