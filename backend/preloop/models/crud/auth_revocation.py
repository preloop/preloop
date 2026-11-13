"""Transaction-local notifications for committed session generation changes.

CRUD records identifiers only. The application service owns delivery after
commit; nothing here performs network I/O or publishes from a transaction.
"""

from typing import TypedDict
from uuid import UUID

from sqlalchemy.orm import Session, SessionTransaction


class RevocationEvent(TypedDict):
    """A hint to recheck the named memberships against authoritative state."""

    user_ids: list[str]
    generation: int


_KEY = "auth_revocation_events"
PendingRevocations = dict[SessionTransaction, list[RevocationEvent]]


def stage_revocation(db: Session, *, user_id: UUID, generation: int) -> None:
    """Stage a membership notification in its current transaction/savepoint."""
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        raise RuntimeError("Session revocation requires a transaction")
    pending: PendingRevocations = db.info.setdefault(_KEY, {})
    pending.setdefault(transaction, []).append(
        RevocationEvent(user_ids=[str(user_id)], generation=generation)
    )


def committed_revocations(db: Session) -> list[RevocationEvent]:
    """Release root-committed events, retaining committed savepoints for later."""
    transaction = db.get_nested_transaction() or db.get_transaction()
    if transaction is None:
        return []
    pending: PendingRevocations = db.info.get(_KEY, {})
    events = pending.pop(transaction, [])
    if transaction.nested:
        assert transaction.parent is not None
        pending.setdefault(transaction.parent, []).extend(events)
        return []
    db.info.pop(_KEY, None)
    return events


def discard_revocations(db: Session, transaction: SessionTransaction) -> None:
    """Discard notifications belonging to a rolled-back transaction."""
    pending: PendingRevocations = db.info.get(_KEY, {})
    pending.pop(transaction, None)
    if transaction.parent is None or not pending:
        db.info.pop(_KEY, None)
