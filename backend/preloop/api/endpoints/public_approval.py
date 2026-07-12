"""Public approval endpoints (token-based authentication, no login required)."""

import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from sqlalchemy.orm import Session

from preloop.models.crud import crud_approval_request
from preloop.models.db.session import get_async_db_session, get_db_session
from preloop.services.approval_service import ApprovalService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/approval", tags=["public-approval"])


class ApprovalDecisionRequest(BaseModel):
    """Request to approve or decline."""

    action: str  # "approve" or "decline"
    comment: Optional[str] = None


class ApprovalRequestPublic(BaseModel):
    """Public view of approval request (no sensitive account data)."""

    id: str
    tool_name: str
    tool_args: dict
    agent_reasoning: Optional[str]
    status: str
    requested_at: str
    expires_at: Optional[str]


@router.get("/{request_id}/data")
def get_approval_request_public(
    request_id: uuid.UUID,
    token: str = Query(..., description="Approval token"),
    db: Session = Depends(get_db_session),
) -> ApprovalRequestPublic:
    """Get approval request details using token (no authentication required).

    Args:
        request_id: UUID of the approval request
        token: Secure token from the approval link
        db: Database session

    Returns:
        Public approval request details

    Raises:
        HTTPException: If token is invalid or request not found
    """
    # Get approval request and validate token using CRUD layer
    approval_request = crud_approval_request.get_by_id_and_token(
        db, request_id=str(request_id), token=token
    )

    if not approval_request:
        logger.warning(f"Invalid token or request not found: {request_id}")
        raise HTTPException(
            status_code=404, detail="Approval request not found or invalid token"
        )

    # Return public data only (redact tool_args for display)
    from preloop.utils.redaction import redact_dict

    return ApprovalRequestPublic(
        id=str(approval_request.id),
        tool_name=approval_request.tool_name,
        tool_args=redact_dict(approval_request.tool_args or {}),
        agent_reasoning=approval_request.agent_reasoning,
        status=approval_request.status,
        requested_at=approval_request.requested_at.isoformat(),
        expires_at=approval_request.expires_at.isoformat()
        if approval_request.expires_at
        else None,
    )


@router.post("/{request_id}/decide")
async def decide_approval_request_public(
    request_id: uuid.UUID,
    decision: ApprovalDecisionRequest,
    token: str = Query(..., description="Approval token"),
    db_sync: Session = Depends(get_db_session),
) -> ApprovalRequestPublic:
    """Approve or decline an approval request using token (no authentication required).

    Args:
        request_id: UUID of the approval request
        decision: Approval decision (approve/decline) and optional comment
        token: Secure token from the approval link
        db_sync: Synchronous database session for validation

    Returns:
        Updated approval request

    Raises:
        HTTPException: If token is invalid, request not found, or already resolved
    """
    # Validate token using CRUD layer (sync)
    approval_request = crud_approval_request.get_by_id_and_token(
        db_sync, request_id=str(request_id), token=token
    )

    if not approval_request:
        logger.warning(f"Invalid token or request not found: {request_id}")
        raise HTTPException(
            status_code=404, detail="Approval request not found or invalid token"
        )

    # Check if already resolved
    if approval_request.status in ["approved", "declined", "cancelled", "expired"]:
        logger.warning(
            f"Approval request {request_id} already resolved: {approval_request.status}"
        )
        raise HTTPException(
            status_code=400,
            detail=f"Approval request already {approval_request.status}",
        )

    # Process decision using approval service (async)
    async with get_async_db_session() as db_async:
        approval_service = ApprovalService(
            db_async, ""
        )  # base_url not needed for this operation

        if decision.action == "approve":
            logger.info(f"Approving request {request_id}")
            updated_request = await approval_service.approve_request(
                request_id, decision.comment
            )
        elif decision.action == "decline":
            logger.info(f"Declining request {request_id}")
            updated_request = await approval_service.decline_request(
                request_id, decision.comment
            )
        else:
            raise HTTPException(
                status_code=400, detail=f"Invalid action: {decision.action}"
            )

        if not updated_request:
            raise HTTPException(
                status_code=500, detail="Failed to update approval request"
            )

        from preloop.utils.redaction import redact_dict

        # Return updated data. Redact tool_args to match the GET read path and
        # the redaction policy — secrets in tool arguments must not leak here.
        return ApprovalRequestPublic(
            id=str(updated_request.id),
            tool_name=updated_request.tool_name,
            tool_args=redact_dict(updated_request.tool_args or {}),
            agent_reasoning=updated_request.agent_reasoning,
            status=updated_request.status,
            requested_at=updated_request.requested_at.isoformat(),
            expires_at=updated_request.expires_at.isoformat()
            if updated_request.expires_at
            else None,
        )
