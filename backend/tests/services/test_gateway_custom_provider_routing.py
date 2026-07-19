"""Gateway routing for custom (non-litellm) provider names.

Hermes configs can declare arbitrary provider names for OpenAI-compatible
endpoints (e.g. ``kimi-for-coding``). litellm rejects unknown provider
prefixes outright ("LLM Provider NOT provided") even when ``api_base`` is
set, which broke onboarding live-validation for tester-imported Hermes
models. Unknown providers with their own endpoint must route through
litellm's generic OpenAI-compatible adapter instead.
"""

from unittest.mock import MagicMock

from preloop.services.openai_gateway import (
    OpenAIGatewayService,
    _known_litellm_providers,
)


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
    assert "mistral" in _known_litellm_providers()
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("mistral", "mistral-large", "https://api.mistral.ai/v1")
        )
        == "mistral/mistral-large"
    )


def test_unknown_provider_with_endpoint_routes_openai_compatible():
    # The tester-reported case: Hermes custom provider kimi-for-coding/k3.
    assert "kimi-for-coding" not in _known_litellm_providers()
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
