"""Tests for approval helper."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from preloop.services.approval_helper import require_approval

pytestmark = pytest.mark.asyncio


def create_mock_db_session():
    """Create a mock async database session."""
    mock_db = AsyncMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=None)
    return mock_db


@pytest.fixture
def mock_context():
    """Create a mock FastMCP Context."""
    ctx = AsyncMock()
    ctx.report_progress = AsyncMock(return_value=None)
    ctx.session = AsyncMock()
    ctx.session.send_progress_notification = AsyncMock()
    ctx.request_id = "test_request_id"
    ctx.request_context = MagicMock()
    ctx.request_context.meta = MagicMock()
    ctx.request_context.meta.progressToken = "test_token"
    return ctx


@pytest.fixture
def tool_config():
    """Create a mock ToolConfiguration."""
    config = MagicMock()
    config.id = str(uuid4())
    config.approval_workflow_id = str(uuid4())  # Tool requires approval if this is set
    return config


@pytest.fixture
def approval_workflow():
    """Create a mock ApprovalWorkflow."""
    policy = MagicMock()
    policy.id = str(uuid4())
    policy.approval_type = "slack"
    policy.channel = "approvals"
    policy.user = None
    policy.timeout_seconds = 300
    policy.notification_channels = ["email"]
    # Escalation fields - default to None (no escalation)
    policy.escalation_user_ids = None
    policy.escalation_team_ids = None
    policy.async_approval_enabled = False
    return policy


@pytest.fixture
def approval_request():
    """Create a mock ApprovalRequest."""
    request = MagicMock()
    request.id = str(uuid4())
    request.approval_token = "test_token"
    request.status = "pending"
    return request


@pytest.fixture
def approval_condition():
    """Create a mock ToolApprovalCondition with no condition enabled."""
    condition = MagicMock()
    condition.is_enabled = False
    condition.condition_expression = None
    condition.condition_type = None
    return condition


class TestRequireApprovalNoConfig:
    """Test cases when no tool configuration exists."""

    async def test_no_tool_configuration_returns_true(self):
        """Test that missing tool configuration allows execution."""
        mock_db = create_mock_db_session()
        # Mock database query to return no config
        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "preloop.models.db.session.get_async_db_session",
            return_value=mock_db,
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={"arg": "value"},
                ctx=None,
            )

        assert approved is True
        assert error == ""

    async def test_tool_without_requires_approval_returns_true(self, tool_config):
        """Test that tool without approval_workflow_id allows execution."""
        mock_db = create_mock_db_session()
        # Set approval_workflow_id to None (no approval required)
        tool_config.approval_workflow_id = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=tool_config)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "preloop.models.db.session.get_async_db_session",
            return_value=mock_db,
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={"arg": "value"},
                ctx=None,
            )

        assert approved is True
        assert error == ""

    async def test_tool_without_approval_workflow_id_returns_true(self, tool_config):
        """Test that tool without approval_workflow_id allows execution."""
        mock_db = create_mock_db_session()
        # Explicitly set approval_workflow_id to None (no approval required)
        tool_config.approval_workflow_id = None

        mock_result = MagicMock()
        mock_result.scalar_one_or_none = MagicMock(return_value=tool_config)
        mock_db.execute = AsyncMock(return_value=mock_result)

        with patch(
            "preloop.models.db.session.get_async_db_session",
            return_value=mock_db,
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={"arg": "value"},
                ctx=None,
            )

        assert approved is True
        assert error == ""


class TestRequireApprovalWorkflowErrors:
    """Test cases for approval workflow errors."""

    async def test_approval_workflow_not_found_returns_false(
        self, tool_config, approval_condition
    ):
        """Test that missing approval workflow returns error."""
        mock_db = create_mock_db_session()
        # First query returns tool config, second returns condition, third returns no policy
        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(return_value=None)

        mock_db.execute = AsyncMock(
            side_effect=[mock_config_result, mock_rules_result, mock_workflow_result]
        )

        with patch(
            "preloop.models.db.session.get_async_db_session",
            return_value=mock_db,
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={"arg": "value"},
                ctx=None,
            )

        assert approved is False
        assert "Approval workflow not found" in error


class TestRequireApprovalSuccess:
    """Test cases for successful approval flow."""

    async def test_approval_granted_returns_true(
        self,
        tool_config,
        approval_workflow,
        approval_request,
        approval_condition,
        mock_context,
    ):
        """Test that granted approval allows execution."""
        mock_db = create_mock_db_session()
        # Set up mock responses
        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        # Approval request is immediately approved
        approval_request.status = "approved"
        approval_request.approver_comment = "Looks good"

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        # Set up execute to return different results
        execute_calls = [
            mock_config_result,  # First: get tool config
            mock_rules_result,  # Second: get approval condition
            mock_workflow_result,  # Third: get approval workflow
            mock_approval_result,  # Fourth: check approval status
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        # Mock approval service
        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={"arg": "value"},
                ctx=mock_context,
            )

        assert approved is True
        assert error == ""
        # Verify progress was reported
        assert mock_context.report_progress.called

    async def test_approval_with_webhook_policy(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test approval with webhook policy (no channel or user)."""
        mock_db = create_mock_db_session()
        approval_workflow.channel = None
        approval_workflow.user = None
        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is True
        assert error == ""

    async def test_approval_with_user_policy(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test approval with user policy (no channel)."""
        mock_db = create_mock_db_session()
        approval_workflow.channel = None
        approval_workflow.user = "john.doe"
        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is True
        assert error == ""


class TestRequireApprovalDeclined:
    """Test cases for declined approvals."""

    async def test_approval_declined_returns_false(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test that declined approval denies execution."""
        mock_db = create_mock_db_session()
        approval_request.status = "declined"
        approval_request.approver_comment = "Not safe"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "declined" in error.lower()
        assert "Not safe" in error

    async def test_approval_declined_without_comment(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test declined approval without comment."""
        mock_db = create_mock_db_session()
        approval_request.status = "declined"
        approval_request.approver_comment = None

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "declined" in error.lower()


class TestRequireApprovalCancelled:
    """Test cases for cancelled approvals."""

    async def test_approval_cancelled_returns_false(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test that cancelled approval denies execution."""
        mock_db = create_mock_db_session()
        approval_request.status = "cancelled"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "cancelled" in error.lower()


class TestRequireApprovalTimeout:
    """Test cases for approval timeout."""

    async def test_approval_timeout_returns_false(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test that approval timeout denies execution."""
        mock_db = create_mock_db_session()
        # Set short timeout
        approval_workflow.timeout_seconds = 1

        # Approval stays pending
        approval_request.status = "pending"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        # Each poll returns pending status
        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        # Need multiple approval results for polling loop
        execute_calls = [
            mock_config_result,  # First: get tool config
            mock_rules_result,  # Second: get approval condition
            mock_workflow_result,  # Third: get approval workflow
            mock_approval_result,  # Fourth: first poll
            mock_approval_result,  # Fifth: second poll
            mock_approval_result,  # Sixth: third poll
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )
        mock_approval_service.update_approval_request = AsyncMock()

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
            patch(
                "preloop.services.approval_helper.asyncio.sleep",
                new_callable=AsyncMock,
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "timeout" in error.lower() or "expired" in error.lower()
        # Verify approval was marked as expired
        assert mock_approval_service.update_approval_request.called


class TestRequireApprovalEdgeCases:
    """Test edge cases and error handling."""

    async def test_approval_request_not_found_during_poll(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test when approval request disappears during polling."""
        mock_db = create_mock_db_session()
        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        # First poll returns None (request not found)
        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(return_value=None)

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "not found" in error.lower()

    async def test_unexpected_approval_status(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test handling unexpected approval status."""
        mock_db = create_mock_db_session()
        # Change status to "approved" on second poll to exit the loop
        # This simulates what happens when status eventually resolves
        approval_request_pending = MagicMock()
        approval_request_pending.id = approval_request.id
        approval_request_pending.status = "unknown_status"

        approval_request_resolved = MagicMock()
        approval_request_resolved.id = approval_request.id
        approval_request_resolved.status = "weird_status"  # Still not a valid status
        approval_request_resolved.approver_comment = None

        mock_config_result = MagicMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = MagicMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        # First poll returns unknown_status (loop continues)
        mock_approval_result1 = MagicMock()
        mock_approval_result1.scalar_one_or_none = MagicMock(
            return_value=approval_request_pending
        )

        # Second poll returns weird_status (still not approved/declined/cancelled)
        # To make the loop exit, let's have it return "approved" but then check fails
        mock_approval_result2 = MagicMock()
        mock_approval_result2.scalar_one_or_none = MagicMock(
            return_value=approval_request_resolved
        )

        # Provide enough execute calls for config, condition, policy, and multiple polls
        execute_calls = [
            mock_config_result,  # get tool config
            mock_rules_result,  # get approval condition
            mock_workflow_result,  # get approval workflow
            mock_approval_result1,  # first poll - unknown_status
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request_pending
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            # The mock will run out of side effects, causing an exception
            # This triggers the fail-open error handler
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        # When polling encounters an error (mock runs out), it returns error
        assert approved is False
        assert "error" in error.lower() or "stopasynciteration" in error.lower()

    async def test_approval_service_creation_error(
        self, tool_config, approval_workflow, approval_condition
    ):
        """Test error during approval request creation."""
        mock_db = create_mock_db_session()
        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_db.execute = AsyncMock(
            side_effect=[mock_config_result, mock_rules_result, mock_workflow_result]
        )

        # Approval service raises error
        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            side_effect=Exception("Service error")
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is False
        assert "error" in error.lower()

    async def test_database_error_fails_closed(self):
        """Database errors must fail CLOSED (block execution).

        A tool gated behind require_approval must never run un-approved just
        because the approval check hit an infrastructure error.
        """
        mock_db = create_mock_db_session()
        # Simulate database error
        mock_db.execute = AsyncMock(side_effect=Exception("Database error"))

        with patch(
            "preloop.models.db.session.get_async_db_session",
            return_value=mock_db,
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="builtin",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        # Should fail closed
        assert approved is False
        assert error != ""


class TestRequireApprovalProgressReporting:
    """Test progress reporting via Context."""

    async def test_progress_report_on_initial_notification(
        self,
        tool_config,
        approval_workflow,
        approval_request,
        approval_condition,
        mock_context,
    ):
        """Test that initial progress is reported via Context."""
        mock_db = create_mock_db_session()
        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={},
                ctx=mock_context,
            )

        assert approved is True
        # Verify progress was reported at least twice (initial + completion)
        assert mock_context.report_progress.call_count >= 2

    async def test_progress_report_error_handling(
        self,
        tool_config,
        approval_workflow,
        approval_request,
        approval_condition,
        mock_context,
    ):
        """Test that progress reporting errors don't break approval flow."""
        mock_db = create_mock_db_session()
        # Make progress reporting fail
        mock_context.report_progress = AsyncMock(
            side_effect=Exception("Progress error")
        )

        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            # Should not raise exception despite progress errors
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={},
                ctx=mock_context,
            )

        # Approval should still succeed
        assert approved is True
        assert error == ""

    async def test_progress_report_without_context(
        self, tool_config, approval_workflow, approval_request, approval_condition
    ):
        """Test approval flow works without Context (no progress reporting)."""
        mock_db = create_mock_db_session()
        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            # Call without context
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={},
                ctx=None,
            )

        assert approved is True
        assert error == ""

    async def test_progress_token_error_handling(
        self,
        tool_config,
        approval_workflow,
        approval_request,
        approval_condition,
        mock_context,
    ):
        """Test handling error when getting progressToken."""
        mock_db = create_mock_db_session()
        # Make progressToken access raise error
        mock_context.request_context.meta = MagicMock()
        type(mock_context.request_context.meta).progressToken = property(
            lambda self: (_ for _ in ()).throw(Exception("Token error"))
        )

        approval_request.status = "approved"

        mock_config_result = AsyncMock()
        mock_config_result.scalar_one_or_none = MagicMock(return_value=tool_config)

        mock_rules_result = MagicMock()
        mock_rules_result.scalars = MagicMock(return_value=iter([]))

        mock_workflow_result = AsyncMock()
        mock_workflow_result.scalar_one_or_none = MagicMock(
            return_value=approval_workflow
        )

        mock_approval_result = AsyncMock()
        mock_approval_result.scalar_one_or_none = MagicMock(
            return_value=approval_request
        )

        execute_calls = [
            mock_config_result,
            mock_rules_result,
            mock_workflow_result,
            mock_approval_result,
        ]
        mock_db.execute = AsyncMock(side_effect=execute_calls)

        mock_approval_service = AsyncMock()
        mock_approval_service.create_and_notify = AsyncMock(
            return_value=approval_request
        )

        with (
            patch(
                "preloop.models.db.session.get_async_db_session",
                return_value=mock_db,
            ),
            patch(
                "preloop.services.approval_service.ApprovalService",
                return_value=mock_approval_service,
            ),
            patch(
                "preloop.services.approval_helper.os.getenv",
                return_value="http://test",
            ),
        ):
            # Should not raise exception despite token error
            approved, error = await require_approval(
                tool_name="test_tool",
                tool_source="mcp",
                account_id="account_123",
                arguments={},
                ctx=mock_context,
            )

        # Approval should still succeed
        assert approved is True
        assert error == ""


async def test_in_band_context_lookup_failure_logs_traceback(caplog):
    """Context lookup is best-effort; keep the exception traceback."""
    from preloop.services.approval_helper import _session_context_for_in_band

    with patch(
        "preloop.services.dynamic_fastmcp_http.get_current_user_context",
        side_effect=RuntimeError("no request context"),
    ):
        ctx = _session_context_for_in_band()
    assert ctx.runtime_session_id is None
    assert ctx.managed_agent_id is None
    assert ctx.user_id is None
    assert "Error getting current user context" in caplog.text
    assert "no request context" in caplog.text


async def test_resolve_responder_display_name_prefers_email() -> None:
    from preloop.services.approval_helper import _resolve_responder_display_name

    user_id = uuid4()
    user = MagicMock()
    user.email = "approver@example.com"
    user.username = "approver"
    db = AsyncMock()
    db.get = AsyncMock(return_value=user)
    assert await _resolve_responder_display_name(db, user_id) == "approver@example.com"


async def test_resolve_responder_display_name_falls_back_on_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from preloop.services.approval_helper import _resolve_responder_display_name

    user_id = uuid4()
    db = AsyncMock()
    db.get = AsyncMock(side_effect=RuntimeError("session closed"))
    with caplog.at_level("DEBUG", logger="preloop.services.approval_helper"):
        assert await _resolve_responder_display_name(db, user_id) == str(user_id)
    assert "Could not resolve approver" in caplog.text
    assert "session closed" in caplog.text
