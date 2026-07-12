"""Tests for PolicyGenerationService and policy generation endpoints."""

import json
import sys
import uuid
from datetime import datetime, timezone
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# litellm is not installed in the test environment — provide a stub so
# the policy_generation module can be imported.
if "litellm" not in sys.modules:
    _stub = ModuleType("litellm")
    _stub.completion = MagicMock()  # type: ignore[attr-defined]
    sys.modules["litellm"] = _stub

from preloop.models.models.account import Account
from preloop.services.policy_generation import (
    PolicyGenerationError,
    PolicyGenerationService,
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
            instance._validate_output.return_value = []
            result = await generate_policy(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        assert result.warnings == []

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
            instance._validate_output.return_value = ["minor warning"]
            result = await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        assert result.warnings == ["minor warning"]

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
            instance._validate_output.return_value = []
            result = await generate_policy_from_audit(
                request,
                account=mock_account,
                current_user=mock_user,
                db=mock_db,
            )

        assert result.yaml == VALID_POLICY_YAML
        instance._summarise_external_logs.assert_called_once_with(logs)
