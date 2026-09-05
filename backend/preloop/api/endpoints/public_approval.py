"""Public approval endpoints (token-based authentication, no login required)."""

import uuid
import logging
from datetime import datetime
from typing import List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from sqlalchemy.orm import Session

from preloop.models.crud import crud_approval_event, crud_approval_request
from preloop.models.db.session import get_async_db_session, get_db_session
from preloop.models.models.approval_event import ApprovalEvent
from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.schemas.approval_request import ApprovalEventPublic
from preloop.services.approval_service import ApprovalService
from preloop.utils.redaction import redact_dict

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
    resolved_at: Optional[str] = None
    history: List[ApprovalEventPublic] = Field(default_factory=list)


def _iso_or_none(value: object) -> Optional[str]:
    """Serialize a datetime-like value, ignoring non-datetime mocks."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    isoformat = getattr(value, "isoformat", None)
    if not callable(isoformat):
        return None
    rendered = isoformat()
    return rendered if isinstance(rendered, str) else None


def _to_public_request(
    approval_request: ApprovalRequest, events: Sequence[ApprovalEvent]
) -> ApprovalRequestPublic:
    """Build the token-page payload, redacting secrets and identities."""
    return ApprovalRequestPublic(
        id=str(approval_request.id),
        tool_name=approval_request.tool_name,
        tool_args=redact_dict(approval_request.tool_args or {}),
        agent_reasoning=approval_request.agent_reasoning,
        status=approval_request.status,
        requested_at=_iso_or_none(approval_request.requested_at) or "",
        expires_at=_iso_or_none(approval_request.expires_at),
        resolved_at=_iso_or_none(approval_request.resolved_at),
        history=[
            ApprovalEventPublic(
                event_type=event.event_type,
                detail=event.detail,
                comment=event.comment,
                timestamp=event.timestamp,
            )
            for event in events
        ],
    )


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

    # Track that the link was opened (one anonymous timeline entry per
    # request; no actor identity is available on the token path).
    try:
        if not crud_approval_event.has_event(
            db,
            approval_request_id=approval_request.id,
            event_type="viewed",
            actor_is_null=True,
        ):
            crud_approval_event.record(
                db,
                approval_request_id=approval_request.id,
                account_id=approval_request.account_id,
                event_type="viewed",
                detail="Approval link opened (token link)",
            )
    except Exception:
        # View tracking must never break reading the request.
        logger.debug(
            "Failed to record token view for approval %s",
            approval_request.id,
            exc_info=True,
        )

    history = crud_approval_event.get_by_request(
        db, approval_request_id=approval_request.id
    )
    return _to_public_request(approval_request, history)


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
                request_id, decision.comment, channel="token link"
            )
        elif decision.action == "decline":
            logger.info(f"Declining request {request_id}")
            updated_request = await approval_service.decline_request(
                request_id, decision.comment, channel="token link"
            )
        else:
            raise HTTPException(
                status_code=400, detail=f"Invalid action: {decision.action}"
            )

        if not updated_request:
            raise HTTPException(
                status_code=500, detail="Failed to update approval request"
            )

        # Re-query the timeline so the token page keeps Workflow History
        # after a decision instead of replacing it with the default [].
        db_sync.expire_all()
        history = crud_approval_event.get_by_request(
            db_sync, approval_request_id=updated_request.id
        )
        return _to_public_request(updated_request, history)
