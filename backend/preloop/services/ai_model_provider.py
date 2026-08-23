"""Service for fetching available models from AI providers.

Every provider ATTEMPTS a live fetch when a key (and endpoint where
applicable) is present. Hardcoded catalogs still exist, but only as named
fallback constants, and every result carries provenance: ``source`` is
``live`` when the provider's own listing endpoint answered, ``fallback``
when a bundled catalog (or an empty list) was returned instead, with a
short machine-readable ``error`` reason. The reason is drawn from a fixed
vocabulary and never contains raw exception text, which can carry endpoint
URLs and key material (2026-08-04 key-leak incident).
"""

import ipaddress
import logging
from dataclasses import dataclass, field
from typing import List, Literal, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ModelKind = Literal["llm", "stt", "tts"]


class ProviderAuthError(ValueError):
    """The provider rejected the caller's API key.

    Subclasses ValueError so existing callers that catch ValueError keep
    working; the endpoint layer maps this to HTTP 401.
    """


class ProviderValidationError(ValueError):
    """The request was invalid before any provider was contacted.

    Covers bad model_kind values and rejected discovery endpoints (including
    SSRF blocks). Subclasses ValueError so existing callers that catch
    ValueError keep working; the endpoint layer maps this to HTTP 400.
    """


# Upper bound, in seconds, on any single live model-listing HTTP call so a
# hung provider endpoint cannot stall the discovery request indefinitely.
MODEL_DISCOVERY_TIMEOUT_SECONDS = 15.0

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

# ---------------------------------------------------------------------------
# Discovery result with provenance
# ---------------------------------------------------------------------------

DiscoverySource = Literal["live", "fallback"]

# Fixed vocabulary for the ``error`` field. These are the ONLY values the
# field may carry: raw exception text is never surfaced because it can embed
# the queried URL or the API key (2026-08-04 access-log key leak).
ERROR_TIMEOUT = "timeout"
ERROR_NETWORK = "network"
ERROR_EMPTY_RESPONSE = "empty_response"
ERROR_UNSUPPORTED = "unsupported"
ERROR_MISSING_ENDPOINT = "missing_endpoint"
ERROR_SDK_MISSING = "sdk_missing"
ERROR_UNKNOWN = "unknown"

FALLBACK_ERROR_REASONS = frozenset(
    {
        ERROR_TIMEOUT,
        ERROR_NETWORK,
        ERROR_EMPTY_RESPONSE,
        ERROR_UNSUPPORTED,
        ERROR_MISSING_ENDPOINT,
        ERROR_SDK_MISSING,
        ERROR_UNKNOWN,
    }
)


@dataclass(frozen=True)
class ModelDiscoveryResult:
    """A provider model listing plus how it was obtained.

    Attributes:
        models: Model identifiers to offer in the picker.
        source: ``live`` when the provider's listing endpoint answered,
            ``fallback`` when a bundled catalog (or empty list) was used.
        error: Short safe reason from FALLBACK_ERROR_REASONS when a live
            attempt failed or was impossible; None for a clean result (a
            keyless bundled catalog is a clean fallback, not an error).
    """

    models: List[str] = field(default_factory=list)
    source: DiscoverySource = "fallback"
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.error is not None and self.error not in FALLBACK_ERROR_REASONS:
            raise ValueError(f"error must be one of {sorted(FALLBACK_ERROR_REASONS)}")


def _live(models: List[str]) -> ModelDiscoveryResult:
    return ModelDiscoveryResult(models=models, source="live")


def _fallback(models: List[str], error: Optional[str] = None) -> ModelDiscoveryResult:
    return ModelDiscoveryResult(models=models, source="fallback", error=error)


def _classify_fetch_error(exc: Exception) -> str:
    """Map a live-fetch failure to a short safe reason.

    Classification is by exception TYPE NAME only: the message is never
    inspected for output purposes because provider SDK messages embed URLs
    (and, for query-string keys, credentials).
    """
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return ERROR_TIMEOUT
    if "connect" in name or isinstance(exc, (ConnectionError, OSError)):
        return ERROR_NETWORK
    return ERROR_UNKNOWN


# ---------------------------------------------------------------------------
# Curated audio catalogs (no provider ships a discovery endpoint for these)
# ---------------------------------------------------------------------------

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
    "moonshot": {"llm"},
    "zai": {"llm"},
    "mistral": {"llm"},
}


async def get_available_models_for_provider(
    provider: str,
    api_key: Optional[str] = None,
    model_kind: ModelKind = "llm",
    api_endpoint: Optional[str] = None,
) -> ModelDiscoveryResult:
    """
    Fetch available models from the specified AI provider, with provenance.

    Args:
        provider: The provider name (openai, anthropic, google, qwen,
            deepseek, moonshot, zai, mistral, openai-compatible, custom,
            openrouter)
        api_key: Optional API key for authentication
        model_kind: Service kind to fetch (llm, stt, or tts)
        api_endpoint: Base URL of an OpenAI-compatible endpoint. Required for
            the openai-compatible/custom providers, which have no fixed
            catalog; defaulted for providers in DEFAULT_PROVIDER_ENDPOINTS.

    Returns:
        ModelDiscoveryResult with the model identifiers, whether they came
        from a live listing or a bundled fallback, and a short safe error
        reason when a live attempt failed.

    Raises:
        ValueError: On an invalid model_kind, an invalid discovery endpoint,
            or when the provider rejected the API key (so the user learns the
            key is bad instead of silently seeing a stale catalog).
    """
    provider = provider.lower()
    normalized_model_kind = model_kind.lower()
    if normalized_model_kind not in {"llm", "stt", "tts"}:
        raise ProviderValidationError("model_kind must be one of llm, stt, or tts")

    supported_kinds = SUPPORTED_AUDIO_PROVIDER_KINDS.get(provider, {"llm"})
    if normalized_model_kind not in supported_kinds:
        return _fallback([], ERROR_UNSUPPORTED)

    # STT/TTS catalogs are curated: no provider exposes a listing endpoint
    # for them, so they are honest fallbacks rather than live results.
    if normalized_model_kind == "stt":
        return _fallback(_get_stt_models(provider))
    if normalized_model_kind == "tts":
        return _fallback(_get_tts_models(provider))

    if provider == "openai":
        return await _get_openai_models(api_key)
    elif provider == "anthropic":
        return await _get_anthropic_models(api_key)
    elif provider == "google":
        return await _get_google_models(api_key)
    elif provider == "qwen":
        return await _get_qwen_models(api_key, api_endpoint)
    elif provider == "deepseek":
        return await _get_deepseek_models(api_key)
    elif provider == "moonshot":
        return await _get_moonshot_models(api_key)
    elif provider == "zai":
        return await _get_zai_models(api_key)
    elif provider == "mistral":
        return await _get_mistral_models(api_key)
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
            return _fallback([], ERROR_MISSING_ENDPOINT)
        return await _get_openai_compatible_models(endpoint, api_key)
    else:
        # Unknown provider with no endpoint convention to follow.
        return _fallback([], ERROR_UNSUPPORTED)


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
        raise ProviderValidationError("api_endpoint is required for this provider")

    parts = urlsplit(raw)
    if parts.scheme not in {"http", "https"}:
        raise ProviderValidationError("api_endpoint must be an http(s) URL")
    host = (parts.hostname or "").strip()
    if not host:
        raise ProviderValidationError("api_endpoint must include a host")

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # A DNS name. Block the obvious loopback aliases; anything else is
        # allowed, matching how api_endpoint is treated elsewhere.
        if host.lower() in {"localhost", "localhost.localdomain"}:
            raise ProviderValidationError(
                "api_endpoint must not point at a private address"
            )
        return raw

    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise ProviderValidationError(
            "api_endpoint must not point at a private address"
        )
    return raw


# Regional DashScope hosts shown in the add-provider UI, plus workspace
# Model Studio hosts under *.maas.aliyuncs.com. Both sit under *.aliyuncs.com.
QWEN_ALLOWED_HOSTS = frozenset(
    {
        "dashscope.aliyuncs.com",
        "dashscope-intl.aliyuncs.com",
        "dashscope-us.aliyuncs.com",
    }
)
QWEN_ALLOWED_HOST_SUFFIXES = (".aliyuncs.com", ".maas.aliyuncs.com")


def _is_qwen_allowed_host(host: str) -> bool:
    """Return True if host is a DashScope / Model Studio hostname."""
    hostname = (host or "").strip().lower().rstrip(".")
    if not hostname:
        return False
    if hostname in QWEN_ALLOWED_HOSTS:
        return True
    return any(hostname.endswith(suffix) for suffix in QWEN_ALLOWED_HOST_SUFFIXES)


def validate_qwen_endpoint(api_endpoint: str) -> str:
    """Validate a Qwen discovery or chat endpoint, or raise ValueError.

    Applies the shared SSRF guard, then restricts the host to known
    DashScope / Model Studio names (``*.aliyuncs.com``, including the
    Beijing / Singapore / US hosts in the UI and workspace
    ``*.maas.aliyuncs.com`` endpoints). Other providers keep using
    ``validate_discovery_endpoint`` alone.
    """
    raw = validate_discovery_endpoint(api_endpoint)
    host = (urlsplit(raw).hostname or "").strip()
    if not _is_qwen_allowed_host(host):
        raise ProviderValidationError(
            "Qwen api_endpoint must be a DashScope or Model Studio host "
            "(*.aliyuncs.com)"
        )
    return raw


async def _get_openai_compatible_models(
    api_endpoint: str, api_key: Optional[str] = None
) -> ModelDiscoveryResult:
    """List models from an OpenAI-compatible endpoint's ``GET /models``.

    Works for OpenRouter, vLLM, LM Studio, Together, Groq and anything else
    that implements the OpenAI list-models shape ``{"data": [{"id": ...}]}``.
    Some servers (OpenRouter included) return a bare list, which is accepted
    too. There is no bundled catalog for arbitrary endpoints, so a failed
    fetch yields an empty fallback with a reason rather than a stale guess.
    """
    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        # Same guard as _get_anthropic_models/_get_google_models: a missing
        # SDK is a named fallback reason, not an unhandled ImportError
        # escaping into the endpoint layer.
        logger.warning("OpenAI package not installed, cannot list models")
        return _fallback([], ERROR_SDK_MISSING)

    base_url = validate_discovery_endpoint(api_endpoint).rstrip("/")
    try:
        client = AsyncOpenAI(
            api_key=api_key or "placeholder",
            base_url=base_url,
            max_retries=1,
            timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
        )
        # The SDK's __aenter__ returns the client itself; the context manager
        # closes the underlying HTTP connection pool on exit.
        async with client:
            response = await client.models.list()
    except AuthenticationError as e:
        # Do not log the key or the full request URL: a key passed as a query
        # param would otherwise land in logs (see the api_key handling in the
        # endpoint layer).
        logger.warning("Authentication failed listing models from %s", base_url)
        raise ProviderAuthError(
            "Invalid API key for this endpoint. Please check your API key and "
            "try again."
        ) from e
    except Exception as e:
        logger.warning("Failed to list models from %s: %s", base_url, type(e).__name__)
        return _fallback([], _classify_fetch_error(e))

    return _live(_extract_model_ids(response))


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


# Fallback catalog used when the OpenAI listing endpoint cannot be reached.
# Provenance: hand-curated from OpenAI's model documentation; every id also
# resolves in litellm's price map. Surfaced with source="fallback".
OPENAI_FALLBACK_MODELS = [
    "gpt-4.1",
    "gpt-4.1-mini",
    "gpt-4o",
    "gpt-4o-mini",
]

# Model-id substrings that mark an OpenAI model as NOT a chat completion
# model (embeddings, audio, image, moderation, legacy completions). This is
# an exclusion list on purpose: the previous allow-list of "gpt-*" silently
# hid the o-series reasoning models and any future chat family, and the
# additional [:10] cap hid everything below the first ten ids.
_OPENAI_NON_CHAT_MARKERS = (
    "instruct",
    "search",
    "similarity",
    "edit",
    "insert",
    "audio",
    "realtime",
    "transcribe",
    "whisper",
    "tts",
    "dall-e",
    "image",
    "sora",
    "embed",
    "moderation",
    "davinci",
    "babbage",
    "curie",
    "computer-use",
)


async def _get_openai_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Fetch available models from the OpenAI API.

    Returns the full chat-capable listing (no arbitrary cap), excluding known
    non-chat families rather than allow-listing "gpt-*" only, so o-series and
    future chat model families stay visible.
    """
    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        # The import must be guarded separately: if it stayed inside the main
        # try below, an ImportError would make the interpreter evaluate
        # ``except AuthenticationError`` against an unbound name and raise
        # NameError instead of returning a fallback.
        logger.warning("OpenAI package not installed, cannot list models")
        return _fallback(OPENAI_FALLBACK_MODELS, ERROR_SDK_MISSING)

    try:
        # Use provided API key or fall back to environment variable. Timeout
        # and single retry match every other provider in this file: without
        # them a hung connection blocks the listing request indefinitely.
        client = (
            AsyncOpenAI(
                api_key=api_key,
                max_retries=1,
                timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
            )
            if api_key
            else AsyncOpenAI(
                max_retries=1,
                timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
            )
        )
        async with client:
            models_response = await client.models.list()
        models = models_response.data

        chat_models = [
            model.id
            for model in models
            if not any(marker in model.id for marker in _OPENAI_NON_CHAT_MARKERS)
        ]

        # Sort with newer models first
        chat_models.sort(reverse=True)

        return _live(chat_models)
    except AuthenticationError as e:
        logger.warning("OpenAI authentication failed: %s", type(e).__name__)
        # Re-raise authentication errors so the user knows their API key is invalid
        raise ProviderAuthError(
            "Invalid OpenAI API key. Please check your API key and try again."
        )
    except Exception as e:
        logger.warning("Failed to fetch OpenAI models: %s", type(e).__name__)
        # Return fallback list for other errors (network issues, etc.)
        return _fallback(OPENAI_FALLBACK_MODELS, _classify_fetch_error(e))


# Fallback catalog used when the Anthropic listing endpoint cannot be
# reached or no key is available. Provenance: hand-curated current ids;
# every id must resolve in services/data/model_prices.json (pinned by
# tests/services/test_ai_model_provider.py).
ANTHROPIC_FALLBACK_MODELS = [
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
]


async def _get_anthropic_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """List Anthropic models live via ``GET /v1/models`` when a key is given.

    Replaces the previous paid ``messages.create`` validation ping: the
    models listing both validates the key (401 on a bad key) and returns the
    actual catalog, for free. Only the first page is fetched, with
    ``limit=1000``; Anthropic serves far fewer models than that, so
    pagination is not followed.
    """
    if not api_key:
        return _fallback(ANTHROPIC_FALLBACK_MODELS)

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.warning("Anthropic package not installed, cannot list models")
        return _fallback(ANTHROPIC_FALLBACK_MODELS, ERROR_SDK_MISSING)

    try:
        client = AsyncAnthropic(api_key=api_key)
        # The SDK's __aenter__ returns the client itself; the context manager
        # closes the underlying HTTP connection pool on exit.
        async with client:
            page = await client.models.list(limit=1000)
    except Exception as e:
        error_msg = str(e).lower()
        error_type = type(e).__name__.lower()
        # AuthenticationError from the SDK, plus keyword matching for
        # proxy-wrapped errors that lose the exception type.
        if any(
            keyword in error_msg
            for keyword in [
                "401",
                "unauthorized",
                "authentication",
                "invalid_api_key",
            ]
        ) or any(
            keyword in error_type
            for keyword in ["authentication", "permission", "unauthorized"]
        ):
            logger.warning("Anthropic authentication failed: %s", type(e).__name__)
            raise ProviderAuthError(
                "Invalid Anthropic API key. Please check your API key and try again."
            )
        logger.warning(
            "Failed to list Anthropic models, using bundled fallback: %s",
            type(e).__name__,
        )
        return _fallback(ANTHROPIC_FALLBACK_MODELS, _classify_fetch_error(e))

    model_ids = _extract_model_ids(page)
    if not model_ids:
        logger.info("Anthropic returned no models, using bundled fallback")
        return _fallback(ANTHROPIC_FALLBACK_MODELS, ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d Anthropic models", len(model_ids))
    return _live(model_ids)


# Fallback catalog used when the Google listing call cannot be made.
# Provenance: hand-curated current Gemini ids, no deprecated preview/exp names.
def _build_scoped_google_client(api_key: str) -> Optional[object]:
    """Build a Gemini model client bound to ``api_key`` only, or None.

    ``genai.configure(api_key=...)`` mutates PROCESS-GLOBAL SDK state, so two
    concurrent discovery requests for different accounts can race and one
    account's listing can be made with the other's key. Building a per-call
    client keeps the key on the call stack instead.

    The pinned SDK (google-generativeai 0.8.x) has no ``genai.Client``, but
    ``genai.list_models`` accepts a ``client`` argument, and the underlying
    ``ModelServiceClient`` takes a per-instance api_key through
    ``ClientOptions``. Returns None when that construction is unavailable, so
    the caller can fall back rather than lose listing entirely.
    """
    try:
        import google.ai.generativelanguage as glm
        from google.api_core import client_options as client_options_lib

        return glm.ModelServiceClient(
            client_options=client_options_lib.ClientOptions(api_key=api_key)
        )
    except Exception as e:
        # Never log the key or the exception text; the type name is enough to
        # tell an operator which construction path is unavailable.
        logger.debug(
            "Key-scoped Google client unavailable (%s), falling back to "
            "process-global configure()",
            type(e).__name__,
        )
        return None


GOOGLE_FALLBACK_MODELS = [
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
]


async def _get_google_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """List Google Gemini models live via ``genai.list_models`` when keyed.

    ``list_models`` itself fails on a bad key, so no separate paid
    ``generate_content`` validation ping is made (it previously ran when the
    listing produced nothing, burning tokens to validate a key the listing
    had already exercised).
    """
    if not api_key:
        return _fallback(GOOGLE_FALLBACK_MODELS)

    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("Google GenerativeAI package not installed, cannot list models")
        return _fallback(GOOGLE_FALLBACK_MODELS, ERROR_SDK_MISSING)

    try:
        fetched_models = []
        list_models = getattr(genai, "list_models", None)
        if not callable(list_models):
            logger.warning("genai.list_models unavailable, using bundled fallback")
            return _fallback(GOOGLE_FALLBACK_MODELS, ERROR_SDK_MISSING)

        scoped_client = _build_scoped_google_client(api_key)
        if scoped_client is not None:
            listing = list_models(client=scoped_client)
        else:
            # No key-scoped client available: fall back to the process-global
            # configure() so listing keeps working on older/newer SDKs. This
            # path carries the cross-request contamination risk described in
            # _build_scoped_google_client.
            genai.configure(api_key=api_key)
            listing = list_models()

        for model in listing:
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
    except Exception as e:
        error_msg = str(e).lower()
        error_type = type(e).__name__.lower()
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
            logger.warning("Google authentication failed: %s", type(e).__name__)
            raise ProviderAuthError(
                "Invalid Google API key. Please check your API key and try again."
            )
        logger.warning(
            "Failed to list Google models, using bundled fallback: %s",
            type(e).__name__,
        )
        return _fallback(GOOGLE_FALLBACK_MODELS, _classify_fetch_error(e))

    if not fetched_models:
        logger.info("Google returned no models, using bundled fallback")
        return _fallback(GOOGLE_FALLBACK_MODELS, ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d Google models", len(fetched_models))
    return _live(sorted(set(fetched_models), reverse=True))


async def _get_catalog_provider_models(
    *,
    provider_label: str,
    base_url: str,
    known_models: List[str],
    api_key: Optional[str] = None,
) -> ModelDiscoveryResult:
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
        return _fallback(known_models)

    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        logger.warning(
            "OpenAI package not installed, cannot list %s models", provider_label
        )
        return _fallback(known_models, ERROR_SDK_MISSING)

    try:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=1,
            timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
        )
        # The SDK's __aenter__ returns the client itself; the context manager
        # closes the underlying HTTP connection pool on exit.
        async with client:
            response = await client.models.list()
    except AuthenticationError as e:
        logger.warning("%s authentication failed: %s", provider_label, type(e).__name__)
        raise ProviderAuthError(
            f"Invalid {provider_label} API key. Please check your API key and "
            "try again."
        ) from e
    except Exception as e:
        logger.warning(
            "Failed to list %s models, using bundled catalog: %s",
            provider_label,
            type(e).__name__,
        )
        return _fallback(known_models, _classify_fetch_error(e))

    live_models = _extract_model_ids(response)
    if not live_models:
        # A valid key that lists nothing is more likely a proxy quirk than an
        # empty account; an empty picker is the worse outcome either way.
        logger.info("%s returned no models, using bundled catalog", provider_label)
        return _fallback(known_models, ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d %s models", len(live_models), provider_label)
    return _live(live_models)


# China (Beijing) DashScope compatible-mode host. Existing keys target this
# URL; do not rename or replace it. International and US Model Studio hosts
# are accepted via the stored api_endpoint (keys are per-region).
QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_INTL_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_US_BASE_URL = "https://dashscope-us.aliyuncs.com/compatible-mode/v1"

# Fallback catalog used when no Qwen / Model Studio key is available.
# Provenance: Alibaba Cloud Model Studio text-generation list and the
# qwen3.8-max product page (fetched 2026-08-17). Chat/completions only:
# image, video, TTS, NSFW, Happy Horse, and Z-Image are excluded.
# ORDER MATTERS: qwen3.8-max first (the headline model as of 2026-08-03).
# Every id here is priced in services/data/model_prices.json under
# dashscope/<id> (pinned by tests/services/test_model_pricing.py).
QWEN_KNOWN_MODELS = [
    "qwen3.8-max",
    "qwen3.7-max",
    "qwen3.7-plus",
    "qwen3.6-flash",
    "qwen3.5-plus",
    "qwen3-max",
    "qwen3-coder-plus",
    "qwen-plus",
    "qwen-flash",
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

# Fallback catalog used when no Moonshot key is available to list the live
# set. Provenance: https://platform.kimi.ai/docs/models.md (fetched
# 2026-08-05). ORDER MATTERS: kimi-k3 first (the headline model). The sunset
# moonshot-v1 series and deprecated kimi-k2 previews are deliberately absent.
# Every id here is priced in services/data/model_prices.json under
# moonshot/<id> (pinned by tests/services/test_model_pricing.py).
MOONSHOT_KNOWN_MODELS = [
    "kimi-k3",
    "kimi-k2.7-code",
    "kimi-k2.7-code-highspeed",
    "kimi-k2.6",
]

# Fallback catalog used when no Z.ai key is available to list the live set.
# Provenance: the zai/* entries in the pinned litellm price map (litellm
# 1.82.1 prices glm-5 and glm-5.1, so they are included). Every id resolves
# to zai/<id> in litellm's map (pinned by tests/services/test_model_pricing.py).
ZAI_KNOWN_MODELS = [
    "glm-5.1",
    "glm-5",
    "glm-4.7",
    "glm-4.7-flash",
]

# Fallback catalog used when no Mistral key is available to list the live
# set. Provenance: current chat models from the mistral/* entries in the
# pinned litellm price map; embeddings (*-embed) and OCR (ocr-*) ids are
# excluded on purpose. The *-latest aliases track Mistral's own rolling
# pointers so this list ages more slowly than dated ids would.
MISTRAL_KNOWN_MODELS = [
    "mistral-large-latest",
    "mistral-medium-latest",
    "mistral-small-latest",
    "codestral-latest",
    "devstral-latest",
    "ministral-8b-latest",
]


def _is_qwen_chat_model(model_id: str) -> bool:
    """Keep chat/completions SKUs; drop dedicated media, audio, and NSFW ids.

    Multimodal chat flagships (``qwen3.8-max``) stay. Dedicated Wan video,
    Qwen-Image, Qwen-VL product lines, audio/TTS, Happy Horse, Z-Image,
    hyphen-delimited omni/MT/s2s families, live-translate SKUs, and any id
    carrying ``nsfw`` are excluded from the picker.
    """
    lowered = (model_id or "").strip().lower()
    if not lowered:
        return False
    blocked_prefixes = (
        "wan",
        "happy-horse",
        "z-image",
        "qwen-image",
        "qwen-vl",
        "qwen-audio",
        "qwen-tts",
        "qwen2.5-vl",
        "qvq-",
        "tongyi-tingwu",
    )
    blocked_substrings = (
        "nsfw",
        "-tts",
        "-asr",
        "-stt",
        "-embedding",
        "-rerank",
        "-vl-",
        "livetranslate",
    )
    # Hyphen-delimited families so a qwen4-omni / qwen3-mt-plus bump stays
    # hidden without another prefix edit. Tokens, not bare substrings, so
    # a third-party id like omnilingual-chat would still list.
    blocked_family_tokens = frozenset({"omni", "mt", "s2s"})
    if any(lowered.startswith(prefix) for prefix in blocked_prefixes):
        return False
    if any(token in lowered for token in blocked_substrings):
        return False
    if any(part in blocked_family_tokens for part in lowered.split("-")):
        return False
    return True


async def _get_qwen_models(
    api_key: Optional[str] = None,
    api_endpoint: Optional[str] = None,
) -> ModelDiscoveryResult:
    """Return Qwen / Model Studio models, live when a key is supplied.

    Default base URL is the China (Beijing) DashScope compatible-mode host so
    existing keys keep working. A caller-supplied endpoint (Singapore intl,
    US Virginia, or a workspace-specific ``*.maas.aliyuncs.com`` host) is
    used instead after DashScope host allowlisting. Region keys are not
    interchangeable.
    """
    raw = (api_endpoint or "").strip()
    if raw:
        base_url = validate_qwen_endpoint(raw).rstrip("/")
    else:
        base_url = QWEN_DEFAULT_BASE_URL

    result = await _get_catalog_provider_models(
        provider_label="Qwen",
        base_url=base_url,
        known_models=QWEN_KNOWN_MODELS,
        api_key=api_key,
    )
    if result.source != "live":
        return result

    filtered = [model_id for model_id in result.models if _is_qwen_chat_model(model_id)]
    if not filtered:
        logger.info(
            "Qwen live list had no chat models after media/NSFW filter, "
            "using bundled catalog"
        )
        return _fallback(QWEN_KNOWN_MODELS, ERROR_EMPTY_RESPONSE)
    return _live(filtered)


async def _get_deepseek_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return DeepSeek models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        known_models=DEEPSEEK_KNOWN_MODELS,
        api_key=api_key,
    )


async def _get_moonshot_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Moonshot (Kimi) models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="Moonshot (Kimi)",
        base_url="https://api.moonshot.ai/v1",
        known_models=MOONSHOT_KNOWN_MODELS,
        api_key=api_key,
    )


async def _get_zai_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Z.ai (GLM) models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="Z.ai (GLM)",
        base_url="https://api.z.ai/api/paas/v4",
        known_models=ZAI_KNOWN_MODELS,
        api_key=api_key,
    )


async def _get_mistral_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Mistral models, live from the API when a key is supplied."""
    return await _get_catalog_provider_models(
        provider_label="Mistral",
        base_url="https://api.mistral.ai/v1",
        known_models=MISTRAL_KNOWN_MODELS,
        api_key=api_key,
    )
