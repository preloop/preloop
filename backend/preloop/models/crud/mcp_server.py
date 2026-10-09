"""CRUD operations for MCPServer model."""

from typing import List, Optional, Tuple
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
            .order_by(self.model.created_at, self.model.id)
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

    def get_active_visible_for_tool(
        self,
        db: Session,
        *,
        account_id: str,
        tool_name: str,
    ) -> List[Tuple[models.MCPServer, bool]]:
        """Active visible servers exposing ``tool_name``, in first-wins order.

        ``tool_name`` is the name agents see: ``<tool_prefix>_<tool>`` for a
        server with a prefix, the upstream name otherwise. One query (plus
        the account hook's shared ids), used to route a proxied call at call
        time without a full tool discovery. Same visibility as
        ``get_active_visible_by_account``.

        Order is the owner order of #1135: own servers first, then shared
        ones, each by ``created_at`` and then ``id``. The first row owns the
        name. Each row carries whether the MCP tool configuration for that
        server leaves the tool enabled, so a disabled owner hides the name
        instead of handing it to a newer server.
        """
        from sqlalchemy import and_, case, func, or_

        from preloop.plugins.account_hooks import (
            VISIBLE_MCP_SERVER,
            extra_visible_ids,
        )

        shared_ids = list(extra_visible_ids(db, account_id, VISIBLE_MCP_SERVER))
        own = self.model.account_id == UUID(str(account_id))
        visible = or_(own, self.model.id.in_(shared_ids)) if shared_ids else own
        config = models.ToolConfiguration
        # Same rule as ``exposed_tool_name``: NULL or "" means no prefix.
        exposed = case(
            (func.coalesce(self.model.tool_prefix, "") == "", models.MCPTool.name),
            else_=self.model.tool_prefix + "_" + models.MCPTool.name,
        )
        rows = (
            db.query(self.model, config.is_enabled)
            .join(models.MCPTool, models.MCPTool.mcp_server_id == self.model.id)
            .outerjoin(
                config,
                and_(
                    config.account_id == UUID(str(account_id)),
                    config.tool_source == "mcp",
                    config.tool_name == tool_name,
                    config.mcp_server_id == self.model.id,
                ),
            )
            .filter(
                visible,
                self.model.status == "active",
                exposed == tool_name,
            )
            .order_by(case((own, 0), else_=1), self.model.created_at, self.model.id)
            .all()
        )
        ordered: List[models.MCPServer] = []
        enabled_by_id: dict = {}
        for server, enabled in rows:
            if server.id not in enabled_by_id:
                ordered.append(server)
                enabled_by_id[server.id] = True
            if enabled is False:
                enabled_by_id[server.id] = False
        return [(server, enabled_by_id[server.id]) for server in ordered]

    def get_all_by_account(
        self, db: Session, account_id: str
    ) -> List[models.MCPServer]:
        """Every MCP server the account owns, any status, in first-wins order."""
        return (
            db.query(self.model)
            .filter(self.model.account_id == UUID(str(account_id)))
            .order_by(self.model.created_at, self.model.id)
            .all()
        )

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
