"""Persistence for the per account session embedding opt in.

Every function is account scoped. Enabling is the only operation that takes
provider details, because naming the provider *is* the opt in: an account
cannot end up embedding against an endpoint nobody chose.
"""

from __future__ import annotations

import ipaddress
import socket
from datetime import UTC, datetime
from typing import Any, Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..models.session_embedding_setting import (
    EMBEDDING_PROVIDERS,
    EMBEDDING_SCOPE_SUMMARIES_ONLY,
    EMBEDDING_SCOPES,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
    SessionEmbeddingSetting,
)
from ..models.session_search_document import EMBEDDING_DIMENSIONS
from .base import CRUDBase


class SessionEmbeddingConfigError(ValueError):
    """The requested embedding configuration cannot be stored as asked."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _ip_for_policy(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Return the address the host-policy checks should look at.

    IPv4-mapped IPv6 answers would otherwise skip the IPv4 private and
    link-local predicates.
    """
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _is_blocked_literal_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Whether a literal host IP is refused at opt-in."""
    candidate = _ip_for_policy(address)
    return (
        candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_private
        or candidate.is_reserved
        or candidate.is_multicast
        or candidate.is_unspecified
    )


def _is_blocked_resolved_address(
    address: ipaddress.IPv4Address | ipaddress.IPv6Address,
) -> bool:
    """Whether a DNS answer is refused as a metadata or loopback target.

    RFC1918 and unique-local answers are allowed: openai_compatible exists
    so an operator can point at an endpoint on their own network. Link-local,
    loopback, multicast, unspecified, and reserved answers are not, so a
    hostname like ``169.254.169.254.nip.io`` cannot bypass the IP-literal
    metadata check.
    """
    candidate = _ip_for_policy(address)
    return (
        candidate.is_loopback
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_unspecified
        or candidate.is_reserved
    )


def _resolved_ip_addresses(
    host: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Resolve ``host`` to IP addresses.

    Tests may replace this so the suite does not depend on live DNS.
    """
    try:
        results = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must resolve to a reachable host",
        ) from exc
    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for _family, _type, _proto, _canon, sockaddr in results:
        raw = sockaddr[0]
        if "%" in raw:
            raw = raw.split("%", 1)[0]
        address = ipaddress.ip_address(raw)
        key = str(address)
        if key in seen:
            continue
        seen.add(key)
        addresses.append(address)
    if not addresses:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must resolve to a reachable host",
        )
    return addresses


def validate_openai_compatible_base_url(url: str) -> str:
    """Return a cleaned https URL, or raise if it is not safe to POST to.

    The worker may attach a deployment-wide API key to this URL, so the
    opt-in is the last moment to refuse a private, loopback, or link-local
    target and anything that is not https. Hostnames are resolved and the
    same loopback, link-local, multicast, unspecified, and reserved
    predicates are applied to every answer. RFC1918 and unique-local
    answers stay allowed so a self-hosted endpoint on the operator network
    can be named by hostname. DNS rebinding between this check and the
    HTTP connect is a residual; deployments should still restrict worker
    egress.
    """
    cleaned = (url or "").strip()
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() != "https":
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must be https",
        )
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must include a host",
        )
    if parsed.username or parsed.password:
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not include credentials",
        )
    if host == "localhost" or host.endswith(".localhost"):
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not target localhost",
        )
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        for resolved in _resolved_ip_addresses(host):
            if _is_blocked_resolved_address(resolved):
                raise SessionEmbeddingConfigError(
                    "invalid_base_url",
                    "an OpenAI compatible base url must not resolve to a "
                    "loopback, link-local, or metadata host",
                )
        return cleaned
    if _is_blocked_literal_address(address):
        raise SessionEmbeddingConfigError(
            "invalid_base_url",
            "an OpenAI compatible base url must not target a private, "
            "loopback, or link-local host",
        )
    return cleaned


def validate_scope(scope: str) -> str:
    """Return a known scope, or raise.

    Raises:
        SessionEmbeddingConfigError: The value is not a scope this build
            knows. Guessing here would either embed more than an account
            asked for or quietly embed nothing.
    """
    cleaned = (scope or "").strip()
    if cleaned not in EMBEDDING_SCOPES:
        raise SessionEmbeddingConfigError(
            "invalid_scope",
            f"scope must be one of {', '.join(EMBEDDING_SCOPES)}",
        )
    return cleaned


class CRUDSessionEmbeddingSetting(CRUDBase[SessionEmbeddingSetting]):
    """CRUD operations for :class:`SessionEmbeddingSetting`."""

    def get_for_account(
        self, db: Session, *, account_id: Any
    ) -> Optional[SessionEmbeddingSetting]:
        """Return one account's setting, or ``None`` when it never opted in."""
        return (
            db.query(SessionEmbeddingSetting)
            .filter(SessionEmbeddingSetting.account_id == account_id)
            .one_or_none()
        )

    def get_or_create(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> SessionEmbeddingSetting:
        """Return the account's setting, creating a disabled one if absent."""
        existing = self.get_for_account(db, account_id=account_id)
        if existing is not None:
            return existing
        setting = SessionEmbeddingSetting(
            account_id=account_id,
            enabled=False,
            provider=PROVIDER_OPENAI_COMPATIBLE,
            dimensions=EMBEDDING_DIMENSIONS,
            scope=EMBEDDING_SCOPE_SUMMARIES_ONLY,
        )
        db.add(setting)
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def enable(
        self,
        db: Session,
        *,
        account_id: Any,
        provider: str,
        model_identifier: str,
        base_url: Optional[str] = None,
        dimensions: int = EMBEDDING_DIMENSIONS,
        daily_cap_usd: Optional[float] = None,
        scope: Optional[str] = None,
        user_id: Optional[Any] = None,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> SessionEmbeddingSetting:
        """Turn embedding on for one account, naming what it will talk to.

        ``scope`` is optional: omitting it keeps whatever the row already
        says, so a re-enable does not silently widen what is embedded, and a
        first opt in takes the ``summaries_only`` default.

        Raises:
            SessionEmbeddingConfigError: The provider is unknown, the model is
                missing, an OpenAI compatible provider has no base url, the
                base url is not https, targets a private IP literal, or
                resolves to a loopback, link-local, or metadata host, the
                requested width is not the width the corpus column stores, or
                the scope is not a scope this build knows.
        """
        if provider not in EMBEDDING_PROVIDERS:
            raise SessionEmbeddingConfigError(
                "unknown_provider",
                f"provider must be one of {', '.join(EMBEDDING_PROVIDERS)}",
            )
        cleaned_model = (model_identifier or "").strip()
        if not cleaned_model:
            raise SessionEmbeddingConfigError(
                "model_required",
                "enabling embedding must name the model the text is sent to",
            )
        cleaned_base_url = (base_url or "").strip() or None
        if provider == PROVIDER_OPENAI_COMPATIBLE and not cleaned_base_url:
            raise SessionEmbeddingConfigError(
                "base_url_required",
                "an OpenAI compatible provider must name its base url",
            )
        if provider == PROVIDER_OPENAI_COMPATIBLE and cleaned_base_url:
            cleaned_base_url = validate_openai_compatible_base_url(cleaned_base_url)
        if provider == PROVIDER_LOCAL:
            # A local model runs in this process; a base url would be a lie
            # about where the text goes.
            cleaned_base_url = None
        if int(dimensions) != EMBEDDING_DIMENSIONS:
            raise SessionEmbeddingConfigError(
                "unsupported_dimensions",
                (
                    "the corpus stores vectors of "
                    f"{EMBEDDING_DIMENSIONS} dimensions; changing the width is "
                    "a migration, not a setting"
                ),
            )
        if daily_cap_usd is not None and float(daily_cap_usd) < 0:
            raise SessionEmbeddingConfigError(
                "invalid_daily_cap", "the daily cap cannot be negative"
            )
        cleaned_scope = validate_scope(scope) if scope is not None else None

        setting = self.get_or_create(db, account_id=account_id)
        setting.enabled = True
        setting.provider = provider
        setting.model_identifier = cleaned_model
        setting.base_url = cleaned_base_url
        setting.dimensions = int(dimensions)
        setting.daily_cap_usd = (
            float(daily_cap_usd) if daily_cap_usd is not None else None
        )
        if cleaned_scope is not None:
            setting.scope = cleaned_scope
        setting.enabled_at = now or datetime.now(UTC)
        setting.enabled_by_user_id = user_id
        # A fresh opt in starts clean: yesterday's cap is not today's state.
        setting.degraded_reason = None
        setting.degraded_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def disable(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> Optional[SessionEmbeddingSetting]:
        """Turn embedding off, keeping the provider details for a re-enable."""
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None:
            return None
        setting.enabled = False
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def set_scope(
        self,
        db: Session,
        *,
        account_id: Any,
        scope: str,
        commit: bool = False,
    ) -> SessionEmbeddingSetting:
        """Say how much of a session this account embeds from now on.

        Narrowing to ``summaries_only`` deletes nothing: vectors already
        written stay, and the next worker pass simply stops claiming
        transcript chunks. Widening to ``full`` hands the backlog back to the
        worker, which still works through it under the daily cap.

        Raises:
            SessionEmbeddingConfigError: The scope is not one this build
                knows.
        """
        cleaned = validate_scope(scope)
        setting = self.get_or_create(db, account_id=account_id)
        setting.scope = cleaned
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def mark_degraded(
        self,
        db: Session,
        *,
        account_id: Any,
        reason: str,
        now: Optional[datetime] = None,
        commit: bool = False,
    ) -> Optional[SessionEmbeddingSetting]:
        """Record why the last run did less than it wanted to.

        Degraded is not failed. The chunks stay pending, the setting stays
        enabled, and the reason is what a console or an operator reads to see
        that the backlog is waiting on a cap rather than broken.
        """
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None:
            return None
        setting.degraded_reason = reason
        setting.degraded_at = now or datetime.now(UTC)
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def clear_degraded(
        self, db: Session, *, account_id: Any, commit: bool = False
    ) -> Optional[SessionEmbeddingSetting]:
        """Drop the degraded marker after a run that did its work."""
        setting = self.get_for_account(db, account_id=account_id)
        if setting is None or setting.degraded_reason is None:
            return setting
        setting.degraded_reason = None
        setting.degraded_at = None
        db.flush()
        if commit:
            db.commit()
            db.refresh(setting)
        return setting

    def enabled_account_ids(self, db: Session) -> list[str]:
        """Accounts that have opted in, for a sweeper that has no trigger."""
        rows = (
            db.query(SessionEmbeddingSetting.account_id)
            .filter(SessionEmbeddingSetting.enabled.is_(True))
            .all()
        )
        return [str(row[0]) for row in rows]
