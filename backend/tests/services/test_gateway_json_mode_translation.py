"""Structured output on upstreams that only offer JSON mode (DeepSeek).

A hosted read-only Codex review with a result schema asks for
``text.format = json_schema``. DeepSeek's chat API rejects that with HTTP 400
although LiteLLM's catalog lists schema support, so the gateway translates
it to JSON mode with the schema stated in the instructions.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_MODEL_CONFIG,
    derive_failure_category,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import ResolvedModelCredentials

_SCHEMA = {
    "type": "object",
    "properties": {"verdict": {"enum": ["approve", "request-changes"]}},
    "required": ["verdict"],
}
_PAYLOAD = {
    "text": {
        "format": {
            "type": "json_schema",
            "name": "review_result",
            "schema": _SCHEMA,
            "strict": True,
        }
    }
}


def _build_kwargs(model: SimpleNamespace, payload: dict) -> tuple[dict, list]:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(
            token="token",
            user=SimpleNamespace(id="user-1", account_id="account-1"),
        ),
    )
    messages = [{"role": "user", "content": "review this diff"}]
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            ResolvedModelCredentials(
                credential_type="api_key", backend_type="local", value="sk-test"
            )
        )
        kwargs = service._build_completion_kwargs(
            model,
            messages=messages,
            payload=payload,
            stream=False,
            provider="openai",
        )
    return kwargs, messages


def test_deepseek_json_schema_is_sent_as_json_mode_with_schema_instruction():
    model = SimpleNamespace(
        provider_name="deepseek",
        model_identifier="deepseek-chat",
        api_endpoint=None,
    )
    kwargs, original = _build_kwargs(model, _PAYLOAD)

    assert kwargs["response_format"] == {"type": "json_object"}
    system = kwargs["messages"][0]
    assert system["role"] == "system"
    assert "JSON" in system["content"]
    assert '"request-changes"' in system["content"]
    assert kwargs["messages"][1:] == original
    assert len(original) == 1, "caller messages must not be mutated"


def test_deepseek_direct_endpoint_is_translated_too():
    model = SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="deepseek-v4-flash",
        api_endpoint="https://api.deepseek.com/v1",
    )
    kwargs, _ = _build_kwargs(model, _PAYLOAD)
    assert kwargs["response_format"] == {"type": "json_object"}


def test_openai_keeps_native_json_schema():
    model = SimpleNamespace(
        provider_name="openai", model_identifier="gpt-5", api_endpoint=None
    )
    kwargs, original = _build_kwargs(model, _PAYLOAD)
    assert kwargs["response_format"]["type"] == "json_schema"
    assert kwargs["messages"] == original


def test_deepseek_json_object_request_is_unchanged():
    model = SimpleNamespace(
        provider_name="deepseek", model_identifier="deepseek-chat", api_endpoint=None
    )
    kwargs, original = _build_kwargs(
        model, {"text": {"format": {"type": "json_object"}}}
    )
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"] == original


def test_unsupported_response_format_upstream_400_is_model_config():
    """The E1 shape: Codex surfaces the provider 400 as its last error line."""
    message = (
        "Codex exited with code 1: unexpected status 400 Bad Request: "
        '{"error":{"message":"This response_format type is unavailable now",'
        '"type":"invalid_request_error"}}'
    )
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        == FAILURE_CATEGORY_MODEL_CONFIG
    )


def test_gateway_structured_output_refusal_is_model_config():
    message = (
        "The selected provider/model does not support structured "
        "response_format; choose a model with structured output support"
    )
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        == FAILURE_CATEGORY_MODEL_CONFIG
    )


def test_non_object_response_format_is_a_400_not_a_crash():
    import pytest

    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    model = SimpleNamespace(
        provider_name="deepseek", model_identifier="deepseek-chat", api_endpoint=None
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        _build_kwargs(model, {"response_format": "json_object"})
    assert error.value.status_code == 400


def test_tool_400_is_not_a_model_config_verdict():
    message = "mcp tool fetch_url failed: HTTP 400 Bad Request from example.org"
    assert (
        derive_failure_category(status="FAILED", error_message=message)
        != FAILURE_CATEGORY_MODEL_CONFIG
    )
