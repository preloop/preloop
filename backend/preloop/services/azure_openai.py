"""Request shaping for Azure OpenAI models routed through the gateway.

An Azure OpenAI model is stored as ``provider_name="azure"`` with the
deployment name as ``model_identifier`` and the resource URL as
``api_endpoint``. LiteLLM's Azure adapter builds the deployment URL itself
(``{api_base}/openai/deployments/{deployment}/chat/completions``), so it needs
the bare resource root plus an ``api-version``.

Operators usually paste whatever URL the Azure portal shows, which is often
the full deployment URL with ``?api-version=...`` or the ``/openai/v1`` base.
Forwarding that verbatim doubles the path and the first request fails with a
404, so the endpoint is reduced to the resource root here and any version it
carried is kept as a fallback. An explicit ``meta_data.provider_runtime``
``api_version`` always wins.

Authentication is a resource key by default. ``provider_runtime.azure_auth =
"entra"`` switches the model to Microsoft Entra ID bearer tokens (see
``azure_entra``); ``provider_runtime.azure_client_id`` optionally selects a
user-assigned identity.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit

from preloop.services.azure_entra import AZURE_OPENAI_ROLE, AzureEntraTokenError
from preloop.services.model_gateway_errors import GatewayProvider, ModelGatewayAPIError

AZURE_PROVIDERS = frozenset({"azure"})

AZURE_AUTH_KEY = "key"
AZURE_AUTH_ENTRA = "entra"
AZURE_AUTH_MODES = frozenset({AZURE_AUTH_KEY, AZURE_AUTH_ENTRA})

# Path segments that follow ``/openai`` in an Azure OpenAI URL. Only an
# ``openai`` segment followed by one of these (or ending the path) marks where
# LiteLLM's own suffix starts; anything before it is kept, so a gateway or
# API Management prefix such as ``/tenant-a`` survives.
_AZURE_OPENAI_TAILS = frozenset({"deployments", "v1"})


def is_azure_model(ai_model: Any) -> bool:
    """Whether a model row is an Azure OpenAI deployment.

    Args:
        ai_model: An ``AIModel`` row or any object with ``provider_name``.

    Returns:
        True when the provider is Azure OpenAI.
    """
    provider = (getattr(ai_model, "provider_name", None) or "").strip().lower()
    return provider in AZURE_PROVIDERS


def normalize_azure_endpoint(
    endpoint: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Reduce a pasted Azure URL to the resource root LiteLLM expects.

    Accepted shapes, all reduced to ``https://<resource>.openai.azure.com``:

    * the resource root, with or without a trailing slash;
    * a full deployment URL such as
      ``.../openai/deployments/<name>/chat/completions?api-version=2024-10-21``
      (the ``api-version`` query value is returned as the version hint);
    * the ``.../openai/v1`` base of Azure's OpenAI-compatible API (returned
      with the version hint ``v1``);
    * any of the above behind a proxy or API Management path prefix, which is
      kept (``https://gw.example.com/tenant-a/openai/v1`` becomes
      ``https://gw.example.com/tenant-a``), because LiteLLM appends
      ``/openai/...`` itself.

    A ``latest`` or ``preview`` api-version is forwarded unchanged; LiteLLM
    routes those, like ``v1``, to the ``/openai/v1`` API.

    Args:
        endpoint: The stored ``api_endpoint`` value.

    Returns:
        ``(resource_base, api_version_hint)``. Both are None for an empty
        endpoint; the hint is None when the URL carries no version.
    """
    raw = (endpoint or "").strip()
    if not raw:
        return None, None
    if "//" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    version_hint: Optional[str] = None
    query_versions = parse_qs(parts.query).get("api-version")
    if query_versions and query_versions[0].strip():
        version_hint = query_versions[0].strip()

    segments = parts.path.rstrip("/").split("/")
    marker = _azure_openai_segment(segments)
    if marker is not None:
        tail = segments[marker + 1 : marker + 2]
        if version_hint is None and [t.lower() for t in tail] == ["v1"]:
            version_hint = "v1"
        segments = segments[:marker]
    path = "/".join(segments).rstrip("/")
    base = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
    return base, version_hint


def _azure_openai_segment(segments: list[str]) -> Optional[int]:
    """Find the ``openai`` path segment where LiteLLM's suffix starts.

    The segment must be exactly ``openai`` and either end the path or be
    followed by ``deployments`` or ``v1``. Scanning from the right keeps a
    proxy prefix that itself ends in ``openai``; requiring the tail keeps a
    deployment that happens to be named ``openai`` in place.

    Args:
        segments: The URL path split on ``/``.

    Returns:
        The index of that segment, or None when the path has no Azure suffix.
    """
    for index in range(len(segments) - 1, -1, -1):
        if segments[index].lower() != "openai":
            continue
        following = segments[index + 1 : index + 2]
        if not following or following[0].lower() in _AZURE_OPENAI_TAILS:
            return index
    return None


def azure_api_version(ai_model: Any) -> Optional[str]:
    """Return the api-version configured on the model, if any.

    Args:
        ai_model: An ``AIModel`` row or a gateway snapshot.

    Returns:
        ``meta_data.provider_runtime.api_version`` when set, else the version
        carried by the stored endpoint URL, else None (LiteLLM then falls back
        to ``AZURE_API_VERSION`` or its own default).
    """
    raw_meta = getattr(ai_model, "meta_data", None)
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    runtime = meta.get("provider_runtime")
    runtime = runtime if isinstance(runtime, dict) else {}
    configured = runtime.get("api_version")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    _, hint = normalize_azure_endpoint(getattr(ai_model, "api_endpoint", None))
    return hint


def _provider_runtime(ai_model: Any) -> Dict[str, Any]:
    raw_meta = getattr(ai_model, "meta_data", None)
    meta = raw_meta if isinstance(raw_meta, dict) else {}
    runtime = meta.get("provider_runtime")
    return runtime if isinstance(runtime, dict) else {}


def azure_auth_mode(ai_model: Any) -> str:
    """Return how an Azure model authenticates: ``key`` or ``entra``.

    Args:
        ai_model: An ``AIModel`` row or a gateway snapshot.

    Returns:
        ``entra`` only for an Azure model whose ``provider_runtime.azure_auth``
        is ``entra``; ``key`` otherwise (the default, and every row written
        before Entra ID support existed).
    """
    if not is_azure_model(ai_model):
        return AZURE_AUTH_KEY
    mode = _provider_runtime(ai_model).get("azure_auth")
    if isinstance(mode, str) and mode.strip().lower() == AZURE_AUTH_ENTRA:
        return AZURE_AUTH_ENTRA
    return AZURE_AUTH_KEY


def uses_azure_entra(ai_model: Any) -> bool:
    """Whether the model authenticates to Azure with Entra ID tokens."""
    return azure_auth_mode(ai_model) == AZURE_AUTH_ENTRA


def azure_client_id(ai_model: Any) -> Optional[str]:
    """Return the user-assigned identity client id configured on the model."""
    value = _provider_runtime(ai_model).get("azure_client_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def normalize_azure_auth_meta(
    meta_data: Optional[Dict[str, Any]],
    *,
    provider_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Validate and normalize the Azure auth keys in model metadata.

    ``azure_auth`` must be ``key`` or ``entra``. On an Azure model, ``entra``
    also sets ``ambient_credentials`` so the model counts as configured
    without a key; ``key`` clears that flag again. A blank
    ``azure_client_id`` is removed.

    Non-Azure models do not keep that metadata. ``azure_auth`` and
    ``azure_client_id`` are dropped, and ``ambient_credentials`` is cleared
    when it arrived with ``azure_auth``, so a stale Entra flag cannot mark an
    unrelated model as configured. A provider that sets
    ``ambient_credentials`` on its own (Bedrock) keeps the flag when no Azure
    auth key is present.

    When ``provider_name`` is omitted, the mode is still validated but
    ``ambient_credentials`` is not set. Callers that learn the provider later
    (a partial update) run this again with the stored provider.

    Args:
        meta_data: The ``meta_data`` a create or update request carries.
        provider_name: The model's provider, when known.

    Returns:
        The same mapping, normalized in place.

    Raises:
        ValueError: ``azure_auth`` or ``azure_client_id`` has an invalid value
            on an Azure model, or when the provider is not yet known.
    """
    if not isinstance(meta_data, dict):
        return meta_data
    runtime = meta_data.get("provider_runtime")
    if not isinstance(runtime, dict):
        return meta_data
    provider = (provider_name or "").strip().lower()
    if provider and provider not in AZURE_PROVIDERS:
        # Only drop ambient credentials that this Entra flag would have set.
        # Bedrock uses the same key without azure_auth.
        had_azure_auth = "azure_auth" in runtime
        runtime.pop("azure_auth", None)
        runtime.pop("azure_client_id", None)
        if had_azure_auth:
            runtime.pop("ambient_credentials", None)
        return meta_data
    if "azure_client_id" in runtime:
        client_id = runtime["azure_client_id"]
        if client_id is None or (isinstance(client_id, str) and not client_id.strip()):
            runtime.pop("azure_client_id")
        elif not isinstance(client_id, str):
            raise ValueError("provider_runtime.azure_client_id must be a string")
        else:
            runtime["azure_client_id"] = client_id.strip()
    if "azure_auth" not in runtime:
        return meta_data
    mode = runtime["azure_auth"]
    normalized = mode.strip().lower() if isinstance(mode, str) else None
    if normalized not in AZURE_AUTH_MODES:
        raise ValueError("provider_runtime.azure_auth must be 'key' or 'entra'")
    runtime["azure_auth"] = normalized
    if normalized == AZURE_AUTH_ENTRA:
        # Unknown provider: do not mark the row configured. The CRUD layer
        # re-runs this once the stored provider is known.
        if provider in AZURE_PROVIDERS:
            runtime["ambient_credentials"] = True
    else:
        runtime.pop("ambient_credentials", None)
        runtime.pop("azure_client_id", None)
    return meta_data


def azure_entra_auth_error(
    exc: AzureEntraTokenError, *, provider: GatewayProvider
) -> ModelGatewayAPIError:
    """Map a failed Entra ID token acquisition to a provider 401.

    The same envelope the gateway uses for a rejected key, so server-side
    generation and gateway requests fail the same way.

    Args:
        exc: The token error. Its message names the failure class, not the
            raw credential-chain exception text.
        provider: Gateway response dialect. Auxiliary callers use ``openai``.

    Returns:
        A ``ModelGatewayAPIError`` with status 401 and code
        ``azure_entra_token_error``.
    """
    return ModelGatewayAPIError(
        provider=provider,
        status_code=401,
        message=(
            f"{exc} The identity needs the {AZURE_OPENAI_ROLE} role on the "
            "Azure OpenAI resource."
        ),
        code="azure_entra_token_error",
    )


def azure_request_kwargs(ai_model: Any) -> Dict[str, Any]:
    """Build the Azure-specific LiteLLM kwargs for one request.

    For an Entra ID model the token is acquired here, before the request is
    sent, so a credential failure surfaces as ``AzureEntraTokenError`` rather
    than as an opaque upstream error. ``api_key`` is set to None so a stored
    key is never sent alongside the bearer token, and the token provider is
    passed on (LiteLLM sends it as
    ``Authorization: Bearer``) so a reused client keeps refreshing.

    Args:
        ai_model: An Azure ``AIModel`` row or gateway snapshot.

    Returns:
        A mapping with ``api_base`` (the resource root) and ``api_version``
        when each is known, plus the Entra ID token kwargs in ``entra`` mode.
        Empty for non-Azure models.

    Raises:
        AzureEntraTokenError: Entra mode and no token could be acquired.
    """
    if not is_azure_model(ai_model):
        return {}
    kwargs: Dict[str, Any] = {}
    base, _ = normalize_azure_endpoint(getattr(ai_model, "api_endpoint", None))
    if base:
        kwargs["api_base"] = base
    version = azure_api_version(ai_model)
    if version:
        kwargs["api_version"] = version
    if uses_azure_entra(ai_model):
        from preloop.services import azure_entra

        client_id = azure_client_id(ai_model)
        kwargs["api_key"] = None
        # Acquire (and cache) now so a failure maps to a provider auth
        # error here; LiteLLM then reads the same cached token through the
        # provider on each call and gets a refreshed one near expiry.
        azure_entra.get_azure_ad_token(client_id)
        kwargs["azure_ad_token_provider"] = azure_entra.azure_ad_token_provider(
            client_id
        )
    return kwargs
