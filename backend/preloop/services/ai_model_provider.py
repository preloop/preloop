"""Service for fetching available models from AI providers."""

import ipaddress
import logging
from typing import List, Literal, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ModelKind = Literal["llm", "stt", "tts"]

# Provider names that mean "an OpenAI-compatible endpoint the user configured".
# These have no fixed catalog: the model list has to come from the endpoint's
# own GET /models (issue #171).
OPENAI_COMPATIBLE_PROVIDERS = {"openai-compatible", "custom", "openrouter"}

# Default endpoints for providers whose base URL we know, used when the caller
# did not supply one.
DEFAULT_PROVIDER_ENDPOINTS = {
    "openrouter": "https://openrouter.ai/api/v1",
}

# Upper bound on models returned from an arbitrary endpoint. OpenRouter alone
# lists 300+ and the picker is a dropdown, so an unbounded list is a UI and
# payload hazard rather than a feature.
MAX_DISCOVERED_MODELS = 1000

OPENAI_STT_MODELS = [
    "gpt-4o-transcribe",
    "gpt-4o-mini-transcribe",
    "whisper-1",
]

OPENAI_TTS_MODELS = [
    "gpt-4o-mini-tts",
    "tts-1",
    "tts-1-hd",
]

GOOGLE_STT_MODELS = [
    "latest_short",
    "latest_long",
    "command_and_search",
    "default",
]

SUPPORTED_AUDIO_PROVIDER_KINDS: dict[str, set[ModelKind]] = {
    "openai": {"llm", "stt", "tts"},
    "custom": {"llm", "stt", "tts"},
    "openai-compatible": {"llm", "stt", "tts"},
    "anthropic": {"llm"},
    "google": {"llm", "stt"},
    "qwen": {"llm"},
    "deepseek": {"llm"},
}


async def get_available_models_for_provider(
    provider: str,
    api_key: Optional[str] = None,
    model_kind: ModelKind = "llm",
    api_endpoint: Optional[str] = None,
) -> List[str]:
    """
    Fetch available models from the specified AI provider.

    Args:
        provider: The provider name (openai, anthropic, google, qwen, deepseek,
            openai-compatible, custom, openrouter)
        api_key: Optional API key for authentication
        model_kind: Service kind to fetch (llm, stt, or tts)
        api_endpoint: Base URL of an OpenAI-compatible endpoint. Required for
            the openai-compatible/custom providers, which have no fixed
            catalog; defaulted for providers in DEFAULT_PROVIDER_ENDPOINTS.

    Returns:
        List of model identifiers/names
    """
    provider = provider.lower()
    normalized_model_kind = model_kind.lower()
    if normalized_model_kind not in {"llm", "stt", "tts"}:
        raise ValueError("model_kind must be one of llm, stt, or tts")

    supported_kinds = SUPPORTED_AUDIO_PROVIDER_KINDS.get(provider, {"llm"})
    if normalized_model_kind not in supported_kinds:
        return []

    if normalized_model_kind == "stt":
        return _get_stt_models(provider)
    if normalized_model_kind == "tts":
        return _get_tts_models(provider)

    if provider == "openai":
        return await _get_openai_models(api_key)
    elif provider == "anthropic":
        return await _get_anthropic_models(api_key)
    elif provider == "google":
        return await _get_google_models(api_key)
    elif provider == "qwen":
        return await _get_qwen_models(api_key)
    elif provider == "deepseek":
        return await _get_deepseek_models(api_key)
    elif provider in OPENAI_COMPATIBLE_PROVIDERS:
        endpoint = (api_endpoint or "").strip() or DEFAULT_PROVIDER_ENDPOINTS.get(
            provider
        )
        if not endpoint:
            # Nothing to query. Previously every openai-compatible provider
            # landed here and silently returned [] (issue #171).
            logger.info(
                "No api_endpoint supplied for provider %s; cannot list models",
                provider,
            )
            return []
        return await _get_openai_compatible_models(endpoint, api_key)
    else:
        # Unknown provider with no endpoint convention to follow.
        return []


def validate_discovery_endpoint(api_endpoint: str) -> str:
    """Validate a user-supplied model-discovery endpoint, or raise ValueError.

    This endpoint makes the server fetch a URL chosen by the caller, so it is
    an SSRF sink. Restrict it to http(s) on a non-literal-private host. DNS
    names that resolve to private space are not caught here (that needs a
    resolving/pinning transport); this blocks the trivial direct attempts at
    link-local metadata and loopback services.
    """
    raw = (api_endpoint or "").strip()
    if not raw:
        raise ValueError("api_endpoint is required for this provider")

    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise ValueError("api_endpoint must be an http(s) URL")
    host = (parts.hostname or "").strip()
    if not host:
        raise ValueError("api_endpoint must include a host")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name. Block the obvious loopback aliases; anything else is
        # allowed, matching how api_endpoint is treated elsewhere.
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ValueError("api_endpoint must not point at a private address")
        return raw

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ValueError("api_endpoint must not point at a private address")
    return raw


async def _get_openai_compatible_models(
    api_endpoint: str, api_key: Optional[str] = None
) -> List[str]:
    """List models from an OpenAI-compatible endpoint's ``GET /models``.

    Works for OpenRouter, vLLM, LM Studio, Together, Groq and anything else
    that implements the OpenAI list-models shape ``{"data": [{"id": ...}]}``.
    Some servers (OpenRouter included) return a bare list, which is accepted
    too.
    """
    from openai import AsyncOpenAI, AuthenticationError

    base_url = validate_discovery_endpoint(api_endpoint).rstrip("/")
    try:
        client = AsyncOpenAI(
            api_key=api_key or "placeholder",
            base_url=base_url,
            max_retries=1,
            timeout=15.0,
        )
        response = await client.models.list()
    except AuthenticationError as e:
        # Do not log the key or the full request URL: a key passed as a query
        # param would otherwise land in logs (see the api_key handling in the
        # endpoint layer).
        logger.warning("Authentication failed listing models from %s", base_url)
        raise ValueError(
            "Invalid API key for this endpoint. Please check your API key and "
            "try again."
        ) from e
    except Exception as e:
        logger.warning("Failed to list models from %s: %s", base_url, type(e).__name__)
        return []

    return _extract_model_ids(response)


def _extract_model_ids(response: object) -> List[str]:
    """Pull model ids out of an OpenAI-compatible list-models response."""
    entries = getattr(response, "data", None)
    if entries is None and isinstance(response, dict):
        entries = response.get("data")
    if entries is None and isinstance(response, list):
        entries = response
    if not entries:
        return []

    model_ids: List[str] = []
    for entry in entries:
        model_id = getattr(entry, "id", None)
        if model_id is None and isinstance(entry, dict):
            model_id = entry.get("id")
        if isinstance(model_id, str) and model_id.strip():
            model_ids.append(model_id.strip())

    # Stable, de-duplicated, alphabetical: the picker is a flat dropdown and
    # upstream ordering is neither stable nor meaningful.
    return sorted(set(model_ids))[:MAX_DISCOVERED_MODELS]


def _get_stt_models(provider: str) -> List[str]:
    """Return known speech-to-text model identifiers for supported providers."""
    if provider in {"openai", "custom", "openai-compatible"}:
        return OPENAI_STT_MODELS
    if provider == "google":
        return GOOGLE_STT_MODELS
    return []


def _get_tts_models(provider: str) -> List[str]:
    """Return known text-to-speech model identifiers for supported providers."""
    if provider in {"openai", "custom", "openai-compatible"}:
        return OPENAI_TTS_MODELS
    return []


async def _get_openai_models(api_key: Optional[str] = None) -> List[str]:
    """Fetch available models from OpenAI API."""
    try:
        from openai import AsyncOpenAI, AuthenticationError

        # Use provided API key or fall back to environment variable
        client = AsyncOpenAI(api_key=api_key) if api_key else AsyncOpenAI()

        models_response = await client.models.list()
        models = models_response.data

        # Filter for chat models (gpt-*) and sort by ID
        chat_models = [
            model.id
            for model in models
            if model.id.startswith("gpt-")
            and not any(
                x in model.id
                for x in [
                    "instruct",
                    "search",
                    "similarity",
                    "edit",
                    "insert",
                    "audio",
                    "realtime",
                ]
            )
        ]

        # Sort with newer models first
        chat_models.sort(reverse=True)

        return chat_models[:10]  # Return top 10 most recent models
    except AuthenticationError as e:
        logger.warning(f"OpenAI authentication failed: {e}")
        # Re-raise authentication errors so the user knows their API key is invalid
        raise ValueError(
            "Invalid OpenAI API key. Please check your API key and try again."
        )
    except Exception as e:
        logger.warning(f"Failed to fetch OpenAI models: {e}")
        # Return fallback list for other errors (network issues, etc.)
        return ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini"]


async def _get_anthropic_models(api_key: Optional[str] = None) -> List[str]:
    """Return list of known Anthropic Claude models and validate API key if provided."""

    # List of known Anthropic models (as of January 2025)
    known_models = [
        "claude-sonnet-4-5-20250929",
        "claude-haiku-4-5-20251001",
    ]

    # If API key is provided, validate it by making a minimal API call
    if api_key:
        try:
            try:
                from anthropic import AsyncAnthropic
            except ImportError:
                logger.warning("Anthropic package not installed, skipping validation")
                # If package not installed, we can't validate - return the list
                return known_models

            client = AsyncAnthropic(api_key=api_key)

            # Make a minimal request to validate the key
            # Use a very short max_tokens to minimize cost
            await client.messages.create(
                model="claude-3-haiku-20240307",
                max_tokens=1,
                messages=[{"role": "user", "content": "Hi"}],
            )

            logger.info("Anthropic API key validated successfully")
        except Exception as e:
            logger.warning(f"Anthropic validation exception: {type(e).__name__}: {e}")
            error_msg = str(e).lower()
            error_type = type(e).__name__.lower()

            logger.debug(f"Error message: {error_msg}")
            logger.debug(f"Error type: {error_type}")

            # Check for authentication errors by message content and exception type
            if any(
                keyword in error_msg
                for keyword in [
                    "401",
                    "unauthorized",
                    "authentication",
                    "invalid_api_key",
                    "api_key",
                ]
            ) or any(
                keyword in error_type
                for keyword in ["authentication", "permission", "unauthorized"]
            ):
                logger.error("Detected authentication error, raising ValueError")
                raise ValueError(
                    "Invalid Anthropic API key. Please check your API key and try again."
                )
            else:
                # For other errors (network, import, etc.), log but don't fail - just return the list
                logger.error(f"Non-authentication error, returning default list: {e}")

    return known_models


async def _get_google_models(api_key: Optional[str] = None) -> List[str]:
    """Return list of known Google Gemini models and validate API key if provided."""

    # List of current Google Gemini models. Avoid deprecated preview/exp names.
    known_models = [
        "gemini-2.5-pro",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
    ]

    # If API key is provided, validate it
    if api_key:
        try:
            try:
                import google.generativeai as genai
            except ImportError:
                logger.warning(
                    "Google GenerativeAI package not installed, skipping validation"
                )
                # If package not installed, we can't validate - return the list
                return known_models

            genai.configure(api_key=api_key)

            list_models = getattr(genai, "list_models", None)
            if callable(list_models):
                fetched_models = []
                for model in list_models():
                    model_name = getattr(model, "name", "")
                    supported_methods = set(
                        getattr(model, "supported_generation_methods", []) or []
                    )
                    if model_name.startswith("models/"):
                        model_name = model_name.removeprefix("models/")
                    if model_name and (
                        not supported_methods or "generateContent" in supported_methods
                    ):
                        fetched_models.append(model_name)
                if fetched_models:
                    return sorted(set(fetched_models), reverse=True)

            # Make a minimal request to validate the key
            # Use the cheapest/fastest model with minimal tokens
            model = genai.GenerativeModel("gemini-2.0-flash")
            model.generate_content(
                "Hi", generation_config=genai.GenerationConfig(max_output_tokens=1)
            )

            logger.info("Google API key validated successfully")
        except Exception as e:
            logger.warning(f"Google validation exception: {type(e).__name__}: {e}")
            error_msg = str(e).lower()
            error_type = type(e).__name__.lower()

            logger.debug(f"Error message: {error_msg}")
            logger.debug(f"Error type: {error_type}")

            # Check for authentication errors by message content and exception type
            if any(
                keyword in error_msg
                for keyword in [
                    "401",
                    "403",
                    "unauthorized",
                    "unauthenticated",
                    "permission",
                    "invalid",
                    "api_key",
                    "api key",
                ]
            ) or any(
                keyword in error_type
                for keyword in [
                    "authentication",
                    "permission",
                    "unauthorized",
                    "unauthenticated",
                    "invalidargument",
                ]
            ):
                logger.error("Detected authentication error, raising ValueError")
                raise ValueError(
                    "Invalid Google API key. Please check your API key and try again."
                )
            else:
                # For other errors (network, import, etc.), log but don't fail - just return the list
                logger.error(f"Non-authentication error, returning default list: {e}")

    return known_models


async def _get_catalog_provider_models(
    *,
    provider_label: str,
    base_url: str,
    known_models: List[str],
    api_key: Optional[str] = None,
) -> List[str]:
    """List models for a provider that ships an OpenAI-compatible ``/models``.

    Without a key there is nothing to query, so the bundled catalog is
    returned. With a key we return what the provider actually serves: these
    catalogs go stale the moment the vendor ships a model (DeepSeek v4 was
    hidden from the picker for exactly this reason) while litellm already
    prices the new ids.

    An authentication failure still raises, since the user needs to know the
    key is bad. Any other failure (network, SDK, unexpected payload) falls
    back to the bundled catalog rather than showing an empty picker.
    """
    if not api_key:
        return known_models

    from openai import AsyncOpenAI, AuthenticationError

    try:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=1,
            timeout=15.0,
        )
        response = await client.models.list()
    except AuthenticationError as e:
        logger.warning("%s authentication failed: %s", provider_label, type(e).__name__)
        raise ValueError(
            f"Invalid {provider_label} API key. Please check your API key and "
            "try again."
        ) from e
    except Exception as e:
        logger.warning(
            "Failed to list %s models, using bundled catalog: %s",
            provider_label,
            type(e).__name__,
        )
        return known_models

    live_models = _extract_model_ids(response)
    if not live_models:
        # A valid key that lists nothing is more likely a proxy quirk than an
        # empty account; an empty picker is the worse outcome either way.
        logger.info("%s returned no models, using bundled catalog", provider_label)
        return known_models

    logger.info("Listed %d %s models", len(live_models), provider_label)
    return live_models


# Fallback catalog used when no Qwen key is available to list the live set.
QWEN_KNOWN_MODELS = [
    "qwen-plus",
    "qwen-turbo",
    "qwen-max",
    "qwq-32b-preview",
]

# Fallback catalog used when no DeepSeek key is available to list the live set.
# Keep in step with the deepseek entries in services/data/model_prices.json:
# a model missing here is invisible in the picker even though it can be priced.
DEEPSEEK_KNOWN_MODELS = [
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
]


async def _get_qwen_models(api_key: Optional[str] = None) -> List[str]:
    """Return Qwen models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="Qwen",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        known_models=QWEN_KNOWN_MODELS,
        api_key=api_key,
    )


async def _get_deepseek_models(api_key: Optional[str] = None) -> List[str]:
    """Return DeepSeek models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        known_models=DEEPSEEK_KNOWN_MODELS,
        api_key=api_key,
    )
