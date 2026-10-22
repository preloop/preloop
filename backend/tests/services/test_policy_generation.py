"""Tests for PolicyGenerationService and policy generation endpoints."""

import json
import re
import sys
import uuid
from datetime import datetime, timezone
from types import ModuleType
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import yaml
from pytest_mock import MockerFixture

# litellm is not installed in the test environment — provide a stub so
# the policy_generation module can be imported.
if "litellm" not in sys.modules:
    _stub = ModuleType("litellm")
    _stub.completion = MagicMock()  # type: ignore[attr-defined]
    sys.modules["litellm"] = _stub

from preloop.models.models.account import Account
from preloop.services.policy import (
    DefaultsDefinition,
    PolicyApplier,
    PolicyDocument,
    UnknownToolsPolicy,
    load_policy_from_string,
)
from preloop.services.policy_generation import (
    PolicyGenerationError,
    PolicyGenerationService,
)
from preloop.services.policy_simulation import (
    PolicyEvaluationRequest,
    PolicyEvaluationResponse,
    simulate_policy,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

VALID_POLICY_YAML = """\
version: "1.0"
metadata:
  name: "Generated Policy"
  description: "Auto-generated"
approval_workflows:
  - name: "review"
    timeout_seconds: 300
tools:
  - name: "bash"
    source: "builtin"
    approval_workflow: "review"
"""

INVALID_POLICY_YAML = """\
version: "1.0"
not_a_field: true
"""


@pytest.fixture
def mock_db():
    return MagicMock()


@pytest.fixture
def mock_account():
    account = MagicMock(spec=Account)
    account.id = uuid.uuid4()
    return account


@pytest.fixture
def mock_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = uuid.uuid4()
    user.username = "testuser"
    return user


@pytest.fixture
def mock_ai_model():
    model = MagicMock()
    model.id = uuid.uuid4()
    model.provider_name = "openai"
    model.model_identifier = "gpt-5.4"
    model.api_key = "sk-test"
    model.api_endpoint = None
    model.is_default = True
    model.created_at = datetime.now(timezone.utc)
    return model


@pytest.fixture
def service(mock_db):
    return PolicyGenerationService(mock_db, str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# PolicyGenerationService._resolve_model
# ---------------------------------------------------------------------------


class TestResolveModel:
    def test_returns_default_model(self, service, mock_ai_model):
        with patch("preloop.services.policy_generation.crud_ai_model") as crud:
            crud.get_default_active_model.return_value = mock_ai_model
            result = service._resolve_model()
            assert result is mock_ai_model

    def test_falls_back_to_most_recent(self, service, mock_ai_model):
        older = MagicMock()
        older.created_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        mock_ai_model.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

        with patch("preloop.services.policy_generation.crud_ai_model") as crud:
            crud.get_default_active_model.return_value = None
            crud.get_by_account.return_value = [older, mock_ai_model]
            result = service._resolve_model()
            assert result is mock_ai_model

    def test_raises_when_no_models(self, service):
        with patch("preloop.services.policy_generation.crud_ai_model") as crud:
            crud.get_default_active_model.return_value = None
            crud.get_by_account.return_value = []
            with pytest.raises(PolicyGenerationError, match="No AI models"):
                service._resolve_model()


# ---------------------------------------------------------------------------
# PolicyGenerationService._extract_yaml
# ---------------------------------------------------------------------------


class TestExtractYaml:
    def test_plain_yaml(self):
        assert (
            PolicyGenerationService._extract_yaml("version: '1.0'") == "version: '1.0'"
        )

    def test_strips_markdown_fences(self):
        raw = "```yaml\nversion: '1.0'\n```"
        assert PolicyGenerationService._extract_yaml(raw) == "version: '1.0'"

    def test_strips_plain_fences(self):
        raw = "```\nversion: '1.0'\n```"
        assert PolicyGenerationService._extract_yaml(raw) == "version: '1.0'"


# ---------------------------------------------------------------------------
# PolicyGenerationService._validate_output
# ---------------------------------------------------------------------------


class TestValidateOutput:
    def test_valid_yaml(self):
        warnings = PolicyGenerationService._validate_output(VALID_POLICY_YAML)
        assert isinstance(warnings, list)

    def test_invalid_yaml_syntax(self):
        with pytest.raises(PolicyGenerationError, match="not valid YAML"):
            PolicyGenerationService._validate_output("key: [invalid")

    def test_non_mapping_yaml(self):
        with pytest.raises(PolicyGenerationError, match="not a YAML mapping"):
            PolicyGenerationService._validate_output("- item1\n- item2")

    def test_schema_errors(self):
        with pytest.raises(PolicyGenerationError, match="schema errors"):
            PolicyGenerationService._validate_output(INVALID_POLICY_YAML)


# ---------------------------------------------------------------------------
# defaults: generation accepts what `preloop policy apply` accepts (#1426)
# ---------------------------------------------------------------------------

DEFAULTS_CASES = [
    {},
    {"unknown_tools": "allow"},
    {"unknown_tools": "deny"},
    {"unknown_tools": "require_approval"},
    {"require_approval_for_new_tools": True},
    {"default_approval_workflow": "review"},
]


def _apply_accepts(defaults: dict[str, Any]) -> bool:
    """Whether ``PolicyApplier._apply_defaults`` accepts these defaults."""
    applier = PolicyApplier(MagicMock(), account_id=str(uuid.uuid4()))
    return applier._apply_defaults(DefaultsDefinition(**defaults), dry_run=True)


def _with_defaults(defaults: dict[str, Any]) -> str:
    policy = yaml.safe_load(VALID_POLICY_YAML)
    policy["defaults"] = defaults
    return yaml.safe_dump(policy, sort_keys=False)


def _yaml_examples(prompt: str) -> list[str]:
    return re.findall(r"```yaml\n(.*?)```", prompt, re.DOTALL)


def _as_policy(example: str) -> str:
    """Add the fields a policy file needs to a YAML example from a prompt."""
    policy = yaml.safe_load(example)
    policy.setdefault("version", "1.0")
    policy.setdefault("metadata", {"name": "Prompt example"})
    return yaml.safe_dump(policy, sort_keys=False)


class TestValidateOutputDefaults:
    @pytest.mark.parametrize("defaults", DEFAULTS_CASES)
    def test_rejects_what_apply_rejects(self, defaults: dict[str, Any]) -> None:
        text = _with_defaults(defaults)
        if _apply_accepts(defaults):
            PolicyGenerationService._validate_output(text)
        else:
            with pytest.raises(PolicyGenerationError, match="defaults"):
                PolicyGenerationService._validate_output(text)

    def test_retry_names_the_rejected_setting(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        # What the norm-based prompt used to ask for.
        bad_response = MagicMock()
        bad_response.choices = [MagicMock()]
        bad_response.choices[0].message.content = _with_defaults(
            {"unknown_tools": "require_approval"}
        )
        good_response = MagicMock()
        good_response.choices = [MagicMock()]
        good_response.choices[0].message.content = VALID_POLICY_YAML

        with patch("preloop.services.policy_generation.litellm") as mock_litellm:
            mock_litellm.completion.side_effect = [bad_response, good_response]
            result = service._call_llm(mock_ai_model, "system", "user")

        assert result == VALID_POLICY_YAML.strip()
        messages = mock_litellm.completion.call_args.kwargs["messages"]
        assert "unknown_tools='require_approval'" in messages[-1]["content"]


@pytest.fixture(params=["_build_system_prompt", "_build_audit_system_prompt"])
def system_prompt(
    request: pytest.FixtureRequest, service: PolicyGenerationService
) -> str:
    prompt: str = getattr(service, request.param)("{}", "")
    return prompt


class TestPromptDefaultsGuidance:
    def test_rules_name_no_unknown_tools_value_apply_rejects(
        self, system_prompt: str
    ) -> None:
        rejected = [
            value.value
            for value in UnknownToolsPolicy
            if not _apply_accepts({"unknown_tools": value.value})
        ]
        assert rejected
        rules = system_prompt.split("### RULES", 1)[1].split("###", 1)[0]
        for bullet in rules.split("\n- "):
            if "unknown_tools" not in bullet:
                continue
            for value in rejected:
                assert not re.search(rf"\b{value}\b", bullet), bullet

    def test_yaml_examples_pass_apply(self, system_prompt: str) -> None:
        examples = _yaml_examples(system_prompt)
        assert examples, "the prompt shows no YAML example"
        for example in examples:
            text = _as_policy(example)
            policy, result = load_policy_from_string(text)
            assert result.is_valid, result.errors
            assert policy is not None
            defaults = policy.defaults.model_dump() if policy.defaults else {}
            assert _apply_accepts(defaults)
            PolicyGenerationService._validate_output(text)

    @pytest.mark.parametrize(
        ("builder", "unlisted_decision"),
        [
            ("_build_system_prompt", "deny"),
            # The audit-log policy puts tools the logs never show behind
            # approval instead of denying them.
            ("_build_audit_system_prompt", "require_approval"),
        ],
    )
    async def test_example_rule_handles_unlisted_tools(
        self,
        service: PolicyGenerationService,
        builder: str,
        unlisted_decision: str,
    ) -> None:
        system_prompt = getattr(service, builder)("{}", "")
        [example] = [e for e in _yaml_examples(system_prompt) if "condition" in e]
        text = _as_policy(example)
        policy, _ = load_policy_from_string(text)
        assert policy is not None and policy.sensitive_data is not None
        [rule] = policy.sensitive_data.rules
        assert rule.condition is not None
        assert rule.action == unlisted_decision
        # An approval rule names a workflow the example defines.
        workflows = {workflow.name for workflow in policy.approval_workflows or []}
        if unlisted_decision == "require_approval":
            assert rule.approval_workflow in workflows
        else:
            assert rule.approval_workflow is None
        [server] = rule.scope.servers
        listed = re.findall(r"'([^']+)'", rule.condition)
        assert listed

        async def decide(name: str) -> PolicyEvaluationResponse:
            request = PolicyEvaluationRequest.model_validate(
                {"name": name, "server": server, "args": {}, "draft_yaml": text}
            )
            return await simulate_policy(
                request, db=MagicMock(), account_id=uuid.uuid4(), user_id=uuid.uuid4()
            )

        for name in listed:
            assert (await decide(name)).decision == "allow", name
        result = await decide("tool_the_example_does_not_list")
        assert result.decision == unlisted_decision
        assert result.matched_rule == rule.id

    def test_example_scope_server_is_a_placeholder_apply_rejects(
        self, system_prompt: str, mocker: MockerFixture
    ) -> None:
        # A copied example fails on apply instead of scoping the rule to a
        # server that no call comes from.
        [example] = [e for e in _yaml_examples(system_prompt) if "condition" in e]
        policy, _ = load_policy_from_string(_as_policy(example))
        assert policy is not None
        server = MagicMock()
        server.name = "github"
        server.auth_config = None
        mocker.patch(
            "preloop.models.crud.crud_mcp_server.get_active_by_account",
            return_value=[server],
        )
        mocker.patch(
            "preloop.models.crud.crud_approval_workflow.get_names_by_account",
            return_value=set(),
        )
        mocker.patch(
            "preloop.services.model_content_policy.load_model_io_rules",
            return_value=[],
        )
        applier = PolicyApplier(MagicMock(), account_id=str(uuid.uuid4()))
        errors = applier._validate_references(policy)
        assert any("<mcp-server-name>" in error for error in errors), errors


# ---------------------------------------------------------------------------
# sensitive_data: apply replaces the block, so the merge keeps stored entries
# ---------------------------------------------------------------------------

STORED_SENSITIVE_DATA = {
    "detectors": {
        "locales": ["de"],
        "custom_patterns": [{"name": "employee_id", "regex": r"EMP-\d{6}"}],
    },
    "rules": [
        {
            "id": "block-ids",
            "on": ["tool.args"],
            "types": ["credit_card", "employee_id"],
            "action": "deny",
        }
    ],
    "reference_only": [{"id": "patient-refs", "scope": {"servers": ["ehr"]}}],
}

# Only the new allowlist rule and a new keyword list: what a model writes
# when it does not copy the stored block.
GENERATED_SENSITIVE_DATA_YAML = """\
version: "1.0"
metadata:
  name: "Generated"
sensitive_data:
  detectors:
    keywords:
      - name: codenames
        terms: ["Project Phoenix"]
  rules:
    - id: github-allowlist
      "on": [tool.args]
      scope:
        servers: [github]
      condition: "!(tool.name in ['get_issue'])"
      action: deny
"""


def _generate_over_stored_block(
    service: PolicyGenerationService,
    mock_ai_model: MagicMock,
    generated_yaml: str,
    prompt: str = "Only allow get_issue on github",
) -> PolicyDocument:
    current = PolicyDocument.model_validate(
        {
            "version": "1.0",
            "metadata": {"name": "current"},
            "sensitive_data": STORED_SENSITIVE_DATA,
        }
    )
    with (
        patch.object(service, "_resolve_model", return_value=mock_ai_model),
        patch.object(service, "_call_llm", return_value=generated_yaml),
        patch.object(service, "_build_context_block", return_value="ctx"),
        patch(
            "preloop.services.policy_generation.export_current_policy",
            return_value=current,
        ),
    ):
        result = service.generate_from_prompt(prompt)
    return PolicyDocument.model_validate(yaml.safe_load(result["yaml"]))


class TestMergeKeepsSensitiveData:
    def test_restores_entries_the_model_left_out(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        document = _generate_over_stored_block(
            service, mock_ai_model, GENERATED_SENSITIVE_DATA_YAML
        )
        block = document.sensitive_data
        assert block is not None and block.detectors is not None
        assert [rule.id for rule in block.rules] == ["github-allowlist", "block-ids"]
        assert [item.id for item in block.reference_only] == ["patient-refs"]
        assert block.detectors.locales == ["de"]
        assert [item.name for item in block.detectors.custom_patterns] == [
            "employee_id"
        ]
        assert [item.name for item in block.detectors.keywords] == ["codenames"]

    def test_values_the_model_wrote_win(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        data = yaml.safe_load(GENERATED_SENSITIVE_DATA_YAML)
        data["sensitive_data"]["detectors"]["locales"] = ["nl"]
        data["sensitive_data"]["rules"].append(
            {"id": "block-ids", "on": ["tool.args"], "action": "notify"}
        )
        document = _generate_over_stored_block(
            service, mock_ai_model, yaml.safe_dump(data, sort_keys=False)
        )
        block = document.sensitive_data
        assert block is not None and block.detectors is not None
        assert block.detectors.locales == ["nl"]
        [stored_id_rule] = [rule for rule in block.rules if rule.id == "block-ids"]
        assert stored_id_rule.action == "notify"

    def test_prompt_can_drop_a_stored_rule(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        document = _generate_over_stored_block(
            service,
            mock_ai_model,
            GENERATED_SENSITIVE_DATA_YAML,
            prompt="Remove the block-ids rule",
        )
        assert document.sensitive_data is not None
        assert [rule.id for rule in document.sensitive_data.rules] == [
            "github-allowlist"
        ]

    def test_block_left_out_stays_out(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        # Apply keeps the stored block when the document has none.
        document = _generate_over_stored_block(
            service, mock_ai_model, VALID_POLICY_YAML
        )
        assert document.sensitive_data is None

    @pytest.mark.parametrize("include_current_config", [True, False])
    def test_prompt_asks_to_keep_entries_only_when_it_shows_them(
        self,
        service: PolicyGenerationService,
        mock_ai_model: MagicMock,
        include_current_config: bool,
    ) -> None:
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(
                service, "_call_llm", return_value=VALID_POLICY_YAML
            ) as mock_call,
            patch.object(service, "_build_context_block", return_value="ctx"),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                side_effect=RuntimeError("not needed"),
            ),
        ):
            service.generate_from_prompt(
                "test", include_current_config=include_current_config
            )
        system_prompt = " ".join(mock_call.call_args.args[1].split())
        assert ("existing detectors" in system_prompt) is include_current_config

    def test_audit_prompt_asks_to_keep_entries_only_when_it_shows_them(
        self, service: PolicyGenerationService
    ) -> None:
        assert "existing detectors" not in service._build_audit_system_prompt("{}", "")
        assert "existing detectors" in service._build_audit_system_prompt("{}", "ctx")


# ---------------------------------------------------------------------------
# PolicyGenerationService._to_litellm_model
# ---------------------------------------------------------------------------


class TestToLitellmModel:
    def test_openai(self, mock_ai_model):
        mock_ai_model.provider_name = "openai"
        mock_ai_model.model_identifier = "gpt-5.4"
        assert (
            PolicyGenerationService._to_litellm_model(mock_ai_model) == "openai/gpt-5.4"
        )

    def test_anthropic(self, mock_ai_model):
        mock_ai_model.provider_name = "anthropic"
        mock_ai_model.model_identifier = "claude-3-5-sonnet-20241022"
        result = PolicyGenerationService._to_litellm_model(mock_ai_model)
        assert result == "anthropic/claude-3-5-sonnet-20241022"

    def test_google(self, mock_ai_model):
        mock_ai_model.provider_name = "google"
        mock_ai_model.model_identifier = "gemini-2.0-flash"
        assert (
            PolicyGenerationService._to_litellm_model(mock_ai_model)
            == "gemini/gemini-2.0-flash"
        )

    def test_identifier_with_prefix_passthrough(self, mock_ai_model):
        mock_ai_model.provider_name = "openai"
        mock_ai_model.model_identifier = "azure/gpt-5.4"
        assert (
            PolicyGenerationService._to_litellm_model(mock_ai_model) == "azure/gpt-5.4"
        )


# ---------------------------------------------------------------------------
# PolicyGenerationService._call_llm
# ---------------------------------------------------------------------------


class TestCallLlm:
    def test_success_first_attempt(self, service, mock_ai_model):
        mock_response = MagicMock()
        mock_response.choices = [MagicMock()]
        mock_response.choices[0].message.content = VALID_POLICY_YAML

        with patch("preloop.services.policy_generation.litellm") as mock_litellm:
            mock_litellm.completion.return_value = mock_response
            result = service._call_llm(mock_ai_model, "system", "user")

        assert "version" in result
        mock_litellm.completion.assert_called_once()

    def test_retries_on_invalid_yaml(self, service, mock_ai_model):
        bad_response = MagicMock()
        bad_response.choices = [MagicMock()]
        bad_response.choices[0].message.content = INVALID_POLICY_YAML

        good_response = MagicMock()
        good_response.choices = [MagicMock()]
        good_response.choices[0].message.content = VALID_POLICY_YAML

        with patch("preloop.services.policy_generation.litellm") as mock_litellm:
            mock_litellm.completion.side_effect = [bad_response, good_response]
            result = service._call_llm(mock_ai_model, "system", "user")

        assert "version" in result
        assert mock_litellm.completion.call_count == 2

    def test_raises_after_two_failures(self, service, mock_ai_model):
        bad_response = MagicMock()
        bad_response.choices = [MagicMock()]
        bad_response.choices[0].message.content = INVALID_POLICY_YAML

        with patch("preloop.services.policy_generation.litellm") as mock_litellm:
            mock_litellm.completion.return_value = bad_response
            with pytest.raises(PolicyGenerationError, match="schema errors"):
                service._call_llm(mock_ai_model, "system", "user")

    def test_raises_on_llm_exception(self, service, mock_ai_model):
        with patch("preloop.services.policy_generation.litellm") as mock_litellm:
            mock_litellm.completion.side_effect = RuntimeError("API down")
            with pytest.raises(PolicyGenerationError, match="LLM call failed"):
                service._call_llm(mock_ai_model, "system", "user")


# ---------------------------------------------------------------------------
# PolicyGenerationService.generate_from_prompt
# ---------------------------------------------------------------------------


class TestGenerateFromPrompt:
    def test_success(self, service, mock_ai_model):
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value=VALID_POLICY_YAML),
            patch.object(service, "_build_context_block", return_value=""),
        ):
            result = service.generate_from_prompt("require approval for bash")

        assert result["yaml"] == VALID_POLICY_YAML
        assert isinstance(result["warnings"], list)

    def test_no_model_raises(self, service):
        with patch.object(
            service,
            "_resolve_model",
            side_effect=PolicyGenerationError("No AI models"),
        ):
            with pytest.raises(PolicyGenerationError, match="No AI models"):
                service.generate_from_prompt("test")

    def test_include_current_config_false(self, service, mock_ai_model):
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(
                service, "_call_llm", return_value=VALID_POLICY_YAML
            ) as mock_call,
            patch.object(service, "_build_context_block") as mock_ctx,
        ):
            service.generate_from_prompt("test", include_current_config=False)
            mock_ctx.assert_not_called()

    def test_no_model_message_points_at_console_ai_models(self, service):
        with patch("preloop.services.policy_generation.crud_ai_model") as crud:
            crud.get_default_active_model.return_value = None
            crud.get_by_account.return_value = []
            with pytest.raises(PolicyGenerationError, match="/console/ai-models"):
                service._resolve_model()

    def test_edit_preserves_unrelated_tool_rules(self, service, mock_ai_model):
        from preloop.services.policy.schema import (
            PolicyDocument,
            PolicyMetadata,
            PolicyVersion,
            ToolDefinition,
        )

        current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="current"),
            tools=[
                ToolDefinition(name="tool_a", source="builtin", enabled=True),
                ToolDefinition(name="tool_b", source="builtin", enabled=True),
            ],
        )
        dropped_tools_yaml = """\
version: "1.0"
metadata:
  name: "Edited"
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
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value=dropped_tools_yaml),
            patch.object(service, "_build_context_block", return_value="ctx"),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                return_value=current,
            ),
        ):
            result = service.generate_from_prompt(
                "Deny model requests when PII is found"
            )

        document = PolicyDocument(**__import__("yaml").safe_load(result["yaml"]))
        tool_names = {tool.name for tool in (document.tools or [])}
        assert tool_names == {"tool_a", "tool_b"}
        assert document.model_io
        assert document.model_io[0].id == "deny-pii"
        assert document.model_io[0].target == "model.request"

    def test_starter_policy_with_native_bash_does_not_raise(
        self, service, mock_ai_model
    ):
        from preloop.services.policy.schema import (
            MCPServerDefinition,
            PolicyDocument,
            PolicyMetadata,
            PolicyVersion,
            ToolDefinition,
        )

        current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="current"),
            mcp_servers=[
                MCPServerDefinition(
                    name="Example MCP Server",
                    url="https://example.com/mcp",
                ),
                MCPServerDefinition(
                    name="Other Server",
                    url="https://other.example.com/mcp",
                ),
            ],
            tools=[
                ToolDefinition(name="Bash", source="agent", enabled=True),
                ToolDefinition(
                    name="list_issues",
                    source="Example MCP Server",
                    enabled=True,
                ),
                ToolDefinition(
                    name="other_search",
                    source="Other Server",
                    enabled=True,
                ),
            ],
        )
        llm_yaml = """\
version: "1.0"
metadata:
  name: "Starter"
mcp_servers:
  - name: "Example MCP Server"
    url: "https://example.com/mcp"
approval_workflows:
  - name: "review"
    timeout_seconds: 300
tools:
  - name: "Bash"
    source: "agent"
    enabled: true
  - name: "list_issues"
    source: "Example MCP Server"
    enabled: true
    approval_workflow: "review"
  - name: "other_search"
    source: "Other Server"
    enabled: true
"""
        prompt = (
            "Generate a conservative starter policy update for the MCP server "
            '"Example MCP Server" (https://example.com/mcp).'
        )
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value=llm_yaml),
            patch.object(service, "_build_context_block", return_value="ctx"),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                return_value=current,
            ),
        ):
            result = service.generate_from_prompt(prompt)

        document = PolicyDocument(**__import__("yaml").safe_load(result["yaml"]))
        tools_by_name = {tool.name: tool for tool in (document.tools or [])}
        assert set(tools_by_name) == {"Bash", "list_issues", "other_search"}
        bash = tools_by_name["Bash"]
        assert bash.source == "agent"
        assert bash.enabled is True
        assert bash.approval_workflow is None
        other = tools_by_name["other_search"]
        assert other.source == "Other Server"
        assert other.enabled is True
        assert other.approval_workflow is None
        issues = tools_by_name["list_issues"]
        assert issues.source == "Example MCP Server"
        assert issues.approval_workflow == "review"

    def test_explicit_scope_mcp_server_name_without_prompt_match(
        self, service, mock_ai_model
    ):
        from preloop.services.policy.schema import (
            MCPServerDefinition,
            PolicyDocument,
            PolicyMetadata,
            PolicyVersion,
            ToolDefinition,
        )

        current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="current"),
            mcp_servers=[
                MCPServerDefinition(
                    name="Example MCP Server",
                    url="https://example.com/mcp",
                ),
                MCPServerDefinition(
                    name="Other Server",
                    url="https://other.example.com/mcp",
                ),
            ],
            tools=[
                ToolDefinition(name="Bash", source="agent", enabled=True),
                ToolDefinition(
                    name="list_issues",
                    source="Example MCP Server",
                    enabled=True,
                ),
                ToolDefinition(
                    name="other_search",
                    source="Other Server",
                    enabled=True,
                ),
            ],
        )
        llm_yaml = """\
version: "1.0"
metadata:
  name: "Starter"
mcp_servers:
  - name: "Example MCP Server"
    url: "https://example.com/mcp"
approval_workflows:
  - name: "review"
    timeout_seconds: 300
tools:
  - name: "Bash"
    source: "agent"
    enabled: true
  - name: "list_issues"
    source: "Example MCP Server"
    enabled: true
    approval_workflow: "review"
"""
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value=llm_yaml),
            patch.object(service, "_build_context_block", return_value="ctx"),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                return_value=current,
            ),
        ):
            result = service.generate_from_prompt(
                "Tighten rules for this server",
                scope_mcp_server_name="Example MCP Server",
            )

        document = PolicyDocument(**__import__("yaml").safe_load(result["yaml"]))
        tools_by_name = {tool.name: tool for tool in (document.tools or [])}
        assert set(tools_by_name) == {"Bash", "list_issues", "other_search"}
        assert tools_by_name["Bash"].approval_workflow is None
        assert tools_by_name["other_search"].approval_workflow is None
        assert tools_by_name["list_issues"].approval_workflow == "review"

    def test_starter_policy_context_block_scopes_to_target_server(self, service):
        from preloop.services.policy.schema import (
            MCPServerDefinition,
            PolicyDocument,
            PolicyMetadata,
            PolicyVersion,
            ToolDefinition,
        )

        current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="current"),
            mcp_servers=[
                MCPServerDefinition(
                    name="Example MCP Server",
                    url="https://example.com/mcp",
                ),
                MCPServerDefinition(
                    name="Other Server",
                    url="https://other.example.com/mcp",
                ),
            ],
            tools=[
                ToolDefinition(name="Bash", source="agent", enabled=True),
                ToolDefinition(
                    name="list_issues",
                    source="Example MCP Server",
                    enabled=True,
                ),
                ToolDefinition(
                    name="other_search",
                    source="Other Server",
                    enabled=True,
                ),
            ],
        )
        prompt = (
            "Generate a conservative starter policy update for the MCP server "
            '"Example MCP Server" (https://example.com/mcp).'
        )
        with patch(
            "preloop.services.policy_generation.export_current_policy",
            return_value=current,
        ):
            context = service._build_context_block(prompt)

        assert "list_issues" in context
        assert "Bash" not in context
        assert "other_search" not in context

    def test_explicit_scope_mcp_server_name_scopes_context_block(self, service):
        from preloop.services.policy.schema import (
            MCPServerDefinition,
            PolicyDocument,
            PolicyMetadata,
            PolicyVersion,
            ToolDefinition,
        )

        current = PolicyDocument(
            version=PolicyVersion.V1_0,
            metadata=PolicyMetadata(name="current"),
            mcp_servers=[
                MCPServerDefinition(
                    name="Example MCP Server",
                    url="https://example.com/mcp",
                ),
            ],
            tools=[
                ToolDefinition(name="Bash", source="agent", enabled=True),
                ToolDefinition(
                    name="list_issues",
                    source="Example MCP Server",
                    enabled=True,
                ),
            ],
        )
        with patch(
            "preloop.services.policy_generation.export_current_policy",
            return_value=current,
        ):
            context = service._build_context_block(
                "Tighten rules for this server",
                scope_mcp_server_name="Example MCP Server",
            )

        assert "list_issues" in context
        assert "Bash" not in context

    def test_generated_model_io_round_trips_schema(self, service, mock_ai_model):
        from preloop.services.policy.schema import PolicyDocument

        yaml_with_model_io = """\
version: "1.0"
metadata:
  name: "Content"
model_io:
  - id: deny-injection
    target: model.request
    detectors:
      injection: true
    conditions:
      - expression: "injection.score > 0.7"
        action: deny
"""
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value=yaml_with_model_io),
            patch.object(service, "_build_context_block", return_value=""),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                side_effect=RuntimeError("empty account"),
            ),
        ):
            result = service.generate_from_prompt(
                "Deny prompts with injection.score > 0.7",
                include_current_config=False,
            )

        document = PolicyDocument(**__import__("yaml").safe_load(result["yaml"]))
        assert document.model_io[0].id == "deny-injection"

    def test_invalid_llm_output_raises(self, service, mock_ai_model):
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_call_llm", return_value="not: [valid"),
            patch.object(service, "_build_context_block", return_value=""),
        ):
            with pytest.raises(PolicyGenerationError, match="not valid YAML"):
                service.generate_from_prompt("anything", include_current_config=False)


# ---------------------------------------------------------------------------
# PolicyGenerationService.generate_from_audit_logs
# ---------------------------------------------------------------------------


class TestGenerateFromAuditLogs:
    def test_success_with_db_logs(self, service, mock_ai_model):
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_summarise_account_logs", return_value="summary"),
            patch.object(service, "_call_llm", return_value=VALID_POLICY_YAML),
            patch.object(service, "_build_context_block", return_value=""),
        ):
            result = service.generate_from_audit_logs()

        assert result["yaml"] == VALID_POLICY_YAML

    def test_success_with_external_json(self, service, mock_ai_model):
        logs_json = json.dumps([{"tool_name": "bash", "args": {}}])
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(
                service, "_summarise_external_logs", return_value="summary"
            ) as mock_ext,
            patch.object(service, "_call_llm", return_value=VALID_POLICY_YAML),
            patch.object(service, "_build_context_block", return_value=""),
        ):
            result = service.generate_from_audit_logs(audit_logs_json=logs_json)

        mock_ext.assert_called_once_with(logs_json)
        assert result["yaml"] == VALID_POLICY_YAML

    def test_restores_configuration_the_model_left_out(
        self, service: PolicyGenerationService, mock_ai_model: MagicMock
    ) -> None:
        current = PolicyDocument.model_validate(
            {
                "version": "1.0",
                "metadata": {"name": "current"},
                "tools": [{"name": "bash", "source": "builtin"}],
                "sensitive_data": STORED_SENSITIVE_DATA,
            }
        )
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_summarise_account_logs", return_value="summary"),
            patch.object(
                service, "_call_llm", return_value=GENERATED_SENSITIVE_DATA_YAML
            ),
            patch.object(service, "_build_context_block", return_value="ctx"),
            patch(
                "preloop.services.policy_generation.export_current_policy",
                return_value=current,
            ),
        ):
            result = service.generate_from_audit_logs()

        document = PolicyDocument.model_validate(yaml.safe_load(result["yaml"]))
        assert [tool.name for tool in document.tools or []] == ["bash"]
        assert document.sensitive_data is not None
        assert [rule.id for rule in document.sensitive_data.rules] == [
            "github-allowlist",
            "block-ids",
        ]

    def test_empty_logs_raises(self, service, mock_ai_model):
        with (
            patch.object(service, "_resolve_model", return_value=mock_ai_model),
            patch.object(service, "_summarise_account_logs", return_value=""),
        ):
            with pytest.raises(PolicyGenerationError, match="No tool-call audit logs"):
                service.generate_from_audit_logs()

    def test_invalid_external_json_raises(self, service):
        with patch.object(service, "_resolve_model", return_value=MagicMock()):
            with pytest.raises(PolicyGenerationError, match="Invalid audit-log JSON"):
                service.generate_from_audit_logs(audit_logs_json="not json")

    def test_external_json_not_array_raises(self, service):
        with patch.object(service, "_resolve_model", return_value=MagicMock()):
            with pytest.raises(PolicyGenerationError, match="must be a JSON array"):
                service.generate_from_audit_logs(audit_logs_json='{"key": "val"}')


# ---------------------------------------------------------------------------
# Log summarisation helpers
# ---------------------------------------------------------------------------


class TestLogSummarisation:
    def test_format_log_summary(self):
        logs = []
        for i in range(3):
            log = MagicMock()
            log.details = {
                "tool_name": "pay",
                "arguments": {"amount": str(100 * (i + 1))},
            }
            log.status = "success"
            logs.append(log)

        summary = PolicyGenerationService._format_log_summary(logs)
        assert "pay" in summary
        assert "3 total calls" in summary
        assert "Numeric ranges" in summary

    def test_format_log_summary_empty(self):
        summary = PolicyGenerationService._format_log_summary([])
        assert "0 total calls" in summary

    def test_format_external_log_summary(self):
        data = [{"tool": "bash", "args": {"cmd": "ls"}}] * 5
        summary = PolicyGenerationService._format_external_log_summary(data)
        assert "5 entries" in summary

    def test_format_external_log_summary_truncates(self):
        data = [{"i": i} for i in range(300)]
        summary = PolicyGenerationService._format_external_log_summary(data)
        assert "300 entries" in summary
        assert "showing first 200" in summary


# ---------------------------------------------------------------------------
# Endpoint: generate_policy (POST /policies/generate)
# ---------------------------------------------------------------------------


PATCH_SERVICE = "preloop.services.policy_generation.PolicyGenerationService"


class TestGeneratePolicyEndpoint:
    async def test_success(self, mock_account, mock_db, mock_user):
        from preloop.api.endpoints.policies import (
            GeneratePolicyRequest,
            generate_policy,
        )

        request = GeneratePolicyRequest(prompt="require approval for bash")

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._build_context_block.return_value = ""
            instance._build_system_prompt.return_value = "system"
            instance._call_llm.return_value = VALID_POLICY_YAML
            instance._merge_preserving_unrelated.side_effect = (
                lambda yaml_text, _prompt, **_kwargs: yaml_text
            )
            instance._validate_output.return_value = []
            result = await generate_policy(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        assert result.warnings == []

    async def test_forwards_scope_mcp_server_name(
        self, mock_account, mock_db, mock_user
    ):
        from preloop.api.endpoints.policies import (
            GeneratePolicyRequest,
            generate_policy,
        )

        request = GeneratePolicyRequest(
            prompt="require approval for bash",
            scope_mcp_server_name="Example MCP Server",
        )

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._build_context_block.return_value = ""
            instance._build_system_prompt.return_value = "system"
            instance._call_llm.return_value = VALID_POLICY_YAML
            instance._merge_preserving_unrelated.side_effect = (
                lambda yaml_text, _prompt, **_kwargs: yaml_text
            )
            instance._validate_output.return_value = []
            result = await generate_policy(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        instance._build_context_block.assert_called_once_with(
            request.prompt,
            scope_mcp_server_name="Example MCP Server",
        )
        instance._merge_preserving_unrelated.assert_called_once_with(
            VALID_POLICY_YAML,
            request.prompt,
            scope_mcp_server_name="Example MCP Server",
        )

    async def test_no_model_returns_400(self, mock_account, mock_db, mock_user):
        from fastapi import HTTPException

        from preloop.api.endpoints.policies import (
            GeneratePolicyRequest,
            generate_policy,
        )

        request = GeneratePolicyRequest(prompt="test")

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.side_effect = PolicyGenerationError(
                "No AI models configured"
            )
            with pytest.raises(HTTPException) as exc_info:
                await generate_policy(
                    request,
                    account=mock_account,
                    current_user=mock_user,
                    db=mock_db,
                )

        assert exc_info.value.status_code == 400
        assert "No AI models" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Endpoint: generate_policy_from_audit (POST /policies/generate-from-audit)
# ---------------------------------------------------------------------------


class TestGeneratePolicyFromAuditEndpoint:
    async def test_success(self, mock_account, mock_db, mock_user):
        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        request = GeneratePolicyFromAuditRequest()

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._summarise_account_logs.return_value = "summary"
            instance._build_context_block.return_value = ""
            instance._build_audit_system_prompt.return_value = "system"
            instance._call_llm.return_value = VALID_POLICY_YAML
            instance._merge_preserving_unrelated.side_effect = (
                lambda yaml_text, _prompt: yaml_text
            )
            instance._validate_output.return_value = ["minor warning"]
            result = await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        assert result.warnings == ["minor warning"]
        instance._merge_preserving_unrelated.assert_called_once_with(
            VALID_POLICY_YAML, ""
        )

    async def test_with_date_range(self, mock_account, mock_db, mock_user):
        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        request = GeneratePolicyFromAuditRequest(
            start_date="2026-01-01", end_date="2026-02-01"
        )

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._summarise_account_logs.return_value = "summary"
            instance._build_context_block.return_value = ""
            instance._build_audit_system_prompt.return_value = "system"
            instance._call_llm.return_value = VALID_POLICY_YAML
            instance._merge_preserving_unrelated.side_effect = (
                lambda yaml_text, _prompt: yaml_text
            )
            instance._validate_output.return_value = []
            result = await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML

    async def test_invalid_start_date_returns_400(
        self, mock_account, mock_db, mock_user
    ):
        from fastapi import HTTPException

        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        request = GeneratePolicyFromAuditRequest(start_date="not-a-date")

        with pytest.raises(HTTPException) as exc_info:
            await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == 400
        assert "Invalid start_date" in exc_info.value.detail

    async def test_invalid_end_date_returns_400(self, mock_account, mock_db, mock_user):
        from fastapi import HTTPException

        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        request = GeneratePolicyFromAuditRequest(end_date="bad")

        with pytest.raises(HTTPException) as exc_info:
            await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert exc_info.value.status_code == 400
        assert "Invalid end_date" in exc_info.value.detail

    async def test_no_logs_returns_400(self, mock_account, mock_db, mock_user):
        from fastapi import HTTPException

        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        request = GeneratePolicyFromAuditRequest()

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._summarise_account_logs.return_value = ""
            with pytest.raises(HTTPException) as exc_info:
                await generate_policy_from_audit(
                    request,
                    account=mock_account,
                    current_user=mock_user,
                    db=mock_db,
                )

        assert exc_info.value.status_code == 400
        assert "No tool-call audit logs" in exc_info.value.detail

    async def test_with_external_logs(self, mock_account, mock_db, mock_user):
        from preloop.api.endpoints.policies import (
            GeneratePolicyFromAuditRequest,
            generate_policy_from_audit,
        )

        logs = json.dumps([{"tool_name": "bash", "args": {"cmd": "ls"}}])
        request = GeneratePolicyFromAuditRequest(audit_logs_json=logs)

        with patch(PATCH_SERVICE) as mock_svc:
            instance = mock_svc.return_value
            instance._resolve_model.return_value = MagicMock()
            instance._summarise_external_logs.return_value = "summary"
            instance._build_context_block.return_value = ""
            instance._build_audit_system_prompt.return_value = "system"
            instance._call_llm.return_value = VALID_POLICY_YAML
            instance._merge_preserving_unrelated.side_effect = (
                lambda yaml_text, _prompt: yaml_text
            )
            instance._validate_output.return_value = []
            result = await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        instance._summarise_external_logs.assert_called_once_with(logs)
