"""Trusted upstream gateways: identity headers, subjects, client attribution.

A Claude apps gateway (``provider: anthropic`` upstream with
``forward_user_identity: true``) forwards each developer's IdP identity to
Preloop in request headers. Any client can forge those headers, so they are
honoured only on an API key carrying :data:`TRUSTED_UPSTREAM_SCOPE` and, when
the key has an upstream secret configured, only when the request carries the
matching :data:`UPSTREAM_SECRET_HEADER`.

This module is the single place that decides trust, resolves the gateway
subject and renders the 429 contract the apps gateway relays to developers.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from preloop.services.model_gateway_errors import ModelGatewayAPIError

logger = logging.getLogger(__name__)

#: API key scope that marks a key as a trusted upstream gateway credential.
TRUSTED_UPSTREAM_SCOPE = "model_gateway:trusted_upstream"
#: ``ApiKey.context_data`` key holding the sha256 hex of the upstream secret.
UPSTREAM_SECRET_HASH_CONTEXT_KEY = "trusted_upstream_secret_hash"
#: ``ApiKey.context_data`` key holding the per-subject default budget.
PER_SUBJECT_BUDGET_CONTEXT_KEY = "per_subject_budget"
#: Request header carrying the upstream secret.
UPSTREAM_SECRET_HEADER = "x-preloop-upstream-secret"

#: Identity headers set by the apps gateway with ``forward_user_identity``.
GATEWAY_USER_ID_HEADER = "x-claude-gateway-user-id"
GATEWAY_USER_EMAIL_HEADER = "x-claude-gateway-user-email"
LITELLM_END_USER_ID_HEADER = "x-litellm-end-user-id"
IDENTITY_HEADERS = (
    GATEWAY_USER_ID_HEADER,
    GATEWAY_USER_EMAIL_HEADER,
    LITELLM_END_USER_ID_HEADER,
)

#: Client marker the Preloop-generated Desktop config sets.
CLIENT_MARKER_HEADER = "x-preloop-client"
CLIENT_MARKER_CLAUDE_DESKTOP = "claude-desktop"

GATEWAY_SOURCE_APPS_GATEWAY = "claude_apps_gateway"
GATEWAY_SOURCE_DIRECT = "direct"
CLIENT_CLAUDE_DESKTOP = "claude_desktop"
CLIENT_CLAUDE_CODE = "claude_code"
CLIENT_UNKNOWN = "unknown"

#: Budget subject type for gateway subjects.
BUDGET_SUBJECT_GATEWAY_SUBJECT = "gateway_subject"

_MAX_HEADER_VALUE = 255
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")


@dataclass(frozen=True)
class GatewaySubjectRef:
    """Detached reference to the resolved gateway subject of one request."""

    id: uuid.UUID
    external_subject: str
    email: Optional[str]
    linked_user_id: Optional[uuid.UUID]
    api_key_id: uuid.UUID

    @property
    def label(self) -> str:
        """Human label: the email when known, else the IdP subject."""
        return self.email or self.external_subject


@dataclass(frozen=True)
class GatewayIdentity:
    """Identity headers read from a trusted upstream request."""

    external_subject: str
    email: Optional[str]


def api_key_scopes(api_key: Any) -> list[Any]:
    """Return the key's scopes as a list (empty when absent or malformed)."""
    scopes = getattr(api_key, "scopes", None)
    return list(scopes) if isinstance(scopes, (list, tuple)) else []


def is_trusted_upstream_key(api_key: Any) -> bool:
    """Whether ``api_key`` carries :data:`TRUSTED_UPSTREAM_SCOPE`."""
    if api_key is None:
        return False
    return TRUSTED_UPSTREAM_SCOPE in api_key_scopes(api_key)


def hash_upstream_secret(secret: str) -> str:
    """Return the sha256 hex digest stored for an upstream secret."""
    # codeql[py/weak-sensitive-data-hashing] High-entropy shared secret fingerprint, not a password
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _context(api_key: Any) -> dict[str, Any]:
    context = getattr(api_key, "context_data", None)
    return context if isinstance(context, dict) else {}


def upstream_secret_matches(api_key: Any, presented: Optional[str]) -> bool:
    """Check the request's upstream secret against the key's stored hash.

    Returns ``True`` when the key has no secret configured. Otherwise the
    presented value must hash to the stored digest (constant-time compare).
    """
    expected = _context(api_key).get(UPSTREAM_SECRET_HASH_CONTEXT_KEY)
    if not expected:
        return True
    if not presented:
        return False
    return hmac.compare_digest(
        hash_upstream_secret(presented).encode("ascii"),
        str(expected).strip().lower().encode("ascii", "ignore"),
    )


def _clean(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if not value or any(ord(ch) < 32 for ch in value):
        return None
    return value[:_MAX_HEADER_VALUE]


def read_identity_headers(headers: Mapping[str, str]) -> Optional[GatewayIdentity]:
    """Read the apps gateway identity headers, or ``None`` without a subject.

    ``x-claude-gateway-user-id`` is required. The email comes from
    ``x-claude-gateway-user-email`` with ``x-litellm-end-user-id`` as the
    fallback; a value that does not look like an email is dropped.
    """
    external_subject = _clean(headers.get(GATEWAY_USER_ID_HEADER))
    if not external_subject:
        return None
    email = None
    for name in (GATEWAY_USER_EMAIL_HEADER, LITELLM_END_USER_ID_HEADER):
        candidate = _clean(headers.get(name))
        if candidate and _EMAIL_RE.match(candidate):
            email = candidate
            break
    return GatewayIdentity(external_subject=external_subject, email=email)


def present_identity_header_names(headers: Mapping[str, str]) -> list[str]:
    """Names (never values) of identity headers present on a request."""
    return [name for name in IDENTITY_HEADERS if headers.get(name) is not None]


def log_ignored_identity_headers(headers: Mapping[str, str]) -> None:
    """Debug-log that identity headers arrived on an untrusted credential."""
    names = present_identity_header_names(headers)
    if names:
        logger.debug(
            "Ignoring gateway identity headers on a non-trusted credential: %s",
            ", ".join(names),
        )


def detect_client(headers: Mapping[str, str]) -> str:
    """Attribute the calling client from request headers.

    ``X-Preloop-Client: claude-desktop`` (set by the Preloop-generated Desktop
    config) wins; a Claude Code session header or a ``claude-cli/`` user
    agent means Claude Code; anything else is ``unknown``.
    """
    marker = (headers.get(CLIENT_MARKER_HEADER) or "").strip().lower()
    if marker == CLIENT_MARKER_CLAUDE_DESKTOP:
        return CLIENT_CLAUDE_DESKTOP
    if headers.get("x-claude-code-session-id"):
        return CLIENT_CLAUDE_CODE
    user_agent = (headers.get("user-agent") or "").strip().lower()
    if user_agent.startswith("claude-cli/"):
        return CLIENT_CLAUDE_CODE
    return CLIENT_UNKNOWN


def fallback_session_id(
    subject: GatewaySubjectRef, now: Optional[datetime] = None
) -> str:
    """Session id for a trusted request without a client session header."""
    now = now or datetime.now(timezone.utc)
    return f"gw:{subject.id}:{now.date().isoformat()}"


def per_subject_budget(api_key: Any) -> Optional[dict[str, Any]]:
    """Return the key's per-subject default budget, when well formed.

    Same shape as a budget policy limit: ``period`` (a ``BudgetPeriod``
    value, default ``monthly``), ``hard_limit_usd`` and/or
    ``soft_limit_usd``, and an optional ``model_alias``.
    """
    raw = _context(api_key).get(PER_SUBJECT_BUDGET_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return None
    hard = raw.get("hard_limit_usd")
    soft = raw.get("soft_limit_usd")
    if hard is None and soft is None:
        return None
    try:
        return {
            "period": str(raw.get("period") or "monthly"),
            "hard_limit_usd": float(hard) if hard is not None else None,
            "soft_limit_usd": float(soft) if soft is not None else None,
            "model_alias": raw.get("model_alias") or None,
        }
    except (TypeError, ValueError):
        return None


_BUDGET_DENIAL_CODES = {"budget_limit_exceeded", "execution_budget_exceeded"}


def is_budget_denial(exc: ModelGatewayAPIError) -> bool:
    """Whether a gateway error is a Preloop budget denial."""
    if exc.status_code != 403:
        return False
    message = (exc.message or "").lower()
    return (
        exc.code in _BUDGET_DENIAL_CODES
        or "budget exceeded" in message
        or "budget hard limit exceeded" in message
        or "limit for hosted model reached" in message
        or "limit for hosted models reached" in message
        or "budget enforcement requires pricing information" in message
    )


def seconds_until(reset_at: Optional[datetime], now: Optional[datetime] = None) -> int:
    """Integer seconds until ``reset_at`` (at least 1)."""
    if reset_at is None:
        return 3600
    now = now or datetime.now(timezone.utc)
    if reset_at.tzinfo is None:
        reset_at = reset_at.replace(tzinfo=timezone.utc)
    return max(1, int(math.ceil((reset_at - now).total_seconds())))


class TrustedUpstreamDenialError(ModelGatewayAPIError):
    """A 429 rendered for the Claude apps gateway, with exact headers."""

    should_retry: Optional[bool] = None

    def response_headers(self) -> dict[str, str]:
        """``retry-after`` (integer seconds) and ``x-should-retry``."""
        headers: dict[str, str] = {
            "retry-after": str(int(self.retry_after_seconds or 1)),
        }
        headers.update(getattr(self, "extra_response_headers", None) or {})
        if self.should_retry is not None:
            headers["x-should-retry"] = "true" if self.should_retry else "false"
        return headers


def to_trusted_upstream_error(
    exc: ModelGatewayAPIError, subject: GatewaySubjectRef
) -> ModelGatewayAPIError:
    """Map a denial to the 429 contract for a trusted identity request.

    The apps gateway fails over to its next upstream on 403, which would let
    a developer bypass a per-user Preloop budget. A 429 on an email-carrying
    request is relayed to the developer as-is. Budget denials become 429
    ``billing_error`` with ``x-should-retry: false``; rate limits become 429
    ``rate_limit_error``. Everything else is returned unchanged.
    """
    if is_budget_denial(exc):
        detail = exc.message or "budget exceeded"
        for prefix in (
            "Model gateway budget exceeded: ",
            "Model gateway budget exceeded",
        ):
            if detail.startswith(prefix):
                detail = detail[len(prefix) :]
                break
        message = f"Preloop budget exceeded for {subject.label}"
        if detail:
            message = f"{message}: {detail}"
        denial = TrustedUpstreamDenialError(
            provider="anthropic",
            status_code=429,
            message=message,
            error_type="billing_error",
            code=exc.code,
            retry_after_seconds=(
                getattr(exc, "budget_reset_seconds", None)
                or exc.retry_after_seconds
                or 3600
            ),
        )
        denial.should_retry = False
        return denial
    if exc.status_code == 429:
        denial = TrustedUpstreamDenialError(
            provider="anthropic",
            status_code=429,
            message=exc.message,
            error_type="rate_limit_error",
            code=exc.code,
            error_class=exc.error_class,
            retry_after_seconds=exc.retry_after_seconds or 1,
        )
        return denial
    return exc


async def apply_trusted_upstream(
    auth_context: Any,
    headers: Mapping[str, str],
    db: Any,
    *,
    provider: str = "anthropic",
) -> Any:
    """Honour identity headers on a trusted upstream key, else ignore them.

    Args:
        auth_context: The authenticated ``ModelGatewayAuthContext``.
        headers: Request headers (case-insensitive mapping).
        db: Request database session; only its engine is used, the subject
            upsert runs in its own short session off the event loop.
        provider: Error envelope for a rejected secret.

    Returns:
        ``auth_context`` unchanged for an untrusted credential, or a copy
        with ``trusted_upstream`` and (when identity headers are present)
        ``gateway_subject`` set.

    Raises:
        ModelGatewayAPIError: 401 when the key has an upstream secret
            configured and the request's secret is missing or wrong.
    """
    from dataclasses import replace

    api_key = getattr(auth_context, "api_key", None)
    if not is_trusted_upstream_key(api_key):
        log_ignored_identity_headers(headers)
        return auth_context
    if not upstream_secret_matches(api_key, headers.get(UPSTREAM_SECRET_HEADER)):
        logger.info(
            "Rejected trusted upstream request: upstream secret missing or wrong "
            "(api_key_id=%s)",
            getattr(api_key, "id", None),
        )
        raise ModelGatewayAPIError(
            provider=provider,  # type: ignore[arg-type]
            status_code=401,
            message="Invalid or missing upstream secret",
        )
    identity = read_identity_headers(headers)
    if identity is None:
        return replace(auth_context, trusted_upstream=True)

    from sqlalchemy.orm import Session

    from preloop.api.loop_safety import run_db_off_loop
    from preloop.models.crud import crud_gateway_subject
    from preloop.models.db.gateway_session import release_gateway_session

    bind = db.get_bind()
    account_id = auth_context.account_id
    api_key_id = api_key.id

    def resolve() -> GatewaySubjectRef:
        with Session(bind=bind, expire_on_commit=False) as session:
            subject = crud_gateway_subject.resolve(
                session,
                account_id=account_id,
                api_key_id=api_key_id,
                external_subject=identity.external_subject,
                email=identity.email,
            )
            ref = GatewaySubjectRef(
                id=subject.id,
                external_subject=subject.external_subject,
                email=subject.email,
                linked_user_id=subject.linked_user_id,
                api_key_id=subject.api_key_id,
            )
            release_gateway_session(session)
            return ref

    subject_ref = await run_db_off_loop(resolve)
    return replace(auth_context, trusted_upstream=True, gateway_subject=subject_ref)
