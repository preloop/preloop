"""Microsoft Entra ID tokens for Azure OpenAI models.

An Azure OpenAI model with ``meta_data.provider_runtime.azure_auth = "entra"``
authenticates with a bearer token instead of a resource key. The token comes
from ``azure-identity``'s ``DefaultAzureCredential``, which tries, in order,
client credentials from the environment (``AZURE_TENANT_ID``,
``AZURE_CLIENT_ID``, ``AZURE_CLIENT_SECRET``), AKS workload identity (the
federated token file the webhook mounts), and the managed identity of the VM
or App Service the process runs on.

Tokens are cached in process per identity and refreshed
``REFRESH_MARGIN_SECONDS`` before they expire, so a request never carries a
token that is about to lapse. Tokens are never persisted or logged.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

AZURE_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"
REFRESH_MARGIN_SECONDS = 300
AZURE_OPENAI_ROLE = "Cognitive Services OpenAI User"


class AzureEntraTokenError(Exception):
    """Raised when no Entra ID token can be acquired for Azure OpenAI."""


def _default_credential_factory(client_id: Optional[str]) -> Any:
    """Build the standard Azure credential chain.

    Args:
        client_id: Client id of a user-assigned managed identity or workload
            identity. None uses the system-assigned identity, or the client id
            the environment provides.

    Returns:
        A ``DefaultAzureCredential`` instance.
    """
    from azure.identity import DefaultAzureCredential

    kwargs: Dict[str, Any] = {"exclude_interactive_browser_credential": True}
    if client_id:
        kwargs["managed_identity_client_id"] = client_id
        kwargs["workload_identity_client_id"] = client_id
    return DefaultAzureCredential(**kwargs)


# Patched in tests; production always uses the azure-identity chain.
credential_factory: Callable[[Optional[str]], Any] = _default_credential_factory
clock: Callable[[], float] = time.time


class _CachedTokenSource:
    """One identity's credential plus its current token."""

    def __init__(self, client_id: Optional[str]) -> None:
        self._client_id = client_id
        self._credential: Any = None
        self._token: Optional[str] = None
        self._expires_on: float = 0.0
        self._lock = threading.Lock()

    def get_token(self) -> str:
        """Return a token valid for at least the refresh margin.

        Returns:
            The bearer token string.

        Raises:
            AzureEntraTokenError: The credential chain could not issue a
                token, or it issued one that has already expired.
        """
        with self._lock:
            now = clock()
            if self._token and self._expires_on - REFRESH_MARGIN_SECONDS > now:
                return self._token
            try:
                if self._credential is None:
                    self._credential = credential_factory(self._client_id)
                access = self._credential.get_token(AZURE_COGNITIVE_SERVICES_SCOPE)
            except Exception as exc:
                self._token = None
                logger.warning(
                    "Azure Entra ID token acquisition failed (client_id=%s): %s",
                    self._client_id or "default",
                    type(exc).__name__,
                )
                raise AzureEntraTokenError(
                    "Could not acquire a Microsoft Entra ID token for Azure "
                    f"OpenAI ({type(exc).__name__}). Check the workload or "
                    "managed identity configuration of the Preloop server."
                ) from exc
            expires_on = float(access.expires_on)
            if expires_on <= clock():
                self._token = None
                raise AzureEntraTokenError(
                    "The Azure credential chain returned an expired Entra ID token"
                )
            self._token = access.token
            self._expires_on = expires_on
            return self._token


_SOURCES: Dict[str, _CachedTokenSource] = {}
_SOURCES_LOCK = threading.Lock()


def _source_for(client_id: Optional[str]) -> _CachedTokenSource:
    key = (client_id or "").strip()
    with _SOURCES_LOCK:
        source = _SOURCES.get(key)
        if source is None:
            source = _CachedTokenSource(key or None)
            _SOURCES[key] = source
        return source


def get_azure_ad_token(client_id: Optional[str] = None) -> str:
    """Return a cached or fresh Entra ID token for Azure OpenAI.

    Args:
        client_id: Optional user-assigned identity client id.

    Returns:
        The bearer token.

    Raises:
        AzureEntraTokenError: When no valid token can be acquired.
    """
    return _source_for(client_id).get_token()


def azure_ad_token_provider(client_id: Optional[str] = None) -> Callable[[], str]:
    """Return a zero-argument token provider for LiteLLM.

    LiteLLM (and the OpenAI SDK under it) call the provider on each request,
    so a cached client still picks up refreshed tokens.

    Args:
        client_id: Optional user-assigned identity client id.

    Returns:
        A callable returning the current bearer token.
    """

    def _provider() -> str:
        return get_azure_ad_token(client_id)

    return _provider


def reset_token_cache() -> None:
    """Drop every cached credential and token (tests and reconfiguration)."""
    with _SOURCES_LOCK:
        _SOURCES.clear()
