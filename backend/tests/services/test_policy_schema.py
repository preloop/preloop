"""Tests for policy schema validation (PolicyDocument and related models)."""

import pytest

from preloop.services.policy.schema import (
    ApprovalWorkflowDefinition,
    ConditionAction,
    ConditionType,
    DefaultsDefinition,
    MCPServerDefinition,
    PolicyDocument,
    PolicyVersion,
    ToolCondition,
    ToolDefinition,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_doc(**overrides) -> dict:
    """Return a minimal valid PolicyDocument dict."""
    base = {
        "version": "1.0",
        "metadata": {"name": "test-policy"},
    }
    base.update(overrides)
    return base


def _server(name: str = "my-server", **kw) -> dict:
    base = {"name": name, "url": "https://example.com/mcp"}
    base.update(kw)
    return base


def _approval_workflow(name: str = "default-policy", **kw) -> dict:
    base = {"name": name, "timeout_seconds": 300, "required_approvals": 1}
    base.update(kw)
    return base


def _tool(name: str = "bash", **kw) -> dict:
    base = {"name": name, "source": "builtin"}
    base.update(kw)
    return base


# ===========================================================================
# PolicyDocument — valid documents
# ===========================================================================


class TestPolicyDocumentValid:
    """Test valid PolicyDocument construction."""

    def test_minimal_document(self):
        doc = PolicyDocument(**_minimal_doc())
        assert doc.version == PolicyVersion.V1_0
        assert doc.metadata.name == "test-policy"
        assert doc.tools is None
        assert doc.defaults is None

    def test_full_document(self):
        data = _minimal_doc(
            mcp_servers=[_server("srv")],
            approval_workflows=[_approval_workflow("pol")],
            tools=[_tool("bash", approval_workflow="pol")],
            defaults={
                "unknown_tools": "deny",
                "default_approval_workflow": "pol",
            },
        )
        doc = PolicyDocument(**data)
        assert len(doc.mcp_servers) == 1
        assert len(doc.approval_workflows) == 1
        assert len(doc.tools) == 1
        assert doc.defaults.unknown_tools == "deny"

    def test_tool_with_mcp_server_source(self):
        data = _minimal_doc(
            mcp_servers=[_server("github")],
            tools=[_tool("create_issue", source="github")],
        )
        doc = PolicyDocument(**data)
        assert doc.tools[0].source == "github"

    def test_tool_with_agent_source(self):
        data = _minimal_doc(
            mcp_servers=[_server("Example MCP Server")],
            tools=[_tool("Bash", source="agent")],
        )
        doc = PolicyDocument(**data)
        assert doc.tools[0].source == "agent"

    def test_tool_with_agent_source_case_insensitive(self):
        data = _minimal_doc(
            tools=[_tool("Bash", source="AGENT")],
        )
        doc = PolicyDocument(**data)
        assert doc.tools[0].source == "agent"

    def test_tool_with_conditions(self):
        data = _minimal_doc(
            approval_workflows=[_approval_workflow("pol")],
            tools=[
                _tool(
                    "bash",
                    approval_workflow="pol",
                    conditions=[
                        {
                            "expression": "args.command.contains('rm')",
                            "action": "deny",
                            "description": "Block destructive commands",
                        },
                    ],
                )
            ],
        )
        doc = PolicyDocument(**data)
        assert len(doc.tools[0].conditions) == 1
        assert doc.tools[0].conditions[0].action == ConditionAction.DENY

    def test_multiple_tools_and_servers(self):
        data = _minimal_doc(
            mcp_servers=[_server("srv-a"), _server("srv-b")],
            approval_workflows=[
                _approval_workflow("pol-a"),
                _approval_workflow("pol-b"),
            ],
            tools=[
                _tool("tool-a", source="srv-a", approval_workflow="pol-a"),
                _tool("tool-b", source="srv-b", approval_workflow="pol-b"),
            ],
        )
        doc = PolicyDocument(**data)
        assert len(doc.tools) == 2


# ===========================================================================
# PolicyDocument — reference validation errors
# ===========================================================================


class TestPolicyDocumentReferenceErrors:
    """Test that invalid references raise ValueError."""

    def test_duplicate_mcp_server_name(self):
        data = _minimal_doc(
            mcp_servers=[_server("dup"), _server("dup")],
        )
        with pytest.raises(ValueError, match="Duplicate MCP server name"):
            PolicyDocument(**data)

    def test_duplicate_approval_workflow_name(self):
        data = _minimal_doc(
            approval_workflows=[_approval_workflow("dup"), _approval_workflow("dup")],
        )
        with pytest.raises(ValueError, match="Duplicate approval workflow name"):
            PolicyDocument(**data)

    def test_tool_references_unknown_approval_workflow(self):
        data = _minimal_doc(
            approval_workflows=[_approval_workflow("real-pol")],
            tools=[_tool("bash", approval_workflow="nonexistent")],
        )
        with pytest.raises(ValueError, match="unknown approval workflow"):
            PolicyDocument(**data)

    def test_tool_references_unknown_mcp_server(self):
        data = _minimal_doc(
            mcp_servers=[_server("real-srv")],
            tools=[_tool("bash", source="nonexistent-srv")],
        )
        with pytest.raises(ValueError, match="unknown MCP server"):
            PolicyDocument(**data)

    def test_default_approval_workflow_unknown(self):
        data = _minimal_doc(
            defaults={"default_approval_workflow": "missing"},
        )
        with pytest.raises(ValueError, match="Default approval workflow"):
            PolicyDocument(**data)

    def test_escalation_workflow_unknown(self):
        data = _minimal_doc(
            approval_workflows=[
                _approval_workflow(
                    "ai-pol",
                    approval_type="ai_driven",
                    ai_model="claude-sonnet-4-20250514",
                    ai_guidelines="Review for safety",
                    escalation_workflow="missing-policy",
                ),
            ],
        )
        with pytest.raises(ValueError, match="unknown.*escalation_workflow"):
            PolicyDocument(**data)


# ===========================================================================
# ApprovalWorkflowDefinition — AI-driven validation
# ===========================================================================


class TestApprovalWorkflowAIDriven:
    """Test AI-driven approval workflow validation."""

    def test_ai_driven_requires_model(self):
        with pytest.raises(ValueError, match="ai_model.*required"):
            ApprovalWorkflowDefinition(
                name="bad",
                approval_type="ai_driven",
                # ai_model missing
                ai_guidelines="some guidelines",
            )

    def test_ai_driven_without_guidelines_is_valid(self):
        # ai_guidelines is optional — only ai_model is required for ai_driven
        policy = ApprovalWorkflowDefinition(
            name="no-guidelines",
            approval_type="ai_driven",
            ai_model="claude-sonnet-4-20250514",
        )
        assert policy.ai_guidelines is None

    def test_ai_driven_valid(self):
        policy = ApprovalWorkflowDefinition(
            name="ai-review",
            approval_type="ai_driven",
            ai_model="claude-sonnet-4-20250514",
            ai_guidelines="Approve only if safe",
            ai_confidence_threshold=0.9,
            ai_fallback_behavior="deny",
        )
        assert policy.ai_confidence_threshold == 0.9
        assert policy.ai_fallback_behavior == "deny"

    def test_standard_policy_valid(self):
        policy = ApprovalWorkflowDefinition(
            name="standard",
            timeout_seconds=600,
            required_approvals=2,
        )
        assert policy.approval_type == "standard"

    def test_ai_driven_with_escalation(self):
        """AI policy with escalation works when referenced in full doc."""
        data = _minimal_doc(
            approval_workflows=[
                _approval_workflow("human-fallback"),
                _approval_workflow(
                    "ai-review",
                    approval_type="ai_driven",
                    ai_model="gpt-5.4",
                    ai_guidelines="Review for safety",
                    ai_fallback_behavior="escalate",
                    escalation_workflow="human-fallback",
                ),
            ],
        )
        doc = PolicyDocument(**data)
        ai_pol = [p for p in doc.approval_workflows if p.name == "ai-review"][0]
        assert ai_pol.escalation_workflow == "human-fallback"


# ===========================================================================
# ToolCondition
# ===========================================================================


class TestToolCondition:
    """Test ToolCondition validation."""

    def test_simple_condition(self):
        cond = ToolCondition(
            expression="args.command.contains('rm')",
            action="deny",
            condition_type="simple",
            description="Block rm commands",
        )
        assert cond.condition_type == ConditionType.SIMPLE

    def test_cel_condition(self):
        cond = ToolCondition(
            expression="args.amount > 1000 && args.currency == 'USD'",
            action="require_approval",
            condition_type="cel",
        )
        assert cond.condition_type == ConditionType.CEL

    def test_empty_expression_rejected(self):
        with pytest.raises(ValueError):
            ToolCondition(expression="", action="deny")

    def test_whitespace_only_expression_rejected(self):
        with pytest.raises(ValueError):
            ToolCondition(expression="   ", action="deny")

    def test_default_action_is_require_approval(self):
        cond = ToolCondition(expression="args.x == 1")
        assert cond.action == ConditionAction.REQUIRE_APPROVAL

    def test_default_type_is_simple(self):
        cond = ToolCondition(expression="args.x == 1")
        assert cond.condition_type == ConditionType.SIMPLE


# ===========================================================================
# ToolDefinition
# ===========================================================================


class TestToolDefinition:
    """Test ToolDefinition validation."""

    def test_builtin_source(self):
        tool = ToolDefinition(name="bash", source="builtin")
        assert tool.source == "builtin"
        assert tool.enabled is True  # default

    def test_mcp_source(self):
        tool = ToolDefinition(name="search", source="mcp")
        assert tool.source == "mcp"

    def test_http_source(self):
        tool = ToolDefinition(name="webhook", source="http")
        assert tool.source == "http"

    def test_agent_source(self):
        tool = ToolDefinition(name="Bash", source="agent")
        assert tool.source == "agent"

    def test_custom_server_source(self):
        tool = ToolDefinition(name="create_issue", source="github-server")
        assert tool.source == "github-server"

    def test_disabled_tool(self):
        tool = ToolDefinition(name="dangerous", source="builtin", enabled=False)
        assert tool.enabled is False


# ===========================================================================
# DefaultsDefinition
# ===========================================================================


class TestDefaultsDefinition:
    """Test DefaultsDefinition construction."""

    def test_defaults_deny_unknown(self):
        d = DefaultsDefinition(unknown_tools="deny")
        assert d.unknown_tools == "deny"

    def test_defaults_require_approval_for_new(self):
        d = DefaultsDefinition(require_approval_for_new_tools=True)
        assert d.require_approval_for_new_tools is True

    def test_defaults_all_fields(self):
        d = DefaultsDefinition(
            unknown_tools="require_approval",
            require_approval_for_new_tools=False,
            default_approval_workflow=None,
            inherit_from_parent=False,
        )
        assert d.inherit_from_parent is False


# ===========================================================================
# MCPServerDefinition
# ===========================================================================


class TestMCPServerDefinition:
    """Test MCPServerDefinition construction."""

    def test_basic_server(self):
        srv = MCPServerDefinition(name="test", url="https://example.com")
        assert srv.name == "test"
        assert srv.auth_type == "none"

    def test_server_with_auth(self):
        srv = MCPServerDefinition(
            name="secure",
            url="https://example.com",
            auth_type="bearer",
            auth_config={"token": "${MY_TOKEN}"},
        )
        assert srv.auth_type == "bearer"
        assert srv.auth_config["token"] == "${MY_TOKEN}"

    def test_server_with_transport(self):
        srv = MCPServerDefinition(
            name="stdio-srv",
            url="https://example.com",
            transport="stdio",
        )
        assert srv.transport == "stdio"
