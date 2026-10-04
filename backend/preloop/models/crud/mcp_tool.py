"""CRUD operations for MCPTool model."""

from typing import List, Optional, Sequence
from uuid import UUID

from sqlalchemy.orm import Session

from .. import models
from .base import CRUDBase


class CRUDMCPTool(CRUDBase[models.MCPTool]):
    """CRUD operations for MCPTool model."""

    def __init__(self):
        """Initialize with the MCPTool model."""
        super().__init__(model=models.MCPTool)

    def get(self, db: Session, id: UUID) -> Optional[models.MCPTool]:
        """Retrieve an MCP tool by its ID.

        Args:
            db: The database session.
            id: The ID of the MCP tool to retrieve.

        Returns:
            The MCP tool object if found, otherwise None.
        """
        return db.query(self.model).filter(self.model.id == id).first()

    def get_by_server(
        self,
        db: Session,
        server_id: UUID,
    ) -> List[models.MCPTool]:
        """Retrieve all tools for a specific MCP server.

        Args:
            db: The database session.
            server_id: The ID of the MCP server.

        Returns:
            List of MCP tool objects.
        """
        return db.query(self.model).filter(self.model.mcp_server_id == server_id).all()

    def get_by_servers_for_account(
        self,
        db: Session,
        *,
        account_id: str,
        server_ids: Sequence[UUID],
    ) -> List[models.MCPTool]:
        """Read an account's selected servers' tools in one query.

        Ownership is checked in SQL even when callers have already fetched the
        account's active servers. An empty selection never widens the query.
        """
        if not server_ids:
            return []
        return (
            db.query(self.model)
            .join(models.MCPServer, self.model.mcp_server_id == models.MCPServer.id)
            .filter(
                models.MCPServer.account_id == UUID(account_id),
                self.model.mcp_server_id.in_(server_ids),
            )
            .all()
        )

    def get_by_server_and_name(
        self,
        db: Session,
        server_id: UUID,
        name: str,
    ) -> Optional[models.MCPTool]:
        """Retrieve a tool by server ID and tool name.

        Args:
            db: The database session.
            server_id: The ID of the MCP server.
            name: The name of the tool.

        Returns:
            The MCP tool object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.mcp_server_id == server_id,
                self.model.name == name,
            )
            .first()
        )

    def get_multi(
        self,
        db: Session,
        skip: int = 0,
        limit: int = 100,
    ) -> List[models.MCPTool]:
        """Retrieve multiple MCP tools.

        Args:
            db: The database session.
            skip: Number of records to skip.
            limit: Maximum number of records to return.

        Returns:
            List of MCP tool objects.
        """
        return db.query(self.model).offset(skip).limit(limit).all()

    def remove(self, db: Session, *, id: UUID) -> Optional[models.MCPTool]:
        """Remove an MCP tool by its ID.

        Args:
            db: The database session.
            id: The ID of the MCP tool to remove.

        Returns:
            The removed MCP tool object if found and deleted, otherwise None.
        """
        db_tool = db.query(self.model).filter(self.model.id == id).first()
        if db_tool:
            db.delete(db_tool)
            db.commit()
        return db_tool

    def remove_by_server(self, db: Session, *, server_id: UUID) -> int:
        """Remove all tools for a specific MCP server.

        Args:
            db: The database session.
            server_id: The ID of the MCP server.

        Returns:
            The number of tools deleted.
        """
        count = (
            db.query(self.model).filter(self.model.mcp_server_id == server_id).delete()
        )
        db.commit()
        return count

    def get_tool_names_by_server_ids(
        self,
        db: Session,
        server_ids: List[UUID],
    ) -> List[str]:
        """Retrieve distinct tool names for a set of MCP servers."""
        if not server_ids:
            return []
        rows = (
            db.query(self.model.name)
            .filter(self.model.mcp_server_id.in_(server_ids))
            .distinct()
            .all()
        )
        return [row.name for row in rows if row.name]


crud_mcp_tool = CRUDMCPTool()
