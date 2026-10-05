"""Responses -> litellm translation failures are gateway bugs, not upstream 502s.

Prod 2026-10-05: Codex on ``/openai/v1/responses`` against a self-hosted
qwen2.5-coder got 38 consecutive 502 ``upstream_error`` responses with
``unhashable type: 'dict'`` raised inside litellm's ``get_optional_params``
(the Ollama mapping does ``reasoning_effort in {"low", "medium", "high"}``).
The provider never received a request, yet alerts blamed it.
"""

import json
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from litellm.utils import get_optional_params

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.upstream_errors import (
    ERROR_CLASS_GATEWAY_TRANSLATION,
    is_gateway_translation_error,
)

FIXTURE = (
    Path(__file__).parents[1] / "fixtures/openai_gateway/codex_responses_request.json"
)
CODEX_REASONING = {"effort": "medium", "summary": "auto"}


def codex_payload() -> dict[str, Any]:
    """A Codex CLI-shaped streaming Responses request (real captured tools)."""
    return {
        "model": "openai-compatible/qwen2.5-coder",
        "instructions": "You are Codex.",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "list files"}],
            }
        ],
        "tools": json.loads(FIXTURE.read_text())["tools"],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": dict(CODEX_REASONING),
        "text": {"verbosity": "medium"},
        "include": ["reasoning.encrypted_content"],
        "store": False,
        "stream": True,
        "prompt_cache_key": "0199-synthetic",
    }


def _service() -> tuple[OpenAIGatewayService, SimpleNamespace]:
    model = SimpleNamespace(
        id="model-1",
        account_id="account-1",
        provider_name="openai-compatible",
        model_identifier="qwen2.5-coder",
        api_endpoint="http://qwen.internal.test/v1",
        meta_data={},
    )
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(id="user-1", account_id="a")
        ),
    )
    return service, model


def _run_stream(completion: Any) -> tuple[Any, MagicMock, MagicMock]:
    service, model = _service()
    credentials = SimpleNamespace(credential_type="api_key", value="sk-synthetic")
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(service, "_resolve_requested_model", return_value=model)
        )
        for name in (
            "_check_budget",
            "_emit_gateway_request_started",
            "_defer_stream_record",
            "_finish_stream_generator",
        ):
            stack.enter_context(patch.object(service, name, return_value=None))
        record = stack.enter_context(patch.object(service, "_record_gateway_request"))
        stack.enter_context(
            patch(
                "preloop.services.openai_gateway.should_use_responses_passthrough",
                return_value=False,
            )
        )
        stack.enter_context(
            patch(
                "preloop.services.openai_gateway.get_secret_service",
                return_value=SimpleNamespace(
                    resolve_ai_model_credentials=lambda *a, **k: credentials
                ),
            )
        )
        stack.enter_context(
            patch(
                "preloop.services.openai_gateway._sleep_before_upstream_retry",
                return_value=None,
            )
        )
        alert = stack.enter_context(
            patch(
                "preloop.services.openai_gateway.reserve_gateway_5xx_alert",
                return_value=(False, 0),
            )
        )
        litellm_completion = stack.enter_context(
            patch(
                "preloop.services.openai_gateway.litellm.completion",
                side_effect=completion,
            )
        )
        with pytest.raises(ModelGatewayAPIError) as excinfo:
            list(service.stream_response(codex_payload()))
    record.error = excinfo.value
    return excinfo.value, record, (alert, litellm_completion)


def _ollama_mapping_crash(**kwargs: Any) -> Any:
    """Run litellm's real param mapping the way the prod route did."""
    return get_optional_params(
        model="qwen2.5-coder",
        custom_llm_provider="ollama",
        drop_params=True,
        reasoning_effort=CODEX_REASONING,
    )


def test_litellm_ollama_mapping_raises_unhashable_dict() -> None:
    """Pin the litellm behaviour the prod incident hit."""
    with pytest.raises(TypeError, match="unhashable type: 'dict'"):
        _ollama_mapping_crash()


def test_translation_typeerror_is_gateway_translation_error_not_upstream_502() -> None:
    error, record, (alert, litellm_completion) = _run_stream(_ollama_mapping_crash)

    assert error.status_code == 500
    assert error.code == ERROR_CLASS_GATEWAY_TRANSLATION
    assert error.error_class == ERROR_CLASS_GATEWAY_TRANSLATION
    assert error.terminal is True
    assert "unhashable type: 'dict'" in error.message
    # Deterministic: one attempt, and no provider-blaming admin alert.
    assert litellm_completion.call_count == 1
    alert.assert_not_called()
    recorded = record.call_args.kwargs
    assert recorded["status_code"] == 500
    assert recorded["error_class"] == ERROR_CLASS_GATEWAY_TRANSLATION
    assert (
        OpenAIGatewayService._audit_error_type(
            500, recorded["error_detail"], recorded["error_class"]
        )
        == ERROR_CLASS_GATEWAY_TRANSLATION
    )


def test_codex_payload_translation_hands_litellm_only_hashable_scalars() -> None:
    """The Codex dict fields (reasoning/text/include) never reach litellm params."""
    captured: dict[str, Any] = {}

    def capture(**kwargs: Any) -> Any:
        captured.update(kwargs)
        raise TypeError("stop after capture")

    _run_stream(capture)

    for dropped in ("reasoning", "text", "include", "store", "prompt_cache_key"):
        assert dropped not in captured
    assert "reasoning_effort" not in captured
    assert captured["tool_choice"] == "auto"
    assert all(tool["type"] == "function" for tool in captured["tools"])
    names = {tool["function"]["name"] for tool in captured["tools"]}
    assert "web_search" not in names
    # Real litellm param mapping accepts the translated kwargs on both the
    # generic OpenAI-compatible route and the Ollama route.
    params = {
        key: captured[key]
        for key in ("tools", "tool_choice", "parallel_tool_calls", "stream")
    }
    for provider in ("openai", "ollama", "ollama_chat", "hosted_vllm"):
        get_optional_params(
            model="qwen2.5-coder",
            custom_llm_provider=provider,
            drop_params=True,
            **params,
        )


def test_is_gateway_translation_error_does_not_swallow_upstream_errors() -> None:
    import litellm

    assert is_gateway_translation_error(TypeError("unhashable type: 'dict'"))
    assert not is_gateway_translation_error(json.JSONDecodeError("bad", "x", 0))
    assert not is_gateway_translation_error(ValueError("unrelated"))
    assert not is_gateway_translation_error(
        litellm.BadRequestError(message="bad", model="m", llm_provider="openai")
    )
    assert not is_gateway_translation_error(RuntimeError("boom"))


def test_valueerror_inside_get_optional_params_is_gateway_translation_error() -> None:
    """ValueError counts when the traceback passes through param mapping."""
    with patch(
        "litellm.utils.pre_process_non_default_params",
        side_effect=ValueError("reasoning_effort must be a string"),
    ):
        with pytest.raises(ValueError) as caught:
            get_optional_params(
                model="qwen2.5-coder",
                custom_llm_provider="ollama",
            )
    assert is_gateway_translation_error(caught.value) is True


def test_translation_error_scrubs_secrets_from_the_client_body() -> None:
    """Exception text that looks like a key must not reach the client."""
    secret = "sk-testsecret1234567890extra"

    def crash(**_kwargs: Any) -> Any:
        raise TypeError(f"unhashable type: 'dict' {secret}")

    error, record, _rest = _run_stream(crash)
    body = error.to_payload()["error"]["message"]
    detail = record.call_args.kwargs["error_detail"]
    assert secret not in error.message
    assert secret not in body
    assert secret not in detail
    assert "sk-[REDACTED]" in body
    assert "unhashable type: 'dict'" in body
