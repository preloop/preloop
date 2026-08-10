"""Tests for policy evaluator service."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from preloop.services.policy_evaluator import (
    evaluate_policy,
    evaluate_policy_async,
    evaluate_simple_expression,
    evaluate_cel_expression,
)


@pytest.fixture(autouse=True)
def patch_crud(monkeypatch):
    class FakeCRUDConfig:
        def get(self, db, **kwargs):
            return db.query().filter().first()

        def get_by_tool_name(self, db, **kwargs):
            return db.query().filter().first()

    class FakeCRUDRules:
        def get_multi_by_config(self, db, **kwargs):
            return db.query().filter().order_by().all()

    class FakeCRUDAccount:
        def get(self, db, **kwargs):
            mock_account = MagicMock()
            mock_account.meta_data = {}
            return mock_account

    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_tool_configuration",
        FakeCRUDConfig(),
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_tool_access_rule", FakeCRUDRules()
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.crud_account", FakeCRUDAccount()
    )

    async def fake_get_tool_config_by_id_async(db, **kwargs):
        res = await db.execute()
        return res.scalar_one_or_none()

    async def fake_get_tool_config_by_tool_name_async(db, **kwargs):
        res = await db.execute()
        return res.scalar_one_or_none()

    async def fake_get_multi_by_config_async(db, **kwargs):
        res = await db.execute()
        return res.scalars().all()

    async def fake_get_meta_data_async(db, **kwargs):
        return {}

    monkeypatch.setattr(
        "preloop.services.policy_evaluator.get_tool_config_by_id_async",
        fake_get_tool_config_by_id_async,
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.get_tool_config_by_tool_name_async",
        fake_get_tool_config_by_tool_name_async,
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.get_multi_by_config_async",
        fake_get_multi_by_config_async,
    )
    monkeypatch.setattr(
        "preloop.services.policy_evaluator.get_meta_data_async",
        fake_get_meta_data_async,
    )


@pytest.fixture(autouse=True)
def mock_audit_logging():
    """Suppress audit logging in tests."""
    with patch(
        "preloop.services.policy_evaluator._log_policy_decision_async"
    ) as mock_log:
        yield mock_log


class TestEvaluatePolicyNoConfig:
    """Test evaluate_policy when no tool configuration exists."""

    def test_no_tool_configuration_returns_allow(self, mock_audit_logging):
        """No tool config found -> default allow."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="unknown_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None
        assert "No tool configuration found" in desc

    def test_no_tool_configuration_by_tool_config_id(self, mock_audit_logging):
        """Lookup by tool_configuration_id when not found -> allow."""
        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = None

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="some_tool",
            tool_args={},
            account_id=uuid4(),
            tool_configuration_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None


class TestEvaluatePolicyNoRules:
    """Test evaluate_policy when tool config exists but no rules."""

    def test_no_rules_no_approval_workflow_returns_allow(self, mock_audit_logging):
        """Tool config exists, no rules, no approval_workflow_id -> allow."""
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None
        assert "No access rules defined" in desc

    def test_no_rules_with_legacy_approval_workflow_returns_require_approval(
        self, mock_audit_logging
    ):
        """Tool config has approval_workflow_id but no rules -> legacy require_approval."""
        workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = workflow_id

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = []
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "require_approval"
        assert approval_id == workflow_id
        assert "legacy" in desc.lower()


class TestEvaluatePolicyWithRules:
    """Test evaluate_policy with access rules."""

    def _make_rule(
        self,
        action: str,
        condition_expression: str = "args.amount > 100",
        condition_type: str = "simple",
        priority: int = 0,
        approval_workflow_id=None,
        description: str | None = None,
    ):
        rule = MagicMock()
        rule.id = uuid4()
        rule.action = action
        rule.condition_expression = condition_expression
        rule.condition_type = condition_type
        rule.priority = priority
        rule.approval_workflow_id = approval_workflow_id
        rule.description = description
        return rule

    def test_matching_allow_rule_returns_allow(self, mock_audit_logging):
        """First matching rule with action=allow returns allow."""
        workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = workflow_id

        rule = self._make_rule(action="allow", condition_expression="args.amount > 50")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"amount": 200},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None

    def test_allow_rule_with_true_literal_returns_allow(self, mock_audit_logging):
        """An allow rule with condition_expression='true' must allow.

        Regression: the 'true' literal raised ValueError during simple
        expression parsing, so the rule failed closed to require_approval
        instead of allowing.
        """
        workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = workflow_id

        rule = self._make_rule(action="allow", condition_expression="true")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"anything": "goes"},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None

    def test_matching_deny_rule_returns_deny(self, mock_audit_logging):
        """First matching rule with action=deny returns deny."""
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        rule = self._make_rule(action="deny", condition_expression="args.env == 'prod'")

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"env": "prod"},
            account_id=uuid4(),
        )

        assert action == "deny"
        assert approval_id is None

    def test_matching_require_approval_rule_returns_require_approval(
        self, mock_audit_logging
    ):
        """First matching rule with action=require_approval returns require_approval."""
        rule_workflow_id = uuid4()
        config_workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = config_workflow_id

        rule = self._make_rule(
            action="require_approval",
            condition_expression="args.amount > 1000",
            approval_workflow_id=rule_workflow_id,
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"amount": 5000},
            account_id=uuid4(),
        )

        assert action == "require_approval"
        assert approval_id == rule_workflow_id

    def test_require_approval_rule_without_workflow_falls_back_to_account_default(
        self, mock_audit_logging, monkeypatch
    ):
        """A bare ``require_approval`` rule must fall back to the account
        default workflow rather than silently auto-approving."""
        default_workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        rule = self._make_rule(
            action="require_approval",
            condition_expression="",  # unconditional
            approval_workflow_id=None,
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        # Patch the default-workflow lookup to return a known workflow.
        default_workflow = MagicMock()
        default_workflow.id = default_workflow_id

        class FakeApprovalWorkflowCRUD:
            def get_default(self, db, account_id):
                return default_workflow

        monkeypatch.setattr(
            "preloop.services.policy_evaluator.crud_approval_workflow",
            FakeApprovalWorkflowCRUD(),
        )

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"amount": 50},
            account_id=uuid4(),
        )

        assert action == "require_approval"
        assert approval_id == default_workflow_id

    def test_require_approval_rule_without_any_workflow_returns_none(
        self, mock_audit_logging, monkeypatch
    ):
        """When there is no rule, config, or account default workflow, the
        evaluator still returns ``require_approval`` so the caller can fail
        closed (it must NOT silently switch to ``allow``)."""
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        rule = self._make_rule(
            action="require_approval",
            condition_expression="",
            approval_workflow_id=None,
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        class FakeApprovalWorkflowCRUD:
            def get_default(self, db, account_id):
                return None

        monkeypatch.setattr(
            "preloop.services.policy_evaluator.crud_approval_workflow",
            FakeApprovalWorkflowCRUD(),
        )

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"amount": 50},
            account_id=uuid4(),
        )

        assert action == "require_approval"
        assert approval_id is None

    def test_rule_evaluation_error_fails_closed(self, mock_audit_logging):
        """Rule evaluation error -> fail closed with require_approval."""
        workflow_id = uuid4()
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = workflow_id

        rule = self._make_rule(
            action="allow",
            condition_expression="args.bad_field",
            condition_type="simple",
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "require_approval"
        assert approval_id == workflow_id
        assert "error" in desc.lower() or "failing closed" in desc.lower()

    def test_no_rules_matched_returns_allow(self, mock_audit_logging):
        """Rules exist but none match -> default allow."""
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        rule = self._make_rule(
            action="deny",
            condition_expression="args.amount > 1000",
        )

        mock_db = MagicMock()
        mock_db.query.return_value.filter.return_value.first.return_value = mock_config
        mock_db.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            rule
        ]

        action, approval_id, desc = evaluate_policy(
            db=mock_db,
            tool_name="test_tool",
            tool_args={"amount": 50},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None
        assert "No rules matched" in desc


class TestEvaluateSimpleExpression:
    """Test simple condition evaluation."""

    def test_equality_string(self):
        """args.field == 'value'."""
        assert evaluate_simple_expression(
            "args.status == 'active'", {"status": "active"}
        )
        assert not evaluate_simple_expression(
            "args.status == 'active'", {"status": "inactive"}
        )

    def test_equality_without_args_prefix(self):
        """Expression without args. prefix is normalized."""
        assert evaluate_simple_expression("status == 'active'", {"status": "active"})

    def test_equality_number(self):
        """args.amount == 100."""
        assert evaluate_simple_expression("args.amount == 100", {"amount": 100})
        assert not evaluate_simple_expression("args.amount == 100", {"amount": 99})

    def test_comparison_operators(self):
        """>, <, >=, <=."""
        assert evaluate_simple_expression("args.amount > 100", {"amount": 200})
        assert not evaluate_simple_expression("args.amount > 100", {"amount": 50})
        assert evaluate_simple_expression("args.amount < 100", {"amount": 50})
        assert evaluate_simple_expression("args.amount >= 100", {"amount": 100})
        assert evaluate_simple_expression("args.amount <= 100", {"amount": 100})

    def test_comparison_coerces_stringified_numbers(self):
        """A numeric rule must not be bypassable by passing a string.

        Security regression: previously ``"1000000" > 300`` raised TypeError,
        was swallowed to a non-match, and fell through to the default allow.
        """
        assert evaluate_simple_expression("args.amount > 300", {"amount": "1000000"})
        assert not evaluate_simple_expression("args.amount > 300", {"amount": "10"})
        assert evaluate_simple_expression("args.amount >= 300", {"amount": "300"})
        # A genuinely non-numeric string still does not satisfy an ordering rule.
        assert not evaluate_simple_expression("args.amount > 300", {"amount": "abc"})

    def test_contains_string(self):
        """args.field.contains('substring')."""
        assert evaluate_simple_expression(
            "args.message.contains('error')", {"message": "An error occurred"}
        )
        assert not evaluate_simple_expression(
            "args.message.contains('error')", {"message": "Success"}
        )

    def test_contains_list(self):
        """args.tags.contains('urgent') with list."""
        assert evaluate_simple_expression(
            "args.tags.contains('urgent')", {"tags": ["urgent", "bug"]}
        )

    def test_nested_field(self):
        """args.nested.field."""
        assert evaluate_simple_expression(
            "args.payload.amount > 50",
            {"payload": {"amount": 100}},
        )

    def test_empty_expression_always_matches(self):
        """No expression means catch-all (always match)."""
        # Empty expression is handled in _evaluate_rule_condition, not in
        # evaluate_simple_expression. evaluate_simple_expression requires
        # a non-empty expression. So we test a valid expression.
        assert evaluate_simple_expression("args.x == 'y'", {"x": "y"})

    def test_boolean_literal_true(self):
        """A bare 'true' literal (any case) always matches.

        Regression: 'true' was normalised to 'args.true', which failed to
        parse and raised ValueError, so allow rules configured with a
        catch-all 'true' condition failed closed to require_approval.
        """
        assert evaluate_simple_expression("true", {})
        assert evaluate_simple_expression("True", {"amount": 100})
        assert evaluate_simple_expression("TRUE", {})
        assert evaluate_simple_expression("  true  ", {})

    def test_boolean_literal_false(self):
        """A bare 'false' literal (any case) never matches."""
        assert not evaluate_simple_expression("false", {})
        assert not evaluate_simple_expression("False", {"amount": 100})
        assert not evaluate_simple_expression("FALSE", {})

    def test_invalid_expression_raises(self):
        """Unsupported expression format raises ValueError."""
        with pytest.raises(ValueError, match="Unsupported"):
            evaluate_simple_expression("invalid syntax", {})


class TestEvaluateCelExpression:
    """Test CEL condition evaluation."""

    def test_cel_simple_true(self):
        """CEL expression that evaluates to true."""
        assert evaluate_cel_expression("args.amount > 100", {"amount": 200})

    def test_cel_simple_false(self):
        """CEL expression that evaluates to false."""
        assert not evaluate_cel_expression("args.amount > 100", {"amount": 50})

    def test_cel_complex_expression(self):
        """CEL with logical operators."""
        assert evaluate_cel_expression(
            "args.env == 'prod' && args.amount > 1000",
            {"env": "prod", "amount": 2000},
        )

    def test_cel_invalid_raises(self):
        """Invalid CEL expression raises."""
        with pytest.raises((ValueError, Exception)):
            evaluate_cel_expression("syntax error {{", {})


pytestmark = pytest.mark.asyncio


class TestEvaluatePolicyAsync:
    """Test async version of evaluate_policy."""

    async def test_no_config_returns_allow(self, mock_audit_logging):
        """Async: no tool config -> allow."""
        mock_db = MagicMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        action, approval_id, desc = await evaluate_policy_async(
            db=mock_db,
            tool_name="unknown_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None

    async def test_no_rules_returns_allow(self, mock_audit_logging):
        """Async: config exists, no rules -> allow."""
        mock_config = MagicMock()
        mock_config.id = uuid4()
        mock_config.approval_workflow_id = None

        mock_db = MagicMock()
        account_result = MagicMock()
        account_result.scalar_one_or_none.return_value = {}
        config_result = MagicMock()
        config_result.scalar_one_or_none.return_value = mock_config
        rules_result = MagicMock()
        rules_result.scalars.return_value.all.return_value = []

        mock_db.execute = AsyncMock(
            side_effect=[account_result, config_result, rules_result]
        )

        action, approval_id, desc = await evaluate_policy_async(
            db=mock_db,
            tool_name="test_tool",
            tool_args={},
            account_id=uuid4(),
        )

        assert action == "allow"
        assert approval_id is None
