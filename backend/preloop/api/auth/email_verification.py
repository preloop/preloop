"""Optional required email verification.

Off by default (``REQUIRE_EMAIL_VERIFICATION``), which is exactly today's
behaviour: an unverified address never stops a sign-in. A deployment that
turns it on (Cloud does) gets one rule, applied where tokens are minted:

- a password (``local``) user with ``email_verified`` false cannot obtain an
  access token, from the login endpoints, from passkey sign-in, or by
  rotating a refresh token. The refusal is a 403 whose detail carries
  ``code: email_not_verified`` so the login page can offer a resend instead
  of printing "incorrect password" at someone whose password was right.
- everyone else is unaffected. OAuth/SSO identities are asserted by the
  provider, and a user created from a completed checkout attached a payment
  method to the address and proved possession of the welcome link that
  claimed the account, so both are verified by construction and never reach
  this gate. The canonical provider credential supplies this exemption;
  a membership source flag cannot grant it.

The resend endpoint is rate limited here rather than in the router because
the limit protects the mail sender, not one handler: the same budget covers
anonymous resends from the login page.
"""

import logging
from typing import Dict, Optional

from fastapi import HTTPException, status

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import crud_person
from preloop.services.rate_limit import (
    AUTH_EMAIL_SEND,
    enforce_all_or_none,
    reset_memory_store,
)
from sqlalchemy.orm import Session

UserModel = models.User
UserSource = models.UserSource

logger = logging.getLogger(__name__)

EMAIL_NOT_VERIFIED_CODE = "email_not_verified"
EMAIL_NOT_VERIFIED_MESSAGE = (
    "Verify your email address to finish signing in. Open the link we sent "
    "you, or ask for a new one."
)

RESEND_RATE_LIMITED_MESSAGE = (
    "Too many verification emails requested. Wait a few minutes and try again."
)


def resend_max_per_window() -> int:
    """Per-window budget for verification resends.

    Keyed by both client IP and target address, so neither one alone can
    drain the mail sender. Read from settings on each call rather than frozen
    at import: a tuning value should not be baked into module state, and
    ``preloop.config`` is where every other env knob is parsed (forgivingly,
    so a typo falls back to the default instead of stopping the server).

    Returns:
        The number of resends one bucket may spend per window.
    """
    return settings.email_verification_resend_limit


def resend_window_seconds() -> int:
    """Length of the resend budget window, in seconds.

    Returns:
        The window length from settings.
    """
    return settings.email_verification_resend_window_seconds


def verification_required(user: UserModel, db: Session | None = None) -> bool:
    """Whether this user must verify the address before holding a session.

    Args:
        user: The user a token is about to be issued for.

    Returns:
        True only when the setting is on, the address is unverified, and the
        identity is a local password one.
    """
    if not settings.require_email_verification:
        return False
    if db is not None:
        person = crud_person.resolve_for_membership(db, membership_id=user.id)
        return (
            person.email_verified_at is None
            and not crud_person.has_external_identity(db, person_id=person.id)
        )
    if user.email_verified:
        return False
    return (user.user_source or UserSource.LOCAL) == UserSource.LOCAL


def raise_email_not_verified(email: Optional[str] = None) -> None:
    """Refuse a token with the machine-readable code clients branch on.

    Args:
        email: Address to resend to, echoed back so the login page can offer
            the resend without asking the person to type it again. Only ever
            the address of the account whose password was just accepted, so
            it discloses nothing the caller did not already prove.

    Raises:
        HTTPException: Always, 403 with ``code``/``message`` detail.
    """
    detail: Dict[str, str] = {
        "code": EMAIL_NOT_VERIFIED_CODE,
        "message": EMAIL_NOT_VERIFIED_MESSAGE,
    }
    if email:
        detail["email"] = email
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=detail,
    )


def enforce_verified_email(user: UserModel, db: Session | None = None) -> None:
    """Gate token issuance for ``user``.

    Args:
        user: The authenticated user.

    Raises:
        HTTPException: 403 ``email_not_verified`` when verification is
            required and missing.
    """
    if verification_required(user, db):
        logger.info(
            "Refusing token for unverified user %s (require_email_verification)",
            user.username,
        )
        raise_email_not_verified(user.email)


def reset_resend_rate_limit() -> None:
    """Drop the in-process limiter counters (tests).

    The resend budget lives in the shared limiter (rule ``auth.email_send``);
    with the Postgres store a restart no longer resets it.
    """
    reset_memory_store()


def check_resend_rate_limit(*keys: str) -> None:
    """Consume one resend from each supplied bucket key.

    Blocking (the shared store does I/O); async callers go through the
    threadpool.

    Args:
        keys: Bucket identities, e.g. the client IP and the target address.
            Empty keys are ignored so a missing IP cannot merge every caller
            into one bucket.

    Raises:
        HTTPException: 429 ``rate_limited`` with ``Retry-After`` when any key
            is out of budget. No key is charged when one refuses, so a
            refused request does not eat the budget.
    """
    enforce_all_or_none(
        AUTH_EMAIL_SEND,
        keys,
        default=(resend_max_per_window(), resend_window_seconds()),
        detail=RESEND_RATE_LIMITED_MESSAGE,
    )
