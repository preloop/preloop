"""CRUD operations for MCPServer model."""

from typing import List, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from .. import models
from .base import CRUDBase


class CRUDMCPServer(CRUDBase[models.MCPServer]):
    """CRUD operations for MCPServer model."""

    def __init__(self):
        """Initialize with the MCPServer model."""
        super().__init__(model=models.MCPServer)

    def get(
        self, db: Session, id: UUID, account_id: Optional[str] = None
    ) -> Optional[models.MCPServer]:
        """Retrieve an MCP server by its ID.

        Args:
            db: The database session.
            id: The ID of the MCP server to retrieve.
            account_id: The ID of the account associated with the server. Optional.

        Returns:
            The MCP server object if found, otherwise None.
        """
        query = db.query(self.model).filter(self.model.id == id)

        if account_id:
            query = query.filter(self.model.account_id == account_id)

        return query.first()

    def get_by_name(
        self, db: Session, account_id: str, name: str
    ) -> Optional[models.MCPServer]:
        """Retrieve an MCP server by name and account.

        Args:
            db: The database session.
            name: The name of the MCP server.
            account_id: The ID of the account.

        Returns:
            The MCP server object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.name == name,
            )
            .first()
        )

    def get_multi_by_account(
        self,
        db: Session,
        account_id: str,
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.MCPServer]:
        """Retrieve MCP servers for a specific account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of MCP server objects.
        """
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id)
            .offset(skip)
            .limit(limit)
            .all()
        )

    def get_active_by_account(
        self,
        db: Session,
        account_id: str,
    ) -> List[models.MCPServer]:
        """Retrieve active MCP servers for a specific account.

        Args:
            db: The database session.
            account_id: The ID of the account.

        Returns:
            List of active MCP server objects.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.status == "active",
            )
            .all()
        )

    def get_active_visible_by_account(
        self,
        db: Session,
        account_id: str,
    ) -> List[models.MCPServer]:
        """Active MCP servers the account may call tools on.

        The account's own servers, then servers another account shares with
        it (account hook H3). Without a visibility provider this is exactly
        ``get_active_by_account``. Only tool discovery and tool calls use
        it: policy and configuration code stays on the own servers.
        """
        from preloop.plugins.account_hooks import (
            VISIBLE_MCP_SERVER,
            extra_visible_ids,
        )

        own = self.get_active_by_account(db, account_id=account_id)
        shared_ids = extra_visible_ids(db, account_id, VISIBLE_MCP_SERVER)
        if not shared_ids:
            return own
        own_ids = {server.id for server in own}
        shared = (
            db.query(self.model)
            .filter(
                self.model.id.in_(shared_ids),
                self.model.status == "active",
            )
            .order_by(self.model.created_at, self.model.id)
            .all()
        )
        return own + [server for server in shared if server.id not in own_ids]

    def get_visible(
        self, db: Session, id: UUID, account_id: str
    ) -> Optional[models.MCPServer]:
        """An MCP server the account owns, or one shared with it (hook H3).

        The server row carries its owner's credentials, which the caller
        uses on the server side to connect and never returns to a client.
        """
        own = self.get(db, id=id, account_id=account_id)
        if own is not None:
            return own
        from preloop.plugins.account_hooks import (
            VISIBLE_MCP_SERVER,
            extra_visible_ids,
        )

        shared_ids = {
            str(shared)
            for shared in extra_visible_ids(db, account_id, VISIBLE_MCP_SERVER)
        }
        if str(id) not in shared_ids:
            return None
        return self.get(db, id=id)

    def remove(
        self, db: Session, *, id: UUID, account_id: str
    ) -> Optional[models.MCPServer]:
        """Remove an MCP server by its ID.

        Args:
            db: The database session.
            id: The ID of the MCP server to remove.
            account_id: The ID of the account.

        Returns:
            The removed MCP server object if found and deleted, otherwise None.
        """
        db_server = (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )
        if db_server:
            db.delete(db_server)
            db.commit()
        return db_server
