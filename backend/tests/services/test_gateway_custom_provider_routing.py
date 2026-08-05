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


# --- issue #172: OpenRouter behind provider "openai-compatible" -------------
#
# Alex Lennon configured provider "openai-compatible" + endpoint
# https://openrouter.ai/api/v1 and got 93 upstream 502s: the vendor prefix in
# the model id ("deepseek/...") was read as a routing instruction, so litellm
# loaded its native DeepSeek adapter, stripped the prefix, and rewrote the
# api_base to api.deepseek.com. The stored provider/endpoint must win.

OPENROUTER = "https://openrouter.ai/api/v1"


def test_openrouter_endpoint_beats_vendor_prefix_in_model_id():
    # The exact configuration from issue #172. Must NOT resolve to deepseek.
    routed = OpenAIGatewayService._to_litellm_model(
        _model("openai-compatible", "deepseek/deepseek-v4-flash-0731", OPENROUTER)
    )
    assert routed == "openrouter/deepseek/deepseek-v4-flash-0731"
    assert not routed.startswith("deepseek/")


def test_openrouter_auto_router_keeps_its_first_party_namespace():
    # "openrouter/auto-beta" is an upstream model id in OpenRouter's own
    # namespace, not a prefixed model. litellm strips one "openrouter/" before
    # calling upstream, so the routed string needs both.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("openai-compatible", "openrouter/auto-beta", OPENROUTER)
        )
        == "openrouter/openrouter/auto-beta"
    )


def test_openrouter_prefixed_identifier_is_not_double_prefixed():
    # The manual workaround (prefixing the id with "openrouter/") stays valid,
    # so users who applied it are not broken by this fix.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model(
                "openai-compatible",
                "openrouter/deepseek/deepseek-v4-flash-0731",
                OPENROUTER,
            )
        )
        == "openrouter/deepseek/deepseek-v4-flash-0731"
    )


def test_openrouter_provider_name_routes_without_endpoint():
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("openrouter", "deepseek/deepseek-v4-flash-0731")
        )
        == "openrouter/deepseek/deepseek-v4-flash-0731"
    )


def test_openrouter_matched_on_host_not_substring():
    # A host that merely mentions openrouter.ai in its path or query must not
    # be routed to OpenRouter.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model(
                "openai-compatible",
                "deepseek/deepseek-v4-flash-0731",
                "https://proxy.example.com/openrouter.ai/api/v1",
            )
        )
        == "openai/deepseek/deepseek-v4-flash-0731"
    )


def test_openai_compatible_endpoint_beats_vendor_prefix():
    # Same precedence rule for any other OpenAI-compatible gateway: the id is
    # forwarded verbatim and api_base (passed by callers) is honored.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model(
                "openai-compatible",
                "deepseek/deepseek-chat",
                "https://gateway.example.com/v1",
            )
        )
        == "openai/deepseek/deepseek-chat"
    )


def test_custom_provider_with_endpoint_forwards_identifier_verbatim():
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("custom", "qwen/qwen3-coder", "https://gateway.example.com/v1")
        )
        == "openai/qwen/qwen3-coder"
    )


def test_openai_compatible_without_endpoint_keeps_prefix_inference():
    # No endpoint means nothing to honor, so the old heuristic still applies.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("openai-compatible", "deepseek/deepseek-chat")
        )
        == "deepseek/deepseek-chat"
    )


def test_native_deepseek_provider_is_unaffected():
    # Regression guard: a genuine DeepSeek model must still route to DeepSeek.
    assert (
        OpenAIGatewayService._to_litellm_model(
            _model("deepseek", "deepseek-chat", "https://api.deepseek.com/v1")
        )
        == "deepseek/deepseek-chat"
    )


@pytest.mark.parametrize(
    "identifier, expected_upstream_id",
    [
        # (a) concrete OpenRouter id, (b) the prefixed workaround form,
        # (c) the Auto Router. All three must reach OpenRouter with the id
        # OpenRouter itself expects.
        ("deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4-flash-0731"),
        (
            "openrouter/deepseek/deepseek-v4-flash-0731",
            "deepseek/deepseek-v4-flash-0731",
        ),
        ("openrouter/auto-beta", "openrouter/auto-beta"),
    ],
)
def test_litellm_resolves_openrouter_models_without_mangling(
    identifier, expected_upstream_id
):
    """What litellm does with our string, not just what our string looks like.

    The pre-fix bug was only visible one layer down: litellm resolved provider
    "deepseek" and sent a truncated model id. Assert on litellm's own
    resolution so a future prefix-map change cannot silently reintroduce it.
    """
    import litellm

    routed = OpenAIGatewayService._to_litellm_model(
        _model("openai-compatible", identifier, OPENROUTER)
    )
    model_sent, provider = litellm.get_llm_provider(routed)[:2]

    assert provider == "openrouter"
    assert model_sent == expected_upstream_id


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
