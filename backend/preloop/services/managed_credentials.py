"""Provider credential resolver contract for managed tracker grants.

A managed tracker (``Tracker.auth_type == "managed_oauth"``) stores no token
on its row: its access credential lives in the tenant-bound grant storage
from ``preloop.models.crud.crud_managed_oauth`` and is rotated by a
provider service that ships as a plugin. The open-source callers in this
repository never implement consent or rotation. They ask the provider
resolver for a fresh credential immediately before every network operation
and treat every failure as explicit: there is no fallback to a stale
``resolved_api_key``, to an anonymous clone, or to another provider.

The resolver is looked up from the plugin manager under
``managed_oauth_resolver:<provider>``. Tests inject an implementation with
:func:`register_managed_resolver`. The contract is duck typed so a provider
plugin does not have to import this module:

* ``await resolver.resolve(account_id=..., tracker_id=..., provider=...,
  repository=..., force_refresh=...)`` returns an object exposing
  ``access_token``, ``expires_at`` (aware UTC), ``rotation_version`` and
  optionally ``git_username``.
* Failures are instances of classes named ``ReconnectRequiredError``,
  ``CredentialPermissionError`` or ``CredentialUnavailableError`` (or
  subclasses of the typed errors below) carrying a short ``code``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, Protocol
from uuid import UUID

logger = logging.getLogger(__name__)

MANAGED_AUTH_TYPE = "managed_oauth"
RESOLVER_SERVICE_PREFIX = "managed_oauth_resolver:"
# Feature flag a configured provider plugin advertises through ``/features``.
MANAGED_FEATURE_FLAGS = {"bitbucket": "bitbucket_cloud_oauth"}
DEFAULT_GIT_USERNAME = "x-token-auth"

_overrides: dict[str, Any] = {}


class ManagedCredentialError(Exception):
    """Typed resolver failure. ``code`` is a short sanitized identifier."""

    code = "unavailable"
    provider: Optional[str] = None

    def __init__(self, code: str | None = None, *, provider: str | None = None):
        self.code = code or type(self).code
        self.provider = provider
        super().__init__(self.code)

    def actionable_message(self) -> str:
        """A sentence an operator can act on; never includes provider bodies."""
        provider = (self.provider or "managed").replace("_", " ")
        return f"{provider} credential unavailable ({self.code})"


class ManagedCredentialUnavailableError(ManagedCredentialError):
    """No credential right now: no resolver, unconfigured, outage, rate limit."""

    code = "unavailable"

    def actionable_message(self) -> str:
        provider = (self.provider or "managed").replace("_", " ")
        if self.code == "resolver_missing":
            return (
                f"The managed {provider} connection cannot be used because no "
                "managed-provider plugin is installed on this deployment."
            )
        return (
            f"The managed {provider} connection could not provide a credential "
            f"({self.code}). Retry later or check the provider configuration."
        )


class ManagedReconnectRequiredError(ManagedCredentialError):
    """The grant was revoked, expired or disconnected: a user must consent again."""

    code = "reconnect_required"

    def actionable_message(self) -> str:
        provider = (self.provider or "managed").replace("_", " ")
        return (
            f"The managed {provider} connection requires reconnect "
            f"({self.code}). Reconnect it from the tracker page."
        )


class ManagedCredentialPermissionError(ManagedCredentialError):
    """The provider or the binding refused the operation; refresh cannot help."""

    code = "forbidden"

    def actionable_message(self) -> str:
        provider = (self.provider or "managed").replace("_", " ")
        return (
            f"The managed {provider} connection is not permitted to act on this "
            f"repository ({self.code})."
        )


@dataclass(frozen=True)
class ManagedCredential:
    """Ephemeral access credential. Plaintext lives only in this value."""

    access_token: str = field(repr=False)
    expires_at: datetime
    rotation_version: int
    provider: str
    git_username: str = DEFAULT_GIT_USERNAME

    def seconds_remaining(self, now: datetime | None = None) -> float:
        current = now or datetime.now(timezone.utc)
        return (self.expires_at - current).total_seconds()


class ManagedCredentialResolver(Protocol):
    """The async interface a provider plugin registers."""

    async def resolve(
        self,
        *,
        account_id: UUID,
        tracker_id: UUID,
        provider: str,
        repository: Optional[str] = None,
        force_refresh: bool = False,
    ) -> Any: ...


def is_managed_tracker(tracker: Any) -> bool:
    """True when the tracker row authenticates through a managed grant."""
    if tracker is None:
        return False
    return str(getattr(tracker, "auth_type", "") or "").lower() == MANAGED_AUTH_TYPE


def is_managed_auth_type(auth_type: Any) -> bool:
    """True for the managed discriminator, however it was spelled."""
    return str(auth_type or "").lower() == MANAGED_AUTH_TYPE


def register_managed_resolver(provider: str, resolver: Any) -> None:
    """Install (or with ``None`` remove) a resolver override; tests use this."""
    if resolver is None:
        _overrides.pop(provider, None)
    else:
        _overrides[provider] = resolver


def get_managed_resolver(provider: str) -> Any:
    """Return the provider resolver, or None when no plugin registered one."""
    if provider in _overrides:
        return _overrides[provider]
    try:
        from preloop.plugins.base import get_plugin_manager

        return get_plugin_manager().get_service(f"{RESOLVER_SERVICE_PREFIX}{provider}")
    except Exception:  # noqa: BLE001 - plugin manager unavailable means no resolver
        logger.debug("Plugin manager unavailable while resolving %s", provider)
        return None


def _to_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _utc(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("expiry missing")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _classify(exc: BaseException, provider: str) -> ManagedCredentialError:
    """Map a provider plugin's typed failure onto the open-source types."""
    if isinstance(exc, ManagedCredentialError):
        if exc.provider is None:
            exc.provider = provider
        return exc
    code = str(getattr(exc, "code", "") or "").strip() or None
    # Most specific class first: the plugin's siblings share one base class.
    for cls in type(exc).__mro__:
        name = cls.__name__
        if name == "ReconnectRequiredError":
            return ManagedReconnectRequiredError(code, provider=provider)
        if name == "CredentialPermissionError":
            return ManagedCredentialPermissionError(code, provider=provider)
        if name in ("CredentialUnavailableError", "ManagedCredentialError"):
            return ManagedCredentialUnavailableError(code, provider=provider)
    logger.warning(
        "Managed %s resolver raised %s; treating the credential as unavailable",
        provider,
        type(exc).__name__,
    )
    return ManagedCredentialUnavailableError("resolver_error", provider=provider)


async def resolve_managed_credential(
    *,
    account_id: Any,
    tracker_id: Any,
    provider: str,
    repository: Optional[str] = None,
    force_refresh: bool = False,
    resolver: Any = None,
) -> ManagedCredential:
    """Obtain a fresh access credential for one managed tracker.

    Args:
        account_id: Tenant that owns the tracker; the resolver enforces it.
        tracker_id: The managed tracker.
        provider: Tracker provider (``bitbucket``).
        repository: Optional repository slug the caller is about to touch; a
            bound tracker refuses a different repository.
        force_refresh: Rotate even when the stored credential looks fresh
            (after a provider 401).
        resolver: Explicit resolver, otherwise the registered one.

    Returns:
        The credential. Callers must not persist it on tracker rows.

    Raises:
        ManagedCredentialUnavailableError: No resolver, unconfigured provider,
            outage or any unexpected resolver failure.
        ManagedReconnectRequiredError: The grant needs new consent.
        ManagedCredentialPermissionError: The binding or provider refused.
    """
    target = resolver if resolver is not None else get_managed_resolver(provider)
    if target is None or not hasattr(target, "resolve"):
        raise ManagedCredentialUnavailableError("resolver_missing", provider=provider)
    try:
        result = await target.resolve(
            account_id=_to_uuid(account_id),
            tracker_id=_to_uuid(tracker_id),
            provider=provider,
            repository=repository or None,
            force_refresh=bool(force_refresh),
        )
    except Exception as exc:  # noqa: BLE001 - every failure becomes a typed error
        raise _classify(exc, provider) from None
    token = getattr(result, "access_token", None)
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n "):
        raise ManagedCredentialUnavailableError("invalid_credential", provider=provider)
    try:
        expires_at = _utc(getattr(result, "expires_at", None))
        version = int(getattr(result, "rotation_version", 0) or 0)
    except (TypeError, ValueError):
        raise ManagedCredentialUnavailableError(
            "invalid_credential", provider=provider
        ) from None
    username = str(getattr(result, "git_username", "") or DEFAULT_GIT_USERNAME)
    if any(c.isspace() for c in username) or ":" in username or "@" in username:
        raise ManagedCredentialUnavailableError("invalid_credential", provider=provider)
    return ManagedCredential(
        access_token=token,
        expires_at=expires_at,
        rotation_version=version,
        provider=provider,
        git_username=username,
    )


CredentialSource = Callable[..., Awaitable[ManagedCredential]]


@dataclass(frozen=True)
class ManagedCredentialSource:
    """A resolver bound to one tenant, tracker, provider and repository.

    Long-lived clients hold this instead of a token and call it before each
    request, so a client created hours ago still authenticates with the
    currently valid credential.
    """

    account_id: UUID
    tracker_id: UUID
    provider: str
    repository: Optional[str] = None
    resolver: Any = field(default=None, repr=False, compare=False)

    async def __call__(self, *, force_refresh: bool = False) -> ManagedCredential:
        return await resolve_managed_credential(
            account_id=self.account_id,
            tracker_id=self.tracker_id,
            provider=self.provider,
            repository=self.repository,
            force_refresh=force_refresh,
            resolver=self.resolver,
        )


def tracker_credential_source(
    tracker: Any, *, repository: Optional[str] = None
) -> Optional[ManagedCredentialSource]:
    """Build the bound source for a managed tracker row, else None.

    Args:
        tracker: ``Tracker`` ORM instance or compatible object.
        repository: Repository slug override; defaults to the tracker's
            bound ``connection_details["repository"]``.
    """
    if not is_managed_tracker(tracker):
        return None
    details = getattr(tracker, "connection_details", None) or {}
    bound = repository or details.get("repository") or None
    return ManagedCredentialSource(
        account_id=_to_uuid(tracker.account_id),
        tracker_id=_to_uuid(tracker.id),
        provider=str(tracker.tracker_type).lower(),
        repository=str(bound) if bound else None,
    )


def managed_feature_flag(provider: str) -> Optional[str]:
    """The ``/features`` flag that advertises a configured provider service."""
    return MANAGED_FEATURE_FLAGS.get(str(provider or "").lower())
