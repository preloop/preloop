"""CRUD operations for account activation milestones."""

from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.account_milestone import AccountMilestone
from .base import CRUDBase

ACTIVATION_MILESTONES = frozenset({"first_agent", "first_flow"})


class CRUDAccountMilestone(CRUDBase[AccountMilestone]):
    """CRUD operations for account milestones."""

    def create_if_absent(
        self,
        db: Session,
        *,
        account_id: UUID,
        milestone: str,
        achieved_at: Optional[datetime] = None,
        meta: Optional[dict] = None,
        commit: bool = True,
    ) -> bool:
        """Idempotently record a milestone.

        Returns:
            True only when the milestone was newly recorded.
        """
        statement = (
            pg_insert(AccountMilestone)
            .values(
                account_id=account_id,
                milestone=milestone,
                achieved_at=achieved_at or datetime.now(timezone.utc),
                meta=meta,
            )
            .on_conflict_do_nothing(constraint="uq_account_milestone_account_milestone")
            .returning(AccountMilestone.id)
        )
        inserted = db.execute(statement).first() is not None
        if commit:
            db.commit()
        return inserted

    def get_for_account(
        self, db: Session, *, account_id: UUID
    ) -> List[AccountMilestone]:
        """All milestones for an account, oldest first."""
        return (
            db.query(AccountMilestone)
            .filter(AccountMilestone.account_id == account_id)
            .order_by(AccountMilestone.achieved_at.asc())
            .all()
        )

    def count_by_milestone(
        self, db: Session, *, since: Optional[datetime] = None
    ) -> Dict[str, int]:
        """Number of accounts having achieved each milestone."""
        from sqlalchemy import func

        query = db.query(AccountMilestone.milestone, func.count(AccountMilestone.id))
        if since is not None:
            query = query.filter(AccountMilestone.achieved_at >= since)
        return dict(query.group_by(AccountMilestone.milestone).all())


crud_account_milestone = CRUDAccountMilestone(AccountMilestone)
