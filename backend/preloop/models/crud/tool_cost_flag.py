"""CRUD operations for persistent tool cost flags."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models.tool_cost_flag import ToolCostFlag
from .base import CRUDBase

# Statuses excluded from the default dashboard listing.
_DEFAULT_HIDDEN_STATUSES = ("dismissed",)

# Allowed lifecycle statuses for a flag.
_VALID_STATUSES = ("open", "dismissed", "snoozed")


class CRUDToolCostFlag(CRUDBase[ToolCostFlag]):
    """CRUD helpers for account-level tool cost flags."""

    def get_for_window(
        self,
        db: Session,
        *,
        account_id: Any,
        tool_name: str,
        window_start: datetime,
    ) -> Optional[ToolCostFlag]:
        """Return the flag for one account/tool/window triple, if any.

        Args:
            db: Database session.
            account_id: Owning account id.
            tool_name: Name of the flagged tool.
            window_start: Inclusive start of the rolling detection window.

        Returns:
            The matching flag row or ``None``.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.tool_name == tool_name,
                self.model.window_start == window_start,
            )
            .first()
        )

    def upsert_flag(
        self,
        db: Session,
        *,
        account_id: Any,
        tool_name: str,
        tool_source: str,
        window_start: datetime,
        window_end: datetime,
        flag_kind: str,
        evidence: dict[str, Any],
        estimated_weekly_cost: float,
        disable_eligible: bool = True,
        commit: bool = True,
    ) -> ToolCostFlag:
        """Create or update a flag keyed by account + tool + window.

        Re-running detection for the same window refreshes the computed
        evidence and cost without creating a duplicate row. The persisted
        ``status`` is preserved so a dismissed or snoozed tool stays suppressed.

        Args:
            db: Database session.
            account_id: Owning account id.
            tool_name: Name of the flagged tool.
            tool_source: Labeled heuristic origin ('mcp' | 'payload').
            window_start: Inclusive start of the rolling detection window.
            window_end: Exclusive end of the rolling detection window.
            flag_kind: Category of the flag (e.g. 'definition_cost').
            evidence: Computed numbers plus human-readable claim text.
            estimated_weekly_cost: Upper-bound estimated weekly cost in USD.
            disable_eligible: Whether the tool can be one-click disabled.
            commit: Whether to commit the transaction.

        Returns:
            The stored flag row.
        """
        existing = self.get_for_window(
            db,
            account_id=account_id,
            tool_name=tool_name,
            window_start=window_start,
        )
        if existing is not None:
            existing.tool_source = tool_source
            existing.window_end = window_end
            existing.flag_kind = flag_kind
            existing.evidence = evidence
            existing.estimated_weekly_cost = estimated_weekly_cost
            existing.disable_eligible = disable_eligible
            db_obj = existing
        else:
            db_obj = ToolCostFlag(
                account_id=account_id,
                tool_name=tool_name,
                tool_source=tool_source,
                window_start=window_start,
                window_end=window_end,
                flag_kind=flag_kind,
                evidence=evidence,
                estimated_weekly_cost=estimated_weekly_cost,
                disable_eligible=disable_eligible,
            )
            db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj

    def list_for_account(
        self,
        db: Session,
        *,
        account_id: Any,
        status: Optional[str] = None,
        include_dismissed: bool = False,
    ) -> list[ToolCostFlag]:
        """List flags for an account, newest window first.

        Args:
            db: Database session.
            account_id: Owning account id.
            status: If set, only return flags with this exact status.
            include_dismissed: When ``status`` is not set, include dismissed
                flags in the result. Defaults to excluding them.

        Returns:
            Matching flag rows ordered by window start (newest first).
        """
        query = db.query(self.model).filter(self.model.account_id == account_id)
        if status is not None:
            query = query.filter(self.model.status == status)
        elif not include_dismissed:
            query = query.filter(self.model.status.notin_(_DEFAULT_HIDDEN_STATUSES))
        return query.order_by(
            self.model.window_start.desc(), self.model.tool_name.asc()
        ).all()

    def set_status(
        self,
        db: Session,
        *,
        account_id: Any,
        flag_id: Any,
        status: str,
        commit: bool = True,
    ) -> Optional[ToolCostFlag]:
        """Set the lifecycle status of one flag, scoped to its account.

        Used for dismiss, snooze, and reopen transitions. The update is scoped
        to ``account_id`` so a caller cannot mutate another account's flag.

        Args:
            db: Database session.
            account_id: Owning account id (authorization scope).
            flag_id: Identifier of the flag to update.
            status: New status: 'open', 'dismissed', or 'snoozed'.
            commit: Whether to commit the transaction.

        Returns:
            The updated flag row, or ``None`` if no matching flag exists for
            this account.

        Raises:
            ValueError: If ``status`` is not a recognized lifecycle status.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}; expected one of {_VALID_STATUSES}."
            )
        db_obj = (
            db.query(self.model)
            .filter(
                self.model.id == flag_id,
                self.model.account_id == account_id,
            )
            .first()
        )
        if db_obj is None:
            return None
        db_obj.status = status
        db.add(db_obj)
        if commit:
            db.commit()
            db.refresh(db_obj)
        else:
            db.flush()
        return db_obj


crud_tool_cost_flag = CRUDToolCostFlag(ToolCostFlag)
