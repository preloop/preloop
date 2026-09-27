"""Token generation and validation for Preloop."""

import hashlib
import os
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
from jwt import PyJWTError

# Configuration
from preloop.config import settings

SECRET_KEY: str = settings.security.secret_key
ALGORITHM = "HS256"
EMAIL_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("EMAIL_TOKEN_EXPIRE_MINUTES", "1440")
)  # 24 hours
PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = int(
    os.getenv("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", "30")
)


class TokenError(Exception):
    """Raised when token validation fails."""

    pass


def _create_user_token(
    email: str, user_id: UUID | str, token_type: str, expire_minutes: int
) -> str:
    """Mint a token bound to one user row and the address it was sent to.

    ``user.email`` is not unique (one address can hold a row in several
    accounts), so the address alone does not name a user. The ``uid`` claim
    does, and the ``sub`` claim keeps the address so the link stops working
    if that row's address changes before it is followed.
    """
    expire = datetime.now(UTC) + timedelta(minutes=expire_minutes)
    to_encode = {
        "sub": email,
        "uid": str(user_id),
        "exp": expire,
        "type": token_type,
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_email_verification_token(email: str, *, user_id: UUID | str) -> str:
    """Create an email verification token for one user row.

    Args:
        email: The email address to verify.
        user_id: The id of the user row the address belongs to.

    Returns:
        A JWT token.
    """
    return _create_user_token(
        email, user_id, "email_verification", EMAIL_TOKEN_EXPIRE_MINUTES
    )


def create_password_reset_token(email: str, *, user_id: UUID | str) -> str:
    """Create a password reset token for one user row.

    Args:
        email: The email address the reset link is sent to.
        user_id: The id of the user row whose password the link resets.

    Returns:
        A JWT token.
    """
    return _create_user_token(
        email, user_id, "password_reset", PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    )


@dataclass(frozen=True)
class UserTokenClaims:
    """What a user-bound token names: one row, and the address it was sent to."""

    user_id: UUID
    email: str


def _decode_typed_token(token: str, token_type: str) -> dict:
    """Decode a token once and check its signature, expiry, subject and type.

    Shared by ``verify_token`` and ``verify_user_token`` so the two cannot
    drift apart.

    Raises:
        TokenError: If the token is invalid, expired, has no subject, or is
            of the wrong type.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except PyJWTError:
        raise TokenError("Invalid or expired token")

    if not payload.get("sub"):
        raise TokenError("Invalid token: Missing email")

    token_purpose = payload.get("type")
    if token_purpose != token_type:
        raise TokenError(
            f"Invalid token: Expected {token_type} token, got {token_purpose}"
        )
    return payload


def verify_user_token(token: str, token_type: str) -> UserTokenClaims:
    """Verify a user-bound token and return the row and address it names.

    A token without a ``uid`` claim is refused: it names an address, and an
    address can belong to more than one row.

    Args:
        token: The JWT token to verify.
        token_type: The expected token type ("email_verification" or
            "password_reset").

    Returns:
        The user id and email address from the token.

    Raises:
        TokenError: If the token is invalid, expired, of the wrong type, or
            not bound to a user row.
    """
    payload = _decode_typed_token(token, token_type)
    try:
        user_id = UUID(str(payload.get("uid") or ""))
    except ValueError:
        raise TokenError(
            "This link is no longer valid. Request a new one and use that instead."
        )
    return UserTokenClaims(user_id=user_id, email=str(payload["sub"]))


def verify_token(token: str, token_type: str) -> str:
    """Verify a token and return the email address.

    Args:
        token: The JWT token to verify.
        token_type: The expected token type ("email_verification" or "password_reset").

    Returns:
        The email address from the token.

    Raises:
        TokenError: If the token is invalid, expired, or has the wrong type.
    """
    return str(_decode_typed_token(token, token_type)["sub"])


def create_token(
    email: str,
    token_type: str,
    *,
    expires_delta: timedelta | None = None,
) -> str:
    """Create a JWT token with optional custom expiry.

    Args:
        email: The email address to encode.
        token_type: Token type (e.g. "onboarding", "email_verification").
        expires_delta: Time until expiry. Defaults to 24 hours.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=EMAIL_TOKEN_EXPIRE_MINUTES)
    )
    to_encode = {"sub": email, "exp": expire, "type": token_type}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_onboarding_token(email: str) -> str:
    """Creates a short-lived token for the onboarding process."""
    return create_token(email, "onboarding", expires_delta=timedelta(hours=1))


#: Type marker for the account-claim token minted after a completed checkout.
ONBOARDING_CLAIM_TOKEN_TYPE = "onboarding_claim"

#: How long the welcome link stays usable. Short on purpose: it is handed to
#: the browser that just completed checkout and is used seconds later. Anyone
#: who loses the link recovers through the ordinary password reset email, not
#: through a second claim.
ONBOARDING_CLAIM_TOKEN_EXPIRE_MINUTES = 60


def create_onboarding_claim_token(
    *,
    email: str,
    account_id: str,
    checkout_session_id: str,
) -> str:
    """Mint the single-use token that claims a checkout-created account.

    The account is created server side with a ``NEEDS_RESET`` placeholder
    password, so something has to prove that the caller who sets the real
    password is the person who paid. That proof is this token: it is signed
    with the instance secret, expires, carries a random ``jti`` so the server
    can consume it exactly once, and is bound to both the account it opens and
    the Stripe checkout session that created it. Knowing the address is not
    enough, and a token for one account cannot claim another.

    Args:
        email: The address on the completed checkout, also the claim subject.
        account_id: The account the token opens.
        checkout_session_id: The Stripe checkout session that created it,
            recorded so a claim can be traced back to the payment.

    Returns:
        A JWT to put in the welcome URL.
    """
    expire = datetime.now(UTC) + timedelta(
        minutes=ONBOARDING_CLAIM_TOKEN_EXPIRE_MINUTES
    )
    to_encode = {
        "sub": email,
        "exp": expire,
        "type": ONBOARDING_CLAIM_TOKEN_TYPE,
        "account_id": str(account_id),
        "checkout_session_id": str(checkout_session_id),
        "jti": secrets.token_urlsafe(32),
    }
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def hash_onboarding_claim_token(token: str) -> str:
    """Fingerprint a claim token for storage.

    The server stores this, never the token, so a database copy does not hand
    anyone a working claim link.

    Args:
        token: The JWT handed to the browser.

    Returns:
        Hex SHA-256 of the token.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_onboarding_claim_token(token: str) -> dict:
    """Verify a claim token's signature, expiry, type and bindings.

    Args:
        token: The JWT presented by the welcome page.

    Returns:
        A dict with ``email``, ``account_id`` and ``checkout_session_id``.

    Raises:
        TokenError: If the token is invalid, expired, of the wrong type, or
            missing either binding.
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except PyJWTError:
        raise TokenError("Invalid or expired onboarding claim token")

    if payload.get("type") != ONBOARDING_CLAIM_TOKEN_TYPE:
        raise TokenError("Invalid onboarding claim token: wrong type")

    email = payload.get("sub")
    account_id = payload.get("account_id")
    checkout_session_id = payload.get("checkout_session_id")
    if not email or not account_id or not checkout_session_id:
        raise TokenError("Invalid onboarding claim token: missing binding")

    return {
        "email": email,
        "account_id": account_id,
        "checkout_session_id": checkout_session_id,
    }
