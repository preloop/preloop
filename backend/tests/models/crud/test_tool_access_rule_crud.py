"""Tests for ToolAccessRule CRUD operations."""

import uuid

from sqlalchemy.orm import Session

from preloop.models.crud import crud_tool_access_rule
from preloop.models.models.tool_configuration import ToolConfiguration
from preloop.models.schemas.tool_configuration import ApprovalWorkflowCreate
from preloop.models.crud import crud_approval_workflow


class TestToolAccessRuleCRUD:
    """Test CRUD operations for ToolAccessRule."""

    def _create_tool_config(
        self, db_session: Session, account_id: uuid.UUID, tool_name: str = "test_tool"
    ) -> ToolConfiguration:
        """Create a tool configuration for testing."""
        crud_approval_workflow.create(
            db_session,
            obj_in=ApprovalWorkflowCreate(name="Test Workflow", approval_type="manual"),
            account_id=str(account_id),
        )
        db_session.flush()

        tool_config = ToolConfiguration(
            tool_name=tool_name,
            tool_source="builtin",
            account_id=account_id,
        )
        db_session.add(tool_config)
        db_session.flush()
        return tool_config

    def test_create_and_get(self, db_session: Session, create_account):
        """Test creating an access rule and retrieving by ID."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        obj_in = {
            "account_id": account.id,
            "tool_configuration_id": tool_config.id,
            "action": "require_approval",
            "priority": 0,
            "condition_type": "simple",
            "is_enabled": True,
        }
        rule = crud_tool_access_rule.create(db_session, obj_in=obj_in)
        db_session.flush()

        assert rule.id is not None
        assert rule.account_id == account.id
        assert rule.action == "require_approval"
        assert rule.priority == 0

        found = crud_tool_access_rule.get(
            db_session, id=rule.id, account_id=str(account.id)
        )
        assert found is not None
        assert found.id == rule.id

    def test_get_with_wrong_account_returns_none(
        self, db_session: Session, create_account
    ):
        """Test get with wrong account_id returns None."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        obj_in = {
            "account_id": account.id,
            "tool_configuration_id": tool_config.id,
            "action": "allow",
            "priority": 0,
            "condition_type": "simple",
        }
        rule = crud_tool_access_rule.create(db_session, obj_in=obj_in)
        db_session.flush()

        found = crud_tool_access_rule.get(
            db_session, id=rule.id, account_id=str(uuid.uuid4())
        )
        assert found is None

    def test_get_multi_by_config(self, db_session: Session, create_account):
        """Test retrieving rules by tool configuration."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        for i in range(3):
            crud_tool_access_rule.create(
                db_session,
                obj_in={
                    "account_id": account.id,
                    "tool_configuration_id": tool_config.id,
                    "action": "allow",
                    "priority": i,
                    "condition_type": "simple",
                },
            )
        db_session.flush()

        rules = crud_tool_access_rule.get_multi_by_config(
            db_session,
            config_id=str(tool_config.id),
            account_id=str(account.id),
        )
        assert len(rules) == 3
        assert rules[0].priority <= rules[1].priority <= rules[2].priority

    def test_get_multi_by_config_enabled_only(
        self, db_session: Session, create_account
    ):
        """Test get_multi_by_config with enabled_only filter."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "allow",
                "priority": 0,
                "condition_type": "simple",
                "is_enabled": True,
            },
        )
        crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "deny",
                "priority": 1,
                "condition_type": "simple",
                "is_enabled": False,
            },
        )
        db_session.flush()

        rules = crud_tool_access_rule.get_multi_by_config(
            db_session,
            config_id=str(tool_config.id),
            account_id=str(account.id),
            enabled_only=True,
        )
        assert len(rules) == 1
        assert rules[0].action == "allow"

    def test_get_first_by_config(self, db_session: Session, create_account):
        """Test get_first_by_config returns highest-priority rule."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "deny",
                "priority": 10,
                "condition_type": "simple",
            },
        )
        crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "require_approval",
                "priority": 0,
                "condition_type": "simple",
            },
        )
        db_session.flush()

        first = crud_tool_access_rule.get_first_by_config(
            db_session,
            config_id=str(tool_config.id),
            account_id=str(account.id),
        )
        assert first is not None
        assert first.action == "require_approval"
        assert first.priority == 0

    def test_update(self, db_session: Session, create_account):
        """Test updating an access rule."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        rule = crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "allow",
                "priority": 0,
                "condition_type": "simple",
            },
        )
        db_session.flush()

        updated = crud_tool_access_rule.update(
            db_session,
            db_obj=rule,
            obj_in={"action": "deny", "description": "Updated rule"},
        )
        assert updated.action == "deny"
        assert updated.description == "Updated rule"

    def test_remove(self, db_session: Session, create_account):
        """Test removing an access rule."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        rule = crud_tool_access_rule.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "tool_configuration_id": tool_config.id,
                "action": "allow",
                "priority": 0,
                "condition_type": "simple",
            },
        )
        db_session.flush()
        rule_id = rule.id

        removed = crud_tool_access_rule.remove(
            db_session, id=str(rule_id), account_id=str(account.id)
        )
        assert removed is not None
        assert removed.id == rule_id

        found = crud_tool_access_rule.get(
            db_session, id=rule_id, account_id=str(account.id)
        )
        assert found is None

    def test_remove_by_config(self, db_session: Session, create_account):
        """Test remove_by_config deletes all rules for a config."""
        account = create_account()
        tool_config = self._create_tool_config(db_session, account.id)

        for i in range(3):
            crud_tool_access_rule.create(
                db_session,
                obj_in={
                    "account_id": account.id,
                    "tool_configuration_id": tool_config.id,
                    "action": "allow",
                    "priority": i,
                    "condition_type": "simple",
                },
            )
        db_session.flush()

        count = crud_tool_access_rule.remove_by_config(
            db_session,
            config_id=str(tool_config.id),
            account_id=str(account.id),
        )
        assert count == 3

        rules = crud_tool_access_rule.get_multi_by_config(
            db_session,
            config_id=str(tool_config.id),
            account_id=str(account.id),
        )
        assert len(rules) == 0
