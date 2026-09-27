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
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlsplit, urlunsplit

AZURE_PROVIDERS = frozenset({"azure"})

# LiteLLM routes these through Azure's OpenAI-compatible ``/openai/v1`` API
# instead of the dated deployment API.
AZURE_V1_API_VERSIONS = frozenset({"v1", "latest", "preview"})


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
      with the version hint ``v1``).

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

    path = parts.path.rstrip("/")
    lowered = path.lower()
    marker = lowered.find("/openai")
    if marker >= 0:
        remainder = lowered[marker + len("/openai") :]
        if version_hint is None and (
            remainder == "/v1" or remainder.startswith("/v1/")
        ):
            version_hint = "v1"
        path = path[:marker]
    base = urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))
    return base, version_hint


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


def azure_request_kwargs(ai_model: Any) -> Dict[str, Any]:
    """Build the Azure-specific LiteLLM kwargs for one request.

    Args:
        ai_model: An Azure ``AIModel`` row or gateway snapshot.

    Returns:
        A mapping with ``api_base`` (the resource root) and ``api_version``
        when each is known. Empty for non-Azure models.
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
    return kwargs
