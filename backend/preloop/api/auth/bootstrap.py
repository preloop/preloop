"""Computed bootstrap-registration rule shared by /register and /features.

A fresh (zero-user) instance with a bootstrap token configured
(``PRELOOP_BOOTSTRAP_TOKEN``) is "unclaimed": registration is allowed ONLY
with the token, regardless of ``settings.registration_enabled``. When the
token is not configured, a zero-user instance keeps today's behavior
(``registration_enabled`` decides). Once the instance has at least one user,
``registration_enabled`` decides — the token is never consulted again.
"""

import secrets
from dataclasses import dataclass
from typing import Dict, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud import crud_user

# Sticky process-level cache for "does this instance have any users?".
# Transitions only once in an instance lifetime (False -> True when the first
# account registers) and never flips back, so caching True forever is safe and
# avoids a DB round-trip on every /features call (landing, signup, authed
# views). Mutable mapping avoids a module-level `global` flag (CodeQL
# unused-global FP). None = unknown (must query); True = at least one user
# exists.
_users_exist_state: Dict[str, Optional[bool]] = {"known": None}

REASON_BOOTSTRAP_TOKEN_REQUIRED = "bootstrap_token_required"
REASON_REGISTRATION_DISABLED = "registration_disabled"


def reset_users_exist_cache() -> None:
    """Clear the sticky users-exist cache (for tests)."""
    _users_exist_state["known"] = None


def users_exist(db: Session) -> bool:
    """Return whether at least one user exists, with a sticky True cache.

    Args:
        db: Database session.

    Returns:
        True when any user row exists (cached process-wide once observed),
        False on a fresh instance (always re-queried, never cached).
    """
    if _users_exist_state["known"] is True:
        return True
    if crud_user.has_any_users(db):
        _users_exist_state["known"] = True
        return True
    return False


def bootstrap_token_configured() -> bool:
    """Return whether a bootstrap token is configured for this instance."""
    return bool(settings.bootstrap_token)


def bootstrap_token_valid(candidate: Optional[str]) -> bool:
    """Constant-time comparison of a submitted token against the configured one.

    Args:
        candidate: Token submitted with the registration request, if any.

    Returns:
        True only when a token is configured AND the candidate matches it.
    """
    configured = settings.bootstrap_token
    if not configured or not candidate:
        return False
    return secrets.compare_digest(candidate.encode("utf-8"), configured.encode("utf-8"))


def bootstrap_pending(db: Session) -> bool:
    """Return True while the instance is unclaimed (zero users + token set)."""
    return bootstrap_token_configured() and not users_exist(db)


@dataclass(frozen=True)
class RegistrationDecision:
    """Outcome of evaluating the computed registration rule.

    Attributes:
        allowed: Whether the registration may proceed.
        reason: Denial reason (``REASON_*`` constant) when not allowed.
    """

    allowed: bool
    reason: Optional[str] = None


def evaluate_registration(
    db: Session, bootstrap_token: Optional[str] = None
) -> RegistrationDecision:
    """Evaluate the computed registration rule for one request.

    Args:
        db: Database session.
        bootstrap_token: Optional token submitted with the request.

    Returns:
        RegistrationDecision with ``allowed`` and, when denied, a ``reason``.
    """
    if not users_exist(db) and bootstrap_token_configured():
        if bootstrap_token_valid(bootstrap_token):
            return RegistrationDecision(allowed=True)
        return RegistrationDecision(
            allowed=False, reason=REASON_BOOTSTRAP_TOKEN_REQUIRED
        )
    if settings.registration_enabled:
        return RegistrationDecision(allowed=True)
    return RegistrationDecision(allowed=False, reason=REASON_REGISTRATION_DISABLED)


@dataclass(frozen=True)
class RegistrationState:
    """Registration-related feature state for /features.

    Attributes:
        registration_open: Whether the signup form should be reachable.
        bootstrap_pending: Zero users + token configured (form needs the
            setup link).
        first_account_pending: Signup would create the instance's first
            admin account.
    """

    registration_open: bool
    bootstrap_pending: bool
    first_account_pending: bool


def registration_state(db: Session) -> RegistrationState:
    """Compute the registration feature state with the same rule as /register.

    Args:
        db: Database session.

    Returns:
        RegistrationState mirroring what ``evaluate_registration`` would
        decide, plus the bootstrap-pending and first-account context flags.
    """
    token_configured = bootstrap_token_configured()
    if not settings.registration_enabled and not token_configured:
        # Registration closed and no bootstrap path: no need to query the
        # users table at all.
        return RegistrationState(
            registration_open=False,
            bootstrap_pending=False,
            first_account_pending=False,
        )
    no_users = not users_exist(db)
    pending = token_configured and no_users
    return RegistrationState(
        registration_open=pending or settings.registration_enabled,
        bootstrap_pending=pending,
        first_account_pending=no_users and (settings.registration_enabled or pending),
    )
