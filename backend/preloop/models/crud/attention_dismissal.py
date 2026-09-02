"""CRUD operations for console attention dismissals."""

from datetime import datetime, timezone
from typing import List, Optional
from uuid import UUID

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..models.attention_dismissal import AttentionDismissal
from .base import CRUDBase

#: The three things an operator can mean by dismissing an item.
DISMISSAL_REASONS = frozenset({"expected", "snoozed", "fixed"})


class CRUDAttentionDismissal(CRUDBase[AttentionDismissal]):
    """CRUD operations for attention dismissals."""

    def get_active_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: Optional[datetime] = None,
    ) -> List[AttentionDismissal]:
        """Dismissals still in force, newest first.

        A snooze whose ``snooze_until`` has passed is not active. Callers that
        want the expired rows gone should follow up with
        :meth:`purge_expired_for_account`.
        """
        moment = now or datetime.now(timezone.utc)
        return (
            db.query(AttentionDismissal)
            .filter(AttentionDismissal.account_id == account_id)
            .filter(
                or_(
                    AttentionDismissal.snooze_until.is_(None),
                    AttentionDismissal.snooze_until > moment,
                )
            )
            .order_by(AttentionDismissal.created_at.desc())
            .all()
        )

    def purge_expired_for_account(
        self,
        db: Session,
        *,
        account_id: UUID,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> int:
        """Delete snoozes that have run out. Returns the number removed.

        Garbage collection on read: an expired snooze has no effect on the
        console anyway, and leaving it would make a later re-dismissal look
        like an update of a row nobody can see.
        """
        moment = now or datetime.now(timezone.utc)
        removed = (
            db.query(AttentionDismissal)
            .filter(AttentionDismissal.account_id == account_id)
            .filter(AttentionDismissal.snooze_until.is_not(None))
            .filter(AttentionDismissal.snooze_until <= moment)
            .delete(synchronize_session=False)
        )
        if commit:
            db.commit()
        return int(removed)

    def get_by_item(
        self, db: Session, *, account_id: UUID, item_id: str
    ) -> Optional[AttentionDismissal]:
        """The dismissal for one item, expired or not."""
        return (
            db.query(AttentionDismissal)
            .filter(AttentionDismissal.account_id == account_id)
            .filter(AttentionDismissal.item_id == item_id)
            .first()
        )

    def upsert(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: str,
        fingerprint: str,
        reason: str,
        snooze_until: Optional[datetime] = None,
        dismissed_by_user_id: Optional[UUID] = None,
        commit: bool = True,
    ) -> AttentionDismissal:
        """Create or replace the dismissal for one item.

        Re-dismissing an item that came back (a new failed run gives it a new
        fingerprint) overwrites the existing row rather than adding a second
        one, so ``(account_id, item_id)`` stays unique and the row always
        describes the state the operator actually looked at.

        The write is a single ``INSERT ... ON CONFLICT DO UPDATE``: a
        read-then-insert let two operators dismissing the same item at the
        same moment both miss the existing row and race into the unique
        constraint, which surfaced as a 500 rather than an idempotent
        re-dismissal.
        """
        # The row records a new decision, so it is dated by it. ``created_at``
        # and ``updated_at`` are Base's naive-UTC timestamps, and neither the
        # server default nor ``onupdate`` fires for the conflict branch of a
        # Core insert, so both are set explicitly.
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        statement = (
            pg_insert(AttentionDismissal)
            .values(
                account_id=account_id,
                item_id=item_id,
                fingerprint=fingerprint,
                reason=reason,
                snooze_until=snooze_until,
                dismissed_by_user_id=dismissed_by_user_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_attention_dismissal_account_item",
                set_={
                    "fingerprint": fingerprint,
                    "reason": reason,
                    "snooze_until": snooze_until,
                    "dismissed_by_user_id": dismissed_by_user_id,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            .returning(AttentionDismissal.id)
        )
        dismissal_id = db.execute(statement).scalar_one()
        if commit:
            db.commit()
        else:
            db.flush()
        # ``populate_existing`` because the caller may already hold a stale
        # copy of this row in the session's identity map.
        dismissal = db.get(AttentionDismissal, dismissal_id, populate_existing=True)
        if dismissal is None:  # pragma: no cover - the row was just written
            raise RuntimeError(f"Attention dismissal {dismissal_id} vanished on write")
        return dismissal

    def delete_by_item(
        self,
        db: Session,
        *,
        account_id: UUID,
        item_id: str,
        commit: bool = True,
    ) -> bool:
        """Restore an item. Returns True when a row was actually removed."""
        removed = (
            db.query(AttentionDismissal)
            .filter(AttentionDismissal.account_id == account_id)
            .filter(AttentionDismissal.item_id == item_id)
            .delete(synchronize_session=False)
        )
        if commit:
            db.commit()
        return bool(removed)


crud_attention_dismissal = CRUDAttentionDismissal(AttentionDismissal)
