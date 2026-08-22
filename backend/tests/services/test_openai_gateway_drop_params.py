"""Gateway LiteLLM kwargs: drop unsupported params and identify as Preloop."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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
