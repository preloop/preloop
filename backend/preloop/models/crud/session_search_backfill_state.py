"""CRUD helpers for the session search backfill watermark.

The backfill sweeper owns this table: one row per account, holding how far
back the walk has reached and whether it finished. Everything here is small
and explicit on purpose, because the sweeper reads it once per account per
pass and the search endpoint reads it once per query.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models.account import Account
from ..models.session_search_backfill_state import SessionSearchBackfillState
from .base import CRUDBase


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CRUDSessionSearchBackfillState(CRUDBase[SessionSearchBackfillState]):
    """CRUD operations for `SessionSearchBackfillState`."""

    def get_for_account(
        self, db: Session, *, account_id: Any
    ) -> Optional[SessionSearchBackfillState]:
        """Return one account's backfill state, or None if it never ran."""
        return (
            db.query(SessionSearchBackfillState)
            .filter(SessionSearchBackfillState.account_id == account_id)
            .first()
        )

    def get_or_create(
        self, db: Session, *, account_id: Any
    ) -> SessionSearchBackfillState:
        """Return the account's state row, creating an empty one if needed.

        Two replicas can both miss the row and insert. The unique
        ``account_id`` index then raises; the loser rolls the savepoint back
        and reads the committed row instead of stamping ``last_error``.
        """
        state = self.get_for_account(db, account_id=account_id)
        if state is not None:
            return state
        savepoint = db.begin_nested()
        try:
            state = SessionSearchBackfillState(account_id=account_id)
            db.add(state)
            db.flush()
        except IntegrityError:
            savepoint.rollback()
            state = self.get_for_account(db, account_id=account_id)
            if state is None:
                raise
            return state
        if savepoint.is_active:
            savepoint.commit()
        return state

    def lock_for_account(
        self, db: Session, *, account_id: Any
    ) -> Optional[SessionSearchBackfillState]:
        """Lock this account's backfill row, or None if another replica holds it.

        The lock is on the backfill row, not the account row, so a walk of up
        to ``max_seconds`` does not serialize billing, config, or the kill
        switch. ``SKIP LOCKED`` leaves the account to the replica that already
        holds it.

        A never-walked account is inserted and committed before the lock is
        taken. That makes the empty row visible so a concurrent replica skip
        locks immediately, instead of waiting on the unique-index key-share
        of an uncommitted INSERT for the rest of the holder's walk.
        """
        created = self.get_for_account(db, account_id=account_id) is None
        self.get_or_create(db, account_id=account_id)
        if created:
            db.commit()
        return db.execute(
            select(SessionSearchBackfillState)
            .where(SessionSearchBackfillState.account_id == account_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        ).scalar_one_or_none()

    def record_pass(
        self,
        db: Session,
        *,
        account_id: Any,
        cursor_started_at: Optional[datetime] = None,
        cursor_session_id: Optional[Any] = None,
        sessions_scanned: int = 0,
        rows_written: int = 0,
        completed: bool = False,
        error: Optional[str] = None,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionSearchBackfillState:
        """Persist what one account slice of a pass achieved.

        The watermark only ever moves backwards in time: a pass that walked
        nothing (or failed before it walked anything) leaves the stored cursor
        alone, so a restarted sweeper never rewinds and re-walks history it
        already indexed.
        """
        stamp = now or _now()
        state = self.get_or_create(db, account_id=account_id)
        if cursor_started_at is not None:
            stored = state.cursor_started_at
            if stored is None or _as_utc(cursor_started_at) <= _as_utc(stored):
                state.cursor_started_at = cursor_started_at
                state.cursor_session_id = cursor_session_id
        state.sessions_scanned = int(state.sessions_scanned or 0) + int(
            sessions_scanned
        )
        state.rows_written = int(state.rows_written or 0) + int(rows_written)
        state.last_pass_at = stamp
        state.last_error = error
        if completed and state.completed_at is None:
            state.completed_at = stamp
        db.add(state)
        db.flush()
        if commit:
            db.commit()
            db.refresh(state)
        return state

    def record_error(
        self,
        db: Session,
        *,
        account_id: Any,
        error: str,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionSearchBackfillState:
        """Store a failure for one account without touching its watermark."""
        return self.record_pass(
            db,
            account_id=account_id,
            error=error[:2000],
            now=now,
            commit=commit,
        )

    def pending_account_ids(
        self,
        db: Session,
        *,
        account_ids: Optional[Sequence[Any]] = None,
        limit: Optional[int] = None,
    ) -> List[uuid.UUID]:
        """Return active accounts whose backfill has not completed.

        A completed account is excluded by the join, so a finished account
        costs one row of an index scan per pass and never a session scan.
        Accounts that have never been walked come first, then the ones whose
        last pass is oldest, so no account can be starved by a busy one.
        ``account_ids=None`` means every pending account; an empty sequence
        means none of them.
        """
        stmt = (
            select(Account.id)
            .select_from(Account)
            .outerjoin(
                SessionSearchBackfillState,
                SessionSearchBackfillState.account_id == Account.id,
            )
            .where(
                Account.is_active.is_(True),
                SessionSearchBackfillState.completed_at.is_(None),
            )
            .order_by(
                SessionSearchBackfillState.last_pass_at.asc().nullsfirst(),
                Account.id,
            )
        )
        if account_ids is not None:
            if not account_ids:
                return []
            stmt = stmt.where(Account.id.in_(list(account_ids)))
        if limit is not None:
            stmt = stmt.limit(int(limit))
        return list(db.execute(stmt).scalars().all())


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime, so stored and fresh values compare."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


crud_session_search_backfill_state = CRUDSessionSearchBackfillState(
    SessionSearchBackfillState
)
