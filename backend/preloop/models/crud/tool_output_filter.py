"""CRUD operations for tool output filters."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..models.tool_output_filter import ToolOutputFilter
from .base import CRUDBase


class CRUDToolOutputFilter(CRUDBase[ToolOutputFilter]):
    """CRUD helpers for account-level tool output filters."""

    def create(
        self,
        db: Session,
        *,
        account_id: Any,
        tool_name: str,
        dropped_fields: list[str],
        server_name: Optional[str] = None,
        managed_agent_id: Optional[Any] = None,
        enabled: bool = True,
        commit: bool = True,
    ) -> ToolOutputFilter:
        """Create a tool output filter for an account.

        Args:
            db: Database session.
            account_id: Owning account id.
            tool_name: Name of the tool whose results are filtered.
            dropped_fields: Top-level field names to strip from each result.
            server_name: MCP server name to scope to; None matches any server.
            managed_agent_id: Managed agent to scope to; None = account-wide.
            enabled: Whether the filter is active.
            commit: Whether to commit the transaction.

        Returns:
            The created filter row.
        """
        db_obj = ToolOutputFilter(
            account_id=account_id,
            tool_name=tool_name,
            dropped_fields=list(dropped_fields),
            server_name=server_name,
            managed_agent_id=managed_agent_id,
            enabled=enabled,
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
    ) -> list[ToolOutputFilter]:
        """Return all filters for an account, newest first.

        Args:
            db: Database session.
            account_id: Owning account id.

        Returns:
            List of filter rows belonging to the account.
        """
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id)
            .order_by(self.model.created_at.desc())
            .all()
        )

    def list_active_for_tool(
        self,
        db: Session,
        *,
        account_id: Any,
        tool_name: str,
        server_name: Optional[str] = None,
        managed_agent_id: Optional[Any] = None,
    ) -> list[ToolOutputFilter]:
        """Return enabled filters matching a tool call.

        A filter matches when it is enabled, owned by the account, and its
        ``tool_name`` equals the call's tool. A filter's ``server_name`` or
        ``managed_agent_id`` of ``NULL`` means "any", so it always matches that
        dimension; a non-null value must equal the call's corresponding value.

        Args:
            db: Database session.
            account_id: Owning account id.
            tool_name: Name of the tool being called.
            server_name: MCP server name for the current call, if known.
            managed_agent_id: Managed agent id for the current call, if known.

        Returns:
            List of enabled, matching filter rows.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.enabled.is_(True),
            self.model.tool_name == tool_name,
        )

        # NULL server_name matches any server; otherwise it must equal the call.
        if server_name is None:
            query = query.filter(self.model.server_name.is_(None))
        else:
            query = query.filter(
                or_(
                    self.model.server_name.is_(None),
                    self.model.server_name == server_name,
                )
            )

        # NULL managed_agent_id matches any agent; otherwise it must equal.
        if managed_agent_id is None:
            query = query.filter(self.model.managed_agent_id.is_(None))
        else:
            query = query.filter(
                or_(
                    self.model.managed_agent_id.is_(None),
                    self.model.managed_agent_id == managed_agent_id,
                )
            )

        return query.all()

    def get(  # type: ignore[override]
        self,
        db: Session,
        *,
        id: Any,
        account_id: Any,
    ) -> Optional[ToolOutputFilter]:
        """Return a single filter scoped to its owning account.

        Args:
            db: Database session.
            id: Identifier of the filter.
            account_id: Owning account id (authorization scope).

        Returns:
            The matching filter row, or ``None`` if not found / not owned.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )

    def delete(  # type: ignore[override]
        self,
        db: Session,
        *,
        id: Any,
        account_id: Any,
        commit: bool = True,
    ) -> bool:
        """Delete a filter scoped to its owning account.

        Args:
            db: Database session.
            id: Identifier of the filter to delete.
            account_id: Owning account id (authorization scope).
            commit: Whether to commit the transaction.

        Returns:
            ``True`` if a row was deleted, ``False`` if no matching filter
            exists for this account.
        """
        db_obj = self.get(db, id=id, account_id=account_id)
        if db_obj is None:
            return False
        db.delete(db_obj)
        if commit:
            db.commit()
        else:
            db.flush()
        return True


crud_tool_output_filter = CRUDToolOutputFilter(ToolOutputFilter)
