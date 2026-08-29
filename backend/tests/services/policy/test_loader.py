"""Tests for policy loader functionality."""

import json

import pytest
import yaml

from preloop.services.policy.loader import (
    PolicyApplier,
    compute_policy_diff,
    export_policy_to_json,
    export_policy_to_yaml,
    load_policy_from_string,
    resolve_env_vars,
)
from preloop.services.policy.schema import (
    ApprovalWorkflowDefinition,
    DefaultsDefinition,
    MCPServerDefinition,
    PolicyDocument,
    PolicyMetadata,
    PolicyVersion,
    ToolDefinition,
    UnknownToolsPolicy,
)


class TestLoadPolicyFromString:
    """Test load_policy_from_string function."""

    def test_load_valid_yaml(self):
        """Test loading valid YAML policy."""
        yaml_content = """
version: "1.0"
metadata:
  name: Test Policy
  description: A test policy
mcp_servers:
  - name: test-server
    url: http://localhost:8080
approval_workflows:
  - name: default-policy
    timeout_seconds: 300
tools:
  - name: test_tool
    source: test-server
    approval_workflow: default-policy
"""
        policy, result = load_policy_from_string(yaml_content, format="yaml")

        assert result.is_valid is True
        assert len(result.errors) == 0
        assert policy is not None
        assert policy.metadata.name == "Test Policy"
        assert len(policy.mcp_servers) == 1
        assert len(policy.approval_workflows) == 1
        assert len(policy.tools) == 1

    def test_load_model_io_yaml(self):
        """YAML model_io targets round-trip through the loader."""
        yaml_content = """
version: "1.0"
metadata:
  name: Content Policy
approval_workflows:
  - name: high-risk
    timeout_seconds: 300
model_io:
  - id: deny-pii
    target: model.request
    detectors:
      pii:
        types: [email]
    conditions:
      - expression: "pii.found == true"
        action: deny
"""
        policy, result = load_policy_from_string(yaml_content, format="yaml")
        assert result.is_valid is True
        assert policy is not None
        assert policy.model_io is not None
        assert policy.model_io[0].id == "deny-pii"
        assert policy.model_io[0].target == "model.request"

    def test_load_valid_json(self):
        """Test loading valid JSON policy."""
        json_content = json.dumps(
            {
                "version": "1.0",
                "metadata": {
                    "name": "JSON Policy",
                },
                "tools": [
                    {
                        "name": "builtin_tool",
                        "source": "builtin",
                    },
                ],
            }
        )
        policy, result = load_policy_from_string(json_content, format="json")

        assert result.is_valid is True
        assert policy is not None
        assert policy.metadata.name == "JSON Policy"

    def test_load_invalid_yaml_syntax(self):
        """Test loading YAML with syntax errors."""
        invalid_yaml = """
version: "1.0"
metadata:
  name: Test
  invalid: [unclosed bracket
"""
        policy, result = load_policy_from_string(invalid_yaml, format="yaml")

        assert result.is_valid is False
        assert policy is None
        assert len(result.errors) > 0
        assert "yaml" in result.errors[0].message.lower()

    def test_load_invalid_json_syntax(self):
        """Test loading JSON with syntax errors."""
        invalid_json = '{"version": "1.0", "metadata": {"name": }'
        policy, result = load_policy_from_string(invalid_json, format="json")

        assert result.is_valid is False
        assert policy is None
        assert len(result.errors) > 0
        assert "json" in result.errors[0].message.lower()

    def test_load_empty_content(self):
        """Test loading empty content."""
        policy, result = load_policy_from_string("", format="yaml")

        assert result.is_valid is False
        assert policy is None
        assert len(result.errors) > 0

    def test_load_missing_required_fields(self):
        """Test loading policy missing required fields."""
        yaml_content = """
version: "1.0"
# Missing metadata
"""
        policy, result = load_policy_from_string(yaml_content, format="yaml")

        assert result.is_valid is False
        assert policy is None
        assert len(result.errors) > 0
        assert "metadata" in result.errors[0].path.lower()

    def test_load_with_warnings(self):
        """Test loading policy that generates warnings."""
        yaml_content = """
version: "1.0"
metadata:
  name: Test Policy
tools:
  - name: test_tool
    source: builtin
    conditions:
      - expression: "args.amount > 100"
        action: require_approval
    # No approval_workflow set - should generate warning
"""
        policy, result = load_policy_from_string(yaml_content, format="yaml")

        assert result.is_valid is True
        assert policy is not None
        assert len(result.warnings) > 0
        assert "approval_workflow" in result.warnings[0].lower()


class TestExportPolicy:
    """Test policy export functions."""

    @pytest.fixture
    def sample_policy(self):
        """Create a sample policy for testing."""
        return PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(
                name="Test Policy",
                description="For testing exports",
            ),
            mcp_servers=[
                MCPServerDefinition(
                    name="test-server",
                    url="http://localhost:8080",
                ),
            ],
            approval_workflows=[
                ApprovalWorkflowDefinition(
                    name="test-policy",
                    timeout_seconds=300,
                ),
            ],
            tools=[
                ToolDefinition(
                    name="test_tool",
                    source="test-server",
                    approval_workflow="test-policy",
                ),
            ],
        )

    def test_export_to_yaml(self, sample_policy):
        """Test exporting policy to YAML."""
        yaml_output = export_policy_to_yaml(sample_policy)

        assert isinstance(yaml_output, str)
        assert "version:" in yaml_output
        assert "metadata:" in yaml_output
        assert "Test Policy" in yaml_output

        # Should be parseable back
        parsed = yaml.safe_load(yaml_output)
        assert parsed["metadata"]["name"] == "Test Policy"

    def test_export_to_json(self, sample_policy):
        """Test exporting policy to JSON."""
        json_output = export_policy_to_json(sample_policy)

        assert isinstance(json_output, str)
        assert '"version"' in json_output
        assert '"metadata"' in json_output
        assert "Test Policy" in json_output

        # Should be parseable back
        parsed = json.loads(json_output)
        assert parsed["metadata"]["name"] == "Test Policy"

    def test_roundtrip_yaml(self, sample_policy):
        """Test YAML export and reimport produces equivalent policy."""
        yaml_output = export_policy_to_yaml(sample_policy)
        reimported, result = load_policy_from_string(yaml_output, format="yaml")

        assert result.is_valid is True
        assert reimported is not None
        assert reimported.metadata.name == sample_policy.metadata.name
        assert len(reimported.mcp_servers) == len(sample_policy.mcp_servers)
        assert len(reimported.approval_workflows) == len(
            sample_policy.approval_workflows
        )
        assert len(reimported.tools) == len(sample_policy.tools)


class TestComputePolicyDiff:
    """Test policy diff computation."""

    @pytest.fixture
    def base_policy(self):
        """Create base policy for diff testing."""
        return PolicyDocument(
            metadata=PolicyMetadata(name="Base Policy"),
            mcp_servers=[
                MCPServerDefinition(name="server1", url="http://a.com"),
            ],
            approval_workflows=[
                ApprovalWorkflowDefinition(name="policy1", timeout_seconds=300),
            ],
            tools=[
                ToolDefinition(
                    name="tool1", source="server1", approval_workflow="policy1"
                ),
            ],
        )

    def test_no_changes(self, base_policy):
        """Test diff with identical policies."""
        diff = compute_policy_diff(base_policy, base_policy)

        assert diff.has_changes is False
        assert len(diff.changes) == 0
        assert diff.summary == "No changes"

    def test_added_server(self, base_policy):
        """Test diff with added MCP server."""
        modified = PolicyDocument(
            metadata=base_policy.metadata,
            mcp_servers=[
                MCPServerDefinition(name="server1", url="http://a.com"),
                MCPServerDefinition(name="server2", url="http://b.com"),
            ],
            approval_workflows=base_policy.approval_workflows,
            tools=base_policy.tools,
        )
        diff = compute_policy_diff(base_policy, modified)

        assert diff.has_changes is True
        assert any(c.operation == "add" and "server2" in c.path for c in diff.changes)
        assert "addition" in diff.summary.lower()

    def test_removed_server(self, base_policy):
        """Test diff with removed MCP server."""
        modified = PolicyDocument(
            metadata=base_policy.metadata,
            mcp_servers=[],
            approval_workflows=base_policy.approval_workflows,
            tools=[
                ToolDefinition(name="tool1", source="builtin"),
            ],
        )
        diff = compute_policy_diff(base_policy, modified)

        assert diff.has_changes is True
        assert any(
            c.operation == "remove" and "server1" in c.path for c in diff.changes
        )
        assert "removal" in diff.summary.lower()

    def test_modified_policy(self, base_policy):
        """Test diff with modified approval workflow."""
        modified = PolicyDocument(
            metadata=base_policy.metadata,
            mcp_servers=base_policy.mcp_servers,
            approval_workflows=[
                ApprovalWorkflowDefinition(
                    name="policy1", timeout_seconds=600
                ),  # Changed
            ],
            tools=base_policy.tools,
        )
        diff = compute_policy_diff(base_policy, modified)

        assert diff.has_changes is True
        assert any(
            c.operation == "modify" and "policy1" in c.path for c in diff.changes
        )
        assert "modification" in diff.summary.lower()

    def test_metadata_change(self, base_policy):
        """Test diff with metadata changes."""
        modified = PolicyDocument(
            metadata=PolicyMetadata(name="Modified Policy"),
            mcp_servers=base_policy.mcp_servers,
            approval_workflows=base_policy.approval_workflows,
            tools=base_policy.tools,
        )
        diff = compute_policy_diff(base_policy, modified)

        assert diff.has_changes is True
        assert any("metadata" in c.path for c in diff.changes)


class TestResolveEnvVars:
    """Test environment variable resolution."""

    def test_resolve_string_var(self, monkeypatch):
        """Test resolving environment variable in string."""
        monkeypatch.setenv("TEST_VAR", "resolved_value")

        result = resolve_env_vars("prefix_${TEST_VAR}_suffix")
        assert result == "prefix_resolved_value_suffix"

    def test_resolve_in_dict(self, monkeypatch):
        """Test resolving environment variables in dict."""
        monkeypatch.setenv("API_KEY", "secret123")

        data = {"auth": {"key": "${API_KEY}"}, "other": "static"}
        result = resolve_env_vars(data)

        assert result["auth"]["key"] == "secret123"
        assert result["other"] == "static"

    def test_resolve_in_list(self, monkeypatch):
        """Test resolving environment variables in list."""
        monkeypatch.setenv("HOST", "localhost")

        data = ["http://${HOST}:8080", "static_value"]
        result = resolve_env_vars(data)

        assert result[0] == "http://localhost:8080"
        assert result[1] == "static_value"

    def test_unset_var_unchanged(self):
        """Test that unset variables remain unchanged."""
        result = resolve_env_vars("${UNSET_VAR}")
        assert result == "${UNSET_VAR}"

    def test_multiple_vars(self, monkeypatch):
        """Test resolving multiple variables in one string."""
        monkeypatch.setenv("HOST", "example.com")
        monkeypatch.setenv("PORT", "8080")

        result = resolve_env_vars("http://${HOST}:${PORT}")
        assert result == "http://example.com:8080"

    def test_non_string_unchanged(self):
        """Test that non-string values are unchanged."""
        assert resolve_env_vars(123) == 123
        assert resolve_env_vars(True) is True
        assert resolve_env_vars(None) is None


class TestPolicyApplierDefaults:
    """Regression tests for restrictive default validation."""

    def test_restrictive_defaults_return_validation_error_without_exception(
        self, db_session, test_user
    ):
        """Unsupported defaults should fail cleanly without BaseModel errors."""
        applier = PolicyApplier(db_session, account_id=str(test_user.account_id))
        defaults = DefaultsDefinition.model_construct(
            unknown_tools=UnknownToolsPolicy.REQUIRE_APPROVAL,
            require_approval_for_new_tools=True,
            default_approval_workflow="david-approval",
        )

        assert applier._apply_defaults(defaults, dry_run=True) is False
        assert len(applier._result.errors) == 1
        assert "restrictive default settings" in applier._result.errors[0]
        assert "unknown_tools='require_approval'" in applier._result.errors[0]
        assert "BaseModel.__init__" not in applier._result.errors[0]
