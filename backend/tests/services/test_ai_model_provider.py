"""Tests for AI model provider service."""

import logging
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.services.ai_model_provider import (
    get_available_models_for_provider,
    QWEN_DEFAULT_BASE_URL,
    QWEN_INTL_BASE_URL,
    QWEN_US_BASE_URL,
    _is_qwen_chat_model,
    ERROR_EMPTY_RESPONSE,
    ERROR_MISSING_KEY,
    ERROR_SDK_MISSING,
    ERROR_UNKNOWN,
    FALLBACK_ERROR_REASONS,
    MODEL_DISCOVERY_TIMEOUT_SECONDS,
    ModelDiscoveryResult,
    ProviderAuthError,
    ProviderValidationError,
    _get_openai_models,
    _get_anthropic_models,
    _get_google_models,
    _get_qwen_models,
    _get_deepseek_models,
    _get_moonshot_models,
    _get_zai_models,
    _get_mistral_models,
)


def _models_response(*model_ids):
    """Build an OpenAI-shaped ``models.list()`` response."""
    response = MagicMock()
    response.data = []
    for model_id in model_ids:
        entry = MagicMock()
        entry.id = model_id
        response.data.append(entry)
    return response


class TestDiscoveryResultShape:
    """The provenance contract: {models, source, error} with a safe error."""

    def test_error_must_come_from_the_fixed_vocabulary(self):
        with pytest.raises(ValueError):
            ModelDiscoveryResult(models=[], source="fallback", error="boom: sk-123")

    def test_clean_fallback_has_no_error(self):
        result = ModelDiscoveryResult(models=["m"], source="fallback")
        assert result.error is None

    def test_vocabulary_is_short_and_url_free(self):
        for reason in FALLBACK_ERROR_REASONS:
            assert "://" not in reason
            assert len(reason) <= 32


class TestGetAvailableModelsForProvider:
    """Test get_available_models_for_provider function."""

    @pytest.mark.asyncio
    async def test_get_openai_models(self):
        """Test routing to OpenAI provider."""
        with patch("preloop.services.ai_model_provider._get_openai_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["gpt-5.4", "gpt-5.4-mini"], source="live"
            )
            result = await get_available_models_for_provider("openai", "test_key")
            assert result.models == ["gpt-5.4", "gpt-5.4-mini"]
            assert result.source == "live"
            mock_get.assert_called_once_with("test_key", model_kind="llm")

    @pytest.mark.asyncio
    async def test_get_openai_stt_models(self):
        """OpenAI STT ids come from the live GET /v1/models list, filtered."""
        with patch("preloop.services.ai_model_provider._get_openai_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["gpt-4o-transcribe", "whisper-1"], source="live"
            )
            result = await get_available_models_for_provider(
                "openai", "test_key", model_kind="stt"
            )
            assert result.models == ["gpt-4o-transcribe", "whisper-1"]
            assert result.source == "live"
            mock_get.assert_called_once_with("test_key", model_kind="stt")

    @pytest.mark.asyncio
    async def test_get_openai_tts_models(self):
        """OpenAI TTS ids come from the live GET /v1/models list, filtered."""
        with patch("preloop.services.ai_model_provider._get_openai_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["gpt-4o-mini-tts", "tts-1"], source="live"
            )
            result = await get_available_models_for_provider(
                "openai", "test_key", model_kind="tts"
            )
            assert result.models == ["gpt-4o-mini-tts", "tts-1"]
            assert result.source == "live"
            mock_get.assert_called_once_with("test_key", model_kind="tts")

    @pytest.mark.asyncio
    async def test_audio_models_only_for_supported_providers(self):
        """Unsupported audio kinds return empty with unsupported, not a catalog."""
        stt = await get_available_models_for_provider(
            "google", "test_key", model_kind="stt"
        )
        assert stt.models == []
        assert stt.source == "fallback"
        assert stt.error == "missing_endpoint"
        tts = await get_available_models_for_provider(
            "google", "test_key", model_kind="tts"
        )
        assert tts.models == []
        assert tts.source == "fallback"
        assert tts.error == "unsupported"

    @pytest.mark.asyncio
    async def test_no_key_returns_empty_missing_key_not_a_catalog(self):
        """Edit without id and without a typed key must not guess a catalog."""
        result = await get_available_models_for_provider("zai")
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_get_anthropic_models(self):
        """Test routing to Anthropic provider."""
        with patch(
            "preloop.services.ai_model_provider._get_anthropic_models"
        ) as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["claude-sonnet-5"], source="live"
            )
            result = await get_available_models_for_provider("anthropic", "test_key")
            assert result.models == ["claude-sonnet-5"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_google_models(self):
        """Test routing to Google provider."""
        with patch("preloop.services.ai_model_provider._get_google_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["gemini-2.5-pro"], source="live"
            )
            result = await get_available_models_for_provider("google", "test_key")
            assert result.models == ["gemini-2.5-pro"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_qwen_models(self):
        """Test routing to Qwen provider."""
        with patch("preloop.services.ai_model_provider._get_qwen_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["qwen-plus", "qwen-turbo"], source="live"
            )
            result = await get_available_models_for_provider("qwen", "test_key")
            assert result.models == ["qwen-plus", "qwen-turbo"]
            mock_get.assert_called_once_with("test_key", None)

    @pytest.mark.asyncio
    async def test_get_qwen_models_forwards_endpoint(self):
        """Intl / US Model Studio hosts must reach the Qwen lister."""
        with patch("preloop.services.ai_model_provider._get_qwen_models") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["qwen3.8-max"], source="live"
            )
            result = await get_available_models_for_provider(
                "qwen",
                "test_key",
                api_endpoint=QWEN_INTL_BASE_URL,
            )
            assert result.models == ["qwen3.8-max"]
            mock_get.assert_called_once_with("test_key", QWEN_INTL_BASE_URL)

    @pytest.mark.asyncio
    async def test_get_deepseek_models(self):
        """Test routing to DeepSeek provider."""
        with patch(
            "preloop.services.ai_model_provider._get_deepseek_models"
        ) as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["deepseek-chat", "deepseek-reasoner"], source="live"
            )
            result = await get_available_models_for_provider("deepseek", "test_key")
            assert result.models == ["deepseek-chat", "deepseek-reasoner"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "provider,helper",
        [
            ("moonshot", "_get_moonshot_models"),
            ("zai", "_get_zai_models"),
            ("mistral", "_get_mistral_models"),
        ],
    )
    async def test_new_providers_are_dispatched(self, provider, helper):
        """moonshot, zai and mistral route to their own helpers."""
        with patch(f"preloop.services.ai_model_provider.{helper}") as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["some-model"], source="live"
            )
            result = await get_available_models_for_provider(provider, "test_key")
            assert result.models == ["some-model"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["moonshot", "zai", "mistral"])
    async def test_new_providers_are_llm_only(self, provider):
        """The new providers offer no STT/TTS catalogs."""
        for kind in ("stt", "tts"):
            result = await get_available_models_for_provider(
                provider, "test_key", model_kind=kind
            )
            assert result.models == []
            assert result.error == "unsupported"

    @pytest.mark.asyncio
    async def test_get_custom_provider_returns_empty(self):
        """Unknown providers return an empty fallback with a reason."""
        result = await get_available_models_for_provider("custom_provider")
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "unsupported"


class TestGetOpenAIModels:
    """Test _get_openai_models function."""

    @pytest.mark.asyncio
    async def test_get_openai_models_success(self):
        """A live listing is returned uncapped with provenance live."""
        mock_response = _models_response(
            "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-codex", "gpt-5.4-instruct"
        )

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert result.source == "live"
            assert "gpt-5.4" in result.models
            assert "gpt-5.4-mini" in result.models
            assert "gpt-5.4-codex" in result.models
            assert "gpt-5.4-instruct" not in result.models  # Filtered out

    @pytest.mark.asyncio
    async def test_get_openai_models_no_longer_capped_at_ten(self):
        """Regression: the old [:10] cap hid models below the first ten ids."""
        ids = [f"gpt-5.4-variant-{i:02d}" for i in range(15)]
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response(*ids))
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert len(result.models) == 15

    @pytest.mark.asyncio
    async def test_get_openai_models_includes_o_series(self):
        """Regression: the old gpt-* allow-list hid o-series reasoning models."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("o3", "o4-mini", "gpt-5.4")
            )
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert "o3" in result.models
            assert "o4-mini" in result.models
            assert "gpt-5.4" in result.models

    @pytest.mark.asyncio
    async def test_get_openai_models_filters_non_chat_models(self):
        """Test that non-chat models are filtered."""
        mock_response = _models_response(
            "gpt-5.4",
            "gpt-5.4-audio-preview",  # Should be filtered
            "gpt-5.4-codex",
            "text-similarity-ada-001",  # Should be filtered
            "text-search-ada-doc-001",  # Should be filtered
            "text-embedding-3-large",  # Should be filtered
            "whisper-1",  # Should be filtered
            "dall-e-3",  # Should be filtered
            "gpt-5.4-mini",
        )

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert "gpt-5.4" in result.models
            assert "gpt-5.4-mini" in result.models
            assert "gpt-5.4-codex" in result.models
            assert "text-similarity-ada-001" not in result.models
            assert "text-embedding-3-large" not in result.models
            assert "whisper-1" not in result.models
            assert "dall-e-3" not in result.models

    @pytest.mark.asyncio
    async def test_get_openai_stt_models_lists_live_audio_ids(self):
        """STT uses GET https://api.openai.com/v1/models, filtered to audio."""
        mock_response = _models_response(
            "gpt-5.4",
            "whisper-1",
            "gpt-4o-transcribe",
            "tts-1",
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key", model_kind="stt")

        assert result.source == "live"
        assert "whisper-1" in result.models
        assert "gpt-4o-transcribe" in result.models
        assert "gpt-5.4" not in result.models
        assert "tts-1" not in result.models

    @pytest.mark.asyncio
    async def test_get_openai_tts_models_lists_live_audio_ids(self):
        mock_response = _models_response("gpt-5.4", "whisper-1", "tts-1", "tts-1-hd")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key", model_kind="tts")

        assert result.source == "live"
        assert set(result.models) == {"tts-1", "tts-1-hd"}

    @pytest.mark.asyncio
    async def test_get_openai_models_authentication_error(self):
        """Test handling of authentication errors."""
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key",
                    response=MagicMock(),
                    body=None,
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid OpenAI API key"):
                await _get_openai_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_openai_models_network_error_returns_fallback(self):
        """Network errors return the named fallback with provenance."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("Network error")
            )
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS

    @pytest.mark.asyncio
    async def test_get_openai_models_without_api_key(self):
        """Test retrieval without providing API key."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("gpt-5.4")
            )
            mock_client.return_value = mock_instance

            result = await _get_openai_models(None)

            mock_client.assert_not_called()
            assert result.models == []
            assert result.source == "fallback"
            assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    @pytest.mark.parametrize("api_key", ["test_key"])
    async def test_get_openai_models_client_is_bounded(self, api_key):
        """The client must carry the shared discovery timeout and one retry.

        Without them a hung upstream blocks the picker request forever; every
        other provider in this module already bounds its client this way.
        """
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("gpt-5.4")
            )
            mock_client.return_value = mock_instance

            await _get_openai_models(api_key)

            kwargs = mock_client.call_args.kwargs
            assert kwargs["timeout"] == MODEL_DISCOVERY_TIMEOUT_SECONDS
            assert kwargs["max_retries"] == 1

    @pytest.mark.asyncio
    async def test_get_openai_models_missing_sdk_returns_fallback(self):
        """A missing openai package is a named fallback, not an ImportError."""
        with patch.dict(sys.modules, {"openai": None}):
            result = await _get_openai_models("test_key")

        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "sdk_missing"


class TestGetAnthropicModels:
    """Anthropic switched from a paid messages.create ping to models.list."""

    @pytest.mark.asyncio
    async def test_get_anthropic_models_without_key(self):
        """No key: empty list with missing_key, not a catalog."""
        result = await _get_anthropic_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_valid_key_lists_models_live(self):
        """A valid key returns the live listing, not the hardcoded catalog."""
        page = _models_response("claude-sonnet-5", "claude-brand-new-model")
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=page)
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("valid_key")

            assert result.source == "live"
            assert result.models == ["claude-brand-new-model", "claude-sonnet-5"]
            mock_instance.models.list.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_paid_validation_ping_is_made(self):
        """The old messages.create validation ping cost money on every fetch."""
        page = _models_response("claude-sonnet-5")
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=page)
            mock_instance.messages.create = AsyncMock(
                side_effect=AssertionError("paid messages.create ping was called")
            )
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("valid_key")

            assert result.source == "live"
            mock_instance.messages.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_anthropic_models_authentication_error(self):
        """Auth failures on models.list still raise the same ValueError."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("401 unauthorized")
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid Anthropic API key"):
                await _get_anthropic_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_anthropic_models_url_with_api_key_param_is_not_auth(self):
        """A URL carrying an api_key query param must not be read as an auth failure.

        Regression: "api_key" was a substring keyword, so any connection error
        whose text embedded such a URL was misreported as an invalid key.
        """
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception(
                    "Connection refused: https://proxy.internal/v1/models?api_key=redacted"
                )
            )
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("valid_key")

        assert result.source == "fallback"
        assert result.error == "unknown"

    @pytest.mark.asyncio
    async def test_get_anthropic_models_import_error(self):
        """Missing SDK: fallback catalog with an sdk_missing reason."""
        anthropic_module = sys.modules.pop("anthropic", None)
        try:
            import builtins

            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "anthropic":
                    raise ImportError("No module named 'anthropic'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                result = await _get_anthropic_models("test_key")
                assert result.models == []
                assert result.source == "fallback"
                assert result.error == "sdk_missing"
        finally:
            if anthropic_module is not None:
                sys.modules["anthropic"] = anthropic_module

    @pytest.mark.asyncio
    async def test_get_anthropic_models_network_error(self):
        """Non-auth failures fall back with a reason instead of raising."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("Network timeout")
            )
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("test_key")
            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS

    @pytest.mark.asyncio
    async def test_empty_listing_falls_back(self):
        """An empty page falls back rather than emptying the picker."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("valid_key")
            assert result.models == []
            assert result.error == "empty_response"

    def test_fallback_ids_are_priced_in_the_bundled_table(self):
        """Pricing coverage lives in test_model_pricing, not a picker catalog."""
        import json

        from preloop.services.model_price_catalog import CATALOG_PATH

        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in (
            "claude-opus-4-8",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
        ):
            assert model_id in prices, f"{model_id} missing from model_prices.json"


class TestGetGoogleModels:
    """Test _get_google_models function."""

    @pytest.mark.asyncio
    async def test_get_google_models_without_key(self):
        """No key: empty list with missing_key, not a catalog."""
        result = await _get_google_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_get_google_models_with_valid_key_lists_live(self):
        """A valid key returns the live listing with provenance live."""
        mock_google = MagicMock()
        mock_genai = MagicMock()

        live_entry = MagicMock()
        live_entry.name = "models/gemini-3.0-pro"
        live_entry.supported_generation_methods = ["generateContent"]
        mock_genai.list_models = MagicMock(return_value=[live_entry])
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            result = await _get_google_models("valid_key")

            assert result.source == "live"
            assert result.models == ["gemini-3.0-pro"]
            # Under a fully mocked google package the key-scoped client cannot
            # be constructed, so this exercises the documented fallback path:
            # process-global configure(). The scoped path (which must NOT call
            # configure) is covered by the real-SDK tests below.
            mock_genai.configure.assert_called_once_with(api_key="valid_key")
            # No paid generate_content validation ping.
            mock_genai.GenerativeModel.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_google_models_empty_listing_is_reported_fallback(self):
        """A fetch that produces nothing is a REPORTED fallback, not a silent
        return of known_models (the old behavior)."""
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_genai.list_models = MagicMock(return_value=[])
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            result = await _get_google_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == "empty_response"
            # And no paid generate_content ping was fired to "validate".
            mock_genai.GenerativeModel.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_google_models_authentication_error(self):
        """Auth failures during listing raise the ValueError."""
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_genai.list_models = MagicMock(
            side_effect=Exception("403 permission denied")
        )
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            with pytest.raises(ValueError, match="Invalid Google API key"):
                await _get_google_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_google_models_url_with_api_key_param_is_not_auth(self):
        """A URL carrying an api_key query param must not be read as an auth failure."""
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_genai.list_models = MagicMock(
            side_effect=Exception(
                "Connection refused: https://proxy.internal/v1beta/models?api_key=redacted"
            )
        )
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            result = await _get_google_models("valid_key")

        assert result.source == "fallback"

    @pytest.mark.asyncio
    async def test_get_google_models_import_error(self):
        """Test handling when Google package not installed."""
        genai_module = sys.modules.pop("google.generativeai", None)
        try:
            import builtins

            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "google.generativeai":
                    raise ImportError("No module named 'google.generativeai'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                result = await _get_google_models("test_key")
                assert result.models == []
                assert result.error == "sdk_missing"
        finally:
            if genai_module is not None:
                sys.modules["google.generativeai"] = genai_module

    @pytest.mark.asyncio
    async def test_get_google_models_network_error(self):
        """Non-auth failures fall back with a reason."""
        mock_genai = MagicMock()
        mock_genai.list_models = MagicMock(side_effect=Exception("Connection reset"))

        with patch.dict(sys.modules, {"google.generativeai": mock_genai}):
            result = await _get_google_models("test_key")
            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS

    @pytest.mark.asyncio
    async def test_scoped_google_client_isolates_keys(self):
        """The key must live on a per-call client, not in global SDK state.

        genai.configure() mutates PROCESS-GLOBAL state, so two concurrent
        discovery requests for different accounts could race and list one
        account's models with the other's key. These assertions run against
        the REAL SDK: a fully mocked google package makes the scoped client
        unconstructible, which is why the mocked tests above cannot cover it.
        """
        from preloop.services.ai_model_provider import _build_scoped_google_client

        first = _build_scoped_google_client("AIza-key-one")
        second = _build_scoped_google_client("AIza-key-two")
        if first is None or second is None:
            pytest.skip("google.ai.generativelanguage not available")

        assert first is not second
        assert first.transport._credentials.token == "AIza-key-one"
        assert second.transport._credentials.token == "AIza-key-two"

    @pytest.mark.asyncio
    async def test_scoped_path_does_not_touch_global_configure_mocked(self):
        """Regression: the scoped path must never call genai.configure().

        This is the SDK-free twin of the real-SDK test below. The google
        packages are optional extras (``ai-providers``) and are not installed
        in CI, so the real-SDK test skips there and this one carries the
        assertion instead.

        The round-3 trap this avoids: mocking the whole google package makes
        _build_scoped_google_client() fail and return None, which silently
        routes the call to the FALLBACK branch, so a naive mocked test asserts
        nothing about the scoped branch. Here the builder itself is patched to
        hand back a sentinel, which forces the scoped branch to be taken.
        """
        from preloop.services import ai_model_provider as module

        sentinel_client = object()
        mock_google = MagicMock()
        mock_genai = MagicMock()

        captured = {}

        def fake_list_models(client=None):
            captured["client"] = client
            entry = MagicMock()
            entry.name = "models/gemini-2.5-pro"
            entry.supported_generation_methods = ["generateContent"]
            return [entry]

        mock_genai.list_models = MagicMock(side_effect=fake_list_models)
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            with patch.object(
                module, "_build_scoped_google_client", return_value=sentinel_client
            ):
                result = await _get_google_models("AIza-scoped-key")

        assert result.source == "live"
        assert result.models == ["gemini-2.5-pro"]
        # The whole point: global state was never mutated.
        mock_genai.configure.assert_not_called()
        # ...and the key-scoped client travelled on the call instead.
        assert captured["client"] is sentinel_client

    @pytest.mark.asyncio
    async def test_live_listing_does_not_touch_global_configure(self):
        """Regression: the scoped path must never call genai.configure().

        Runs against the REAL SDK, which additionally proves the key rides on
        the client's credentials. The google packages are optional extras and
        are absent in CI, hence importorskip; the assertion still runs in CI
        in mocked form via the test above.
        """
        real_genai = pytest.importorskip(
            "google.generativeai",
            reason="google-generativeai extra not installed",
        )
        pytest.importorskip(
            "google.ai.generativelanguage",
            reason="google-generativeai extra not installed",
        )
        from preloop.services import ai_model_provider as module

        if module._build_scoped_google_client("AIza-probe") is None:
            pytest.skip("google.ai.generativelanguage not available")

        configure_calls = []
        captured = {}

        def fake_list_models(client=None):
            captured["client"] = client
            entry = MagicMock()
            entry.name = "models/gemini-2.5-pro"
            entry.supported_generation_methods = ["generateContent"]
            return [entry]

        with (
            patch.object(
                real_genai,
                "configure",
                side_effect=lambda **kw: configure_calls.append(kw),
            ),
            patch.object(real_genai, "list_models", side_effect=fake_list_models),
        ):
            result = await _get_google_models("AIza-scoped-key")

        assert result.source == "live"
        assert result.models == ["gemini-2.5-pro"]
        # The whole point: global state was never mutated.
        assert configure_calls == []
        # ...and the key travelled on the per-call client instead.
        assert captured["client"].transport._credentials.token == "AIza-scoped-key"


class TestGetQwenModels:
    """Test _get_qwen_models function."""

    @pytest.mark.asyncio
    async def test_get_qwen_models_without_key(self):
        """No key: empty list with missing_key, not a catalog."""
        result = await _get_qwen_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_get_qwen_models_with_valid_key_returns_live_list(self):
        """A valid key returns what Qwen actually serves, not the fallback."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("qwen3-max", "qwen3.5-plus")
            )
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key")

            assert result.models == ["qwen3-max", "qwen3.5-plus"]
            assert result.source == "live"
            # The stale bundled catalog must not leak into a live answer.
            assert "qwq-32b-preview" not in result.models
            mock_client.assert_called_once()
            # Verify it's using Qwen's China DashScope default
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == QWEN_DEFAULT_BASE_URL

    @pytest.mark.asyncio
    async def test_get_qwen_models_empty_live_list_falls_back(self):
        """An empty listing is a proxy quirk, not a reason for an empty picker."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == "empty_response"

    @pytest.mark.asyncio
    async def test_get_qwen_models_authentication_error(self):
        """Test handling of authentication errors."""
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key",
                    response=MagicMock(),
                    body=None,
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid Qwen API key"):
                await _get_qwen_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_qwen_models_network_error(self):
        """Test handling of non-authentication errors."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("Network error")
            )
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("test_key")
            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS

    @pytest.mark.asyncio
    async def test_get_qwen_models_uses_supplied_intl_endpoint(self):
        """A Singapore / intl base URL must be queried, not the China default."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("qwen3.8-max")
            )
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key", QWEN_INTL_BASE_URL)

            assert result.models == ["qwen3.8-max"]
            assert result.source == "live"
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == QWEN_INTL_BASE_URL

    @pytest.mark.asyncio
    async def test_get_qwen_models_rejects_private_endpoint(self):
        """User-supplied discovery URLs are an SSRF sink and must be validated."""
        with pytest.raises(ValueError, match="private address"):
            await _get_qwen_models("valid_key", "http://127.0.0.1/v1")

    @pytest.mark.asyncio
    async def test_get_qwen_models_rejects_non_aliyun_host(self):
        """Qwen discovery must not fetch a host outside DashScope / Model Studio."""
        with pytest.raises(ValueError, match="DashScope or Model Studio"):
            await _get_qwen_models(
                "valid_key", "https://evil.example.com/compatible-mode/v1"
            )

    @pytest.mark.asyncio
    async def test_get_qwen_models_filters_media_and_nsfw_from_live_list(self):
        """Dedicated media / NSFW SKUs must not appear in the chat picker."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response(
                    "qwen3.8-max",
                    "wan2.2-t2v",
                    "qwen-image-plus",
                    "qwen-vl-max",
                    "qwen3-omni-flash",
                    "qwen3.5-omni-plus",
                    "qwen4-omni-flash",
                    "qwen3-s2s-flash-realtime",
                    "qwen3-livetranslate-flash",
                    "qwen-mt-plus",
                    "qwen3-mt-plus",
                    "tongyi-tingwu-slp",
                    "happy-horse-1.1",
                    "z-image",
                    "some-nsfw-model",
                    "deepseek-v4-pro",
                    "glm-5.2",
                    "kimi-k2.7-code",
                )
            )
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key")

            assert result.source == "live"
            assert result.models == [
                "deepseek-v4-pro",
                "glm-5.2",
                "kimi-k2.7-code",
                "qwen3.8-max",
            ]

    def test_qwen_chat_filter_keeps_completions_and_drops_media(self):
        """Live Qwen ids are filtered; there is no keyless catalog."""
        assert _is_qwen_chat_model("qwen3.8-max")
        assert _is_qwen_chat_model("deepseek-v4-pro")
        assert _is_qwen_chat_model("glm-5.2")
        assert _is_qwen_chat_model("kimi-k2.7-code")
        assert not _is_qwen_chat_model("wan2.2-t2v")
        assert not _is_qwen_chat_model("qwen3-vl-plus")
        assert not _is_qwen_chat_model("qwen3-omni-flash")
        assert not _is_qwen_chat_model("qwen3.5-omni-plus")
        assert not _is_qwen_chat_model("qwen4-omni-flash")
        assert not _is_qwen_chat_model("qwen-mt-plus")
        assert not _is_qwen_chat_model("qwen3-mt-plus")
        assert not _is_qwen_chat_model("qwen4-s2s-flash")


class TestGetDeepSeekModels:
    """Test _get_deepseek_models function."""

    @pytest.mark.asyncio
    async def test_get_deepseek_models_without_key(self):
        """No key: empty list with missing_key, not a catalog."""
        result = await _get_deepseek_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_get_deepseek_models_with_valid_key_returns_live_list(self):
        """A valid key returns the live catalog, which is the point of #171.

        The old code called models.list() purely to validate the key and threw
        the response away, so deepseek-v4-* stayed invisible in the console.
        """
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response(
                    "deepseek-v4-flash", "deepseek-chat", "deepseek-v4-flash"
                )
            )
            mock_client.return_value = mock_instance

            result = await _get_deepseek_models("valid_key")

            # Sorted and de-duplicated.
            assert result.models == ["deepseek-chat", "deepseek-v4-flash"]
            assert result.source == "live"
            mock_client.assert_called_once()
            # Verify it's using DeepSeek's base URL
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.deepseek.com/v1"

    @pytest.mark.asyncio
    async def test_get_deepseek_models_empty_live_list_falls_back(self):
        """An empty listing falls back rather than emptying the picker."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_deepseek_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == ERROR_EMPTY_RESPONSE

    @pytest.mark.asyncio
    async def test_get_deepseek_models_authentication_error(self):
        """Test handling of authentication errors."""
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key",
                    response=MagicMock(),
                    body=None,
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid DeepSeek API key"):
                await _get_deepseek_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_deepseek_models_network_error(self):
        """Test handling of non-authentication errors."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("Network error")
            )
            mock_client.return_value = mock_instance

            result = await _get_deepseek_models("test_key")
            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS


class TestGetMoonshotModels:
    """Moonshot (Kimi): live discovery plus a kimi-k3-first fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_missing_key(self):
        """No key: empty list with missing_key, not a catalog."""
        result = await _get_moonshot_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_with_valid_key_lists_live_from_moonshot_base_url(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("kimi-k3", "kimi-k2.7-code")
            )
            mock_client.return_value = mock_instance

            result = await _get_moonshot_models("valid_key")

            assert result.source == "live"
            assert result.models == ["kimi-k2.7-code", "kimi-k3"]
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.moonshot.ai/v1"

    @pytest.mark.asyncio
    async def test_empty_live_list_falls_back(self):
        """A listing with zero models must yield the bundled catalog, not []."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_moonshot_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == "empty_response"

    @pytest.mark.asyncio
    async def test_authentication_error(self):
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key", response=MagicMock(), body=None
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match=r"Invalid Moonshot \(Kimi\) API key"):
                await _get_moonshot_models("invalid_key")

    @pytest.mark.asyncio
    async def test_network_error_falls_back(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(side_effect=Exception("boom"))
            mock_client.return_value = mock_instance

            result = await _get_moonshot_models("test_key")
            assert result.models == []
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS


class TestGetZaiModels:
    """Z.ai (GLM): live discovery plus a litellm-priced fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_missing_key(self):
        result = await _get_zai_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_with_valid_key_lists_live_from_zai_base_url(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("glm-5.2", "glm-5", "glm-5.3-flash")
            )
            mock_client.return_value = mock_instance

            result = await _get_zai_models("valid_key")

            assert result.source == "live"
            assert result.models == ["glm-5", "glm-5.2", "glm-5.3-flash"]
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.z.ai/api/paas/v4"

    @pytest.mark.asyncio
    async def test_empty_live_list_falls_back(self):
        """A listing with zero models must yield the bundled catalog, not []."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_zai_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == "empty_response"

    @pytest.mark.asyncio
    async def test_authentication_error(self):
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key", response=MagicMock(), body=None
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match=r"Invalid Z.ai \(GLM\) API key"):
                await _get_zai_models("invalid_key")

    @pytest.mark.asyncio
    async def test_network_error_falls_back(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(side_effect=Exception("boom"))
            mock_client.return_value = mock_instance

            result = await _get_zai_models("test_key")
            assert result.models == []
            assert result.source == "fallback"


class TestGetMistralModels:
    """Mistral: live discovery plus a chat-only fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_missing_key(self):
        result = await _get_mistral_models(None)
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_with_valid_key_lists_live_from_mistral_base_url(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("mistral-large-3", "codestral-2508")
            )
            mock_client.return_value = mock_instance

            result = await _get_mistral_models("valid_key")

            assert result.source == "live"
            assert result.models == ["codestral-2508", "mistral-large-3"]
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.mistral.ai/v1"

    @pytest.mark.asyncio
    async def test_empty_live_list_falls_back(self):
        """A listing with zero models must yield the bundled catalog, not []."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_mistral_models("valid_key")

            assert result.models == []
            assert result.source == "fallback"
            assert result.error == "empty_response"

    @pytest.mark.asyncio
    async def test_authentication_error(self):
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key", response=MagicMock(), body=None
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid Mistral API key"):
                await _get_mistral_models("invalid_key")

    @pytest.mark.asyncio
    async def test_network_error_falls_back(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(side_effect=Exception("boom"))
            mock_client.return_value = mock_instance

            result = await _get_mistral_models("test_key")
            assert result.models == []
            assert result.source == "fallback"


class TestOpenAICompatibleModels:
    """Listing models from a user-configured OpenAI-compatible endpoint.

    Issue #171: provider "openai-compatible" pointed at OpenRouter returned []
    from the model picker, even though the same key against OpenRouter's own
    GET /models returns hundreds of models. The generic adapter had no
    discovery path at all and fell through to "return []".
    """

    @staticmethod
    def _models_response(*model_ids):
        response = MagicMock()
        response.data = []
        for model_id in model_ids:
            entry = MagicMock()
            entry.id = model_id
            response.data.append(entry)
        return response

    @pytest.mark.asyncio
    async def test_openai_compatible_lists_models_from_endpoint(self):
        response = self._models_response(
            "deepseek/deepseek-v4-flash-0731", "openrouter/auto-beta"
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://openrouter.ai/api/v1",
            )

        assert result.models == [
            "deepseek/deepseek-v4-flash-0731",
            "openrouter/auto-beta",
        ]
        assert result.source == "live"
        # The endpoint the user configured is the one queried.
        assert (
            mock_client.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"
        )

    @pytest.mark.asyncio
    async def test_openrouter_provider_defaults_its_endpoint(self):
        response = self._models_response("openrouter/auto-beta")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider("openrouter", "test_key")

        assert result.models == ["openrouter/auto-beta"]
        assert result.source == "live"
        assert (
            mock_client.call_args.kwargs["base_url"] == "https://openrouter.ai/api/v1"
        )

    @pytest.mark.asyncio
    async def test_custom_provider_lists_models_from_endpoint(self):
        response = self._models_response("k3", "k3-turbo")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "custom", "test_key", api_endpoint="https://api.kimi.example/v1"
            )

        assert result.models == ["k3", "k3-turbo"]

    @pytest.mark.asyncio
    async def test_results_are_deduplicated_and_sorted(self):
        response = self._models_response("b-model", "a-model", "b-model", "  ", None)
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.example.com/v1",
            )

        assert result.models == ["a-model", "b-model"]

    @pytest.mark.asyncio
    async def test_stt_kind_narrows_the_live_list(self):
        """Selecting "Speech to text" must not show the endpoint's chat ids."""
        response = self._models_response(
            "k3", "k3-turbo", "whisper-large-v3", "gpt-4o-transcribe"
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "custom",
                "test_key",
                model_kind="stt",
                api_endpoint="https://api.kimi.example/v1",
            )

        assert result.models == ["gpt-4o-transcribe", "whisper-large-v3"]
        assert result.source == "live"

    @pytest.mark.asyncio
    async def test_tts_kind_narrows_the_live_list(self):
        response = self._models_response("k3", "tts-1", "tts-1-hd")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                model_kind="tts",
                api_endpoint="https://gateway.example.com/v1",
            )

        assert result.models == ["tts-1", "tts-1-hd"]
        assert result.source == "live"

    @pytest.mark.asyncio
    async def test_audio_kind_with_no_matching_ids_is_an_empty_fallback(self):
        """An endpoint serving only chat ids offers no stt suggestions."""
        response = self._models_response("k3", "k3-turbo")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "custom",
                "test_key",
                model_kind="stt",
                api_endpoint="https://api.kimi.example/v1",
            )

        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "empty_response"

    @pytest.mark.asyncio
    async def test_llm_kind_keeps_the_full_live_list(self):
        """The OpenAI chat exclusion list must not run against a custom endpoint.

        Markers like "instruct" name ordinary chat models on a self-hosted
        server, so excluding them here would hide real models. Curation of
        chat ids is an OpenAI-specific concern.
        """
        response = self._models_response(
            "Llama-3-8B-Instruct", "k3", "text-embedding-ada-002"
        )
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "custom",
                "test_key",
                api_endpoint="https://api.kimi.example/v1",
            )

        assert result.models == [
            "Llama-3-8B-Instruct",
            "k3",
            "text-embedding-ada-002",
        ]

    @pytest.mark.asyncio
    async def test_openai_compatible_without_endpoint_returns_empty(self):
        # Nothing to query, but this must not raise: the picker just shows no
        # suggestions until an endpoint is entered.
        result = await get_available_models_for_provider("openai-compatible", "key")
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "missing_endpoint"

    @pytest.mark.asyncio
    async def test_auth_error_surfaces_as_value_error(self):
        from openai import AuthenticationError

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=AuthenticationError(
                    message="Invalid API key", response=MagicMock(), body=None
                )
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid API key for this endpoint"):
                await get_available_models_for_provider(
                    "openai-compatible",
                    "bad_key",
                    api_endpoint="https://openrouter.ai/api/v1",
                )

    @pytest.mark.asyncio
    async def test_network_error_returns_empty_rather_than_500(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(side_effect=Exception("boom"))
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.example.com/v1",
            )

        assert result.models == []
        assert result.source == "fallback"
        assert result.error in FALLBACK_ERROR_REASONS

    @pytest.mark.asyncio
    async def test_bare_list_response_shape_is_accepted(self):
        # Some OpenAI-compatible servers return a bare list, not {"data": [...]}.
        entry = MagicMock()
        entry.id = "some-model"
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=[entry])
            mock_client.return_value = mock_instance

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.example.com/v1",
            )

        assert result.models == ["some-model"]

    @pytest.mark.asyncio
    async def test_missing_sdk_returns_named_fallback_not_import_error(self):
        """No openai package: a named fallback, never an escaping ImportError.

        _get_openai_compatible_models used to import at function top level
        outside any guard, so an environment without the SDK produced a 500
        from the model-picker endpoint instead of an empty picker.
        """
        with patch.dict(sys.modules, {"openai": None}):
            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.example.com/v1",
            )

        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "sdk_missing"

    @pytest.mark.asyncio
    async def test_openai_compatible_passes_ca_bundle_to_http_client(
        self, monkeypatch, tmp_path
    ):
        ca = tmp_path / "ca.crt"
        ca.write_text("dummy-ca")
        monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
        monkeypatch.setenv("SSL_CERT_FILE", str(ca))
        response = self._models_response("llama-3")
        with (
            patch("httpx.AsyncClient") as mock_httpx,
            patch("openai.AsyncOpenAI") as mock_client,
        ):
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance
            mock_httpx.return_value.aclose = AsyncMock()

            result = await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.internal/v1",
            )

        assert result.models == ["llama-3"]
        assert result.source == "live"
        assert mock_httpx.call_args.kwargs["verify"] == str(ca)
        assert mock_client.call_args.kwargs["http_client"] is mock_httpx.return_value
        mock_httpx.return_value.aclose.assert_awaited()

    @pytest.mark.asyncio
    async def test_openai_compatible_default_has_no_custom_http_client(
        self, monkeypatch
    ):
        monkeypatch.delenv("PRELOOP_SSL_VERIFY", raising=False)
        monkeypatch.delenv("SSL_CERT_FILE", raising=False)
        monkeypatch.delenv("REQUESTS_CA_BUNDLE", raising=False)
        monkeypatch.delenv("CURL_CA_BUNDLE", raising=False)
        response = self._models_response("llama-3")
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=response)
            mock_client.return_value = mock_instance

            await get_available_models_for_provider(
                "openai-compatible",
                "test_key",
                api_endpoint="https://gateway.internal/v1",
            )

        assert "http_client" not in mock_client.call_args.kwargs


class TestCatalogProviderSdkMissing:
    """The shared OpenAI-compatible listing path (Qwen, DeepSeek, Moonshot...).

    _get_catalog_provider_models imported the SDK outside a guard too, so a
    missing package raised instead of returning a named empty fallback.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "fetch",
        [
            _get_qwen_models,
            _get_deepseek_models,
            _get_moonshot_models,
            _get_zai_models,
            _get_mistral_models,
        ],
    )
    async def test_missing_sdk_returns_empty_with_sdk_missing(self, fetch):
        with patch.dict(sys.modules, {"openai": None}):
            result = await fetch("test_key")

        assert result.models == []
        assert result.source == "fallback"
        assert result.error == "sdk_missing"


class TestDiscoveryEndpointValidation:
    """SSRF guard on the user-supplied discovery endpoint.

    This endpoint makes the server fetch a URL the caller chose, so the
    obvious internal targets have to be refused.
    """

    @pytest.mark.parametrize(
        "endpoint",
        [
            "http://127.0.0.1:8000/v1",
            "http://localhost/v1",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/v1",
            "http://192.168.1.10/v1",
            "http://[::1]/v1",
            "file:///etc/passwd",
            "gopher://example.com/v1",
            "",
        ],
    )
    def test_rejected_endpoints(self, endpoint):
        from preloop.services.ai_model_provider import validate_discovery_endpoint

        with pytest.raises(ValueError):
            validate_discovery_endpoint(endpoint)

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://openrouter.ai/api/v1",
            "https://gateway.example.com/v1",
            "http://api.example.com:8080/v1",
        ],
    )
    def test_allowed_endpoints(self, endpoint):
        from preloop.services.ai_model_provider import validate_discovery_endpoint

        assert validate_discovery_endpoint(endpoint) == endpoint


class TestQwenEndpointAllowlist:
    """Qwen discovery/chat URLs are restricted to DashScope / Model Studio."""

    @pytest.mark.parametrize(
        "endpoint",
        [
            QWEN_DEFAULT_BASE_URL,
            QWEN_INTL_BASE_URL,
            QWEN_US_BASE_URL,
            "https://workspace-abc.maas.aliyuncs.com/v1",
        ],
    )
    def test_allowed_dashscope_hosts(self, endpoint):
        from preloop.services.ai_model_provider import validate_qwen_endpoint

        assert validate_qwen_endpoint(endpoint) == endpoint

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://evil.example.com/v1",
            "https://openrouter.ai/api/v1",
            "https://dashscope.aliyuncs.com.evil.test/v1",
            "https://notaliyuncs.com/v1",
            "https://1.2.3.4/v1",
        ],
    )
    def test_rejects_non_aliyun_host(self, endpoint):
        from preloop.services.ai_model_provider import validate_qwen_endpoint

        with pytest.raises(ValueError, match="DashScope or Model Studio"):
            validate_qwen_endpoint(endpoint)

    def test_openai_compatible_still_allows_non_aliyun_host(self):
        """Other providers must not inherit the Qwen host allowlist."""
        from preloop.services.ai_model_provider import validate_discovery_endpoint

        endpoint = "https://gateway.example.com/v1"
        assert validate_discovery_endpoint(endpoint) == endpoint

    def test_crud_persist_rejects_non_aliyun_qwen_host(self):
        """Saved Qwen chat endpoints use the same DashScope allowlist."""
        from preloop.models.crud.ai_model import CRUDAIModel

        with pytest.raises(ValueError, match="DashScope or Model Studio"):
            CRUDAIModel._validate_qwen_api_endpoint(
                {
                    "provider_name": "qwen",
                    "api_endpoint": "https://evil.example.com/v1",
                }
            )
        CRUDAIModel._validate_qwen_api_endpoint(
            {
                "provider_name": "qwen",
                "api_endpoint": QWEN_INTL_BASE_URL,
            }
        )
        CRUDAIModel._validate_qwen_api_endpoint(
            {
                "provider_name": "openai",
                "api_endpoint": "https://evil.example.com/v1",
            }
        )


class TestOpenAIAuthErrorDoesNotLeakSecrets:
    """Regression: OpenAI AuthenticationError must not log key material.

    The handler previously used ``f"OpenAI authentication failed: {e}"``,
    which embedded the full SDK message (including the request URL and,
    for query-string keys, the api key itself) into the log output.
    """

    @pytest.mark.asyncio
    async def test_auth_error_message_with_key_never_reaches_log(self, caplog):
        """A synthetic AuthenticationError carrying a URL and API key must not
        appear in the warning log; only the exception type name is logged.

        The ``preloop`` logger is configured with ``propagate: False`` (see
        preloop/logging.py), so records stop there and never reach the root
        logger that ``caplog`` attaches to. Setting a level is not enough;
        the capture handler is attached to the emitting logger directly.
        Without that ``caplog.text`` is empty and the leak assertion below
        would pass vacuously, which is worse than no test at all.
        """
        from openai import AuthenticationError

        fake_key = "sk-live-TESTSECRET1234567890abcdef"
        fake_url = f"https://api.openai.com/v1/models?api_key={fake_key}"
        body = {
            "error": {
                "message": f"Incorrect API key provided: {fake_key}. "
                f"Request URL: {fake_url}",
                "type": "invalid_request_error",
                "code": "invalid_api_key",
            }
        }
        import httpx

        mock_response = httpx.Response(
            status_code=401,
            json=body,
            request=httpx.Request("GET", fake_url),
        )
        exc = AuthenticationError(
            message=body["error"]["message"],
            response=mock_response,
            body=body,
        )

        with patch("openai.AsyncOpenAI") as mock_cls:
            mock_instance = AsyncMock()
            mock_instance.models.list.side_effect = exc
            mock_cls.return_value = mock_instance

            emitting_logger = logging.getLogger("preloop.services.ai_model_provider")
            emitting_logger.addHandler(caplog.handler)
            try:
                with caplog.at_level(
                    logging.WARNING, logger="preloop.services.ai_model_provider"
                ):
                    with pytest.raises(ValueError, match="Invalid OpenAI API key"):
                        await _get_openai_models("some-key")
            finally:
                emitting_logger.removeHandler(caplog.handler)

        # Guard against a vacuous pass: if capture silently broke, the
        # "key not in log" assertion below would succeed against an empty
        # string while proving nothing.
        assert caplog.records, "log capture produced no records"

        # The key must not appear anywhere in the captured log output.
        full_log = caplog.text
        assert fake_key not in full_log, (
            "API key leaked into log output via AuthenticationError message"
        )
        # The type name IS logged (consistency with every other provider).
        assert "AuthenticationError" in full_log


# ---------------------------------------------------------------------------
# AWS Bedrock
# ---------------------------------------------------------------------------


def _foundation_summary(model_id, modalities=("TEXT",), status="ACTIVE"):
    """Build a bedrock list_foundation_models summary entry."""
    summary = MagicMock()
    summary.modelId = model_id
    summary.outputModalities = list(modalities)
    summary.modelLifecycle.status = status
    return summary


def _bedrock_client(summaries):
    client = MagicMock()
    response = MagicMock()
    response.modelSummaries = summaries
    client.list_foundation_models.return_value = response
    return client


class TestBedrockModels:
    """Bedrock discovery: live control-plane listing with a curated fallback."""

    @pytest.mark.asyncio
    async def test_routes_to_bedrock_handler(self):
        with patch(
            "preloop.services.ai_model_provider._get_bedrock_models"
        ) as mock_get:
            mock_get.return_value = ModelDiscoveryResult(
                models=["anthropic.claude-sonnet-4-5"], source="live"
            )
            result = await get_available_models_for_provider("bedrock")
            assert result.source == "live"
            assert result.models == ["anthropic.claude-sonnet-4-5"]

    @pytest.mark.asyncio
    async def test_live_listing_keeps_text_chat_models_only(self):
        client = _bedrock_client(
            [
                _foundation_summary("anthropic.claude-sonnet-4-5"),
                # Embedding and image models cannot serve chat completions.
                _foundation_summary(
                    "amazon.titan-embed-text-v2:0", modalities=("EMBEDDING",)
                ),
                _foundation_summary("stability.sd3-large-v1:0", modalities=("IMAGE",)),
                # Non-ACTIVE lifecycle entries are not usable on demand.
                _foundation_summary("anthropic.claude-old", status="LEGACY"),
                # Summaries without modality reporting predate the field;
                # keep rather than guess.
                _foundation_summary("legacy.model-v1", modalities=[]),
            ]
        )
        session = MagicMock()
        session.region_name = None
        session.client.return_value = client

        with patch("boto3.Session", return_value=session):
            result = await get_available_models_for_provider(
                "bedrock",
                aws_auth={
                    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                    "aws_secret_access_key": "secret",
                    "aws_region_name": "us-east-1",
                },
            )

        assert result.source == "live"
        assert result.error is None
        assert set(result.models) == {
            "anthropic.claude-sonnet-4-5",
            "legacy.model-v1",
        }
        session.client.assert_called_once()

    @pytest.mark.asyncio
    async def test_rejected_credentials_raise_auth_error(self):
        from botocore.exceptions import ClientError

        client = MagicMock()
        client.list_foundation_models.side_effect = ClientError(
            {
                "Error": {"Code": "UnrecognizedClientException"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            },
            "ListFoundationModels",
        )
        session = MagicMock()
        session.region_name = None
        session.client.return_value = client

        with (
            patch("boto3.Session", return_value=session),
            pytest.raises(ProviderAuthError, match="Invalid AWS credentials"),
        ):
            await get_available_models_for_provider(
                "bedrock",
                aws_auth={
                    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                    "aws_secret_access_key": "wrong",
                    "aws_region_name": "us-east-1",
                },
            )

    @pytest.mark.asyncio
    async def test_missing_credentials_return_missing_key(self):
        result = await get_available_models_for_provider(
            "bedrock", aws_auth={"aws_region_name": "us-east-1"}
        )
        assert result.models == []
        assert result.source == "fallback"
        assert result.error == ERROR_MISSING_KEY

    @pytest.mark.asyncio
    async def test_control_plane_failure_returns_fallback_catalog(self):
        client = MagicMock()
        client.list_foundation_models.side_effect = Exception("boom")
        session = MagicMock()
        session.region_name = None
        session.client.return_value = client

        with patch("boto3.Session", return_value=session):
            result = await get_available_models_for_provider(
                "bedrock",
                aws_auth={
                    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                    "aws_secret_access_key": "secret",
                    "aws_region_name": "us-east-1",
                },
            )

        assert result.source == "fallback"
        assert result.error == ERROR_UNKNOWN
        assert result.models == []

    @pytest.mark.asyncio
    async def test_missing_region_is_a_validation_error(self):
        session = MagicMock()
        session.region_name = None

        with (
            patch("boto3.Session", return_value=session),
            pytest.raises(ProviderValidationError, match="region"),
        ):
            await get_available_models_for_provider(
                "bedrock",
                aws_auth={
                    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                    "aws_secret_access_key": "secret",
                },
            )

    @pytest.mark.asyncio
    async def test_sdk_missing_returns_empty_with_sdk_missing(self):
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setitem(sys.modules, "boto3", None)
        try:
            result = await get_available_models_for_provider(
                "bedrock",
                aws_auth={
                    "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
                    "aws_secret_access_key": "secret",
                    "aws_region_name": "us-east-1",
                },
            )
        finally:
            monkeypatch.undo()
        assert result.source == "fallback"
        assert result.error == ERROR_SDK_MISSING
        assert result.models == []
