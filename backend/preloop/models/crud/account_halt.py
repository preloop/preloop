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

    def lock_account(self, db: Session, *, account_id: Union[uuid.UUID, str]) -> None:
        """Serialize halt transitions, approval expiry and runtime admission."""
        account = (
            db.query(models.Account.id)
            .filter(models.Account.id == account_id)
            .with_for_update()
            .first()
        )
        if account is None:
            raise ValueError("Account not found")

    def transition_scopes(
        self,
        db: Session,
        *,
        account_id: Union[uuid.UUID, str],
        scopes: List[str],
        active: bool,
        user_id: Union[uuid.UUID, str],
        reason: Optional[str] = None,
    ) -> List[models.AccountHalt]:
        """Atomically commit state, stop requests, deadline recovery and audit."""
        from .audit_log import crud_audit_log

        try:
            self.lock_account(db, account_id=account_id)
            previous = self.active_scopes(db, account_id=account_id)
            rows = self.set_scopes(
                db,
                account_id=account_id,
                scopes=scopes,
                active=active,
                user_id=user_id,
                reason=reason,
                commit=False,
            )
            for scope in scopes:
                crud_audit_log.log_action(
                    db,
                    account_id=account_id,
                    user_id=user_id,
                    action="kill_switch_activated"
                    if active
                    else "kill_switch_deactivated",
                    resource_type="account",
                    resource_id=str(account_id),
                    status="success",
                    details={
                        "scope": scope,
                        "scopes": scopes,
                        "reason": reason,
                        "changed": (scope in previous) != active,
                    },
                    commit=False,
                )
            db.commit()
            return rows
        except Exception:
            db.rollback()
            raise

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
            .populate_existing()
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
            .populate_existing()
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

        self.lock_account(db, account_id=account_id)
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
                row.deactivation_reason = None
                if scope == "flows":
                    from preloop.models.crud import crud_flow_execution

                    crud_flow_execution.request_account_stop(
                        db,
                        account_id=account_id,
                        now=timestamp,
                        reason=reason,
                    )
            elif not active and row.is_active:
                row.is_active = False
                row.deactivated_by_user_id = user_id
                row.deactivated_at = timestamp
                row.deactivation_reason = reason
                if scope == "tools" and row.activated_at is not None:
                    from .approval_request import crud_approval_request

                    crud_approval_request.restore_halted_deadlines(
                        db,
                        account_id=account_id,
                        activated_at=row.activated_at,
                        deactivated_at=timestamp,
                    )

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
