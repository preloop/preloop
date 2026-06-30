"""Tests for the AI-powered approval service.

All LLM/provider calls are mocked so the tests are hermetic.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from preloop.services.ai_approval_service import (
    AI_APPROVAL_PROMPT,
    AIApprovalResult,
    AIApprovalService,
    DEFAULT_MODEL,
    get_ai_approval_service,
)


def make_workflow(**overrides):
    """Build a minimal ApprovalWorkflow-like object for evaluate()."""
    base = {
        "approval_config": {},
        "ai_model": None,
        "ai_guidelines": None,
        "ai_context": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def service():
    return AIApprovalService()


class TestDetectProvider:
    @pytest.mark.parametrize(
        "model,expected",
        [
            ("claude-opus-4", "anthropic"),
            ("anthropic.foo", "anthropic"),
            ("gemini-1.5-pro", "google"),
            ("deepseek-chat", "deepseek"),
            ("qwen-max", "qwen"),
            ("gpt-5.4-mini", "openai"),
            ("some-unknown-model", "openai"),
        ],
    )
    def test_detect(self, service, model, expected):
        assert service._detect_provider(model) == expected


class TestDefaultGuidelines:
    def test_shell(self, service):
        g = service._get_default_guidelines("execute_command")
        assert "shell/command" in g.lower()

    def test_file(self, service):
        g = service._get_default_guidelines("write_file")
        assert "file write" in g.lower()

    def test_http(self, service):
        g = service._get_default_guidelines("http_request")
        assert "http" in g.lower()

    def test_database(self, service):
        g = service._get_default_guidelines("run_sql_query")
        assert "database" in g.lower()

    def test_generic(self, service):
        g = service._get_default_guidelines("frobnicate")
        assert "safety and appropriateness" in g.lower()


class TestCleanJsonResponse:
    def test_strips_json_fence(self, service):
        raw = '```json\n{"decision": "APPROVE"}\n```'
        assert service._clean_json_response(raw) == '{"decision": "APPROVE"}'

    def test_strips_plain_fence(self, service):
        raw = '```\n{"a": 1}\n```'
        assert service._clean_json_response(raw) == '{"a": 1}'

    def test_unclosed_fence(self, service):
        raw = '```json\n{"decision": "DENY"}'
        cleaned = service._clean_json_response(raw)
        assert cleaned == '{"decision": "DENY"}'

    def test_no_fence_passthrough(self, service):
        raw = '{"decision": "UNCERTAIN"}'
        assert service._clean_json_response(raw) == raw


class TestBuildPrompt:
    def test_includes_tool_and_args(self, service):
        prompt = service._build_prompt(
            tool_name="delete_thing",
            tool_args={"id": 7},
            guidelines="be careful",
            context=None,
        )
        assert "delete_thing" in prompt
        assert '"id": 7' in prompt
        assert "be careful" in prompt
        assert "Execution context" not in prompt

    def test_includes_context_section(self, service):
        prompt = service._build_prompt(
            tool_name="t",
            tool_args={},
            guidelines="g",
            context={"env": "staging"},
        )
        assert "Execution context" in prompt
        assert "staging" in prompt

    def test_non_serializable_args_fallback(self, service):
        prompt = service._build_prompt(
            tool_name="t",
            tool_args={"obj": object()},
            guidelines="g",
            context=None,
        )
        # default=str makes objects serializable, so prompt still renders
        assert "t" in prompt


class TestParseResponse:
    def test_valid_approve(self, service):
        raw = '{"decision": "APPROVE", "confidence": 0.9, "reasoning": "safe"}'
        result = service._parse_response(raw, "m")
        assert result.decision == "approve"
        assert result.confidence == 0.9
        assert result.reasoning == "safe"
        assert result.model_used == "m"

    def test_valid_deny(self, service):
        raw = '{"decision": "DENY", "confidence": 0.8, "reasoning": "risky"}'
        assert service._parse_response(raw, "m").decision == "deny"

    def test_valid_uncertain(self, service):
        raw = '{"decision": "MAYBE", "confidence": 0.5, "reasoning": "?"}'
        assert service._parse_response(raw, "m").decision == "uncertain"

    def test_confidence_clamped_high(self, service):
        raw = '{"decision": "APPROVE", "confidence": 5.0, "reasoning": "x"}'
        assert service._parse_response(raw, "m").confidence == 1.0

    def test_confidence_clamped_low(self, service):
        raw = '{"decision": "DENY", "confidence": -3, "reasoning": "x"}'
        assert service._parse_response(raw, "m").confidence == 0.0

    def test_empty_response(self, service):
        result = service._parse_response("", "m")
        assert result.decision == "uncertain"
        assert result.confidence == 0.0
        assert "Empty response" in result.reasoning

    def test_invalid_json_fallback_approve(self, service):
        raw = "I think we should APPROVE this request."
        result = service._parse_response(raw, "m")
        assert result.decision == "approve"
        assert result.confidence == 0.3

    def test_invalid_json_fallback_deny(self, service):
        raw = "This must be DENY for safety."
        result = service._parse_response(raw, "m")
        assert result.decision == "deny"

    def test_invalid_json_fallback_uncertain(self, service):
        raw = "no clear signal here"
        result = service._parse_response(raw, "m")
        assert result.decision == "uncertain"

    def test_fenced_valid_json(self, service):
        raw = '```json\n{"decision": "APPROVE", "confidence": 0.7, "reasoning": "ok"}\n```'
        result = service._parse_response(raw, "m")
        assert result.decision == "approve"
        assert result.confidence == 0.7


@pytest.mark.asyncio
class TestEvaluate:
    async def test_happy_path_approve(self, service):
        workflow = make_workflow(ai_guidelines="Always approve reads")
        with patch.object(
            service,
            "_call_llm",
            new=AsyncMock(
                return_value='{"decision": "APPROVE", "confidence": 0.95, "reasoning": "read only"}'
            ),
        ) as mock_llm:
            result = await service.evaluate(
                tool_name="get_issue",
                tool_args={"id": 1},
                workflow=workflow,
            )
        assert result.decision == "approve"
        assert result.confidence == 0.95
        mock_llm.assert_awaited_once()

    async def test_uses_default_model_when_unset(self, service):
        workflow = make_workflow()
        with patch.object(
            service,
            "_call_llm",
            new=AsyncMock(
                return_value='{"decision": "DENY", "confidence": 0.6, "reasoning": "no"}'
            ),
        ) as mock_llm:
            result = await service.evaluate("t", {}, workflow)
        assert result.model_used == DEFAULT_MODEL
        # model kwarg passed to _call_llm should be the default
        assert mock_llm.await_args.kwargs["model"] == DEFAULT_MODEL

    async def test_dedicated_columns_take_precedence(self, service):
        workflow = make_workflow(
            ai_model="claude-x",
            approval_config={"model": "gpt-ignored"},
        )
        with patch.object(
            service,
            "_call_llm",
            new=AsyncMock(
                return_value='{"decision": "APPROVE", "confidence": 0.5, "reasoning": "y"}'
            ),
        ) as mock_llm:
            result = await service.evaluate("t", {}, workflow)
        assert result.model_used == "claude-x"
        assert mock_llm.await_args.kwargs["model"] == "claude-x"

    async def test_timeout_returns_uncertain(self, service):
        workflow = make_workflow(approval_config={"timeout": 0.01})

        async def slow(*args, **kwargs):
            import asyncio

            await asyncio.sleep(1)
            return "never"

        with patch.object(service, "_call_llm", new=slow):
            result = await service.evaluate("t", {}, workflow)
        assert result.decision == "uncertain"
        assert result.confidence == 0.0
        assert "timed out" in result.reasoning.lower()

    async def test_exception_returns_uncertain(self, service):
        workflow = make_workflow()
        with patch.object(
            service,
            "_call_llm",
            new=AsyncMock(side_effect=RuntimeError("provider down")),
        ):
            result = await service.evaluate("t", {}, workflow)
        assert result.decision == "uncertain"
        assert "provider down" in result.reasoning


class TestSingletonAndTemplate:
    def test_get_prompt_template(self, service):
        assert service.get_prompt_template() == AI_APPROVAL_PROMPT

    def test_singleton_identity(self):
        a = get_ai_approval_service()
        b = get_ai_approval_service()
        assert a is b
        assert isinstance(a, AIApprovalService)

    def test_result_dataclass_defaults(self):
        r = AIApprovalResult(
            decision="approve",
            confidence=0.5,
            reasoning="r",
            model_used="m",
        )
        assert r.raw_response is None
