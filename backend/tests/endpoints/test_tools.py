"""Tests for tools API endpoints."""

import uuid
from unittest.mock import MagicMock, create_autospec

import pytest
from fastapi import HTTPException, status

from preloop.api.endpoints import tools
from preloop.models.models.account import Account
from preloop.models.models.mcp_server import MCPServer
from preloop.models.models.mcp_tool import MCPTool
from preloop.models.models.tool_configuration import ApprovalWorkflow, ToolConfiguration
from preloop.models.models.user import User
from preloop.models.schemas.tool_configuration import (
    ApprovalWorkflowCreate,
    ApprovalWorkflowResponse,
    ApprovalWorkflowUpdate,
    ToolConfigurationCreate,
    ToolConfigurationResponse,
    ToolConfigurationUpdate,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def mock_user():
    """Autospec ``User`` — the type tools endpoints inject via
    ``get_current_active_user`` (not ``AuthUserResponse``, which lacks
    ``id`` / ``account_id``). Unknown attribute access raises AttributeError.
    """
    user = create_autospec(User, instance=True)
    user.id = uuid.uuid4()
    user.username = "testuser"
    user.email = "test@example.com"
    user.account_id = uuid.uuid4()
    user.is_active = True
    return user


@pytest.fixture
def mock_account():
    """Create mock account for testing."""
    account = MagicMock(spec=Account)
    account.id = uuid.uuid4()
    account.username = "testuser"
    return account


@pytest.fixture
def mock_db():
    """Create mock database session."""
    return MagicMock()


class TestListAllTools:
    """Test list_all_tools endpoint."""

    async def test_list_tools_with_no_configs(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test listing tools when no configurations exist."""
        # Mock CRUD operations
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[],
        )

        result = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )

        # Should return all builtin tools with defaults
        assert len(result) == len(tools.BUILTIN_TOOLS)
        assert all(tool["source"] == "builtin" for tool in result)
        # Default-disabled tools report is_enabled=False without a config row
        default_disabled = {
            t["name"] for t in tools.BUILTIN_TOOLS if not t.get("default_enabled", True)
        }
        assert default_disabled == {
            "estimate_compliance",
            "improve_compliance",
            "permission_prompt",
        }
        for tool in result:
            expected = tool["name"] not in default_disabled
            assert tool["is_enabled"] is expected
            assert isinstance(tool["schema_tokens_estimate"], int)
            assert tool["schema_tokens_estimate"] > 0

    async def test_default_disabled_tool_enabled_by_config(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """An explicit enable config row overrides default_enabled=False."""
        config = MagicMock(spec=ToolConfiguration)
        config.id = uuid.uuid4()
        config.tool_name = "estimate_compliance"
        config.tool_source = "builtin"
        config.mcp_server_id = None
        config.managed_agent_id = None
        config.is_enabled = True
        config.approval_workflow_id = None
        config.justification_mode = None

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[config],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[],
        )

        result = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )

        estimate = next(t for t in result if t["name"] == "estimate_compliance")
        improve = next(t for t in result if t["name"] == "improve_compliance")
        assert estimate["is_enabled"] is True
        assert improve["is_enabled"] is False

    async def test_agent_scoped_enable_reported_without_flipping_account_state(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """An agent-scoped enable is surfaced via enabled_for_agents while the
        account-wide is_enabled keeps reporting the default-disabled state."""
        agent_id = uuid.uuid4()
        agent_row = MagicMock(spec=ToolConfiguration)
        agent_row.id = uuid.uuid4()
        agent_row.tool_name = "permission_prompt"
        agent_row.tool_source = "builtin"
        agent_row.mcp_server_id = None
        agent_row.managed_agent_id = agent_id
        agent_row.is_enabled = True
        agent_row.approval_workflow_id = None
        agent_row.justification_mode = None

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[agent_row],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[],
        )

        result = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )

        permission_prompt = next(t for t in result if t["name"] == "permission_prompt")
        # The agent-scoped row must not read as an account-wide enable...
        assert permission_prompt["is_enabled"] is False
        assert permission_prompt["config_id"] is None
        # ...but the UI can see exactly which agents opted in.
        assert permission_prompt["enabled_for_agents"] == [str(agent_id)]

    async def test_list_tools_with_configs(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test listing tools with existing configurations."""
        # Create mock tool configuration
        workflow_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = uuid.uuid4()
        config.tool_name = "get_issue"
        config.tool_source = "builtin"
        config.mcp_server_id = None
        config.managed_agent_id = None
        config.is_enabled = False
        config.approval_workflow_id = workflow_id
        config.justification_mode = None

        # Mock CRUD operations
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[config],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[],
        )

        result = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )

        # Find the configured tool
        get_issue_tool = next(t for t in result if t["name"] == "get_issue")
        assert get_issue_tool["is_enabled"] is False
        assert get_issue_tool["approval_workflow_id"] == str(workflow_id)

    async def test_list_tools_schema_tokens_include_justification(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Required justification increases the served schema token estimate."""
        config = MagicMock(spec=ToolConfiguration)
        config.id = uuid.uuid4()
        config.tool_name = "get_issue"
        config.tool_source = "builtin"
        config.mcp_server_id = None
        config.is_enabled = True
        config.approval_workflow_id = None
        config.justification_mode = "required"

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[config],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_access_rule.get_multi_by_account",
            return_value=[],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tracker.get_for_account",
            return_value=[],
        )

        with_justification = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )
        justified = next(t for t in with_justification if t["name"] == "get_issue")

        config.justification_mode = None
        without = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )
        plain = next(t for t in without if t["name"] == "get_issue")

        assert justified["schema_tokens_estimate"] > plain["schema_tokens_estimate"]
        assert justified["justification_mode"] == "required"

    async def test_list_tools_with_mcp_servers(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test listing tools including MCP server tools."""
        # Create mock MCP server and tools
        server_id = uuid.uuid4()
        mcp_server = MagicMock(spec=MCPServer)
        mcp_server.id = server_id
        mcp_server.name = "Test MCP Server"
        mcp_server.status = "active"

        mcp_tool = MagicMock(spec=MCPTool)
        mcp_tool.name = "custom_tool"
        mcp_tool.description = "A custom MCP tool"
        mcp_tool.input_schema = {"type": "object", "properties": {}}

        # Mock CRUD operations
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_server.get_active_by_account",
            return_value=[mcp_server],
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_mcp_tool.get_by_server",
            return_value=[mcp_tool],
        )

        result = tools.list_all_tools(
            account=mock_account, current_user=mock_user, db=mock_db
        )

        # Should have builtin tools + MCP tool
        assert len(result) == len(tools.BUILTIN_TOOLS) + 1

        # Check MCP tool is included
        custom_tool = next(t for t in result if t["name"] == "custom_tool")
        assert custom_tool["source"] == "mcp"
        assert custom_tool["source_id"] == str(server_id)
        assert custom_tool["source_name"] == "Test MCP Server"
        assert custom_tool["description"] == "A custom MCP tool"


class TestToolConfigurationEndpoints:
    """Test tool configuration CRUD endpoints."""

    async def test_create_tool_configuration_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating a new tool configuration."""
        config_data = ToolConfigurationCreate(
            tool_name="get_issue",
            tool_source="builtin",
            account_id=str(mock_account.id),
            is_enabled=False,
        )

        # Mock no existing config
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[],
        )

        # Mock db.refresh to set database-generated fields
        def mock_refresh(obj):
            obj.id = uuid.uuid4()
            from datetime import datetime, UTC

            obj.created_at = datetime.now(UTC)
            obj.updated_at = datetime.now(UTC)

        mock_db.refresh.side_effect = mock_refresh

        result = await tools.create_tool_configuration(
            config_data=config_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ToolConfigurationResponse)
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()

    async def test_create_agent_scoped_tool_configuration(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """An agent-scoped create validates the agent and persists the scope."""
        agent_id = uuid.uuid4()
        config_data = ToolConfigurationCreate(
            tool_name="permission_prompt",
            tool_source="builtin",
            account_id=str(mock_account.id),
            is_enabled=True,
            managed_agent_id=str(agent_id),
        )

        mock_get_agent = mocker.patch(
            "preloop.api.endpoints.tools.crud_managed_agent.get_for_account",
            return_value=MagicMock(id=agent_id),
        )
        # An account-wide row for the same tool must not block the
        # agent-scoped create: the scopes are distinct configurations.
        account_row = MagicMock(spec=ToolConfiguration)
        account_row.tool_name = "permission_prompt"
        account_row.tool_source = "builtin"
        account_row.mcp_server_id = None
        account_row.managed_agent_id = None
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[account_row],
        )

        def mock_refresh(obj):
            obj.id = uuid.uuid4()
            from datetime import datetime, UTC

            obj.created_at = datetime.now(UTC)
            obj.updated_at = datetime.now(UTC)

        mock_db.refresh.side_effect = mock_refresh

        result = await tools.create_tool_configuration(
            config_data=config_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ToolConfigurationResponse)
        assert str(result.managed_agent_id) == str(agent_id)
        mock_get_agent.assert_called_once_with(
            mock_db, account_id=str(mock_account.id), agent_id=str(agent_id)
        )
        mock_db.add.assert_called_once()

    async def test_create_agent_scoped_configuration_unknown_agent_rejected(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """An agent id outside the account is rejected with 404."""
        config_data = ToolConfigurationCreate(
            tool_name="permission_prompt",
            tool_source="builtin",
            account_id=str(mock_account.id),
            is_enabled=True,
            managed_agent_id=str(uuid.uuid4()),
        )

        mocker.patch(
            "preloop.api.endpoints.tools.crud_managed_agent.get_for_account",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.create_tool_configuration(
                config_data=config_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        mock_db.add.assert_not_called()

    async def test_create_agent_scoped_duplicate_rejected(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """A second row for the same tool and agent scope is rejected."""
        agent_id = uuid.uuid4()
        config_data = ToolConfigurationCreate(
            tool_name="permission_prompt",
            tool_source="builtin",
            account_id=str(mock_account.id),
            is_enabled=True,
            managed_agent_id=str(agent_id),
        )

        mocker.patch(
            "preloop.api.endpoints.tools.crud_managed_agent.get_for_account",
            return_value=MagicMock(id=agent_id),
        )
        existing_row = MagicMock(spec=ToolConfiguration)
        existing_row.tool_name = "permission_prompt"
        existing_row.tool_source = "builtin"
        existing_row.mcp_server_id = None
        existing_row.managed_agent_id = agent_id
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[existing_row],
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.create_tool_configuration(
                config_data=config_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    async def test_create_tool_configuration_already_exists(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating tool configuration that already exists."""
        config_data = ToolConfigurationCreate(
            tool_name="get_issue",
            tool_source="builtin",
            account_id=str(mock_account.id),
        )

        # Mock existing config
        existing_config = MagicMock(spec=ToolConfiguration)
        existing_config.tool_name = "get_issue"
        existing_config.tool_source = "builtin"
        existing_config.mcp_server_id = None
        existing_config.managed_agent_id = None

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            return_value=[existing_config],
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.create_tool_configuration(
                config_data=config_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    async def test_create_tool_configuration_race_condition(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating tool configuration with race condition (IntegrityError).

        The endpoint should be idempotent - if a race condition causes IntegrityError,
        it should fetch and return the existing config instead of failing.
        """
        from sqlalchemy.exc import IntegrityError

        config_data = ToolConfigurationCreate(
            tool_name="get_issue",
            tool_source="builtin",
            account_id=str(mock_account.id),
        )

        # Create mock existing config that will be returned after IntegrityError
        mock_existing_config = MagicMock()
        mock_existing_config.id = uuid.uuid4()
        mock_existing_config.account_id = mock_account.id
        mock_existing_config.tool_name = "get_issue"
        mock_existing_config.tool_source = "builtin"
        mock_existing_config.is_enabled = True
        mock_existing_config.mcp_server_id = None
        mock_existing_config.http_endpoint_id = None
        mock_existing_config.managed_agent_id = None
        mock_existing_config.approval_workflow_id = None
        mock_existing_config.tool_description = None
        mock_existing_config.tool_schema = None
        mock_existing_config.custom_config = None
        mock_existing_config.justification_mode = None

        # No existing config in the pre-check; the same lookup runs again
        # after the IntegrityError and must find the racing row.
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get_multi_by_account",
            side_effect=[[], [mock_existing_config]],
        )

        # Mock IntegrityError on commit (race condition)
        mock_db.commit.side_effect = IntegrityError(
            "statement", "params", "orig", connection_invalidated=False
        )

        # Should succeed and return existing config (idempotent)
        result = await tools.create_tool_configuration(
            config_data=config_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        # Verify idempotent behavior - should return existing config
        assert result.tool_name == "get_issue"
        mock_db.rollback.assert_called_once()

    async def test_get_tool_configuration_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting a tool configuration."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = config_id
        config.account_id = str(mock_account.id)
        config.tool_name = "get_issue"
        config.tool_source = "builtin"
        config.mcp_server_id = None
        config.http_endpoint_id = None
        config.managed_agent_id = None
        config.approval_workflow_id = None
        config.is_enabled = True
        config.tool_description = None
        config.tool_schema = None
        config.custom_config = None
        config.justification_mode = None
        from datetime import datetime, UTC

        config.created_at = datetime.now(UTC)
        config.updated_at = datetime.now(UTC)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )

        result = await tools.get_tool_configuration(
            config_id=config_id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ToolConfigurationResponse)

    async def test_get_tool_configuration_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting non-existent tool configuration."""
        config_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.get_tool_configuration(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_update_tool_configuration_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating a tool configuration."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = config_id
        config.account_id = str(mock_account.id)
        config.tool_name = "get_issue"
        config.tool_source = "builtin"
        config.mcp_server_id = None
        config.http_endpoint_id = None
        config.managed_agent_id = None
        config.approval_workflow_id = None
        config.is_enabled = True
        config.tool_description = None
        config.tool_schema = None
        config.custom_config = None
        config.justification_mode = None
        from datetime import datetime, UTC

        config.created_at = datetime.now(UTC)
        config.updated_at = datetime.now(UTC)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )
        mocker.patch("preloop.api.endpoints.tools.log_config_change")

        update_data = ToolConfigurationUpdate(is_enabled=False)

        result = await tools.update_tool_configuration(
            config_id=config_id,
            config_update=update_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ToolConfigurationResponse)
        mock_db.commit.assert_called_once()

    async def test_delete_tool_configuration_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting a tool configuration."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )
        mock_remove = mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.remove",
        )

        result = await tools.delete_tool_configuration(
            config_id=config_id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert "message" in result
        mock_remove.assert_called_once_with(
            mock_db, id=str(config_id), account_id=str(mock_account.id)
        )


class TestApprovalWorkflowEndpoints:
    """Test approval workflow CRUD endpoints."""

    async def test_list_approval_workflows(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test listing approval workflows."""
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = uuid.uuid4()
        policy.account_id = str(mock_account.id)
        policy.name = "Test Workflow"
        policy.description = "Test description"
        policy.approval_type = "slack"
        policy.channel = "#approvals"
        policy.user = None
        policy.approval_config = {}
        policy.timeout_seconds = 300
        policy.require_reason = False
        policy.is_default = False
        policy.workflow_type = "simple"
        policy.workflow_config = None
        policy.approver_user_ids = None
        policy.approver_team_ids = None
        policy.approvals_required = 1
        policy.escalation_user_ids = None
        policy.escalation_team_ids = None
        policy.notification_channels = ["email"]
        policy.channel_configs = None
        # AI approval fields
        policy.approval_mode = "standard"
        policy.ai_model = None
        policy.ai_guidelines = None
        policy.ai_context = None
        policy.ai_confidence_threshold = 0.8
        policy.ai_fallback_behavior = "escalate"
        policy.escalation_workflow_id = None
        from datetime import datetime, UTC

        policy.created_at = datetime.now(UTC)
        policy.updated_at = datetime.now(UTC)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_multi_by_account",
            return_value=[policy],
        )

        result = await tools.list_approval_workflows(
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, list)
        assert len(result) == 1

    async def test_create_approval_workflow_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating an approval workflow."""
        workflow_data = ApprovalWorkflowCreate(
            name="Test Workflow",
            approval_type="slack",
            channel="#approvals",
        )

        # Mock no existing policy with same name
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_by_name",
            return_value=None,
        )

        # Mock the created policy
        created_policy = MagicMock(spec=ApprovalWorkflow)
        created_policy.id = uuid.uuid4()
        created_policy.account_id = str(mock_account.id)
        created_policy.name = workflow_data.name
        created_policy.description = None
        created_policy.approval_type = workflow_data.approval_type
        created_policy.channel = workflow_data.channel
        created_policy.user = None
        created_policy.approval_config = None
        created_policy.timeout_seconds = 300
        created_policy.require_reason = False
        created_policy.is_default = True  # First policy becomes default
        created_policy.workflow_type = "simple"
        created_policy.workflow_config = None
        created_policy.approver_user_ids = None
        created_policy.approver_team_ids = None
        created_policy.approvals_required = 1
        created_policy.escalation_user_ids = None
        created_policy.escalation_team_ids = None
        created_policy.notification_channels = ["email"]
        created_policy.channel_configs = None
        # AI approval fields
        created_policy.approval_mode = "standard"
        created_policy.ai_model = None
        created_policy.ai_guidelines = None
        created_policy.ai_context = None
        created_policy.ai_confidence_threshold = 0.8
        created_policy.ai_fallback_behavior = "escalate"
        created_policy.escalation_workflow_id = None
        from datetime import datetime, UTC

        created_policy.created_at = datetime.now(UTC)
        created_policy.updated_at = datetime.now(UTC)

        # Mock crud_approval_workflow.create
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.create",
            return_value=created_policy,
        )

        result = await tools.create_approval_workflow(
            workflow_data=workflow_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ApprovalWorkflowResponse)
        assert result.name == workflow_data.name
        assert result.is_default

    async def test_create_approval_workflow_duplicate_name(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating approval workflow with duplicate name."""
        workflow_data = ApprovalWorkflowCreate(
            name="Test Workflow",
            approval_type="slack",
            channel="#approvals",
        )

        # Mock existing policy
        existing_policy = MagicMock(spec=ApprovalWorkflow)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_by_name",
            return_value=existing_policy,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.create_approval_workflow(
                workflow_data=workflow_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    async def test_get_approval_workflow_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting an approval workflow."""
        workflow_id = uuid.uuid4()
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = workflow_id
        policy.account_id = str(mock_account.id)
        policy.name = "Test Workflow"
        policy.description = "Test description"
        policy.approval_type = "slack"
        policy.channel = "#approvals"
        policy.user = None
        policy.approval_config = {}
        policy.timeout_seconds = 300
        policy.require_reason = False
        policy.is_default = False
        policy.workflow_type = "simple"
        policy.workflow_config = None
        policy.approver_user_ids = None
        policy.approver_team_ids = None
        policy.approvals_required = 1
        policy.escalation_user_ids = None
        policy.escalation_team_ids = None
        policy.notification_channels = ["email"]
        policy.channel_configs = None
        # AI approval fields
        policy.approval_mode = "standard"
        policy.ai_model = None
        policy.ai_guidelines = None
        policy.ai_context = None
        policy.ai_confidence_threshold = 0.8
        policy.ai_fallback_behavior = "escalate"
        policy.escalation_workflow_id = None
        from datetime import datetime, UTC

        policy.created_at = datetime.now(UTC)
        policy.updated_at = datetime.now(UTC)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=policy,
        )

        result = await tools.get_approval_workflow(
            workflow_id=workflow_id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ApprovalWorkflowResponse)

    async def test_update_approval_workflow_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating an approval workflow."""
        workflow_id = uuid.uuid4()
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = workflow_id
        policy.account_id = str(mock_account.id)
        policy.name = "Old Name"
        policy.description = "Test description"
        policy.approval_type = "slack"
        policy.channel = "#approvals"
        policy.user = None
        policy.approval_config = {}
        policy.timeout_seconds = 300
        policy.require_reason = False
        policy.is_default = False
        policy.workflow_type = "simple"
        policy.workflow_config = None
        policy.approver_user_ids = None
        policy.approver_team_ids = None
        policy.approvals_required = 1
        policy.escalation_user_ids = None
        policy.escalation_team_ids = None
        policy.notification_channels = ["email"]
        policy.channel_configs = None
        # AI approval fields
        policy.approval_mode = "standard"
        policy.ai_model = None
        policy.ai_guidelines = None
        policy.ai_context = None
        policy.ai_confidence_threshold = 0.8
        policy.ai_fallback_behavior = "escalate"
        policy.escalation_workflow_id = None
        from datetime import datetime, UTC

        policy.created_at = datetime.now(UTC)
        policy.updated_at = datetime.now(UTC)

        # Mock the get method to return policy
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=policy,
        )

        # Mock the get_by_name method for duplicate check (no duplicate with new name)
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_by_name",
            return_value=None,
        )

        # Mock the update method to return the updated policy
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.update",
            return_value=policy,
        )

        update_data = ApprovalWorkflowUpdate(name="New Name")

        result = await tools.update_approval_workflow(
            workflow_id=workflow_id,
            workflow_update=update_data,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert isinstance(result, ApprovalWorkflowResponse)

    async def test_delete_approval_workflow_success(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting an approval workflow."""
        workflow_id = uuid.uuid4()
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = workflow_id

        # Mock policy lookup
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=policy,
        )

        # Mock tool count query
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.count_by_workflow",
            return_value=2,
        )

        # Mock crud_approval_workflow.remove (which handles the actual deletion)
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.remove",
            return_value=policy,
        )

        result = await tools.delete_approval_workflow(
            workflow_id=workflow_id,
            account=mock_account,
            current_user=mock_user,
            db=mock_db,
        )

        assert "message" in result
        assert "2 tool(s)" in result["message"]


class TestApprovalWorkflowEndpointsErrorHandling:
    """Test approval workflow endpoints error handling."""

    async def test_get_approval_workflow_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting non-existent approval workflow returns 404."""
        workflow_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.get_approval_workflow(
                workflow_id=workflow_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "not found" in exc_info.value.detail.lower()

    async def test_update_approval_workflow_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating non-existent approval workflow returns 404."""
        workflow_id = uuid.uuid4()
        update_data = ApprovalWorkflowUpdate(name="New Name")

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.update_approval_workflow(
                workflow_id=workflow_id,
                workflow_update=update_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_update_approval_workflow_duplicate_name(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating approval workflow with duplicate name returns 400."""
        workflow_id = uuid.uuid4()
        another_workflow_id = uuid.uuid4()

        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = workflow_id
        policy.name = "Original Name"
        policy.account_id = str(mock_account.id)
        policy.is_default = False

        existing_policy = MagicMock(spec=ApprovalWorkflow)
        existing_policy.id = another_workflow_id
        existing_policy.name = "Conflicting Name"

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=policy,
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_by_name",
            return_value=existing_policy,
        )

        update_data = ApprovalWorkflowUpdate(name="Conflicting Name")

        with pytest.raises(HTTPException) as exc_info:
            await tools.update_approval_workflow(
                workflow_id=workflow_id,
                workflow_update=update_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_400_BAD_REQUEST
        assert "already exists" in exc_info.value.detail

    async def test_delete_approval_workflow_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting non-existent approval workflow returns 404."""
        workflow_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_approval_workflow(
                workflow_id=workflow_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_approval_workflow_remove_fails(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting approval workflow when remove returns None."""
        workflow_id = uuid.uuid4()
        policy = MagicMock(spec=ApprovalWorkflow)
        policy.id = workflow_id

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get",
            return_value=policy,
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.count_by_workflow",
            return_value=0,
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.remove",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_approval_workflow(
                workflow_id=workflow_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_create_approval_workflow_error(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test creating approval workflow with database error."""
        workflow_data = ApprovalWorkflowCreate(
            name="Test Workflow",
            approval_type="slack",
            channel="#approvals",
        )

        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.get_by_name",
            return_value=None,
        )
        mocker.patch(
            "preloop.api.endpoints.tools.crud_approval_workflow.create",
            side_effect=Exception("Database error"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.create_approval_workflow(
                workflow_data=workflow_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        mock_db.rollback.assert_called()


class TestToolConfigurationEndpointsErrorHandling:
    """Test tool configuration endpoints error handling."""

    async def test_update_tool_configuration_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating non-existent tool configuration returns 404."""
        config_id = uuid.uuid4()
        update_data = ToolConfigurationUpdate(is_enabled=False)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.update_tool_configuration(
                config_id=config_id,
                config_update=update_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_tool_configuration_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting non-existent tool configuration returns 404."""
        config_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_tool_configuration(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_update_tool_configuration_error(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating tool configuration with database error."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = config_id

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )

        # Simulate error during commit
        mock_db.commit.side_effect = Exception("Database error")

        update_data = ToolConfigurationUpdate(is_enabled=False)

        with pytest.raises(HTTPException) as exc_info:
            await tools.update_tool_configuration(
                config_id=config_id,
                config_update=update_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        mock_db.rollback.assert_called()

    async def test_delete_tool_configuration_error(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting tool configuration with database error."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.remove",
            side_effect=Exception("Database error"),
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_tool_configuration(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_500_INTERNAL_SERVER_ERROR
        mock_db.rollback.assert_called()


class TestApprovalConditionEndpoints:
    """Test tool approval condition endpoints."""

    async def test_get_approval_condition_tool_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting approval condition when tool config not found."""
        config_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.get_tool_approval_condition(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "Tool configuration not found" in exc_info.value.detail

    async def test_get_approval_condition_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test getting approval condition when no access rules exist."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = config_id

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )

        # Mock crud_tool_access_rule.get_first_by_config to return None
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_access_rule.get_first_by_config",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.get_tool_approval_condition(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
        assert "No access rule found" in exc_info.value.detail

    async def test_delete_approval_condition_tool_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting approval condition when tool config not found."""
        config_id = uuid.uuid4()

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_tool_approval_condition(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND

    async def test_delete_approval_condition_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test deleting approval condition when no access rules exist."""
        config_id = uuid.uuid4()
        config = MagicMock(spec=ToolConfiguration)
        config.id = config_id

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=config,
        )

        # Mock crud_tool_access_rule.get_multi_by_config to return empty list
        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_access_rule.get_multi_by_config",
            return_value=[],
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.delete_tool_approval_condition(
                config_id=config_id,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


class TestUpdateToolApprovalCondition:
    """Test update_tool_approval_condition endpoint."""

    async def test_update_approval_condition_tool_not_found(
        self, mock_db, mock_user, mock_account, mocker
    ):
        """Test updating approval condition when tool config not found."""
        config_id = uuid.uuid4()
        condition_data = {"approval_condition": "args.severity == 'high'"}

        mocker.patch(
            "preloop.api.endpoints.tools.crud_tool_configuration.get",
            return_value=None,
        )

        with pytest.raises(HTTPException) as exc_info:
            await tools.update_tool_approval_condition(
                config_id=config_id,
                condition_data=condition_data,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND
