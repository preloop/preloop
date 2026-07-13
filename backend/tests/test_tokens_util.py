"""Tests for token generation and validation utilities."""

import os
from datetime import datetime, timedelta, UTC

import pytest
from jose import jwt

from preloop.utils.tokens import (
    ALGORITHM,
    SECRET_KEY,
    TokenError,
    create_email_verification_token,
    create_onboarding_token,
    create_password_reset_token,
    create_token,
    verify_token,
)


class TestTokenCreation:
    """Test token creation functions."""

    def test_create_email_verification_token(self):
        """Test creating email verification token."""
        email = "test@example.com"
        token = create_email_verification_token(email)

        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token content
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == email
        assert payload["type"] == "email_verification"
        assert "exp" in payload

    def test_create_password_reset_token(self):
        """Test creating password reset token."""
        email = "user@example.com"
        token = create_password_reset_token(email)

        assert isinstance(token, str)
        assert len(token) > 0

        # Decode and verify token content
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == email
        assert payload["type"] == "password_reset"
        assert "exp" in payload

    def test_email_verification_token_has_correct_expiry(self):
        """Test that email verification token has correct expiration."""
        email = "test@example.com"
        token = create_email_verification_token(email)

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        exp_timestamp = payload["exp"]
        exp_time = datetime.fromtimestamp(exp_timestamp, UTC)
        now = datetime.now(UTC)

        # Should expire in approximately 24 hours (EMAIL_TOKEN_EXPIRE_MINUTES)
        time_diff = exp_time - now
        expected_minutes = int(os.getenv("EMAIL_TOKEN_EXPIRE_MINUTES", "1440"))
        # Allow 1 minute tolerance for test execution time
        assert abs(time_diff.total_seconds() - expected_minutes * 60) < 60

    def test_password_reset_token_has_correct_expiry(self):
        """Test that password reset token has correct expiration."""
        email = "test@example.com"
        token = create_password_reset_token(email)

        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        exp_timestamp = payload["exp"]
        exp_time = datetime.fromtimestamp(exp_timestamp, UTC)
        now = datetime.now(UTC)

        # Should expire in 30 minutes (PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
        time_diff = exp_time - now
        expected_minutes = int(os.getenv("PASSWORD_RESET_TOKEN_EXPIRE_MINUTES", "30"))
        # Allow 1 minute tolerance for test execution time
        assert abs(time_diff.total_seconds() - expected_minutes * 60) < 60


class TestTokenVerification:
    """Test token verification function."""

    def test_verify_valid_email_verification_token(self):
        """Test verifying a valid email verification token."""
        email = "test@example.com"
        token = create_email_verification_token(email)

        verified_email = verify_token(token, "email_verification")

        assert verified_email == email

    def test_verify_valid_password_reset_token(self):
        """Test verifying a valid password reset token."""
        email = "user@example.com"
        token = create_password_reset_token(email)

        verified_email = verify_token(token, "password_reset")

        assert verified_email == email

    def test_verify_token_with_wrong_type(self):
        """Test that verifying token with wrong type raises error."""
        email = "test@example.com"
        token = create_email_verification_token(email)

        with pytest.raises(TokenError) as exc_info:
            verify_token(token, "password_reset")

        assert "Expected password_reset token" in str(exc_info.value)
        assert "got email_verification" in str(exc_info.value)

    def test_verify_expired_token(self):
        """Test that expired token raises TokenError."""
        email = "test@example.com"
        # Create a token that's already expired
        expire = datetime.now(UTC) - timedelta(minutes=10)
        to_encode = {"sub": email, "exp": expire, "type": "email_verification"}
        expired_token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

        with pytest.raises(TokenError) as exc_info:
            verify_token(expired_token, "email_verification")

        assert "Invalid or expired token" in str(exc_info.value)

    def test_verify_token_without_email(self):
        """Test that token without email raises TokenError."""
        # Create a token without 'sub' field
        expire = datetime.now(UTC) + timedelta(hours=1)
        to_encode = {"exp": expire, "type": "email_verification"}
        token = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

        with pytest.raises(TokenError) as exc_info:
            verify_token(token, "email_verification")

        assert "Missing email" in str(exc_info.value)

    def test_verify_invalid_token_format(self):
        """Test that invalid token format raises TokenError."""
        invalid_token = "this.is.not.a.valid.token"

        with pytest.raises(TokenError) as exc_info:
            verify_token(invalid_token, "email_verification")

        assert "Invalid or expired token" in str(exc_info.value)

    def test_verify_token_with_wrong_secret(self):
        """Test that token signed with wrong secret raises TokenError."""
        email = "test@example.com"
        expire = datetime.now(UTC) + timedelta(hours=1)
        to_encode = {"sub": email, "exp": expire, "type": "email_verification"}
        # Sign with wrong secret
        token = jwt.encode(to_encode, "wrong_secret_key", algorithm=ALGORITHM)

        with pytest.raises(TokenError) as exc_info:
            verify_token(token, "email_verification")

        assert "Invalid or expired token" in str(exc_info.value)

    def test_verify_malformed_token(self):
        """Test that malformed token raises TokenError."""
        malformed_token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"  # Only header

        with pytest.raises(TokenError) as exc_info:
            verify_token(malformed_token, "email_verification")

        assert "Invalid or expired token" in str(exc_info.value)


class TestCreateToken:
    """Test generic create_token and create_onboarding_token."""

    def test_create_token_basic(self):
        """Test create_token with custom type and expiry."""
        email = "onboard@example.com"
        token = create_token(email, "onboarding", expires_delta=timedelta(hours=1))
        assert isinstance(token, str)
        assert len(token) > 0
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        assert payload["sub"] == email
        assert payload["type"] == "onboarding"
        assert "exp" in payload

    def test_create_onboarding_token(self):
        """Test create_onboarding_token returns valid token."""
        email = "newuser@example.com"
        token = create_onboarding_token(email)
        assert isinstance(token, str)
        verified = verify_token(token, "onboarding")
        assert verified == email

    def test_create_token_default_expiry(self):
        """Test create_token uses default expiry when not specified."""
        email = "test@example.com"
        token = create_token(email, "custom_type")
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        exp_time = datetime.fromtimestamp(payload["exp"], UTC)
        now = datetime.now(UTC)
        # Should be ~24 hours (EMAIL_TOKEN_EXPIRE_MINUTES)
        assert (exp_time - now).total_seconds() > 3600


class TestTokenError:
    """Test TokenError exception."""

    def test_token_error_can_be_raised(self):
        """Test that TokenError can be raised and caught."""
        with pytest.raises(TokenError):
            raise TokenError("Test error")

    def test_token_error_message(self):
        """Test that TokenError preserves error message."""
        error_message = "Custom error message"
        error = TokenError(error_message)
        assert str(error) == error_message
        with pytest.raises(TokenError, match=error_message):
            raise error
