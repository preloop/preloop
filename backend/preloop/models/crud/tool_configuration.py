"""CRUD operations for ToolConfiguration model."""

from typing import Any, List, Optional

from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    def get_disabled_builtin_tool_names(
        self,
        db: Session,
        account_id: str,
        tool_names: List[str],
    ) -> set[str]:
        """Names among ``tool_names`` an account-wide builtin row disables.

        Only account-wide rows (no MCP server, no managed-agent scope) count:
        they are the ones that hide a builtin from every caller in the
        account.

        Args:
            db: Database session.
            account_id: Account to look in.
            tool_names: Builtin tool names to check.

        Returns:
            The subset of ``tool_names`` with an explicit disabled row.
        """
        if not tool_names:
            return set()
        rows = (
            db.query(self.model.tool_name)
            .filter(
                self.model.account_id == account_id,
                self.model.tool_source == "builtin",
                self.model.is_enabled.is_(False),
                self.model.mcp_server_id.is_(None),
                self.model.managed_agent_id.is_(None),
                self.model.tool_name.in_(tool_names),
            )
            .distinct()
            .all()
        )
        return {row.tool_name for row in rows if row.tool_name}


crud_tool_configuration = CRUDToolConfiguration()


def _account_wide_first_order() -> tuple:
    """Order clauses that put the account-wide row first, then the oldest.

    ``uq_account_tool_source`` lets an account-wide row coexist with rows
    scoped to one MCP server or one managed agent for the same tool, and
    databases migrated before the constraint treated NULL scopes as distinct
    may still hold exact duplicates. Lookups keyed only by name and source
    therefore pick deterministically instead of asserting a single row.
    """
    return (
        models.ToolConfiguration.managed_agent_id.is_not(None),
        models.ToolConfiguration.mcp_server_id.is_not(None),
        models.ToolConfiguration.created_at,
        models.ToolConfiguration.id,
    )


def _insert_if_absent_stmt(values: dict[str, Any]) -> Any:
    """``INSERT ... ON CONFLICT DO NOTHING RETURNING id`` for one config row.

    ``uq_account_tool_source`` treats NULL scopes as equal, so a concurrent
    get-or-create loser inserts nothing instead of raising ``IntegrityError``
    and the caller re-selects the winner. No rollback is needed, so objects
    the caller already loaded in the session stay usable.
    """
    return (
        pg_insert(models.ToolConfiguration)
        .values(**values)
        # No arbiter: matches both the NULLS NOT DISTINCT constraint (PG 15+)
        # and the expression unique index the migration creates on older
        # servers. The only other unique key is the generated primary key.
        .on_conflict_do_nothing()
        .returning(models.ToolConfiguration.id)
    )


def _same_scope_stmt(values: dict[str, Any]) -> Any:
    """Select the row occupying the scope that ``values`` would insert into."""
    cfg = models.ToolConfiguration
    return select(cfg).where(
        cfg.account_id == values["account_id"],
        cfg.tool_name == values["tool_name"],
        cfg.tool_source == values.get("tool_source", "builtin"),
        cfg.mcp_server_id.is_not_distinct_from(values.get("mcp_server_id")),
        cfg.managed_agent_id.is_not_distinct_from(values.get("managed_agent_id")),
    )


def get_or_create_tool_configuration_sync(
    db: Session, *, values: dict[str, Any]
) -> tuple[models.ToolConfiguration, bool]:
    """Insert a tool configuration unless its scope is taken; flush only.

    Args:
        db: The sync database session (the caller owns the transaction).
        values: Column values for the new row, including ``account_id``.

    Returns:
        ``(row, created)``; ``created`` is False when a concurrent writer or
        an existing row already held the scope.
    """
    new_id = db.execute(_insert_if_absent_stmt(values)).scalar()
    if new_id is not None:
        return db.get(models.ToolConfiguration, new_id), True
    return db.execute(_same_scope_stmt(values)).scalars().one(), False


async def get_or_create_tool_configuration_async(
    db: Session,
    *,
    obj_in: schemas.ToolConfigurationCreate,
    account_id: str,
    commit: bool = True,
) -> models.ToolConfiguration:
    """Async: create a tool configuration, or return the row holding its scope.

    Safe under concurrent first inserts: the loser adopts the winner's row
    without rolling back the caller's session.

    Args:
        db: The async database session.
        obj_in: The data for the new tool configuration.
        account_id: The ID of the account.

    Returns:
        The created or already existing tool configuration.
    """
    values = obj_in.model_dump()
    values["account_id"] = account_id
    new_id = (await db.execute(_insert_if_absent_stmt(values))).scalar()
    if commit:
        await db.commit()
    else:
        await db.flush()
    if new_id is not None:
        return await db.get(models.ToolConfiguration, new_id)
    return (await db.execute(_same_scope_stmt(values))).scalars().one()


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
        The tool configuration object if found, otherwise None. An
        account-wide row (no MCP server, no managed-agent scope) wins over a
        scoped row for the same tool; among equals the oldest row wins, so
        legacy duplicates never raise ``MultipleResultsFound``.
    """
    result = await db.execute(
        select(models.ToolConfiguration)
        .where(
            models.ToolConfiguration.account_id == account_id,
            models.ToolConfiguration.tool_name == tool_name,
            models.ToolConfiguration.tool_source == tool_source,
        )
        .order_by(*_account_wide_first_order())
        .limit(1)
    )
    return result.scalars().first()


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
        select(models.ToolConfiguration)
        .where(
            models.ToolConfiguration.account_id == account_id,
            models.ToolConfiguration.tool_name == tool_name,
        )
        .order_by(*_account_wide_first_order())
        .limit(1)
    )
    return result.scalars().first()


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


def _route_query(
    *,
    account_id: Any,
    tool_name: str,
    tool_source: str,
    mcp_server_id: Optional[Any] = None,
) -> Any:
    """Select only this dispatch route; never borrow a same-name tool's rules."""
    return select(models.ToolConfiguration).where(
        models.ToolConfiguration.account_id == account_id,
        models.ToolConfiguration.tool_name == tool_name,
        models.ToolConfiguration.tool_source == tool_source,
        models.ToolConfiguration.mcp_server_id == mcp_server_id,
        models.ToolConfiguration.managed_agent_id.is_(None),
    )


def get_tool_config_for_route(
    db: Session, **identity: Any
) -> Optional[models.ToolConfiguration]:
    """Read an exact route; ambiguous stored configurations fail closed."""
    return db.execute(_route_query(**identity)).scalar_one_or_none()


async def get_tool_config_for_route_async(
    db: Any, **identity: Any
) -> Optional[models.ToolConfiguration]:
    """Read an exact route asynchronously, failing closed on ambiguity."""
    result = await db.execute(_route_query(**identity))
    return result.scalar_one_or_none()
