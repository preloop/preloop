"""CRUD operations for ApprovalRequest model."""

from datetime import datetime
from typing import Optional
from uuid import UUID
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.future import select

from ..models.approval_request import ApprovalRequest
from .base import CRUDBase


class CRUDApprovalRequest(CRUDBase[ApprovalRequest]):
    """CRUD operations for ApprovalRequest model."""

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
        now = now or datetime.utcnow()
        query = db.query(self.model).filter(
            self.model.status == "pending",
            self.model.expires_at.isnot(None),
            self.model.expires_at <= now,
        )

        if account_id:
            query = query.filter(self.model.account_id == account_id)

        if execution_id:
            query = query.filter(self.model.execution_id == execution_id)

        expired = query.update(
            {"status": "expired", "resolved_at": now},
            synchronize_session="fetch",
        )
        if commit:
            db.commit()
        return int(expired)

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
        """Get approval requests for an account with optional filters."""
        if status in (None, "pending"):
            self.expire_stale_pending(
                db,
                account_id=account_id,
                execution_id=execution_id,
            )

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
    result = await db.execute(
        select(ApprovalRequest)
        .where(ApprovalRequest.id == request_id)
        .options(selectinload(ApprovalRequest.approval_workflow))
        .with_for_update()
    )
    return result.scalar_one_or_none()
