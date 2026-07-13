"""Tests for policy schema validation."""

import pytest
from pydantic import ValidationError

from preloop.services.policy.schema import (
    ApprovalWorkflowDefinition,
    ConditionAction,
    DefaultsDefinition,
    MCPServerAuthType,
    MCPServerDefinition,
    MCPServerTransport,
    PolicyDocument,
    PolicyMetadata,
    PolicyVersion,
    ToolCondition,
    ToolDefinition,
    UnknownToolsPolicy,
)


class TestPolicyMetadata:
    """Test PolicyMetadata schema."""

    def test_valid_metadata(self):
        """Test valid metadata creation."""
        metadata = PolicyMetadata(
            name="Test Policy",
            description="A test policy",
            author="test@example.com",
            tags=["production", "security"],
        )
        assert metadata.name == "Test Policy"
        assert metadata.description == "A test policy"
        assert metadata.author == "test@example.com"
        assert metadata.tags == ["production", "security"]

    def test_minimal_metadata(self):
        """Test metadata with only required fields."""
        metadata = PolicyMetadata(name="Minimal Policy")
        assert metadata.name == "Minimal Policy"
        assert metadata.description is None
        assert metadata.author is None
        assert metadata.tags is None

    def test_missing_name_fails(self):
        """Test that missing name raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PolicyMetadata()
        assert "name" in str(exc_info.value)


class TestMCPServerDefinition:
    """Test MCPServerDefinition schema."""

    def test_valid_server(self):
        """Test valid MCP server definition."""
        server = MCPServerDefinition(
            name="github-mcp",
            url="https://mcp.github.com",
            transport=MCPServerTransport.STREAMABLE_HTTP,
            auth_type=MCPServerAuthType.BEARER,
            auth_config={"token": "${GITHUB_TOKEN}"},
        )
        assert server.name == "github-mcp"
        assert server.url == "https://mcp.github.com"
        assert server.transport == "streamable-http"
        assert server.auth_type == "bearer"
        assert server.auth_config == {"token": "${GITHUB_TOKEN}"}

    def test_server_defaults(self):
        """Test MCP server with defaults."""
        server = MCPServerDefinition(name="test", url="http://localhost:8080")
        assert server.transport == "streamable-http"
        assert server.auth_type == "none"
        assert server.auth_config is None

    def test_missing_required_fields(self):
        """Test validation errors for missing fields."""
        with pytest.raises(ValidationError) as exc_info:
            MCPServerDefinition()
        errors = str(exc_info.value)
        assert "name" in errors
        assert "url" in errors


class TestApprovalWorkflowDefinition:
    """Test ApprovalWorkflowDefinition schema."""

    def test_valid_policy(self):
        """Test valid approval workflow definition."""
        policy = ApprovalWorkflowDefinition(
            name="high-risk",
            description="Policy for high-risk operations",
            timeout_seconds=600,
            require_reason=True,
            approvals_required=2,
        )
        assert policy.name == "high-risk"
        assert policy.timeout_seconds == 600
        assert policy.require_reason is True
        assert policy.approvals_required == 2

    def test_policy_defaults(self):
        """Test approval workflow with defaults."""
        policy = ApprovalWorkflowDefinition(name="default")
        assert policy.timeout_seconds == 300
        assert policy.require_reason is False
        assert policy.is_default is False
        assert policy.approvals_required == 1

    def test_timeout_bounds(self):
        """Test timeout_seconds validation bounds."""
        # Too short
        with pytest.raises(ValidationError) as exc_info:
            ApprovalWorkflowDefinition(name="test", timeout_seconds=10)
        assert "timeout_seconds" in str(exc_info.value)

        # Too long
        with pytest.raises(ValidationError) as exc_info:
            ApprovalWorkflowDefinition(name="test", timeout_seconds=100000)
        assert "timeout_seconds" in str(exc_info.value)

        # Just right
        policy = ApprovalWorkflowDefinition(name="test", timeout_seconds=30)
        assert policy.timeout_seconds == 30


class TestToolCondition:
    """Test ToolCondition schema."""

    def test_valid_condition(self):
        """Test valid tool condition."""
        condition = ToolCondition(
            expression="args.amount > 1000",
            action=ConditionAction.REQUIRE_APPROVAL,
            description="Require approval for large transactions",
        )
        assert condition.expression == "args.amount > 1000"
        assert condition.action == "require_approval"
        assert condition.description == "Require approval for large transactions"

    def test_condition_defaults(self):
        """Test condition with defaults."""
        condition = ToolCondition(expression="args.env == 'prod'")
        assert condition.action == "require_approval"
        assert condition.description is None

    def test_empty_expression_fails(self):
        """Test that empty expression raises validation error."""
        with pytest.raises(ValidationError) as exc_info:
            ToolCondition(expression="   ")
        assert "expression" in str(exc_info.value).lower()

    def test_condition_actions(self):
        """Test all condition actions."""
        for action in tuple(ConditionAction):
            condition = ToolCondition(expression="true", action=action)
            assert condition.action == action.value


class TestToolDefinition:
    """Test ToolDefinition schema."""

    def test_valid_tool(self):
        """Test valid tool definition."""
        tool = ToolDefinition(
            name="execute_command",
            source="builtin",
            enabled=True,
            approval_workflow="high-risk",
            conditions=[
                ToolCondition(expression="args.command.contains('rm')"),
            ],
        )
        assert tool.name == "execute_command"
        assert tool.source == "builtin"
        assert tool.enabled is True
        assert tool.approval_workflow == "high-risk"
        assert len(tool.conditions) == 1

    def test_tool_defaults(self):
        """Test tool with defaults."""
        tool = ToolDefinition(name="test_tool")
        assert tool.source == "builtin"
        assert tool.enabled is True
        assert tool.approval_workflow is None
        assert tool.conditions is None

    def test_tool_with_mcp_server_source(self):
        """Test tool with MCP server as source."""
        tool = ToolDefinition(name="github_tool", source="github-mcp")
        assert tool.source == "github-mcp"


class TestDefaultsDefinition:
    """Test DefaultsDefinition schema."""

    def test_valid_defaults(self):
        """Test valid defaults definition."""
        defaults = DefaultsDefinition(
            unknown_tools=UnknownToolsPolicy.DENY,
            require_approval_for_new_tools=True,
            default_approval_workflow="default-policy",
        )
        assert defaults.unknown_tools == "deny"
        assert defaults.require_approval_for_new_tools is True
        assert defaults.default_approval_workflow == "default-policy"

    def test_defaults_with_defaults(self):
        """Test defaults with default values."""
        defaults = DefaultsDefinition()
        assert defaults.unknown_tools == "allow"
        assert defaults.require_approval_for_new_tools is False
        assert defaults.default_approval_workflow is None
        assert defaults.inherit_from_parent is True


class TestPolicyDocument:
    """Test PolicyDocument schema."""

    def test_minimal_policy(self):
        """Test minimal valid policy document."""
        policy = PolicyDocument(
            metadata=PolicyMetadata(name="Minimal Policy"),
        )
        assert policy.version == PolicyVersion.V1_0
        assert policy.metadata.name == "Minimal Policy"
        assert policy.mcp_servers is None
        assert policy.approval_workflows is None
        assert policy.tools is None
        assert policy.defaults is None

    def test_full_policy(self):
        """Test full policy document with all sections."""
        policy = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(
                name="Production Security Policy",
                description="Comprehensive security policy for production",
            ),
            mcp_servers=[
                MCPServerDefinition(
                    name="github-mcp",
                    url="https://mcp.github.com",
                ),
            ],
            approval_workflows=[
                ApprovalWorkflowDefinition(
                    name="high-risk",
                    timeout_seconds=300,
                ),
            ],
            tools=[
                ToolDefinition(
                    name="execute_command",
                    source="builtin",
                    approval_workflow="high-risk",
                ),
            ],
            defaults=DefaultsDefinition(
                unknown_tools=UnknownToolsPolicy.DENY,
            ),
        )
        assert len(policy.mcp_servers) == 1
        assert len(policy.approval_workflows) == 1
        assert len(policy.tools) == 1
        assert policy.defaults.unknown_tools == "deny"

    def test_duplicate_server_names_fail(self):
        """Test that duplicate server names raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PolicyDocument(
                metadata=PolicyMetadata(name="Test"),
                mcp_servers=[
                    MCPServerDefinition(name="server1", url="http://a.com"),
                    MCPServerDefinition(name="server1", url="http://b.com"),
                ],
            )
        assert "Duplicate MCP server name" in str(exc_info.value)

    def test_duplicate_policy_names_fail(self):
        """Test that duplicate policy names raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PolicyDocument(
                metadata=PolicyMetadata(name="Test"),
                approval_workflows=[
                    ApprovalWorkflowDefinition(name="policy1"),
                    ApprovalWorkflowDefinition(name="policy1"),
                ],
            )
        assert "Duplicate approval workflow name" in str(exc_info.value)

    def test_invalid_policy_reference_fails(self):
        """Test that invalid policy references raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PolicyDocument(
                metadata=PolicyMetadata(name="Test"),
                tools=[
                    ToolDefinition(
                        name="test_tool",
                        approval_workflow="nonexistent-policy",
                    ),
                ],
            )
        assert "unknown approval workflow" in str(exc_info.value).lower()

    def test_invalid_server_reference_fails(self):
        """Test that invalid server references raise validation error."""
        with pytest.raises(ValidationError) as exc_info:
            PolicyDocument(
                metadata=PolicyMetadata(name="Test"),
                tools=[
                    ToolDefinition(
                        name="test_tool",
                        source="nonexistent-server",
                    ),
                ],
            )
        error_msg = str(exc_info.value).lower()
        assert "unknown" in error_msg and "server" in error_msg

    def test_valid_references(self):
        """Test that valid references pass validation."""
        policy = PolicyDocument(
            metadata=PolicyMetadata(name="Test"),
            mcp_servers=[
                MCPServerDefinition(name="my-server", url="http://localhost"),
            ],
            approval_workflows=[
                ApprovalWorkflowDefinition(name="my-policy"),
            ],
            tools=[
                ToolDefinition(
                    name="mcp_tool",
                    source="my-server",
                    approval_workflow="my-policy",
                ),
            ],
            defaults=DefaultsDefinition(
                default_approval_workflow="my-policy",
            ),
        )
        assert policy.tools[0].source == "my-server"
        assert policy.tools[0].approval_workflow == "my-policy"

    def test_builtin_source_doesnt_require_server(self):
        """Test that builtin source doesn't require a server definition."""
        policy = PolicyDocument(
            metadata=PolicyMetadata(name="Test"),
            tools=[
                ToolDefinition(name="test_tool", source="builtin"),
            ],
        )
        assert policy.tools[0].source == "builtin"
