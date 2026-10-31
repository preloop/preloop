"""CRUD operations for ToolAccessRule model."""

from typing import Dict, List, Optional, Any

from sqlalchemy.orm import Session
from sqlalchemy.future import select

from ..models.tool_access_rule import ToolAccessRule
from .base import CRUDBase


class CRUDToolAccessRule(CRUDBase[ToolAccessRule]):
    """CRUD operations for ToolAccessRule model."""

    def __init__(self):
        """Initialize with the ToolAccessRule model."""
        super().__init__(model=ToolAccessRule)

    def get(
        self, db: Session, id: Any, *, account_id: Optional[str] = None
    ) -> Optional[ToolAccessRule]:
        """Retrieve an access rule by its ID.

        Args:
            db: The database session.
            id: The ID of the access rule to retrieve.
            account_id: The ID of the account (required for security).

        Returns:
            The access rule object if found, otherwise None.
        """
        query = db.query(self.model).filter(self.model.id == id)
        if account_id:
            query = query.filter(self.model.account_id == account_id)
        return query.first()

    def get_multi_by_config(
        self,
        db: Session,
        *,
        config_id: str,
        account_id: str,
        enabled_only: bool = False,
    ) -> List[ToolAccessRule]:
        """Retrieve all access rules for a tool configuration, ordered by priority.

        Args:
            db: The database session.
            config_id: The ID of the tool configuration.
            account_id: The ID of the account.
            enabled_only: If True, only return enabled rules.

        Returns:
            List of access rules ordered by priority (ascending).
        """
        query = db.query(self.model).filter(
            self.model.tool_configuration_id == config_id,
            self.model.account_id == account_id,
        )
        if enabled_only:
            query = query.filter(self.model.is_enabled.is_(True))
        return query.order_by(self.model.priority.asc()).all()

    def get_first_by_config(
        self,
        db: Session,
        *,
        config_id: str,
        account_id: str,
        action: Optional[str] = None,
    ) -> Optional[ToolAccessRule]:
        """Retrieve the first (highest-priority) access rule for a tool configuration.

        Args:
            db: The database session.
            config_id: The ID of the tool configuration.
            account_id: The ID of the account.
            action: Optional action filter (e.g., 'require_approval').

        Returns:
            The first matching access rule, or None.
        """
        query = db.query(self.model).filter(
            self.model.tool_configuration_id == config_id,
            self.model.account_id == account_id,
        )
        if action:
            query = query.filter(self.model.action == action)
        return query.order_by(self.model.priority.asc()).first()

    def get_multi_by_account(
        self,
        db: Session,
        *,
        account_id: str,
    ) -> List[ToolAccessRule]:
        """Retrieve all access rules for an account, ordered by priority.

        Args:
            db: The database session.
            account_id: The ID of the account.

        Returns:
            List of access rules for the account ordered by priority.
        """
        return (
            db.query(self.model)
            .filter(self.model.account_id == account_id)
            .order_by(self.model.priority.asc())
            .all()
        )

    def create(self, db: Session, *, obj_in: Dict[str, Any]) -> ToolAccessRule:
        """Create a new access rule.

        Args:
            db: The database session.
            obj_in: Dictionary with the rule data.

        Returns:
            The created access rule object.
        """
        db_obj = self.model(**obj_in)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def update(
        self, db: Session, *, db_obj: ToolAccessRule, obj_in: Dict[str, Any]
    ) -> ToolAccessRule:
        """Update an existing access rule.

        Args:
            db: The database session.
            db_obj: The existing access rule to update.
            obj_in: Dictionary with the update data.

        Returns:
            The updated access rule object.
        """
        for field, value in obj_in.items():
            if hasattr(db_obj, field):
                setattr(db_obj, field, value)
        db.add(db_obj)
        db.commit()
        db.refresh(db_obj)
        return db_obj

    def remove(
        self, db: Session, *, id: str, account_id: str
    ) -> Optional[ToolAccessRule]:
        """Remove an access rule by its ID.

        Args:
            db: The database session.
            id: The ID of the access rule to remove.
            account_id: The ID of the account (required for security).

        Returns:
            The removed access rule if found, otherwise None.
        """
        db_obj = (
            db.query(self.model)
            .filter(
                self.model.id == id,
                self.model.account_id == account_id,
            )
            .first()
        )
        if db_obj:
            db.delete(db_obj)
            db.commit()
        return db_obj

    def remove_by_config(self, db: Session, *, config_id: str, account_id: str) -> int:
        """Remove all access rules for a tool configuration.

        Args:
            db: The database session.
            config_id: The ID of the tool configuration.
            account_id: The ID of the account.

        Returns:
            The number of deleted rules.
        """
        count = (
            db.query(self.model)
            .filter(
                self.model.tool_configuration_id == config_id,
                self.model.account_id == account_id,
            )
            .delete(synchronize_session="fetch")
        )
        db.commit()
        return count


def list_native_require_approval_tool_names(
    db: Session, *, account_id: Any, tool_source: str = "agent"
) -> List[str]:
    """Native tools with an enabled require-approval rule for every agent.

    Covers account-wide native tool configurations (no managed agent pin):
    the ones a flow run's native commands are evaluated against. Conditions
    are not evaluated: the caller uses this to refuse an executor that cannot
    evaluate any of them.

    Args:
        db: Database session.
        account_id: Account id.
        tool_source: Native tool source (``agent``).

    Returns:
        Sorted distinct tool names.
    """
    from ..models.tool_configuration import ToolConfiguration

    rows = (
        db.query(ToolConfiguration.tool_name)
        .join(
            ToolAccessRule,
            ToolAccessRule.tool_configuration_id == ToolConfiguration.id,
        )
        .filter(
            ToolConfiguration.account_id == account_id,
            ToolConfiguration.tool_source == tool_source,
            ToolConfiguration.is_enabled.is_(True),
            ToolConfiguration.managed_agent_id.is_(None),
            ToolAccessRule.account_id == account_id,
            ToolAccessRule.is_enabled.is_(True),
            ToolAccessRule.action == "require_approval",
        )
        .distinct()
        .all()
    )
    return sorted(str(row[0]) for row in rows)


async def get_multi_by_config_async(
    db: Session,
    *,
    config_id: str,
    account_id: str,
    enabled_only: bool = False,
) -> List[ToolAccessRule]:
    """Async: Retrieve all access rules for a tool configuration, ordered by priority."""
    query = select(ToolAccessRule).where(
        ToolAccessRule.tool_configuration_id == config_id,
        ToolAccessRule.account_id == account_id,
    )
    if enabled_only:
        query = query.where(ToolAccessRule.is_enabled.is_(True))

    result = await db.execute(query.order_by(ToolAccessRule.priority.asc()))
    return result.scalars().all()
