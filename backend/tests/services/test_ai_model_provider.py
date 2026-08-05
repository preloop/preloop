"""Tests for AI model provider service."""

import logging
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.services.ai_model_provider import (
    get_available_models_for_provider,
    ANTHROPIC_FALLBACK_MODELS,
    DEEPSEEK_KNOWN_MODELS,
    GOOGLE_FALLBACK_MODELS,
    MISTRAL_KNOWN_MODELS,
    MOONSHOT_KNOWN_MODELS,
    OPENAI_FALLBACK_MODELS,
    QWEN_KNOWN_MODELS,
    ZAI_KNOWN_MODELS,
    FALLBACK_ERROR_REASONS,
    ModelDiscoveryResult,
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
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_openai_stt_models(self):
        """OpenAI speech-to-text catalog is curated, so provenance is fallback."""
        result = await get_available_models_for_provider(
            "openai", "test_key", model_kind="stt"
        )
        assert result.models == [
            "gpt-4o-transcribe",
            "gpt-4o-mini-transcribe",
            "whisper-1",
        ]
        assert result.source == "fallback"
        assert result.error is None

    @pytest.mark.asyncio
    async def test_get_openai_tts_models(self):
        """Test OpenAI text-to-speech model catalog."""
        result = await get_available_models_for_provider(
            "openai", "test_key", model_kind="tts"
        )
        assert result.models == ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]
        assert result.source == "fallback"

    @pytest.mark.asyncio
    async def test_audio_models_only_for_supported_providers(self):
        """Non-audio providers should not offer STT/TTS choices."""
        stt = await get_available_models_for_provider(
            "google", "test_key", model_kind="stt"
        )
        assert stt.models == [
            "latest_short",
            "latest_long",
            "command_and_search",
            "default",
        ]
        tts = await get_available_models_for_provider(
            "google", "test_key", model_kind="tts"
        )
        assert tts.models == []
        assert tts.source == "fallback"
        assert tts.error == "unsupported"

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
            mock_get.assert_called_once_with("test_key")

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

            assert result.models == OPENAI_FALLBACK_MODELS
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

            # Should call AsyncOpenAI without api_key parameter
            mock_client.assert_called_once_with()
            assert "gpt-5.4" in result.models


class TestGetAnthropicModels:
    """Anthropic switched from a paid messages.create ping to models.list."""

    @pytest.mark.asyncio
    async def test_get_anthropic_models_without_key(self):
        """No key: the named fallback with clean fallback provenance."""
        result = await _get_anthropic_models(None)
        assert result.models == ANTHROPIC_FALLBACK_MODELS
        assert result.source == "fallback"
        assert result.error is None

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
                assert result.models == ANTHROPIC_FALLBACK_MODELS
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
            assert result.models == ANTHROPIC_FALLBACK_MODELS
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
            assert result.models == ANTHROPIC_FALLBACK_MODELS
            assert result.error == "empty_response"

    def test_fallback_ids_are_priced_in_the_bundled_table(self):
        """Every fallback id must resolve in the vendored price snapshot."""
        import json

        from preloop.services.model_price_catalog import CATALOG_PATH

        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in ANTHROPIC_FALLBACK_MODELS:
            assert model_id in prices, f"{model_id} missing from model_prices.json"


class TestGetGoogleModels:
    """Test _get_google_models function."""

    @pytest.mark.asyncio
    async def test_get_google_models_without_key(self):
        """No key: the named fallback with clean fallback provenance."""
        result = await _get_google_models(None)
        assert result.models == GOOGLE_FALLBACK_MODELS
        assert result.source == "fallback"
        assert result.error is None

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

            assert result.models == GOOGLE_FALLBACK_MODELS
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
                assert result.models == GOOGLE_FALLBACK_MODELS
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
            assert result.models == GOOGLE_FALLBACK_MODELS
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS


class TestGetQwenModels:
    """Test _get_qwen_models function."""

    @pytest.mark.asyncio
    async def test_get_qwen_models_without_key(self):
        """Test getting Qwen models without API key."""
        result = await _get_qwen_models(None)
        assert result.models == QWEN_KNOWN_MODELS
        assert result.source == "fallback"
        assert result.error is None

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
            # Verify it's using Qwen's base URL
            call_kwargs = mock_client.call_args[1]
            assert (
                call_kwargs["base_url"]
                == "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )

    @pytest.mark.asyncio
    async def test_get_qwen_models_empty_live_list_falls_back(self):
        """An empty listing is a proxy quirk, not a reason for an empty picker."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=_models_response())
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key")

            assert "qwen-plus" in result.models
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
            # Should return known models even on network error
            assert "qwen-plus" in result.models
            assert result.source == "fallback"


class TestGetDeepSeekModels:
    """Test _get_deepseek_models function."""

    @pytest.mark.asyncio
    async def test_get_deepseek_models_without_key(self):
        """Test getting DeepSeek models without API key."""
        result = await _get_deepseek_models(None)
        assert "deepseek-chat" in result.models
        assert "deepseek-reasoner" in result.models
        assert result.source == "fallback"

    @pytest.mark.asyncio
    async def test_keyless_catalog_includes_v4_models(self):
        """The bundled catalog must not hide models litellm already prices."""
        result = await _get_deepseek_models(None)
        assert "deepseek-v4-flash" in result.models
        assert "deepseek-v4-pro" in result.models

    @pytest.mark.asyncio
    async def test_keyless_catalog_entries_are_priced(self):
        """Every fallback id should resolve in the bundled price table.

        This is what keeps the hardcoded list honest as DeepSeek ships models.
        """
        import json

        from preloop.services.model_price_catalog import CATALOG_PATH

        prices = json.loads(CATALOG_PATH.read_text())
        for model_id in DEEPSEEK_KNOWN_MODELS:
            assert model_id in prices, f"{model_id} missing from model_prices.json"

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

            assert "deepseek-chat" in result.models
            assert result.source == "fallback"

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
            # Should return known models even on network error
            assert "deepseek-chat" in result.models


class TestGetMoonshotModels:
    """Moonshot (Kimi): live discovery plus a kimi-k3-first fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_fallback_with_kimi_k3_first(self):
        """kimi-k3 is the headline model and must lead the fallback list."""
        result = await _get_moonshot_models(None)
        assert result.models == MOONSHOT_KNOWN_MODELS
        assert result.models[0] == "kimi-k3"
        assert result.source == "fallback"
        assert result.error is None

    def test_fallback_excludes_sunset_and_deprecated_ids(self):
        """No sunset moonshot-v1 series, no deprecated kimi-k2 previews."""
        for model_id in MOONSHOT_KNOWN_MODELS:
            assert not model_id.startswith("moonshot-v1")
            assert "preview" not in model_id
            assert "thinking" not in model_id
            assert model_id != "kimi-latest"
            assert model_id != "kimi-k2.5"  # sunset Aug 31

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
            assert result.models == MOONSHOT_KNOWN_MODELS
            assert result.source == "fallback"
            assert result.error in FALLBACK_ERROR_REASONS


class TestGetZaiModels:
    """Z.ai (GLM): live discovery plus a litellm-priced fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_fallback(self):
        result = await _get_zai_models(None)
        assert result.models == ZAI_KNOWN_MODELS
        assert "glm-5" in result.models
        assert "glm-4.7" in result.models
        assert result.source == "fallback"

    @pytest.mark.asyncio
    async def test_with_valid_key_lists_live_from_zai_base_url(self):
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                return_value=_models_response("glm-5.2", "glm-5")
            )
            mock_client.return_value = mock_instance

            result = await _get_zai_models("valid_key")

            assert result.source == "live"
            assert result.models == ["glm-5", "glm-5.2"]
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.z.ai/api/paas/v4"

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
            assert result.models == ZAI_KNOWN_MODELS
            assert result.source == "fallback"


class TestGetMistralModels:
    """Mistral: live discovery plus a chat-only fallback."""

    @pytest.mark.asyncio
    async def test_without_key_returns_fallback(self):
        result = await _get_mistral_models(None)
        assert result.models == MISTRAL_KNOWN_MODELS
        assert result.source == "fallback"

    def test_fallback_excludes_embeddings_and_ocr(self):
        for model_id in MISTRAL_KNOWN_MODELS:
            assert "embed" not in model_id
            assert "ocr" not in model_id

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
            assert result.models == MISTRAL_KNOWN_MODELS
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
