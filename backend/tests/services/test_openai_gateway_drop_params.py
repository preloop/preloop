"""Gateway LiteLLM kwargs: drop unsupported params and identify as Preloop."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.secret_service import ResolvedModelCredentials


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


def _creds() -> ResolvedModelCredentials:
    return ResolvedModelCredentials(
        credential_type="api_key",
        backend_type="local",
        value="sk-test",
    )


def _build_kwargs(model: SimpleNamespace, payload: dict | None = None) -> dict:
    service = _service()
    with patch("preloop.services.openai_gateway.get_secret_service") as mock_secrets:
        mock_secrets.return_value.resolve_ai_model_credentials.return_value = _creds()
        return service._build_completion_kwargs(
            model,
            messages=[{"role": "user", "content": "hi"}],
            payload=payload or {},
            stream=False,
            provider="openai",
        )


def _assert_user_agent_is_preloop(headers: dict) -> None:
    user_agent = headers.get("User-Agent") or headers.get("user-agent") or ""
    assert user_agent, "User-Agent must be set on LiteLLM extra_headers"
    assert "litellm" not in user_agent.lower()
    assert user_agent.lower().startswith("preloop/")


def test_build_completion_kwargs_rejects_localhost_endpoint() -> None:
    """A saved localhost base is the container, so the gateway must not dial it."""
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    model = SimpleNamespace(
        provider_name="lmstudio",
        model_identifier="local-model",
        api_endpoint="http://localhost:1234/v1",
    )
    with pytest.raises(ModelGatewayAPIError) as error:
        _build_kwargs(model)
    assert error.value.status_code == 400


def test_build_completion_kwargs_sets_drop_params_for_zai():
    """z.ai rejects parallel_tool_calls; fallback metadata still forwards it."""
    model = SimpleNamespace(
        provider_name="zai",
        model_identifier="glm-5.3",
        api_endpoint="https://api.z.ai/api/paas/v4",
    )
    kwargs = _build_kwargs(model, payload={"parallel_tool_calls": True})
    assert kwargs["drop_params"] is True
    assert kwargs.get("api_key") == "sk-test"


def test_build_completion_kwargs_sets_drop_params_globally():
    """drop_params is the gateway default for every provider, not only zai."""
    model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
    )
    kwargs = _build_kwargs(model, payload={"parallel_tool_calls": True})
    assert kwargs["drop_params"] is True


def test_build_completion_kwargs_user_agent_is_preloop_for_zai():
    """Non-OpenRouter upstreams must not identify as LiteLLM."""
    model = SimpleNamespace(
        provider_name="zai",
        model_identifier="glm-5.3",
        api_endpoint="https://api.z.ai/api/paas/v4",
    )
    kwargs = _build_kwargs(model)
    extra = kwargs["extra_headers"]
    _assert_user_agent_is_preloop(extra)


def test_build_completion_kwargs_user_agent_is_preloop_for_openai():
    """OpenAI-native completions also replace LiteLLM's default User-Agent."""
    model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
    )
    kwargs = _build_kwargs(model)
    _assert_user_agent_is_preloop(kwargs["extra_headers"])


def test_build_completion_kwargs_user_agent_is_preloop_for_openrouter():
    """OpenRouter dashboards attribute by User-Agent / X-Title / HTTP-Referer."""
    model = SimpleNamespace(
        provider_name="openrouter",
        model_identifier="openrouter/auto-beta",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    kwargs = _build_kwargs(model)
    extra = kwargs["extra_headers"]
    _assert_user_agent_is_preloop(extra)
    assert extra["X-Title"] == "Preloop"
    assert extra["HTTP-Referer"] == "https://preloop.ai"


def test_openai_compatible_openrouter_base_url_gets_attribution_headers():
    model = SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="anthropic/claude-opus-4.6",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    kwargs = _build_kwargs(model)
    extra = kwargs["extra_headers"]
    _assert_user_agent_is_preloop(extra)
    assert extra["X-Title"] == "Preloop"
    assert extra["HTTP-Referer"] == "https://preloop.ai"


def test_openai_compatible_private_upstream_sets_api_base_and_ssl_verify(
    monkeypatch, tmp_path
):
    """A completion through Preloop targets the operator upstream and CA."""
    ca = tmp_path / "private-ca.crt"
    ca.write_text("dummy-ca")
    monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    model = SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="llama-3",
        api_endpoint="https://gateway.internal/v1",
    )
    kwargs = _build_kwargs(model)
    assert kwargs["api_base"] == "https://gateway.internal/v1"
    assert kwargs["model"] == "openai/llama-3"
    assert kwargs["ssl_verify"] == str(ca)
    assert kwargs["api_key"] == "sk-test"


def test_openai_compatible_skip_verify_last_resort(monkeypatch):
    monkeypatch.setenv("PRELOOP_SSL_VERIFY", "false")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/private-ca/ca.crt")
    model = SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="llama-3",
        api_endpoint="https://gateway.internal/v1",
    )
    kwargs = _build_kwargs(model)
    assert kwargs["ssl_verify"] is False
    assert kwargs["api_base"] == "https://gateway.internal/v1"


def test_openai_compatible_openrouter_completion_does_not_set_ssl_verify(
    monkeypatch, tmp_path
):
    """openai-compatible + OpenRouter api_endpoint keeps the default trust store."""
    ca = tmp_path / "private-ca.crt"
    ca.write_text("dummy-ca")
    monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    model = SimpleNamespace(
        provider_name="openai-compatible",
        model_identifier="anthropic/claude-opus-4.6",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    kwargs = _build_kwargs(model)
    assert kwargs["api_base"] == "https://openrouter.ai/api/v1"
    assert "ssl_verify" not in kwargs


def test_public_openai_completion_does_not_set_ssl_verify(monkeypatch, tmp_path):
    ca = tmp_path / "private-ca.crt"
    ca.write_text("dummy-ca")
    monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
    )
    kwargs = _build_kwargs(model)
    assert "ssl_verify" not in kwargs
    assert "api_base" not in kwargs


@pytest.mark.parametrize("provider", ["openrouter", "openai-compatible"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "cap_key", [None, "max_output_tokens", "max_completion_tokens", "max_tokens"]
)
def test_openrouter_responses_preserves_reasoning_and_output_cap(
    provider: str, stream: bool, cap_key: str | None
) -> None:
    """Responses controls survive translation, with client caps taking precedence."""
    model = SimpleNamespace(
        provider_name=provider,
        model_identifier="deepseek/deepseek-v4-flash-0731",
        api_endpoint="https://openrouter.ai/api/v1",
        model_parameters={"max_output_tokens": 8192},
        endpoint_type="direct",
    )
    payload = {"reasoning": {"effort": "low", "summary": "auto"}}
    if cap_key:
        payload[cap_key] = 1024
    with patch("preloop.services.openai_gateway.get_secret_service") as secrets:
        secrets.return_value.resolve_ai_model_credentials.return_value = _creds()
        kwargs = _service()._build_completion_kwargs(
            model, messages=[], payload=payload, stream=stream, provider="openai"
        )
    assert kwargs["extra_body"]["reasoning"] == {"effort": "low"}
    assert kwargs["max_tokens"] == (1024 if cap_key else 8192)
    assert kwargs.get("stream", False) is stream
    assert kwargs["extra_body"]["usage"] == {"include": True}


@pytest.mark.parametrize("provider", ["openrouter", "openai-compatible"])
@pytest.mark.parametrize("client_cap,expected_cap", [(None, 8192), (256, 256)])
def test_openrouter_controls_reach_litellm_http_body(
    provider: str, client_cap: int | None, expected_cap: int
) -> None:
    """The locked adapter must not drop reasoning for an unlisted model."""
    import json

    import httpx
    import litellm
    from litellm.llms.custom_httpx.http_handler import HTTPHandler

    bodies = []

    def respond(*, url: str, data: str, **_kwargs: object) -> httpx.Response:
        bodies.append(json.loads(data))
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "example/unlisted-reasoning-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    model = SimpleNamespace(
        provider_name=provider,
        model_identifier="example/unlisted-reasoning-model",
        api_endpoint="https://openrouter.ai/api/v1",
        model_parameters={"max_output_tokens": 8192},
    )
    payload = {"reasoning": {"effort": "low", "summary": "auto"}}
    if client_cap is not None:
        payload["max_output_tokens"] = client_cap
    kwargs = _build_kwargs(model, payload)
    with (
        patch.object(HTTPHandler, "post", side_effect=respond),
        patch.object(
            httpx.Client, "send", side_effect=AssertionError("Unexpected HTTP request")
        ),
    ):
        litellm.completion(**kwargs)
    assert len(bodies) == 1
    assert bodies[0]["reasoning"] == {"effort": "low"}
    assert bodies[0]["max_tokens"] == expected_cap
    assert bodies[0]["usage"] == {"include": True}


def test_openrouter_chat_effort_precedes_responses_effort() -> None:
    model = SimpleNamespace(
        provider_name="openrouter",
        model_identifier="example/model",
        api_endpoint=None,
    )
    kwargs = _build_kwargs(
        model, {"reasoning_effort": "high", "reasoning": {"effort": "low"}}
    )
    assert kwargs["extra_body"]["reasoning"] == {"effort": "high"}
    assert "max_tokens" not in kwargs


def test_openrouter_controls_do_not_leak_to_other_providers() -> None:
    model = SimpleNamespace(
        provider_name="openai",
        model_identifier="gpt-5",
        api_endpoint=None,
        model_parameters={"max_output_tokens": 8192},
    )
    kwargs = _build_kwargs(model, {"reasoning": {"effort": "low"}})
    assert "reasoning" not in kwargs.get("extra_body", {})
    assert "max_tokens" not in kwargs


@pytest.mark.parametrize(
    "default_key", ["max_completion_tokens", "max_output_tokens", "max_tokens"]
)
def test_openrouter_registered_output_cap_aliases(default_key: str) -> None:
    model = SimpleNamespace(
        provider_name="openrouter",
        model_identifier="example/model",
        api_endpoint=None,
        model_parameters={default_key: 8192},
    )
    assert _build_kwargs(model, {"max_output_tokens": None})["max_tokens"] == 8192


def test_openrouter_output_cap_alias_precedence() -> None:
    model = SimpleNamespace(
        provider_name="openrouter",
        model_identifier="example/model",
        api_endpoint=None,
        model_parameters={
            "max_completion_tokens": 8192,
            "max_output_tokens": 4096,
            "max_tokens": 2048,
        },
    )
    assert _build_kwargs(model)["max_tokens"] == 8192
    assert (
        _build_kwargs(
            model,
            {
                "max_completion_tokens": 1024,
                "max_output_tokens": 512,
                "max_tokens": 256,
            },
        )["max_tokens"]
        == 1024
    )


def test_native_responses_schema_is_forwarded_without_silent_drop() -> None:
    model = SimpleNamespace(
        provider_name="openrouter",
        model_identifier="deepseek/test",
        api_endpoint="https://openrouter.ai/api/v1",
    )
    schema = {
        "type": "object",
        "properties": {"status": {"type": "string"}},
        "required": ["status"],
        "additionalProperties": False,
    }
    native_format = {
        "type": "json_schema",
        "name": "review",
        "schema": schema,
        "strict": True,
    }
    kwargs = _build_kwargs(model, payload={"text": {"format": native_format}})
    assert kwargs["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "review", "schema": schema, "strict": True},
    }
    assert kwargs["drop_params"] is True


def test_schema_keeps_selective_dropping_for_unrelated_unsupported_flags() -> None:
    model = SimpleNamespace(
        provider_name="openai", model_identifier="example", api_endpoint=None
    )
    with patch(
        "preloop.services.openai_gateway.litellm.get_supported_openai_params",
        return_value=["response_format", "temperature"],
    ):
        kwargs = _build_kwargs(
            model,
            {
                "response_format": {"type": "json_object"},
                "parallel_tool_calls": True,
                "temperature": 0.1,
            },
        )
    assert kwargs["response_format"] == {"type": "json_object"}
    # drop_params stays on, so LiteLLM still drops unrelated unsupported flags.
    assert kwargs["drop_params"] is True
    assert kwargs["temperature"] == 0.1
    assert kwargs["messages"] and kwargs["model"]


def test_build_completion_kwargs_keeps_drop_params_for_zai_with_schema() -> None:
    """A schema never switches drop_params off for unrelated z.ai flags."""
    model = SimpleNamespace(
        provider_name="zai",
        model_identifier="glm-5.3",
        api_endpoint="https://api.z.ai/api/paas/v4",
    )
    with patch(
        "preloop.services.openai_gateway.litellm.get_supported_openai_params",
        return_value=["response_format", "tools"],
    ):
        kwargs = _build_kwargs(
            model,
            {"response_format": {"type": "json_object"}, "parallel_tool_calls": True},
        )
    assert kwargs["drop_params"] is True
    assert kwargs["response_format"] == {"type": "json_object"}


def test_zai_schema_rejects_unsupported_format_explicitly() -> None:
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    model = SimpleNamespace(
        provider_name="zai",
        model_identifier="glm-5.3",
        api_endpoint="https://api.z.ai/api/paas/v4",
    )
    with pytest.raises(ModelGatewayAPIError, match="structured response_format"):
        _build_kwargs(
            model,
            {"response_format": {"type": "json_object"}, "parallel_tool_calls": True},
        )
