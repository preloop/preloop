"""Gateway compatibility for native reviews through model aggregators."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.deepseek_responses_reasoning import DeepSeekResponsesReasoning
from preloop.services.openai_gateway import (
    OpenAIGatewayService,
    _upstream_supports_json_object_only,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext


@pytest.mark.parametrize(
    "model",
    [
        "openrouter/deepseek/deepseek-v4-flash-0731",
        "openai/aggregator/deepseek/deepseek-v4-flash-0731",
    ],
)
def test_deepseek_family_behind_aggregator(model: str) -> None:
    assert _upstream_supports_json_object_only(
        {"model": model, "api_base": "https://example.com/v1"}
    )
    assert (
        DeepSeekResponsesReasoning.for_model(
            SimpleNamespace(
                id="m",
                provider_name="openrouter",
                model_identifier=model,
                api_endpoint="https://example.com/v1",
            ),
            "a",
        )
        is not None
    )


@pytest.mark.parametrize(
    "codex,tools,tool_result,deferred",
    [
        (True, True, False, True),
        (True, True, True, False),
        (False, True, False, False),
        (True, False, False, False),
    ],
)
def test_format_waits_for_codex_tool_result_only(
    codex: bool,
    tools: bool,
    tool_result: bool,
    deferred: bool,
) -> None:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(account_id="a")
        ),
        client_identity_headers={
            "User-Agent": "codex_exec/0.153.4" if codex else "example-sdk/1"
        },
    )
    model = SimpleNamespace(
        provider_name="openai", model_identifier="gpt-5", api_endpoint=None
    )
    messages = [{"role": "user", "content": "Review the change."}]
    if tool_result:
        messages.append({"role": "tool", "tool_call_id": "c", "content": "diff output"})
    payload = {"response_format": {"type": "json_object"}}
    if tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": "exec_command",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ]
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="synthetic")
        )
        kwargs = service._build_completion_kwargs(
            model, messages=messages, payload=payload, stream=True, provider="openai"
        )
    assert ("response_format" not in kwargs) is deferred
    assert payload["response_format"] == {"type": "json_object"}


@pytest.mark.parametrize(
    "extra", [{"tool_choice": "none"}, {"previous_response_id": "response-1"}]
)
def test_explicit_no_tools_and_server_history_keep_format(extra: dict) -> None:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="synthetic", user=SimpleNamespace(account_id="a")
        ),
        client_identity_headers={"User-Agent": "codex_exec/0.153.4"},
    )
    payload = {
        "tools": [{"type": "function", "name": "read"}],
        "text": {"format": {"type": "json_object"}},
        **extra,
    }
    assert service._defer_codex_response_format(payload, []) == payload


@pytest.mark.parametrize(
    "model",
    [
        "openrouter/other-deepseek/model",
        "openai/gpt-5",
        "openrouter/example/deepseekish",
    ],
)
def test_unrelated_model_families_keep_schema(model: str) -> None:
    assert not _upstream_supports_json_object_only({"model": model})
