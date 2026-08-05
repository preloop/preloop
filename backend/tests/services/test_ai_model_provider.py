"""Tests for AI model provider service."""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from preloop.services.ai_model_provider import (
    get_available_models_for_provider,
    _get_openai_models,
    _get_anthropic_models,
    _get_google_models,
    _get_qwen_models,
    _get_deepseek_models,
)


class TestGetAvailableModelsForProvider:
    """Test get_available_models_for_provider function."""

    @pytest.mark.asyncio
    async def test_get_openai_models(self):
        """Test routing to OpenAI provider."""
        with patch("preloop.services.ai_model_provider._get_openai_models") as mock_get:
            mock_get.return_value = ["gpt-5.4", "gpt-5.4-mini"]
            result = await get_available_models_for_provider("openai", "test_key")
            assert result == ["gpt-5.4", "gpt-5.4-mini"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_openai_stt_models(self):
        """Test OpenAI speech-to-text model catalog."""
        result = await get_available_models_for_provider(
            "openai", "test_key", model_kind="stt"
        )
        assert result == ["gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"]

    @pytest.mark.asyncio
    async def test_get_openai_tts_models(self):
        """Test OpenAI text-to-speech model catalog."""
        result = await get_available_models_for_provider(
            "openai", "test_key", model_kind="tts"
        )
        assert result == ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]

    @pytest.mark.asyncio
    async def test_audio_models_only_for_supported_providers(self):
        """Non-audio providers should not offer STT/TTS choices."""
        assert await get_available_models_for_provider(
            "google", "test_key", model_kind="stt"
        ) == ["latest_short", "latest_long", "command_and_search", "default"]
        assert (
            await get_available_models_for_provider(
                "google", "test_key", model_kind="tts"
            )
            == []
        )

    @pytest.mark.asyncio
    async def test_get_anthropic_models(self):
        """Test routing to Anthropic provider."""
        with patch(
            "preloop.services.ai_model_provider._get_anthropic_models"
        ) as mock_get:
            mock_get.return_value = ["claude-sonnet-4-5-20250929"]
            result = await get_available_models_for_provider("anthropic", "test_key")
            assert result == ["claude-sonnet-4-5-20250929"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_google_models(self):
        """Test routing to Google provider."""
        with patch("preloop.services.ai_model_provider._get_google_models") as mock_get:
            mock_get.return_value = ["gemini-2.5-pro"]
            result = await get_available_models_for_provider("google", "test_key")
            assert result == ["gemini-2.5-pro"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_qwen_models(self):
        """Test routing to Qwen provider."""
        with patch("preloop.services.ai_model_provider._get_qwen_models") as mock_get:
            mock_get.return_value = ["qwen-plus", "qwen-turbo"]
            result = await get_available_models_for_provider("qwen", "test_key")
            assert result == ["qwen-plus", "qwen-turbo"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_deepseek_models(self):
        """Test routing to DeepSeek provider."""
        with patch(
            "preloop.services.ai_model_provider._get_deepseek_models"
        ) as mock_get:
            mock_get.return_value = ["deepseek-chat", "deepseek-reasoner"]
            result = await get_available_models_for_provider("deepseek", "test_key")
            assert result == ["deepseek-chat", "deepseek-reasoner"]
            mock_get.assert_called_once_with("test_key")

    @pytest.mark.asyncio
    async def test_get_custom_provider_returns_empty(self):
        """Test that custom/unknown providers return empty list."""
        result = await get_available_models_for_provider("custom_provider")
        assert result == []


class TestGetOpenAIModels:
    """Test _get_openai_models function."""

    @pytest.mark.asyncio
    async def test_get_openai_models_success(self):
        """Test successful retrieval of OpenAI models."""
        mock_model_1 = MagicMock()
        mock_model_1.id = "gpt-5.4"
        mock_model_2 = MagicMock()
        mock_model_2.id = "gpt-5.4-mini"
        mock_model_3 = MagicMock()
        mock_model_3.id = "gpt-5.4-codex"
        mock_model_4 = MagicMock()
        mock_model_4.id = "gpt-5.4-instruct"  # Should be filtered out

        mock_response = MagicMock()
        mock_response.data = [mock_model_1, mock_model_2, mock_model_3, mock_model_4]

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert len(result) <= 10
            assert "gpt-5.4" in result
            assert "gpt-5.4-mini" in result
            assert "gpt-5.4-codex" in result
            assert "gpt-5.4-instruct" not in result  # Filtered out

    @pytest.mark.asyncio
    async def test_get_openai_models_filters_non_chat_models(self):
        """Test that non-chat models are filtered."""
        mock_models = []
        for model_id in [
            "gpt-5.4",
            "gpt-5.4-audio-preview",  # Should be filtered
            "gpt-5.4-codex",
            "text-similarity-ada-001",  # Should be filtered
            "text-search-ada-doc-001",  # Should be filtered
            "gpt-5.4-mini",
        ]:
            mock_model = MagicMock()
            mock_model.id = model_id
            mock_models.append(mock_model)

        mock_response = MagicMock()
        mock_response.data = mock_models

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            assert "gpt-5.4" in result
            assert "gpt-5.4-mini" in result
            assert "gpt-5.4-codex" in result
            assert "text-similarity-ada-001" not in result

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
        """Test that network errors return fallback list."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(
                side_effect=Exception("Network error")
            )
            mock_client.return_value = mock_instance

            result = await _get_openai_models("test_key")

            # Should return fallback list
            assert "gpt-4.1" in result
            assert "gpt-4.1-mini" in result
            assert "gpt-4o" in result

    @pytest.mark.asyncio
    async def test_get_openai_models_without_api_key(self):
        """Test retrieval without providing API key."""
        mock_model = MagicMock()
        mock_model.id = "gpt-5.4"
        mock_response = MagicMock()
        mock_response.data = [mock_model]

        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=mock_response)
            mock_client.return_value = mock_instance

            result = await _get_openai_models(None)

            # Should call AsyncOpenAI without api_key parameter
            mock_client.assert_called_once_with()
            assert "gpt-5.4" in result


class TestGetAnthropicModels:
    """Test _get_anthropic_models function."""

    @pytest.mark.asyncio
    async def test_get_anthropic_models_without_key(self):
        """Test getting Anthropic models without API key."""
        result = await _get_anthropic_models(None)
        assert "claude-sonnet-4-5-20250929" in result
        assert "claude-haiku-4-5-20251001" in result

    @pytest.mark.asyncio
    async def test_get_anthropic_models_with_valid_key(self):
        """Test validation with valid API key."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.messages.create = AsyncMock(return_value=MagicMock())
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("valid_key")

            assert "claude-sonnet-4-5-20250929" in result
            mock_instance.messages.create.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_anthropic_models_authentication_error(self):
        """Test handling of authentication errors."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.messages.create = AsyncMock(
                side_effect=Exception("401 unauthorized")
            )
            mock_client.return_value = mock_instance

            with pytest.raises(ValueError, match="Invalid Anthropic API key"):
                await _get_anthropic_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_anthropic_models_import_error(self):
        """Test handling when Anthropic package not installed."""
        with patch(
            "anthropic.AsyncAnthropic",
            side_effect=ImportError("No module named 'anthropic'"),
        ):
            result = await _get_anthropic_models("test_key")
            # Should return known models even if package not installed
            assert "claude-sonnet-4-5-20250929" in result

    @pytest.mark.asyncio
    async def test_get_anthropic_models_network_error(self):
        """Test handling of non-authentication errors."""
        with patch("anthropic.AsyncAnthropic") as mock_client:
            mock_instance = MagicMock()
            mock_instance.messages.create = AsyncMock(
                side_effect=Exception("Network timeout")
            )
            mock_client.return_value = mock_instance

            result = await _get_anthropic_models("test_key")
            # Should return known models even on network error
            assert "claude-sonnet-4-5-20250929" in result


class TestGetGoogleModels:
    """Test _get_google_models function."""

    @pytest.mark.asyncio
    async def test_get_google_models_without_key(self):
        """Test getting Google models without API key."""
        result = await _get_google_models(None)
        assert "gemini-2.5-pro" in result
        assert "gemini-2.5-flash" in result

    @pytest.mark.asyncio
    async def test_get_google_models_with_valid_key(self):
        """Test validation with valid API key."""
        # Mock both the google and google.generativeai modules
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_model.generate_content = MagicMock(return_value=MagicMock())
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.GenerationConfig = MagicMock
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            result = await _get_google_models("valid_key")

            assert "gemini-2.5-pro" in result
            mock_genai.configure.assert_called_once_with(api_key="valid_key")

    @pytest.mark.asyncio
    async def test_get_google_models_authentication_error(self):
        """Test handling of authentication errors."""
        # Mock both the google and google.generativeai modules
        mock_google = MagicMock()
        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_model.generate_content = MagicMock(
            side_effect=Exception("403 permission denied")
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.GenerationConfig = MagicMock
        mock_google.generativeai = mock_genai

        with patch.dict(
            sys.modules,
            {"google": mock_google, "google.generativeai": mock_genai},
            clear=False,
        ):
            with pytest.raises(ValueError, match="Invalid Google API key"):
                await _get_google_models("invalid_key")

    @pytest.mark.asyncio
    async def test_get_google_models_import_error(self):
        """Test handling when Google package not installed."""
        # Ensure google.generativeai is not in sys.modules
        genai_module = sys.modules.pop("google.generativeai", None)
        try:
            # Mock the import to raise ImportError
            import builtins

            original_import = builtins.__import__

            def mock_import(name, *args, **kwargs):
                if name == "google.generativeai":
                    raise ImportError("No module named 'google.generativeai'")
                return original_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=mock_import):
                result = await _get_google_models("test_key")
                # Should return known models even if package not installed
                assert "gemini-2.5-pro" in result
        finally:
            # Restore google.generativeai if it was there
            if genai_module is not None:
                sys.modules["google.generativeai"] = genai_module

    @pytest.mark.asyncio
    async def test_get_google_models_network_error(self):
        """Test handling of non-authentication errors."""
        mock_genai = MagicMock()
        mock_model = MagicMock()
        mock_model.generate_content = MagicMock(
            side_effect=Exception("Connection reset")
        )
        mock_genai.GenerativeModel.return_value = mock_model
        mock_genai.GenerationConfig = MagicMock

        with patch.dict(sys.modules, {"google.generativeai": mock_genai}):
            result = await _get_google_models("test_key")
            # Should return known models even on network error
            assert "gemini-2.5-pro" in result


class TestGetQwenModels:
    """Test _get_qwen_models function."""

    @pytest.mark.asyncio
    async def test_get_qwen_models_without_key(self):
        """Test getting Qwen models without API key."""
        result = await _get_qwen_models(None)
        assert "qwen-plus" in result
        assert "qwen-turbo" in result
        assert "qwen-max" in result

    @pytest.mark.asyncio
    async def test_get_qwen_models_with_valid_key(self):
        """Test validation with valid API key."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=MagicMock())
            mock_client.return_value = mock_instance

            result = await _get_qwen_models("valid_key")

            assert "qwen-plus" in result
            mock_client.assert_called_once()
            # Verify it's using Qwen's base URL
            call_kwargs = mock_client.call_args[1]
            assert (
                call_kwargs["base_url"]
                == "https://dashscope.aliyuncs.com/compatible-mode/v1"
            )

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
            assert "qwen-plus" in result


class TestGetDeepSeekModels:
    """Test _get_deepseek_models function."""

    @pytest.mark.asyncio
    async def test_get_deepseek_models_without_key(self):
        """Test getting DeepSeek models without API key."""
        result = await _get_deepseek_models(None)
        assert "deepseek-chat" in result
        assert "deepseek-reasoner" in result

    @pytest.mark.asyncio
    async def test_get_deepseek_models_with_valid_key(self):
        """Test validation with valid API key."""
        with patch("openai.AsyncOpenAI") as mock_client:
            mock_instance = MagicMock()
            mock_instance.models.list = AsyncMock(return_value=MagicMock())
            mock_client.return_value = mock_instance

            result = await _get_deepseek_models("valid_key")

            assert "deepseek-chat" in result
            mock_client.assert_called_once()
            # Verify it's using DeepSeek's base URL
            call_kwargs = mock_client.call_args[1]
            assert call_kwargs["base_url"] == "https://api.deepseek.com/v1"

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
            assert "deepseek-chat" in result


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

        assert result == ["deepseek/deepseek-v4-flash-0731", "openrouter/auto-beta"]
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

        assert result == ["openrouter/auto-beta"]
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

        assert result == ["k3", "k3-turbo"]

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

        assert result == ["a-model", "b-model"]

    @pytest.mark.asyncio
    async def test_openai_compatible_without_endpoint_returns_empty(self):
        # Nothing to query, but this must not raise: the picker just shows no
        # suggestions until an endpoint is entered.
        result = await get_available_models_for_provider("openai-compatible", "key")
        assert result == []

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

        assert result == []

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

        assert result == ["some-model"]


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
