"""Service for fetching available models from AI providers.

Every provider lists live when credentials (and an endpoint where required)
are present. There is no bundled picker catalog: a failed or impossible live
attempt returns an empty list with provenance. ``source`` is ``live`` when
the provider's own listing endpoint answered, ``fallback`` when the list is
empty, with a short machine-readable ``error`` reason. The reason is drawn
from a fixed vocabulary and never contains raw exception text, which can
carry endpoint URLs and key material (2026-08-04 key-leak incident).
"""

import asyncio
import ipaddress
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional
from urllib.parse import urlsplit

from preloop.services.tls_verify import ssl_verify_setting

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
ERROR_MISSING_KEY = "missing_key"
ERROR_AUTH = "auth"
ERROR_UNKNOWN = "unknown"

FALLBACK_ERROR_REASONS = frozenset(
    {
        ERROR_TIMEOUT,
        ERROR_NETWORK,
        ERROR_EMPTY_RESPONSE,
        ERROR_UNSUPPORTED,
        ERROR_MISSING_ENDPOINT,
        ERROR_SDK_MISSING,
        ERROR_MISSING_KEY,
        ERROR_AUTH,
        ERROR_UNKNOWN,
    }
)


@dataclass(frozen=True)
class ModelDiscoveryResult:
    """A provider model listing plus how it was obtained.

    Attributes:
        models: Model identifiers to offer in the picker.
        source: ``live`` when the provider's listing endpoint answered,
            ``fallback`` when the list is empty because live listing failed
            or was impossible.
        error: Short safe reason from FALLBACK_ERROR_REASONS when a live
            attempt failed or was impossible; None for a clean live result.
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
    "bedrock": {"llm"},
}


async def get_available_models_for_provider(
    provider: str,
    api_key: Optional[str] = None,
    model_kind: ModelKind = "llm",
    api_endpoint: Optional[str] = None,
    *,
    aws_auth: Optional[Dict[str, Any]] = None,
) -> ModelDiscoveryResult:
    """
    Fetch available models from the specified AI provider, with provenance.

    Args:
        provider: The provider name (openai, anthropic, google, qwen,
            deepseek, moonshot, zai, mistral, bedrock, openai-compatible,
            custom, openrouter)
        api_key: Optional API key for authentication
        model_kind: Service kind to fetch (llm, stt, or tts)
        api_endpoint: Base URL of an OpenAI-compatible endpoint. Required for
            the openai-compatible/custom providers, which have no fixed
            catalog; defaulted for providers in DEFAULT_PROVIDER_ENDPOINTS.
        aws_auth: Optional AWS credential mapping for the bedrock provider,
            with any of the keys ``aws_access_key_id``,
            ``aws_secret_access_key``, ``aws_session_token`` and
            ``aws_region_name``. Typed or stored credentials are required;
            ambient instance-profile listing is not used for the picker.

    Returns:
        ModelDiscoveryResult with the model identifiers, whether they came
        from a live listing, and a short safe error reason when a live
        attempt failed or credentials were missing.

    Raises:
        ValueError: On an invalid model_kind, an invalid discovery endpoint,
            or when the provider rejected the API key (so the user learns the
            key is bad instead of silently seeing an empty picker).
    """
    provider = provider.lower()
    normalized_model_kind = model_kind.lower()
    if normalized_model_kind not in {"llm", "stt", "tts"}:
        raise ProviderValidationError("model_kind must be one of llm, stt, or tts")

    supported_kinds = SUPPORTED_AUDIO_PROVIDER_KINDS.get(provider, {"llm"})
    if normalized_model_kind not in supported_kinds:
        return _fallback([], ERROR_UNSUPPORTED)

    if provider == "openai":
        return await _get_openai_models(api_key, model_kind=normalized_model_kind)
    elif provider == "anthropic":
        return await _get_anthropic_models(api_key)
    elif provider == "google":
        if normalized_model_kind == "stt":
            return _get_google_stt_models(api_key)
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
    elif provider == "bedrock":
        return await _get_bedrock_models(aws_auth)
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
        if provider == "openrouter" and not (api_key or "").strip():
            return _fallback([], ERROR_MISSING_KEY)
        return await _get_openai_compatible_models(
            endpoint, api_key, model_kind=normalized_model_kind
        )
    else:
        # Unknown provider with no endpoint convention to follow.
        return _fallback([], ERROR_UNSUPPORTED)


def _discovery_http_client() -> Optional[Any]:
    """Return an httpx client that trusts a mounted private CA, if configured.

    The OpenAI SDK uses certifi by default and ignores SSL_CERT_FILE.
    Operators mount a CA via Helm extraVolumes and set SSL_CERT_FILE;
    that path is passed as httpx ``verify``.
    """
    verify = ssl_verify_setting()
    if verify is None:
        return None
    import httpx

    return httpx.AsyncClient(
        verify=verify,
        timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
    )


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
    api_endpoint: str,
    api_key: Optional[str] = None,
    model_kind: ModelKind = "llm",
) -> ModelDiscoveryResult:
    """List models from an OpenAI-compatible endpoint's ``GET /models``.

    Works for OpenRouter, vLLM, LM Studio, Together, Groq and anything else
    that implements the OpenAI list-models shape ``{"data": [{"id": ...}]}``.
    Some servers (OpenRouter included) return a bare list, which is accepted
    too. There is no bundled catalog for arbitrary endpoints, so a failed
    fetch yields an empty fallback with a reason rather than a stale guess.

    ``stt``/``tts`` narrow the live ids by the same name markers OpenAI ids
    use, which needs no catalog. The ``llm`` kind keeps the full live list:
    the LLM filter is an exclusion list tuned to OpenAI's naming, and markers
    like ``instruct`` are ordinary chat models on a self-hosted endpoint
    (``Llama-3-8B-Instruct``), so applying it here would hide real models.
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
    http_client = _discovery_http_client()
    try:
        client_kwargs: Dict[str, Any] = {
            "api_key": api_key or "placeholder",
            "base_url": base_url,
            "max_retries": 1,
            "timeout": MODEL_DISCOVERY_TIMEOUT_SECONDS,
        }
        if http_client is not None:
            client_kwargs["http_client"] = http_client
        client = AsyncOpenAI(**client_kwargs)
        # The SDK's __aenter__ returns the client itself; the context manager
        # closes the underlying HTTP connection pool on exit. A custom
        # http_client is owned here and closed in finally.
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
    finally:
        if http_client is not None:
            await http_client.aclose()

    model_ids = _extract_model_ids(response)
    if model_kind in ("stt", "tts"):
        model_ids = _openai_ids_for_kind(model_ids, model_kind)
        if not model_ids:
            return _fallback([], ERROR_EMPTY_RESPONSE)
    return _live(model_ids)


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


def _get_google_stt_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Google Cloud Speech-to-Text has no API-key list-models resource.

    Recognition uses POST https://speech.googleapis.com/v1/speech:recognize
    (see ``audio_model.GOOGLE_SPEECH_RECOGNIZE_URL``). The v2 models list
    (https://cloud.google.com/speech-to-text/docs/reference/rest/v2/projects.locations.models/list)
    requires ``projects/{project}/locations/{location}``, which the API-key
    flow does not have. Return empty rather than a stale guess.
    """
    if not (api_key or "").strip():
        return _fallback([], ERROR_MISSING_KEY)
    return _fallback([], ERROR_MISSING_ENDPOINT)


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


_OPENAI_STT_MARKERS = ("transcribe", "whisper")
_OPENAI_TTS_MARKERS = ("tts",)


def _openai_ids_for_kind(model_ids: List[str], model_kind: ModelKind) -> List[str]:
    """Keep OpenAI ids that match the requested service kind."""
    if model_kind == "stt":
        return [
            model_id
            for model_id in model_ids
            if any(marker in model_id.lower() for marker in _OPENAI_STT_MARKERS)
        ]
    if model_kind == "tts":
        return [
            model_id
            for model_id in model_ids
            if any(marker in model_id.lower() for marker in _OPENAI_TTS_MARKERS)
        ]
    return [
        model_id
        for model_id in model_ids
        if not any(marker in model_id for marker in _OPENAI_NON_CHAT_MARKERS)
    ]


async def _get_openai_models(
    api_key: Optional[str] = None,
    model_kind: ModelKind = "llm",
) -> ModelDiscoveryResult:
    """Fetch available models from OpenAI ``GET https://api.openai.com/v1/models``.

    LLM ids exclude known non-chat families. STT/TTS ids are the same live
    list filtered to transcribe/whisper and tts families.
    """
    if not (api_key or "").strip():
        return _fallback([], ERROR_MISSING_KEY)

    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        # The import must be guarded separately: if it stayed inside the main
        # try below, an ImportError would make the interpreter evaluate
        # ``except AuthenticationError`` against an unbound name and raise
        # NameError instead of returning a fallback.
        logger.warning("OpenAI package not installed, cannot list models")
        return _fallback([], ERROR_SDK_MISSING)

    try:
        client = AsyncOpenAI(
            api_key=api_key,
            max_retries=1,
            timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
        )
        async with client:
            models_response = await client.models.list()
        models = models_response.data

        model_ids = [model.id for model in models if getattr(model, "id", None)]
        filtered = _openai_ids_for_kind(model_ids, model_kind)
        filtered.sort(reverse=True)
        if not filtered:
            return _fallback([], ERROR_EMPTY_RESPONSE)
        return _live(filtered)
    except AuthenticationError:
        logger.warning("OpenAI authentication failed: %s", "AuthenticationError")
        # Re-raise authentication errors so the user knows their API key is invalid
        raise ProviderAuthError(
            "Invalid OpenAI API key. Please check your API key and try again."
        )
    except Exception as e:
        logger.warning("Failed to fetch OpenAI models: %s", type(e).__name__)
        return _fallback([], _classify_fetch_error(e))


async def _get_anthropic_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """List Anthropic models live via ``GET https://api.anthropic.com/v1/models``.

    Replaces the previous paid ``messages.create`` validation ping: the
    models listing both validates the key (401 on a bad key) and returns the
    actual catalog, for free. Only the first page is fetched, with
    ``limit=1000``; Anthropic serves far fewer models than that, so
    pagination is not followed.
    """
    if not api_key:
        return _fallback([], ERROR_MISSING_KEY)

    try:
        from anthropic import AsyncAnthropic
    except ImportError:
        logger.warning("Anthropic package not installed, cannot list models")
        return _fallback([], ERROR_SDK_MISSING)

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
            "Failed to list Anthropic models: %s",
            type(e).__name__,
        )
        return _fallback([], _classify_fetch_error(e))

    model_ids = _extract_model_ids(page)
    if not model_ids:
        logger.info("Anthropic returned no models")
        return _fallback([], ERROR_EMPTY_RESPONSE)

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


async def _get_google_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """List Google Gemini models live via ``genai.list_models``.

    Listing API: Google AI Generative Language ``models.list``
    (https://generativelanguage.googleapis.com/v1beta/models). ``list_models``
    itself fails on a bad key, so no separate paid ``generate_content``
    validation ping is made.
    """
    if not api_key:
        return _fallback([], ERROR_MISSING_KEY)

    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("Google GenerativeAI package not installed, cannot list models")
        return _fallback([], ERROR_SDK_MISSING)

    try:
        fetched_models = []
        list_models = getattr(genai, "list_models", None)
        if not callable(list_models):
            logger.warning("genai.list_models unavailable")
            return _fallback([], ERROR_SDK_MISSING)

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
            "Failed to list Google models: %s",
            type(e).__name__,
        )
        return _fallback([], _classify_fetch_error(e))

    if not fetched_models:
        logger.info("Google returned no models")
        return _fallback([], ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d Google models", len(fetched_models))
    return _live(sorted(set(fetched_models), reverse=True))


async def _get_catalog_provider_models(
    *,
    provider_label: str,
    base_url: str,
    api_key: Optional[str] = None,
) -> ModelDiscoveryResult:
    """List models from an OpenAI-compatible ``GET {base_url}/models``.

    Without a key there is nothing to query. With a key we return what the
    provider actually serves. Authentication failures still raise so the
    user knows the key is bad. Any other failure returns an empty list with
    a safe reason rather than a stale guess.
    """
    if not api_key:
        return _fallback([], ERROR_MISSING_KEY)

    try:
        from openai import AsyncOpenAI, AuthenticationError
    except ImportError:
        logger.warning(
            "OpenAI package not installed, cannot list %s models", provider_label
        )
        return _fallback([], ERROR_SDK_MISSING)

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
            "Failed to list %s models: %s",
            provider_label,
            type(e).__name__,
        )
        return _fallback([], _classify_fetch_error(e))

    live_models = _extract_model_ids(response)
    if not live_models:
        logger.info("%s returned no models", provider_label)
        return _fallback([], ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d %s models", len(live_models), provider_label)
    return _live(live_models)


# China (Beijing) DashScope compatible-mode host. Existing keys target this
# URL; do not rename or replace it. International and US Model Studio hosts
# are accepted via the stored api_endpoint (keys are per-region).
QWEN_DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
QWEN_INTL_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
QWEN_US_BASE_URL = "https://dashscope-us.aliyuncs.com/compatible-mode/v1"


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
    """Return Qwen / Model Studio models via DashScope compatible-mode
    ``GET {base}/models``.

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
        api_key=api_key,
    )
    if result.source != "live":
        return result

    filtered = [model_id for model_id in result.models if _is_qwen_chat_model(model_id)]
    if not filtered:
        logger.info("Qwen live list had no chat models after media/NSFW filter")
        return _fallback([], ERROR_EMPTY_RESPONSE)
    return _live(filtered)


async def _get_deepseek_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return DeepSeek models via ``GET https://api.deepseek.com/v1/models``."""
    return await _get_catalog_provider_models(
        provider_label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        api_key=api_key,
    )


async def _get_moonshot_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Moonshot (Kimi) models via ``GET https://api.moonshot.ai/v1/models``."""
    return await _get_catalog_provider_models(
        provider_label="Moonshot (Kimi)",
        base_url="https://api.moonshot.ai/v1",
        api_key=api_key,
    )


async def _get_zai_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Z.ai (GLM) models via ``GET https://api.z.ai/api/paas/v4/models``."""
    return await _get_catalog_provider_models(
        provider_label="Z.ai (GLM)",
        base_url="https://api.z.ai/api/paas/v4",
        api_key=api_key,
    )


async def _get_mistral_models(api_key: Optional[str] = None) -> ModelDiscoveryResult:
    """Return Mistral models via ``GET https://api.mistral.ai/v1/models``."""
    return await _get_catalog_provider_models(
        provider_label="Mistral",
        base_url="https://api.mistral.ai/v1",
        api_key=api_key,
    )


# ClientError codes (and HTTP statuses) that mean the AWS credentials were
# rejected or are missing permissions, mapped to ProviderAuthError so the
# user learns their key is bad instead of seeing a stale catalog.
_BEDROCK_AUTH_ERROR_CODES = frozenset(
    {
        "unrecognizedclientexception",
        "invalidclienttokenid",
        "invalidsignatureexception",
        "expiredtoken",
        "expiredtokenexception",
        "accessdeniedexception",
        "unauthorizedoperation",
        "accessdenied",
    }
)


def _is_bedrock_auth_error(exc: Exception) -> bool:
    """Whether a botocore exception represents rejected AWS credentials."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        http_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if http_status in (401, 403):
            return True
        code = str(response.get("Error", {}).get("Code") or "").lower()
        if code in _BEDROCK_AUTH_ERROR_CODES:
            return True
    # NoCredentialsError and friends carry no response payload at all.
    name = type(exc).__name__.lower()
    return "nocredentials" in name or "credential" in name


def _is_bedrock_chat_model(summary: Any) -> bool:
    """Keep ACTIVE foundation models that emit text; drop the rest.

    ``list_foundation_models`` also returns embedding, image and video
    generation models, which the LLM picker cannot use. A summary without an
    ``outputModalities`` field predates modality reporting and is kept rather
    than guessed away.
    """
    lifecycle = getattr(summary, "modelLifecycle", None)
    if lifecycle is not None:
        status = str(getattr(lifecycle, "status", "") or "").upper()
        if status and status != "ACTIVE":
            return False

    modalities = getattr(summary, "outputModalities", None) or []
    if not modalities:
        return True
    return any(str(m).upper() == "TEXT" for m in modalities)


async def _get_bedrock_models(
    aws_auth: Optional[Dict[str, Any]] = None,
) -> ModelDiscoveryResult:
    """List AWS Bedrock foundation models via ``list_foundation_models``.

    Control-plane API:
    https://docs.aws.amazon.com/bedrock/latest/APIReference/API_ListFoundationModels.html
    Uses boto3's ``bedrock`` client. Explicit access keys (typed or stored)
    are required for the picker; ambient instance-profile listing is not
    used. A bad key fails the call with an auth error, which raises
    ProviderAuthError.

    Args:
        aws_auth: Mapping with ``aws_access_key_id``,
            ``aws_secret_access_key``, optional ``aws_session_token`` and
            ``aws_region_name``.

    Returns:
        ModelDiscoveryResult with sorted text-output foundation model ids.

    Raises:
        ProviderValidationError: When no region can be resolved.
        ProviderAuthError: When AWS rejects the credentials.
    """
    auth = {key: str(value).strip() for key, value in (aws_auth or {}).items() if value}
    if not auth.get("aws_access_key_id") or not auth.get("aws_secret_access_key"):
        return _fallback([], ERROR_MISSING_KEY)

    try:
        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]
    except ImportError:
        logger.warning("boto3 package not installed, cannot list Bedrock models")
        return _fallback([], ERROR_SDK_MISSING)

    session_kwargs = {
        key: auth[key]
        for key in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token")
        if auth.get(key)
    }

    try:
        session = boto3.Session(**session_kwargs)
        region = auth.get("aws_region_name") or session.region_name
        if not region:
            raise ProviderValidationError(
                "AWS region is required. Enter a region (e.g. us-east-1) "
                "or set AWS_DEFAULT_REGION."
            )
        client = session.client(
            "bedrock",
            region_name=region,
            config=Config(
                connect_timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
                read_timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
                retries={"max_attempts": 1},
            ),
        )
        # list_foundation_models is synchronous; keep it off the event loop.
        response = await asyncio.to_thread(client.list_foundation_models)
    except ProviderValidationError:
        raise
    except Exception as e:
        from botocore.exceptions import (  # type: ignore[import-untyped]
            BotoCoreError,
            ClientError,
        )

        if isinstance(e, (ClientError, BotoCoreError)) and _is_bedrock_auth_error(e):
            logger.warning("Bedrock authentication failed: %s", type(e).__name__)
            raise ProviderAuthError(
                "Invalid AWS credentials. Check the access key, secret key "
                "and region, then try again."
            ) from e
        logger.warning(
            "Failed to list Bedrock models: %s",
            type(e).__name__,
        )
        return _fallback([], _classify_fetch_error(e))

    summaries = getattr(response, "modelSummaries", None) or []
    model_ids = []
    for summary in summaries:
        model_id = str(getattr(summary, "modelId", "") or "").strip()
        if model_id and _is_bedrock_chat_model(summary):
            model_ids.append(model_id)

    model_ids = sorted(set(model_ids))[:MAX_DISCOVERED_MODELS]
    if not model_ids:
        logger.info("Bedrock returned no chat models")
        return _fallback([], ERROR_EMPTY_RESPONSE)

    logger.info("Listed %d Bedrock models", len(model_ids))
    return _live(model_ids)
