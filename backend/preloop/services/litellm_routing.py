"""Shared litellm model-string routing for AIModel rows.

Every service that hands an ``AIModel`` to litellm must build the
``provider/model`` string the same way — this module is the single
implementation (the gateway, policy generation, approval summaries, and
session-explorer analysis previously carried divergent copies).

Resolution order:

1. An OpenRouter ``api_endpoint`` (or the ``openrouter`` provider) routes via
   litellm's OpenRouter adapter, keeping the vendor-prefixed upstream id
   intact (``openrouter/deepseek/deepseek-v4-flash-0731`` is sent upstream as
   ``deepseek/deepseek-v4-flash-0731``).
2. An explicit OpenAI-compatible provider with its own ``api_endpoint``
   dispatches via litellm's generic OpenAI-compatible adapter, with the
   stored identifier forwarded verbatim.
3. An identifier that already carries a routable prefix (``azure/gpt-5.4``)
   passes through untouched.
4. The local prefix map translates Preloop provider names to litellm ones
   (``google`` -> ``gemini``, ``qwen`` -> ``openai``).
5. Providers litellm routes natively (``mistral``, ``groq``, ...) pass
   through by name.
6. Unknown provider names with their own endpoint — arbitrary
   OpenAI-compatible providers imported from agent configs, e.g. Hermes
   ``model.provider: custom`` entries like ``kimi-for-coding`` — dispatch via
   litellm's generic OpenAI-compatible adapter (``openai/<model>``); callers
   pass the model's ``api_endpoint`` as ``api_base``. litellm rejects unknown
   prefixes outright ("LLM Provider NOT provided") even when ``api_base`` is
   set, so the name must not be forwarded verbatim.
7. Unknown provider names without an endpoint keep the name so the litellm
   error stays attributable instead of silently hitting api.openai.com.

Rules 1 and 2 exist because the vendor prefix inside a model id is not a
routing instruction: ``deepseek/deepseek-v4-flash-0731`` on
``https://openrouter.ai/api/v1`` is an OpenRouter model id, not a request to
call api.deepseek.com. Inferring the provider from that prefix (rule 3) ahead
of the stored provider/endpoint sent live traffic to the wrong vendor with a
truncated model id (issue #172).
"""

import os
from functools import lru_cache
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import litellm

from preloop.models.models.ai_model import AIModel
from preloop.services.tls_verify import ssl_verify_setting

PROVIDER_PREFIX: Dict[str, str] = {
    "openai": "openai",
    "openai-codex": "openai",
    "anthropic": "anthropic",
    "google": "gemini",
    "gemini": "gemini",
    "bedrock": "bedrock",
    "amazon-bedrock": "bedrock",
    "qwen": "openai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    # moonshot and zai match their litellm provider names, so they would
    # also resolve through known_litellm_providers(); pinned here so routing
    # and pricing-candidate generation do not depend on litellm's provider
    # list contents across versions.
    "moonshot": "moonshot",
    "zai": "zai",
    "openrouter": "openrouter",
    "azure": "azure",
    "aws": "bedrock",
    "vertex": "vertex_ai",
    "vertex_ai": "vertex_ai",
}


# Provider names that mean "an OpenAI-compatible endpoint I configured myself".
# For these the stored ``api_endpoint`` is the routing authority, never the
# vendor prefix inside the model id.
OPENAI_COMPATIBLE_PROVIDERS = frozenset({"openai-compatible", "custom"})

# Hosts served by OpenRouter. Matched on the endpoint host, not a substring of
# the whole URL, so "https://evil.test/?x=openrouter.ai" is not treated as
# OpenRouter.
OPENROUTER_HOSTS = frozenset({"openrouter.ai"})

OPENROUTER_PREFIX = "openrouter/"

# LiteLLM's HTTP client defaults User-Agent to ``litellm/{version}`` (overridable
# via ``LITELLM_USER_AGENT``). Its OpenRouter adapter also defaults
# ``HTTP-Referer`` / ``X-Title`` to ``https://litellm.ai`` / ``liteLLM``
# (``OR_SITE_URL`` / ``OR_APP_NAME``). Provider dashboards attribute traffic
# from those headers, so Preloop must replace them on every completion.
PRELOOP_APP_NAME = "Preloop"
PRELOOP_SITE_URL = "https://preloop.ai"
OPENROUTER_APP_NAME = PRELOOP_APP_NAME
OPENROUTER_SITE_URL = PRELOOP_SITE_URL


def preloop_user_agent() -> str:
    """User-Agent sent on every LiteLLM completion.

    LiteLLM otherwise stamps ``litellm/{version}``, which is what provider
    dashboards (OpenRouter, z.ai, OpenAI, ...) display as the client app.
    """
    from preloop import __version__

    return f"Preloop/{__version__}"


def preloop_client_headers() -> Dict[str, str]:
    """LiteLLM-default client headers replaced on every upstream."""
    return {"User-Agent": preloop_user_agent()}


def openrouter_app_headers() -> Dict[str, str]:
    """OpenRouter app-attribution headers (HTTP-Referer / X-Title)."""
    return {
        "HTTP-Referer": PRELOOP_SITE_URL,
        "X-Title": PRELOOP_APP_NAME,
    }


def is_openrouter_model(ai_model: Any) -> bool:
    """Whether this model's traffic terminates at OpenRouter.

    True for the explicit ``openrouter`` provider and for any model whose
    ``api_endpoint`` points at openrouter.ai (e.g. an ``openai-compatible``
    provider with an OpenRouter base URL). Mirrors the routing decision in
    :func:`to_litellm_model`.
    """
    provider = (getattr(ai_model, "provider_name", None) or "").strip().lower()
    if provider == "openrouter":
        return True
    return is_openrouter_endpoint(getattr(ai_model, "api_endpoint", None))


def apply_preloop_client_headers(
    kwargs: Dict[str, Any], ai_model: Any = None
) -> Dict[str, Any]:
    """Replace LiteLLM client branding on every completion kwargs dict.

    User-Agent is always Preloop (not gated on OpenRouter). OpenRouter
    attribution headers are added when the request terminates there.
    Merges into existing ``extra_headers`` so provider-specific headers
    (Claude Code OAuth betas, etc.) are kept.
    """
    extra = dict(kwargs.get("extra_headers") or {})
    extra.update(preloop_client_headers())
    if ai_model is not None and is_openrouter_model(ai_model):
        extra.update(openrouter_app_headers())
    kwargs["extra_headers"] = extra
    # Honor SSL_CERT_FILE / REQUESTS_CA_BUNDLE / PRELOOP_SSL_VERIFY only
    # for custom OpenAI-compatible upstreams. httpx treats a verify path
    # as the sole trust store, so applying a private-CA-only bundle to
    # public OpenAI/Anthropic would break those completions.
    if "ssl_verify" not in kwargs and _ssl_verify_applies_to_upstream(ai_model, kwargs):
        verify = ssl_verify_setting()
        if verify is not None:
            kwargs["ssl_verify"] = verify
    return kwargs


def _ssl_verify_applies_to_upstream(ai_model: Any, kwargs: Dict[str, Any]) -> bool:
    """True when this completion targets a custom api_base / compatible provider."""
    api_base = kwargs.get("api_base")
    if isinstance(api_base, str) and api_base.strip():
        return True
    if ai_model is None:
        return False
    provider = (getattr(ai_model, "provider_name", None) or "").strip().lower()
    if provider in OPENAI_COMPATIBLE_PROVIDERS:
        return True
    endpoint = getattr(ai_model, "api_endpoint", None)
    return bool(isinstance(endpoint, str) and endpoint.strip())


@lru_cache(maxsize=1)
def ensure_preloop_client_identity() -> None:
    """Fill LiteLLM env defaults once so new HTTP clients are not ``litellm/...``.

    ``LITELLM_USER_AGENT`` is the supported override for the default
    ``User-Agent: litellm/{version}`` header baked into LiteLLM's httpx
    client. ``OR_SITE_URL`` / ``OR_APP_NAME`` override the OpenRouter
    adapter's ``liteLLM`` attribution. Does not clobber an operator-set
    value.

    Call this at process startup, not per request. Cached so later calls
    are a no-op. LiteLLM caches httpx clients, so an env change after the
    first client is created is also a no-op.
    ``apply_preloop_client_headers`` is the source of truth for branding
    on every completion.
    """
    if "LITELLM_USER_AGENT" not in os.environ:
        os.environ["LITELLM_USER_AGENT"] = preloop_user_agent()
    os.environ.setdefault("OR_SITE_URL", OPENROUTER_SITE_URL)
    os.environ.setdefault("OR_APP_NAME", OPENROUTER_APP_NAME)


@lru_cache(maxsize=1)
def known_litellm_providers() -> frozenset:
    """Provider names litellm can route natively (mistral, groq, ...)."""
    try:
        return frozenset(
            str(getattr(entry, "value", entry)) for entry in litellm.provider_list
        )
    except Exception:  # pragma: no cover - defensive against litellm changes
        return frozenset()


def _endpoint_host(endpoint: Optional[str]) -> str:
    """Return the lowercase hostname of an endpoint URL, or "" if unparseable."""
    if not isinstance(endpoint, str):
        return ""
    raw = endpoint.strip()
    if not raw:
        return ""
    if "//" not in raw:
        # Tolerate host-only endpoints such as "openrouter.ai/api/v1".
        raw = f"//{raw}"
    try:
        return (urlsplit(raw).hostname or "").lower()
    except ValueError:  # pragma: no cover - malformed URLs (bad IPv6 literal)
        return ""


def is_openrouter_endpoint(endpoint: Optional[str]) -> bool:
    """Whether an api_endpoint points at OpenRouter."""
    host = _endpoint_host(endpoint)
    if not host:
        return False
    return any(
        host == known or host.endswith(f".{known}") for known in OPENROUTER_HOSTS
    )


def _openrouter_model(identifier: str) -> str:
    """Build the litellm string for an identifier that is an OpenRouter model id.

    litellm strips the ``openrouter/`` prefix before calling upstream, so the
    prefix has to be added on top of the id OpenRouter itself expects:

    * ``deepseek/deepseek-v4-flash-0731`` -> ``openrouter/deepseek/...``
    * ``openrouter/auto-beta`` (the Auto Router lives in OpenRouter's own
      first-party namespace) -> ``openrouter/openrouter/auto-beta``

    An identifier of the form ``openrouter/<vendor>/<model>`` is the manual
    workaround people applied before this fix, where the prefix is a routing
    hint rather than part of the upstream id. It is already the correct
    litellm string, so it passes through unchanged.
    """
    if identifier.startswith(OPENROUTER_PREFIX):
        remainder = identifier[len(OPENROUTER_PREFIX) :]
        if "/" in remainder:
            return identifier
    return f"{OPENROUTER_PREFIX}{identifier}"


def to_litellm_model(ai_model: AIModel) -> str:
    """Build the litellm model string for an AI model row."""
    provider = (ai_model.provider_name or "openai").strip().lower()
    identifier = (ai_model.model_identifier or "").strip()
    endpoint = getattr(ai_model, "api_endpoint", None)

    # The user's explicit provider/endpoint choice outranks any vendor prefix
    # inside the model id (issue #172).
    if provider == "openrouter" or is_openrouter_endpoint(endpoint):
        return _openrouter_model(identifier)

    if provider in OPENAI_COMPATIBLE_PROVIDERS and endpoint:
        return f"openai/{identifier}"

    if "/" in identifier:
        head = identifier.split("/", 1)[0].strip().lower()
        if head in known_litellm_providers() or head in set(PROVIDER_PREFIX.values()):
            return identifier

    prefix = PROVIDER_PREFIX.get(provider)
    if prefix is None:
        if provider in known_litellm_providers():
            prefix = provider
        elif ai_model.api_endpoint:
            prefix = "openai"
        else:
            prefix = provider
    return f"{prefix}/{identifier}"
