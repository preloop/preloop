"""Tests for shared litellm model-string routing.

Focus: the three providers added for live discovery (moonshot, zai,
mistral) must route to litellm's native adapters, and the slash-prefix
heuristic must not regress issue #172 (a vendor prefix inside a model id is
not a routing instruction when the stored provider says otherwise).
"""

import os
from unittest.mock import patch

from preloop.models.models.ai_model import AIModel
from preloop.services.litellm_routing import (
    PRELOOP_APP_NAME,
    PRELOOP_SITE_URL,
    PROVIDER_PREFIX,
    apply_preloop_client_headers,
    ensure_preloop_client_identity,
    known_litellm_providers,
    preloop_user_agent,
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


class TestPreloopClientIdentity:
    """LiteLLM client branding is Preloop on every upstream, not just OpenRouter."""

    def test_user_agent_never_contains_litellm(self):
        assert "litellm" not in preloop_user_agent().lower()
        assert preloop_user_agent().lower().startswith("preloop/")

    def test_apply_stamps_user_agent_on_openai(self):
        kwargs: dict = {}
        apply_preloop_client_headers(kwargs, _model("openai", "gpt-5"))
        ua = kwargs["extra_headers"]["User-Agent"]
        assert "litellm" not in ua.lower()
        assert ua.lower().startswith("preloop/")

    def test_apply_stamps_user_agent_on_zai(self):
        kwargs: dict = {}
        apply_preloop_client_headers(kwargs, _model("zai", "glm-5.3"))
        ua = kwargs["extra_headers"]["User-Agent"]
        assert "litellm" not in ua.lower()
        assert ua.lower().startswith("preloop/")

    def test_apply_stamps_user_agent_on_anthropic(self):
        kwargs: dict = {}
        apply_preloop_client_headers(kwargs, _model("anthropic", "claude-opus-4-6"))
        ua = kwargs["extra_headers"]["User-Agent"]
        assert "litellm" not in ua.lower()
        assert ua.lower().startswith("preloop/")

    def test_apply_preserves_existing_extra_headers(self):
        kwargs = {"extra_headers": {"anthropic-beta": "oauth-2025-04-20"}}
        apply_preloop_client_headers(kwargs, _model("anthropic", "claude-opus-4-6"))
        assert kwargs["extra_headers"]["anthropic-beta"] == "oauth-2025-04-20"
        assert "litellm" not in kwargs["extra_headers"]["User-Agent"].lower()

    def test_openrouter_also_gets_attribution_headers(self):
        kwargs: dict = {}
        apply_preloop_client_headers(
            kwargs,
            _model(
                "openrouter",
                "anthropic/claude-opus-4.6",
                endpoint="https://openrouter.ai/api/v1",
            ),
        )
        extra = kwargs["extra_headers"]
        assert extra["X-Title"] == PRELOOP_APP_NAME == "Preloop"
        assert extra["HTTP-Referer"] == PRELOOP_SITE_URL == "https://preloop.ai"
        assert "litellm" not in extra["User-Agent"].lower()

    def test_non_openrouter_does_not_require_xtitle(self):
        kwargs: dict = {}
        apply_preloop_client_headers(kwargs, _model("openai", "gpt-5"))
        extra = kwargs["extra_headers"]
        assert "User-Agent" in extra
        assert extra.get("X-Title") is None
        assert extra.get("HTTP-Referer") is None

    def test_ensure_identity_is_idempotent_and_skips_eval_when_set(self, monkeypatch):
        monkeypatch.setenv("LITELLM_USER_AGENT", "already-set")
        monkeypatch.setenv("OR_SITE_URL", "https://already.example")
        monkeypatch.setenv("OR_APP_NAME", "already")
        ensure_preloop_client_identity.cache_clear()

        with patch("preloop.services.litellm_routing.preloop_user_agent") as mock_ua:
            ensure_preloop_client_identity()
            ensure_preloop_client_identity()
            mock_ua.assert_not_called()

        assert os.environ["LITELLM_USER_AGENT"] == "already-set"
        assert os.environ["OR_SITE_URL"] == "https://already.example"
        assert os.environ["OR_APP_NAME"] == "already"

    def test_ensure_identity_sets_missing_env_once(self, monkeypatch):
        monkeypatch.delenv("LITELLM_USER_AGENT", raising=False)
        monkeypatch.delenv("OR_SITE_URL", raising=False)
        monkeypatch.delenv("OR_APP_NAME", raising=False)
        ensure_preloop_client_identity.cache_clear()

        ensure_preloop_client_identity()
        first_ua = os.environ["LITELLM_USER_AGENT"]
        assert "litellm" not in first_ua.lower()
        assert first_ua.lower().startswith("preloop/")
        assert os.environ["OR_SITE_URL"] == PRELOOP_SITE_URL
        assert os.environ["OR_APP_NAME"] == PRELOOP_APP_NAME

        monkeypatch.setenv("LITELLM_USER_AGENT", "should-not-replace")
        ensure_preloop_client_identity()
        assert os.environ["LITELLM_USER_AGENT"] == "should-not-replace"


class TestSslVerifyOnCompletions:
    """Private CA env vars must reach LiteLLM as ssl_verify."""

    def test_ssl_cert_file_sets_ssl_verify(self, monkeypatch, tmp_path):
        ca = tmp_path / "ca.crt"
        ca.write_text("dummy-ca")
        monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        kwargs: dict = {}
        apply_preloop_client_headers(
            kwargs,
            _model(
                "openai-compatible",
                "llama-3",
                endpoint="https://gateway.internal/v1",
            ),
        )
        assert kwargs["ssl_verify"] == str(ca)

    def test_no_override_leaves_ssl_verify_unset(self, monkeypatch):
        monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        kwargs: dict = {}
        apply_preloop_client_headers(kwargs, _model("openai", "gpt-5"))
        assert "ssl_verify" not in kwargs

    def test_explicit_ssl_verify_is_preserved(self, monkeypatch):
        monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/private-ca/ca.crt")
        kwargs = {"ssl_verify": True}
        apply_preloop_client_headers(kwargs, _model("openai", "gpt-5"))
        assert kwargs["ssl_verify"] is True
