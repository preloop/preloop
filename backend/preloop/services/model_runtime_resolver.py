"""Resolve model runtime settings for agents and gateway routing."""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from preloop.models.models.ai_model import AIModel
from preloop.services.secret_service import get_secret_service


DEFAULT_GATEWAY_PROVIDER = "preloop"
DEFAULT_GATEWAY_TRANSPORT_MODE = "preloop_gateway"
DEFAULT_GATEWAY_URL = "http://host.docker.internal:8000/openai/v1"
GATEWAY_API_PATHS = {
    "openai": "/openai/v1",
    "anthropic": "/anthropic/v1",
    "gemini": "/gemini/v1beta",
}


@dataclass
class ResolvedModelRuntime:
    """Resolved runtime configuration for model access."""

    model_identifier: Optional[str]
    model_provider: Optional[str]
    model_endpoint: Optional[str]
    model_api_key: Optional[str]
    model_api_key_backend: Optional[str]
    model_auth_type: Optional[str]
    model_auth_payload: Optional[Dict[str, Any]]
    model_parameters: Optional[Dict[str, Any]]
    model_transport_mode: str
    model_gateway_enabled: bool
    model_gateway_url: Optional[str]
    model_gateway_model_alias: Optional[str]
    model_gateway_provider: Optional[str]

    def to_execution_context(
        self, gateway_token: Optional[str] = None
    ) -> Dict[str, Any]:
        """Convert the resolved runtime to execution-context fields."""
        return {
            "model_identifier": self.model_identifier,
            "model_provider": self.model_provider,
            "model_endpoint": self.model_endpoint,
            "model_api_key": self.model_api_key,
            "model_api_key_backend": self.model_api_key_backend,
            "model_auth_type": self.model_auth_type,
            "model_auth_payload": self.model_auth_payload,
            "model_parameters": self.model_parameters,
            "model_transport_mode": self.model_transport_mode,
            "model_gateway_enabled": self.model_gateway_enabled,
            "model_gateway_url": self.model_gateway_url,
            "model_gateway_token": gateway_token,
            "model_gateway_model_alias": self.model_gateway_model_alias,
            "model_gateway_provider": self.model_gateway_provider,
        }


def _build_default_gateway_alias(ai_model: AIModel) -> str:
    provider = (ai_model.provider_name or "openai").strip().lower()
    model_identifier = (ai_model.model_identifier or "").strip()
    return f"{provider}/{model_identifier}" if model_identifier else provider


def _get_gateway_config(ai_model: AIModel) -> Dict[str, Any]:
    meta_data = ai_model.meta_data or {}
    gateway_config = meta_data.get("gateway") if isinstance(meta_data, dict) else None
    return gateway_config if isinstance(gateway_config, dict) else {}


def gateway_url_for_api(gateway_url: Optional[str], api: str) -> Optional[str]:
    """Return the sibling gateway URL for the requested client API shape."""
    if not gateway_url:
        return gateway_url

    api_path = GATEWAY_API_PATHS.get(api)
    if not api_path:
        raise ValueError(f"Unsupported gateway API: {api}")

    parsed = urlsplit(gateway_url.rstrip("/"))
    current_path = parsed.path.rstrip("/")
    base_path = current_path
    for known_path in GATEWAY_API_PATHS.values():
        if current_path.endswith(known_path):
            base_path = current_path[: -len(known_path)].rstrip("/")
            break

    return urlunsplit(parsed._replace(path=f"{base_path}{api_path}"))


def default_model_gateway_url() -> str:
    """Resolve the default gateway URL for the current runtime environment."""
    configured_url = os.getenv("PRELOOP_MODEL_GATEWAY_URL")
    if configured_url:
        return configured_url

    configured_k8s_url = os.getenv("PRELOOP_MODEL_GATEWAY_URL_K8S")
    if configured_k8s_url:
        return configured_k8s_url

    if os.getenv("KUBERNETES_SERVICE_HOST"):
        api_service_endpoint = os.getenv("PRELOOP_API_SERVICE_HTTP_ENDPOINT")
        if api_service_endpoint:
            return (
                gateway_url_for_api(api_service_endpoint, "openai")
                or DEFAULT_GATEWAY_URL
            )

    return DEFAULT_GATEWAY_URL


def _resolve_direct_ai_model_runtime(ai_model: AIModel) -> ResolvedModelRuntime:
    """Resolve direct-provider runtime settings for an AI model."""
    resolved_credentials = get_secret_service().resolve_ai_model_credentials(ai_model)
    return ResolvedModelRuntime(
        model_identifier=ai_model.model_identifier,
        model_provider=ai_model.provider_name,
        model_endpoint=ai_model.api_endpoint,
        model_api_key=(
            resolved_credentials.value
            if resolved_credentials
            and resolved_credentials.credential_type == "api_key"
            else None
        ),
        model_api_key_backend=(
            resolved_credentials.backend_type if resolved_credentials else None
        ),
        model_auth_type=(
            resolved_credentials.credential_type if resolved_credentials else None
        ),
        model_auth_payload=resolved_credentials.payload
        if resolved_credentials
        else None,
        model_parameters=ai_model.model_parameters,
        model_transport_mode="direct_provider",
        model_gateway_enabled=False,
        model_gateway_url=None,
        model_gateway_model_alias=None,
        model_gateway_provider=None,
    )


def gateway_model_alias_candidates(ai_model: AIModel) -> set[str]:
    """Return every alias spelling that addresses one model on the gateway.

    The gateway's model resolver accepts both a model's canonical alias
    (``anthropic/claude-opus-4-1``) and its bare provider-suffix form
    (``claude-opus-4-1``). Governance decisions must therefore treat all of
    those spellings as one model rather than as distinct policy keys.

    Args:
        ai_model: The already-resolved model to describe.

    Returns:
        The spellings that resolve to ``ai_model``; empty only for a model
        that carries no identifier and no configured alias.
    """
    candidates: set[str] = set()
    gateway_config = _get_gateway_config(ai_model)

    if gateway_config.get("enabled"):
        # Only resolve the full runtime for gateway models: the direct-provider
        # branch resolves secrets, which the budget preflight must not trigger.
        runtime_alias = resolve_ai_model_runtime(ai_model).model_gateway_model_alias
        if runtime_alias:
            candidates.add(runtime_alias.strip())

    configured_alias = gateway_config.get("model_alias")
    if isinstance(configured_alias, str) and configured_alias.strip():
        candidates.add(configured_alias.strip())

    model_identifier = (ai_model.model_identifier or "").strip()
    if model_identifier:
        candidates.add(model_identifier)
        candidates.add(_build_default_gateway_alias(ai_model))

    # The resolver's suffix match means a prefixed alias is also reachable by
    # its bare tail, so both halves name the same model.
    for alias in list(candidates):
        _, separator, tail = alias.partition("/")
        if separator and tail.strip():
            candidates.add(tail.strip())

    return {candidate for candidate in candidates if candidate}


def resolve_ai_model_runtime(
    ai_model: AIModel, *, allow_gateway: bool = True
) -> ResolvedModelRuntime:
    """Resolve the runtime configuration for an AI model."""
    gateway_config = _get_gateway_config(ai_model)
    gateway_enabled = bool(gateway_config.get("enabled")) and allow_gateway
    model_parameters = ai_model.model_parameters

    if gateway_enabled:
        gateway_url = gateway_config.get("url") or default_model_gateway_url()
        gateway_model_alias = gateway_config.get(
            "model_alias"
        ) or _build_default_gateway_alias(ai_model)
        gateway_provider = (
            gateway_config.get("provider_adapter") or DEFAULT_GATEWAY_PROVIDER
        )

        return ResolvedModelRuntime(
            model_identifier=gateway_model_alias,
            model_provider=gateway_provider,
            model_endpoint=gateway_url,
            model_api_key=None,
            model_api_key_backend=None,
            model_auth_type=None,
            model_auth_payload=None,
            model_parameters=model_parameters,
            model_transport_mode=gateway_config.get("transport_mode")
            or DEFAULT_GATEWAY_TRANSPORT_MODE,
            model_gateway_enabled=True,
            model_gateway_url=gateway_url,
            model_gateway_model_alias=gateway_model_alias,
            model_gateway_provider=gateway_provider,
        )

    return _resolve_direct_ai_model_runtime(ai_model)
