"""CRUD operations for ApprovalRequest model."""

from datetime import datetime, timezone
from typing import Optional, Union
from uuid import UUID
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from preloop.models import models

from ..models.approval_request import ApprovalRequest
from .base import CRUDBase


class CRUDApprovalRequest(CRUDBase[ApprovalRequest]):
    """CRUD operations for ApprovalRequest model."""

    def detach_loaded_result(
        self, db: Union[Session, AsyncSession], request: ApprovalRequest
    ) -> ApprovalRequest:
        """Preserve loaded scalar values before a read transaction rolls back."""
        db.expunge(request)
        return request

    def expire_stale_pending(
        self,
        db: Session,
        *,
        account_id: Optional[str] = None,
        execution_id: Optional[str] = None,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> int:
        """Mark pending approval requests past their expiry as expired."""
        from .account_halt import crud_account_halt

        now = now or datetime.utcnow()
        query = db.query(self.model.account_id).filter(
            self.model.status == "pending",
            self.model.expires_at <= now,
        )
        if account_id:
            query = query.filter(self.model.account_id == account_id)
        if execution_id:
            query = query.filter(self.model.execution_id == execution_id)
        accounts = sorted({row[0] for row in query.all()}, key=str)
        expired = 0
        for owner in accounts:
            crud_account_halt.lock_account(db, account_id=owner)
            halted = crud_account_halt.get_for_scope(
                db, account_id=owner, scope="tools"
            )
            pending = db.query(self.model).filter(
                self.model.account_id == owner,
                self.model.status == "pending",
                self.model.expires_at <= now,
            )
            if execution_id:
                pending = pending.filter(self.model.execution_id == execution_id)
            if halted is not None and halted.is_active:
                # A halt cannot revive approvals whose deadline already passed.
                pending = pending.filter(
                    self.model.expires_at < halted.activated_at.replace(tzinfo=None)
                )
            expired += pending.update(
                {"status": "expired", "resolved_at": now},
                synchronize_session="fetch",
            )
        if commit:
            db.commit()
        return int(expired)

    def restore_halted_deadlines(
        self,
        db: Session,
        *,
        account_id: Union[UUID, str],
        activated_at: datetime,
        deactivated_at: datetime,
    ) -> int:
        """Restore remaining timeout once when tools resume, even without polls.

        Caller holds the account lock. Pending requests created during the halt
        retain their full original timeout; already-expired requests stay expired.
        """
        from sqlalchemy import func

        start = activated_at.astimezone(timezone.utc).replace(tzinfo=None)
        end = deactivated_at.astimezone(timezone.utc).replace(tzinfo=None)
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.status == "pending",
                self.model.expires_at >= start,
                self.model.requested_at <= end,
            )
            .update(
                {
                    self.model.expires_at: self.model.expires_at
                    + (end - func.greatest(self.model.requested_at, start))
                },
                synchronize_session=False,
            )
        )

    def get_by_token(
        self,
        db: Session,
        *,
        token: str,
    ) -> Optional[ApprovalRequest]:
        """Get approval request by approval token."""
        return db.query(self.model).filter(self.model.approval_token == token).first()

    def get_by_id_and_token(
        self,
        db: Session,
        *,
        request_id: str,
        token: str,
    ) -> Optional[ApprovalRequest]:
        """Get approval request by ID and token (for public token-based access).

        Args:
            db: Database session
            request_id: Approval request ID
            token: Approval token

        Returns:
            Approval request if found and token matches, None otherwise
        """
        return (
            db.query(self.model)
            .filter(
                self.model.id == request_id,
                self.model.approval_token == token,
            )
            .first()
        )

    def get_multi_by_execution(
        self,
        db: Session,
        *,
        execution_id: str,
        account_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        """Get approval requests for a specific execution."""
        query = db.query(self.model).filter(self.model.execution_id == execution_id)

        if account_id:
            query = query.filter(self.model.account_id == account_id)

        if status:
            query = query.filter(self.model.status == status)

        return (
            query.order_by(self.model.requested_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_multi_by_account(
        self,
        db: Session,
        *,
        account_id: str,
        execution_id: Optional[str] = None,
        status: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[ApprovalRequest]:
        """Get approval requests for an account with optional filters.

        Expiry is handled by explicit sweep callers via ``expire_stale_pending``;
        keeping this method read-only avoids hidden UPDATE+COMMIT work on list
        endpoints.
        """
        query = db.query(self.model).filter(self.model.account_id == account_id)

        if execution_id:
            query = query.filter(self.model.execution_id == execution_id)

        if status:
            query = query.filter(self.model.status == status)

        return (
            query.order_by(self.model.requested_at.desc())
            .offset(skip)
            .limit(limit)
            .all()
        )


# Create instance
crud_approval_request = CRUDApprovalRequest(ApprovalRequest)


# Async helper functions
async def get_approval_request_async(
    db: Session, request_id: UUID
) -> Optional[ApprovalRequest]:
    """Async: Retrieve an approval request by its ID.

    Eagerly loads the approval_workflow relationship to avoid lazy loading
    issues in async context.

    Args:
        db: The async database session.
        request_id: The ID of the approval request.

    Returns:
        The approval request object if found, otherwise None.
    """
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .options(selectinload(ApprovalRequest.approval_workflow))
    )
    return result.scalar_one_or_none()


async def get_approval_request_for_update_async(
    db: Session, request_id: UUID
) -> Optional[ApprovalRequest]:
    """Async: Retrieve an approval request by its ID with row-level locking.

    Uses SELECT ... FOR UPDATE to prevent concurrent modifications.
    This should be used when updating the responses field to avoid
    lost updates from concurrent votes.

    Eagerly loads the approval_workflow relationship to avoid lazy loading
    issues in async context.

    Args:
        db: The async database session.
        request_id: The ID of the approval request.

    Returns:
        The approval request object if found, otherwise None.
    """
    # Always lock account before approval, matching halt/deadline transitions.
    account_id = await db.scalar(
        select(models.ApprovalRequest.account_id).where(
            models.ApprovalRequest.id == request_id
        )
    )
    if account_id is None:
        return None
    from .account_halt import crud_account_halt

    await db.run_sync(
        lambda session: crud_account_halt.lock_account(session, account_id=account_id)
    )
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .options(selectinload(ApprovalRequest.approval_workflow))
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()
