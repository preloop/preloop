"""CRUD operations for account kill-switch (halt) state."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Union

from sqlalchemy.orm import Session

from preloop.models import models
from ..models.account_halt import HALT_SCOPES
from .base import CRUDBase


class CRUDAccountHalt(CRUDBase[models.AccountHalt]):
    """CRUD operations for per-scope account halt state.

    A row exists for an ``(account, scope)`` pair only after the first time
    that scope is halted; absence of a row means "never halted", which reads
    the same as inactive.
    """

    def get_for_scope(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        scope: str,
    ) -> Optional[models.AccountHalt]:
        """Return the halt row for one account scope, if any."""
        return (
            db.query(models.AccountHalt)
            .filter(
                models.AccountHalt.account_id == account_id,
                models.AccountHalt.scope == scope,
            )
            .first()
        )

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
    ) -> List[models.AccountHalt]:
        """Return every halt row for an account (all scopes)."""
        return (
            db.query(models.AccountHalt)
            .filter(models.AccountHalt.account_id == account_id)
            .order_by(models.AccountHalt.scope)
            .all()
        )

    def active_scopes(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
    ) -> Set[str]:
        """Return the set of currently-halted scope names for an account."""
        rows = (
            db.query(models.AccountHalt.scope)
            .filter(
                models.AccountHalt.account_id == account_id,
                models.AccountHalt.is_active,
            )
            .all()
        )
        return {row[0] for row in rows}

    def set_scopes(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        scopes: List[str],
        active: bool,
        user_id: Optional[Union[uuid.UUID, str]],
        reason: Optional[str] = None,
        now: Optional[datetime] = None,
        commit: bool = True,
    ) -> List[models.AccountHalt]:
        """Activate or deactivate a set of scopes for an account.

        Idempotent: activating an already-active scope keeps its original
        activation attribution (an operator confirming the halt must not
        rewrite history), while deactivating an inactive scope is a no-op.

        Args:
            db: Database session.
            account_id: Owning account.
            scopes: Scope names to transition; unknown names raise ValueError.
            active: True to halt, False to re-enable.
            user_id: Acting user (recorded for audit).
            reason: Free-text reason (recorded on activation).
            now: Transition timestamp; defaults to ``datetime.now(UTC)``.
            commit: When False, flush only so callers can batch writes.

        Returns:
            The halt rows for the requested scopes, after the transition.
        """
        unknown = [scope for scope in scopes if scope not in HALT_SCOPES]
        if unknown:
            raise ValueError(f"Unknown halt scopes: {', '.join(sorted(unknown))}")

        timestamp = now or datetime.now(timezone.utc)
        rows: Dict[str, models.AccountHalt] = {
            row.scope: row for row in self.list_for_account(db, account_id=account_id)
        }

        for scope in scopes:
            row = rows.get(scope)
            if row is None:
                row = models.AccountHalt(
                    account_id=account_id,
                    scope=scope,
                    is_active=False,
                )
                db.add(row)
                rows[scope] = row
            if active and not row.is_active:
                row.is_active = True
                row.activated_by_user_id = user_id
                row.activated_at = timestamp
                row.reason = reason
                row.deactivated_by_user_id = None
                row.deactivated_at = None
            elif not active and row.is_active:
                row.is_active = False
                row.deactivated_by_user_id = user_id
                row.deactivated_at = timestamp

        if commit:
            db.commit()
            for row in rows.values():
                db.refresh(row)
        else:
            db.flush()
        return [rows[scope] for scope in scopes]

    def snapshot_for_account(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
    ) -> Dict[str, Any]:
        """Return a render-ready status dict for the account's halt state."""
        rows = self.list_for_account(db, account_id=account_id)
        scopes: Dict[str, Any] = {}
        for scope in HALT_SCOPES:
            row = next((r for r in rows if r.scope == scope), None)
            if row is None or not row.is_active:
                continue
            scopes[scope] = {
                "activated_by_user_id": (
                    str(row.activated_by_user_id)
                    if row.activated_by_user_id is not None
                    else None
                ),
                "activated_at": (
                    row.activated_at.isoformat() if row.activated_at else None
                ),
                "reason": row.reason,
            }
        return {"active": bool(scopes), "scopes": scopes}


# Global instance
crud_account_halt = CRUDAccountHalt(models.AccountHalt)
