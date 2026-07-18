"""API endpoints for time-boxed approval bypasses.

These endpoints are the escape hatch for approval and notification fatigue: an
onboarded agent generating approval requests faster than a human can act, with
push, watch, and email all firing at once.

Design constraints encoded here:

* Every bypass expires. ``duration_minutes`` is required and bounded, so there
  is no request shape that means "forever".
* A bypass is created by an authenticated user and always applies to *that*
  user. You cannot open a hole in somebody else's gating.
* Creating and revoking are both cheap and idempotent-ish, because the person
  using them is in a hurry and probably on a phone.
"""

import logging
import uuid
from datetime import datetime, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from preloop.api.auth import get_current_active_user
from preloop.models.crud import crud_approval_bypass
from preloop.models.db.session import get_db_session
from preloop.models.models.approval_bypass import (
    ApprovalBypass,
    ApprovalBypassMode,
)
from preloop.models.models.user import User
from preloop.schemas.approval_bypass import (
    ApprovalBypassCreate,
    ApprovalBypassResponse,
    ApprovalBypassStatus,
)
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/approval-bypasses",
    tags=["approval_bypasses"],
)


@router.get("/status", response_model=ApprovalBypassStatus)
def get_bypass_status(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalBypassStatus:
    """Report whether any bypass is currently in force for this user.

    Console and mobile poll this to drive the persistent warning banner. It is
    intentionally readable by any authenticated user without a special
    permission: knowing that governance is currently relaxed is not privileged
    information, and hiding it would defeat the point.

    Args:
        current_user: Current authenticated user.
        db: Database session.

    Returns:
        Aggregate bypass status for banner rendering.
    """
    active = [
        b
        for b in crud_approval_bypass.list_active_for_account(
            db, account_id=current_user.account_id
        )
        if b.user_id == current_user.id
    ]
    responses = [ApprovalBypassResponse.model_validate(b) for b in active]
    return ApprovalBypassStatus(
        active=bool(responses),
        auto_approve_active=any(
            b.mode == ApprovalBypassMode.AUTO_APPROVE for b in active
        ),
        bypasses=responses,
    )


@router.get("", response_model=List[ApprovalBypassResponse])
@require_permission("view_approvals")
def list_bypasses(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ApprovalBypass]:
    """List every active bypass in the account.

    Account-wide visibility is deliberate: if a teammate has relaxed gating,
    everyone governing the same fleet should be able to see it.

    Args:
        current_user: Current authenticated user.
        db: Database session.

    Returns:
        Active bypasses, soonest-expiring last.
    """
    return crud_approval_bypass.list_active_for_account(
        db, account_id=current_user.account_id
    )


@router.post("", response_model=ApprovalBypassResponse, status_code=201)
@require_permission("view_approvals")
def create_bypass(
    payload: ApprovalBypassCreate,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalBypass:
    """Open a time-boxed bypass for the calling user.

    The bypass always targets ``current_user``; there is no parameter to target
    somebody else. Duration is validated by the schema against the hard cap, so
    an unbounded bypass cannot be constructed.

    Args:
        payload: Mode, duration, optional agent scope and reason.
        current_user: Current authenticated user (the bypass subject).
        db: Database session.

    Returns:
        The created bypass.

    Raises:
        HTTPException: If the duration is outside the permitted range.
    """
    expires_at = datetime.utcnow() + timedelta(minutes=payload.duration_minutes)

    bypass = ApprovalBypass(
        id=uuid.uuid4(),
        account_id=current_user.account_id,
        user_id=current_user.id,
        managed_agent_id=payload.managed_agent_id,
        mode=payload.mode,
        reason=payload.reason,
        created_by_user_id=current_user.id,
        created_via=payload.created_via,
        expires_at=expires_at,
        auto_approved_count=0,
    )
    db.add(bypass)
    db.commit()
    db.refresh(bypass)

    # Log loudly. A governance bypass being opened is a security-relevant
    # event and should be greppable in the server log without a DB query.
    logger.warning(
        "APPROVAL BYPASS OPENED: mode=%s user=%s account=%s scope=%s "
        "expires_at=%s via=%s reason=%r",
        bypass.mode,
        bypass.user_id,
        bypass.account_id,
        bypass.managed_agent_id or "account-wide",
        bypass.expires_at.isoformat(),
        bypass.created_via,
        bypass.reason,
    )
    return bypass


@router.delete("/{bypass_id}", response_model=ApprovalBypassResponse)
@require_permission("view_approvals")
def revoke_bypass(
    bypass_id: uuid.UUID,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> ApprovalBypass:
    """End a bypass early.

    Any user who can view approvals in the account may revoke a bypass, even
    one they did not create. Re-tightening a control is always safe, so it
    should never be blocked on ownership.

    Args:
        bypass_id: The bypass to revoke.
        current_user: Current authenticated user.
        db: Database session.

    Returns:
        The revoked bypass.

    Raises:
        HTTPException: If the bypass is not found in this account.
    """
    existing = crud_approval_bypass.get(
        db, id=str(bypass_id), account_id=str(current_user.account_id)
    )
    if existing is None:
        raise HTTPException(status_code=404, detail="Approval bypass not found")

    revoked = crud_approval_bypass.revoke(
        db, bypass_id=bypass_id, revoked_by_user_id=current_user.id
    )
    if revoked is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=404, detail="Approval bypass not found")

    logger.warning(
        "APPROVAL BYPASS REVOKED: id=%s mode=%s by=%s auto_approved_count=%s",
        revoked.id,
        revoked.mode,
        current_user.id,
        revoked.auto_approved_count,
    )
    return revoked


@router.post("/revoke-all", response_model=List[ApprovalBypassResponse])
@require_permission("view_approvals")
def revoke_all_bypasses(
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db_session),
) -> List[ApprovalBypass]:
    """Revoke every active bypass in the account — the 'panic off' button.

    Exists so that restoring full governance is always a single unambiguous
    action, with no list to scroll and no per-item taps, from any surface.

    Args:
        current_user: Current authenticated user.
        db: Database session.

    Returns:
        The bypasses that were revoked.
    """
    active = crud_approval_bypass.list_active_for_account(
        db, account_id=current_user.account_id
    )
    revoked: List[ApprovalBypass] = []
    for bypass in active:
        result = crud_approval_bypass.revoke(
            db,
            bypass_id=bypass.id,
            revoked_by_user_id=current_user.id,
            commit=False,
        )
        if result is not None:
            revoked.append(result)
    db.commit()

    logger.warning(
        "ALL APPROVAL BYPASSES REVOKED: account=%s count=%s by=%s",
        current_user.account_id,
        len(revoked),
        current_user.id,
    )
    return revoked
