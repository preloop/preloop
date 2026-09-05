"""Token generation and validation for Preloop."""

import os
from datetime import UTC, datetime, timedelta

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


def create_email_verification_token(email: str) -> str:
    """Create an email verification token.

    Args:
        email: The email address to verify.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + timedelta(minutes=EMAIL_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": email, "exp": expire, "type": "email_verification"}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def create_password_reset_token(email: str) -> str:
    """Create a password reset token.

    Args:
        email: The email address of the user.

    Returns:
        A JWT token.
    """
    expire = datetime.now(UTC) + timedelta(minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
    to_encode = {"sub": email, "exp": expire, "type": "password_reset"}
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


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
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        token_purpose: str = payload.get("type")

        if not email:
            raise TokenError("Invalid token: Missing email")

        if token_purpose != token_type:
            raise TokenError(
                f"Invalid token: Expected {token_type} token, got {token_purpose}"
            )

        return email
    except PyJWTError:
        raise TokenError("Invalid or expired token")


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
