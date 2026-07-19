"""Gateway routing for custom (non-litellm) provider names.

Hermes configs can declare arbitrary provider names for OpenAI-compatible
endpoints (e.g. ``kimi-for-coding``). litellm rejects unknown provider
prefixes outright ("LLM Provider NOT provided") even when ``api_base`` is
set, which broke onboarding live-validation for tester-imported Hermes
models. Unknown providers with their own endpoint must route through
litellm's generic OpenAI-compatible adapter instead.
"""

from unittest.mock import MagicMock

import pytest

from preloop.services.litellm_routing import known_litellm_providers
from preloop.services.openai_gateway import OpenAIGatewayService


def _model(provider: str, identifier: str, endpoint: str = "") -> MagicMock:
    model = MagicMock()
    model.provider_name = provider
    model.model_identifier = identifier
    model.api_endpoint = endpoint
    return model


def test_known_prefix_map_providers_keep_their_prefix():
    assert (
        OpenAIGatewayService._to_litellm_model(_model("anthropic", "claude-sonnet-4-5"))
        == "anthropic/claude-sonnet-4-5"
    )
    assert (
        OpenAIGatewayService._to_litellm_model(_model("google", "gemini-3.1-pro"))
        == "gemini/gemini-3.1-pro"
    )


def test_litellm_native_provider_passes_through():
    # mistral is not in the local prefix map but litellm routes it natively.
    assert "mistral" in known_litellm_providers()
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("mistral", "mistral-large", "https://api.mistral.ai/v1")
        )
        == "mistral/mistral-large"
    )


def test_unknown_provider_with_endpoint_routes_openai_compatible():
    # The tester-reported case: Hermes custom provider kimi-for-coding/k3.
    assert "kimi-for-coding" not in known_litellm_providers()
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("kimi-for-coding", "k3", "https://api.kimi.example/v1")
        )
        == "openai/k3"
    )


def test_unknown_provider_without_endpoint_keeps_name():
    # No endpoint to route to - keep the name so the litellm error stays
    # attributable instead of silently hitting api.openai.com.
    assert (
        OpenAIGatewayService._to_litellm_model(_model("kimi-for-coding", "k3"))
        == "kimi-for-coding/k3"
    )


def test_prefixed_identifier_passes_through():
    # Stored identifiers that already carry a routable prefix must not be
    # double-prefixed (pinned behavior of the pre-unification copies).
    assert (
        OpenAIGatewayService._to_litellm_model(_model("openai", "azure/gpt-5.4"))
        == "azure/gpt-5.4"
    )


def test_unknown_headed_identifier_still_gets_prefixed():
    # "meta-llama/llama-3" is a model path, not a litellm prefix - the
    # provider prefix must still be applied.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("openrouter", "meta-llama/llama-3.1-70b")
        )
        == "openrouter/meta-llama/llama-3.1-70b"
    )


@pytest.mark.asyncio
async def test_extract_agent_name_uses_shared_routing(mocker):
    # Regression: this endpoint lazily imported the removed _PROVIDER_PREFIX
    # from policy_generation, which module-level import checks never caught.
    from preloop.api.endpoints import account as account_endpoint

    model = _model("kimi-for-coding", "k3", "https://api.kimi.example/v1")
    model.api_key = None
    crud = mocker.patch.object(account_endpoint, "crud_ai_model")
    crud.get_default_active_model.return_value = model
    completion = mocker.patch("litellm.completion")
    completion.return_value.choices[0].message.content = "  AgentX  "

    response = await account_endpoint.extract_agent_name(
        request=account_endpoint.AgentNameExtractionRequest(
            identity_content="# I am AgentX"
        ),
        account=MagicMock(id="acc-1"),
        current_user=MagicMock(),
        db=MagicMock(),
    )

    assert response.name == "AgentX"
    kwargs = completion.call_args.kwargs
    assert kwargs["model"] == "openai/k3"
    assert kwargs["api_base"] == "https://api.kimi.example/v1"
