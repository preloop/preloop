"""CRUD operations for ToolConfiguration model."""

from typing import Any, List, Optional

from sqlalchemy.orm import Session
from sqlalchemy.future import select

from .. import models, schemas
from .base import CRUDBase


class CRUDToolConfiguration(CRUDBase[models.ToolConfiguration]):
    """CRUD operations for ToolConfiguration model."""

    def __init__(self):
        """Initialize with the ToolConfiguration model."""
        super().__init__(model=models.ToolConfiguration)

    def get(
        self, db: Session, id: str, account_id: str
    ) -> Optional[models.ToolConfiguration]:
        """Retrieve a tool configuration by its ID.

        Args:
            db: The database session.
            id: The ID of the tool configuration to retrieve.
            account_id: The ID of the account associated with the configuration.

        Returns:
            The tool configuration object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )

    def get_by_tool_identifier(
        self, db: Session, tool_identifier: str, account_id: str
    ) -> Optional[models.ToolConfiguration]:
        """Retrieve a tool configuration by tool identifier and account.

        Args:
            db: The database session.
            tool_identifier: The tool identifier (e.g., "default:create_issue").
            account_id: The ID of the account.

        Returns:
            The tool configuration object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.tool_identifier == tool_identifier,
                self.model.account_id == account_id,
            )
            .first()
        )

    def get_by_tool_name_and_source(
        self, db: Session, account_id: str, tool_name: str, tool_source: str
    ) -> Optional[models.ToolConfiguration]:
        """Retrieve a tool configuration by tool name, source, and account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            tool_name: The name of the tool.
            tool_source: The source of the tool (e.g., "mcp", "builtin").

        Returns:
            The tool configuration object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.tool_name == tool_name,
                self.model.tool_source == tool_source,
            )
            .first()
        )

    def get_by_tool_name(
        self, db: Session, account_id: str, tool_name: str
    ) -> Optional[models.ToolConfiguration]:
        """Retrieve a tool configuration by tool name and account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            tool_name: The name of the tool.

        Returns:
            The tool configuration object if found, otherwise None.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.tool_name == tool_name,
            )
            .first()
        )

    def get_for_server(
        self,
        db: Session,
        *,
        account_id: str,
        tool_name: str,
        mcp_server_id: Optional[str] = None,
    ) -> List[models.ToolConfiguration]:
        """Fetch at most two exact matches, retaining ambiguity detection.

        A missing server id selects only builtin configurations. All predicates
        run in SQL; this never materializes the account's tool inventory.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.tool_name == tool_name,
        )
        if mcp_server_id is None:
            query = query.filter(self.model.tool_source == "builtin")
        else:
            query = query.filter(self.model.mcp_server_id == mcp_server_id)
        return query.limit(2).all()

    def get_multi_by_account(
        self,
        db: Session,
        account_id: str,
        skip: int = 0,
        limit: int = 100,
        enabled_only: bool = False,
    ) -> List[models.ToolConfiguration]:
        """Retrieve tool configurations for a specific account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            skip: Number of records to skip.
            limit: Maximum number of records to return.
            enabled_only: If True, only return enabled tools.

        Returns:
            List of tool configuration objects.
        """
        query = db.query(self.model).filter(self.model.account_id == account_id)

        if enabled_only:
            query = query.filter(self.model.enabled)

        return query.offset(skip).limit(limit).all()

    def get_default_tools(
        self,
        db: Session,
        account_id: str,
        enabled_only: bool = True,
    ) -> List[models.ToolConfiguration]:
        """Retrieve default tool configurations for an account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            enabled_only: If True, only return enabled tools.

        Returns:
            List of default tool configuration objects.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            self.model.is_default_tool,
        )

        if enabled_only:
            query = query.filter(self.model.enabled)

        return query.all()

    def get_proxied_tools(
        self,
        db: Session,
        account_id: str,
        enabled_only: bool = True,
    ) -> List[models.ToolConfiguration]:
        """Retrieve proxied tool configurations for an account.

        Args:
            db: The database session.
            account_id: The ID of the account.
            enabled_only: If True, only return enabled tools.

        Returns:
            List of proxied tool configuration objects.
        """
        query = db.query(self.model).filter(
            self.model.account_id == account_id,
            not self.model.is_default_tool,
        )

        if enabled_only:
            query = query.filter(self.model.enabled)

        return query.all()

    def create(
        self,
        db: Session,
        *,
        config_in: schemas.ToolConfigurationCreate,
    ) -> models.ToolConfiguration:
        """Create a new tool configuration.

        Args:
            db: The database session.
            config_in: The data for the new tool configuration.

        Returns:
            The created tool configuration object.
        """
        db_config = self.model(**config_in.model_dump())
        db.add(db_config)
        db.commit()
        db.refresh(db_config)
        return db_config

    def update(
        self,
        db: Session,
        *,
        db_obj: models.ToolConfiguration,
        config_in: schemas.ToolConfigurationUpdate,
    ) -> models.ToolConfiguration:
        """Update an existing tool configuration.

        Args:
            db: The database session.
            db_obj: The existing tool configuration object to update.
            config_in: The new data for the tool configuration.

        Returns:
            The updated tool configuration object.
        """
        update_data = config_in.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def remove(
        self, db: Session, *, id: str, account_id: str
    ) -> Optional[models.ToolConfiguration]:
        """Remove a tool configuration by its ID.

        Args:
            db: The database session.
            id: The ID of the tool configuration to remove.
            account_id: The ID of the account.

        Returns:
            The removed tool configuration object if found and deleted, otherwise None.
        """
        db_config = (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )
        if db_config:
            db.delete(db_config)
            db.commit()
        return db_config

    def get_by_source(
        self,
        db: Session,
        account_id: str,
        tool_source: str,
    ) -> List[models.ToolConfiguration]:
        """Retrieve tool configurations by account and source.

        Args:
            db: The database session.
            account_id: The ID of the account.
            tool_source: The tool source (e.g., "mcp", "builtin").

        Returns:
            List of tool configuration objects.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.tool_source == tool_source,
            )
            .all()
        )

    def count_by_workflow(self, db: Session, workflow_id: str) -> int:
        """Count tool configurations using a specific approval workflow.

        Args:
            db: The database session.
            workflow_id: The ID of the approval workflow.

        Returns:
            The count of tool configurations using this workflow.
        """
        return (
            db.query(self.model)
            .filter(self.model.approval_workflow_id == workflow_id)
            .count()
        )

    def get_by_mcp_server(
        self, db: Session, mcp_server_id: str
    ) -> List[models.ToolConfiguration]:
        """Get all tool configurations for a specific MCP server.

        Args:
            db: The database session.
            mcp_server_id: The ID of the MCP server.

        Returns:
            List of tool configurations associated with the MCP server.
        """
        return (
            db.query(self.model)
            .filter(
                self.model.tool_source == "mcp",
                self.model.mcp_server_id == mcp_server_id,
            )
            .all()
        )

    def get_enabled_tool_names(
        self,
        db: Session,
        account_id: str,
        tool_names: List[str],
        allowed_server_ids: List[Any],
    ) -> set[str]:
        """Retrieve enabled tool names for an account given a requested list of tool names and allowed servers."""
        from sqlalchemy import or_

        query = db.query(self.model.tool_name).filter(
            self.model.account_id == account_id,
            self.model.is_enabled.is_(True),
            self.model.tool_name.in_(tool_names),
        )
        if allowed_server_ids:
            query = query.filter(
                or_(
                    self.model.mcp_server_id.is_(None),
                    self.model.mcp_server_id.in_(allowed_server_ids),
                )
            )
        else:
            query = query.filter(self.model.mcp_server_id.is_(None))

        rows = query.distinct().all()
        return {row.tool_name for row in rows if row.tool_name}


crud_tool_configuration = CRUDToolConfiguration()


# Async helper functions
async def get_tool_config_by_name_and_source_async(
    db: Session,
    account_id: str,
    tool_name: str,
    tool_source: str,
) -> Optional[models.ToolConfiguration]:
    """Async: Retrieve a tool configuration by tool name, source, and account.

    Args:
        db: The async database session.
        account_id: The ID of the account.
        tool_name: The name of the tool.
        tool_source: The source of the tool (e.g., "mcp", "builtin").

    Returns:
        The tool configuration object if found, otherwise None.
    """
    result = await db.execute(
        select(models.ToolConfiguration).where(
            models.ToolConfiguration.account_id == account_id,
            models.ToolConfiguration.tool_name == tool_name,
            models.ToolConfiguration.tool_source == tool_source,
        )
    )
    return result.scalar_one_or_none()


async def get_tool_config_by_id_async(
    db: Session,
    account_id: str,
    id: str,
) -> Optional[models.ToolConfiguration]:
    """Async: Retrieve a tool configuration by ID and account."""
    result = await db.execute(
        select(models.ToolConfiguration).where(
            models.ToolConfiguration.account_id == account_id,
            models.ToolConfiguration.id == id,
        )
    )
    return result.scalar_one_or_none()


async def get_tool_config_by_tool_name_async(
    db: Session,
    account_id: str,
    tool_name: str,
) -> Optional[models.ToolConfiguration]:
    """Async: Retrieve a tool configuration by tool name and account."""
    result = await db.execute(
        select(models.ToolConfiguration).where(
            models.ToolConfiguration.account_id == account_id,
            models.ToolConfiguration.tool_name == tool_name,
        )
    )
    return result.scalar_one_or_none()


async def create_tool_configuration_async(
    db: Session,
    *,
    obj_in: schemas.ToolConfigurationCreate,
    account_id: str,
) -> models.ToolConfiguration:
    """Async: Create a new tool configuration.

    Args:
        db: The async database session.
        obj_in: The data for the new tool configuration.
        account_id: The ID of the account.

    Returns:
        The created tool configuration object.
    """
    # Convert Pydantic model to dict and add account_id
    config_data = obj_in.model_dump()
    config_data["account_id"] = account_id

    db_config = models.ToolConfiguration(**config_data)
    db.add(db_config)
    await db.commit()
    await db.refresh(db_config)
    return db_config
