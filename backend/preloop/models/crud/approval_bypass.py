"""CRUD operations for ApprovalBypass model.

Resolution rule used by :func:`get_active_bypass_async`: when several bypasses
could apply, the **strongest** one wins, and among equals the one expiring
latest. Agent-scoped and account-wide bypasses are both considered, because a
user who has muted everything account-wide should not keep getting buzzed by
one agent that happens to have no agent-scoped row.
"""

from datetime import datetime
from typing import List, Optional
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import Session

from ..models.approval_bypass import ApprovalBypass, ApprovalBypassMode
from .base import CRUDBase

#: Strength ordering. ``auto_approve`` subsumes ``mute_notifications``: it also
#: suppresses notifications, so it must win when both are in force.
_MODE_STRENGTH = {
    ApprovalBypassMode.MUTE_NOTIFICATIONS: 1,
    ApprovalBypassMode.AUTO_APPROVE: 2,
}


class CRUDApprovalBypass(CRUDBase[ApprovalBypass]):
    """CRUD operations for ApprovalBypass model."""

    def get_active_for_user(
        self,
        db: Session,
        *,
        account_id: UUID,
        user_id: UUID,
        managed_agent_id: Optional[UUID] = None,
        now: Optional[datetime] = None,
    ) -> Optional[ApprovalBypass]:
        """Return the strongest active bypass for a user/agent pair.

        Args:
            db: Sync database session.
            account_id: Owning account.
            user_id: User whose approvals may be bypassed.
            managed_agent_id: Agent raising the request, if known.
            now: Reference time; defaults to :func:`datetime.utcnow`.

        Returns:
            The winning :class:`ApprovalBypass`, or None when none applies.
        """
        reference = now or datetime.utcnow()
        candidates = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.user_id == user_id,
                self.model.revoked_at.is_(None),
                self.model.expires_at > reference,
                or_(
                    self.model.managed_agent_id.is_(None),
                    self.model.managed_agent_id == managed_agent_id,
                )
                if managed_agent_id is not None
                else self.model.managed_agent_id.is_(None),
            )
            .all()
        )
        return _strongest(candidates)

    def list_active_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: Optional[datetime] = None,
    ) -> List[ApprovalBypass]:
        """List every active bypass in an account, newest expiry first."""
        reference = now or datetime.utcnow()
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.revoked_at.is_(None),
                self.model.expires_at > reference,
            )
            .order_by(self.model.expires_at.desc())
            .all()
        )

    def revoke(
        self,
        db: Session,
        *,
        bypass_id: UUID,
        revoked_by_user_id: UUID,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> Optional[ApprovalBypass]:
        """End a bypass early, preserving the record that it existed."""
        bypass = db.query(self.model).filter(self.model.id == bypass_id).first()
        if bypass is None:
            return None
        if bypass.revoked_at is None:
            bypass.revoked_at = now or datetime.utcnow()
            bypass.revoked_by_user_id = revoked_by_user_id
            if commit:
                db.commit()
                db.refresh(bypass)
        return bypass


crud_approval_bypass = CRUDApprovalBypass(ApprovalBypass)


def _strongest(candidates: List[ApprovalBypass]) -> Optional[ApprovalBypass]:
    """Pick the strongest bypass, tie-broken by latest expiry.

    Args:
        candidates: Active bypasses that all apply to the same request.

    Returns:
        The winning bypass, or None when the list is empty.
    """
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda b: (_MODE_STRENGTH.get(b.mode, 0), b.expires_at),
    )


async def get_active_bypass_async(
    db: AsyncSession,
    *,
    account_id: UUID,
    user_id: UUID,
    managed_agent_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
) -> Optional[ApprovalBypass]:
    """Async variant of :meth:`CRUDApprovalBypass.get_active_for_user`.

    Args:
        db: Async database session.
        account_id: Owning account.
        user_id: User whose approvals may be bypassed.
        managed_agent_id: Agent raising the request, if known.
        now: Reference time; defaults to :func:`datetime.utcnow`.

    Returns:
        The winning :class:`ApprovalBypass`, or None when none applies.
    """
    reference = now or datetime.utcnow()
    scope_filter = (
        or_(
            ApprovalBypass.managed_agent_id.is_(None),
            ApprovalBypass.managed_agent_id == managed_agent_id,
        )
        if managed_agent_id is not None
        else ApprovalBypass.managed_agent_id.is_(None)
    )
    result = await db.execute(
        select(ApprovalBypass).where(
            ApprovalBypass.account_id == account_id,
            ApprovalBypass.user_id == user_id,
            ApprovalBypass.revoked_at.is_(None),
            ApprovalBypass.expires_at > reference,
            scope_filter,
        )
    )
    return _strongest(list(result.scalars().all()))


async def record_bypass_use_async(
    db: AsyncSession,
    *,
    bypass_id: UUID,
) -> None:
    """Increment the auto-approval counter on a bypass.

    Args:
        db: Async database session.
        bypass_id: Bypass that auto-approved a request.
    """
    result = await db.execute(
        select(ApprovalBypass).where(ApprovalBypass.id == bypass_id)
    )
    bypass = result.scalars().first()
    if bypass is not None:
        bypass.auto_approved_count = (bypass.auto_approved_count or 0) + 1
        await db.commit()
