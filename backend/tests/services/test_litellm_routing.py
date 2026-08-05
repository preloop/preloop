"""Tests for shared litellm model-string routing.

Focus: the three providers added for live discovery (moonshot, zai,
mistral) must route to litellm's native adapters, and the slash-prefix
heuristic must not regress issue #172 (a vendor prefix inside a model id is
not a routing instruction when the stored provider says otherwise).
"""

from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import (
    PROVIDER_PREFIX,
    known_litellm_providers,
    to_litellm_model,
)


def _model(provider: str, identifier: str, endpoint: str | None = None) -> AIModel:
    return AIModel(
        provider_name=provider,
        model_identifier=identifier,
        api_endpoint=endpoint,
    )


class TestNewProviderRouting:
    def test_moonshot_routes_to_litellm_moonshot(self):
        assert to_litellm_model(_model("moonshot", "kimi-k3")) == "moonshot/kimi-k3"

    def test_moonshot_k27_code(self):
        assert (
            to_litellm_model(_model("moonshot", "kimi-k2.7-code"))
            == "moonshot/kimi-k2.7-code"
        )

    def test_zai_routes_to_litellm_zai(self):
        assert to_litellm_model(_model("zai", "glm-5")) == "zai/glm-5"
        assert to_litellm_model(_model("zai", "glm-4.7")) == "zai/glm-4.7"

    def test_mistral_routes_to_litellm_mistral(self):
        assert (
            to_litellm_model(_model("mistral", "mistral-large-latest"))
            == "mistral/mistral-large-latest"
        )

    def test_prefix_map_pins_the_new_providers(self):
        """Routing must not depend on litellm's provider list contents."""
        assert PROVIDER_PREFIX["moonshot"] == "moonshot"
        assert PROVIDER_PREFIX["zai"] == "zai"
        assert PROVIDER_PREFIX["mistral"] == "mistral"

    def test_litellm_recognizes_the_new_provider_prefixes(self):
        """The pinned litellm must be able to route these provider names."""
        providers = known_litellm_providers()
        for name in ("moonshot", "zai", "mistral"):
            assert name in providers


class TestSlashPrefixHeuristicNoRegression:
    """Issue #172: the user's provider/endpoint choice outranks any vendor
    prefix inside the model id."""

    def test_already_prefixed_moonshot_id_passes_through(self):
        # An identifier that already carries the litellm prefix is used as-is
        # rather than double-prefixed.
        assert (
            to_litellm_model(_model("moonshot", "moonshot/kimi-k3"))
            == "moonshot/kimi-k3"
        )

    def test_openrouter_endpoint_still_wins_over_vendor_prefix(self):
        model = _model(
            "openrouter",
            "moonshotai/kimi-k3",
            endpoint="https://openrouter.ai/api/v1",
        )
        assert to_litellm_model(model) == "openrouter/moonshotai/kimi-k3"

    def test_openai_compatible_endpoint_wins_over_vendor_prefix(self):
        model = _model(
            "openai-compatible",
            "moonshot/kimi-k3",
            endpoint="https://my-gateway.example.com/v1",
        )
        assert to_litellm_model(model) == "openai/moonshot/kimi-k3"
