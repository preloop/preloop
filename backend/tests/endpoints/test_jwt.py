"""Tests for JWT authentication functions."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import jwt
import pytest
from fastapi import HTTPException

from preloop.api.auth import jwt as jwt_module


class TestPasswordHashing:
    """Tests for password hashing functions."""

    def test_get_password_hash(self):
        """Test password hashing."""
        password = "test_password_123"
        hashed = jwt_module.get_password_hash(password)

        assert hashed != password
        assert len(hashed) > 0
        assert hashed.startswith("$2b$")  # bcrypt prefix

    def test_verify_password_success(self):
        """Test successful password verification."""
        password = "test_password_123"
        hashed = jwt_module.get_password_hash(password)

        assert jwt_module.verify_password(password, hashed) is True

    def test_verify_password_failure(self):
        """Test failed password verification."""
        password = "test_password_123"
        wrong_password = "wrong_password"
        hashed = jwt_module.get_password_hash(password)

        assert jwt_module.verify_password(wrong_password, hashed) is False


class TestCreateAccessToken:
    """Tests for token creation."""

    def test_create_access_token_default_expiry(self):
        """Test token creation with default expiry."""
        data = {"sub": str(uuid.uuid4())}
        token = jwt_module.create_access_token(data)

        assert token is not None
        assert len(token) > 0
        assert "." in token  # JWT has three parts separated by dots

    def test_create_access_token_custom_expiry(self):
        """Test token creation with custom expiry."""
        data = {"sub": str(uuid.uuid4())}
        expires_delta = timedelta(hours=1)
        token = jwt_module.create_access_token(data, expires_delta=expires_delta)

        # Decode and verify expiry
        payload = jwt.decode(
            token, jwt_module.SECRET_KEY, algorithms=[jwt_module.ALGORITHM]
        )
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        now = datetime.now(UTC)

        # Expiry should be roughly 1 hour from now
        time_diff = exp - now
        assert timedelta(minutes=55) < time_diff < timedelta(minutes=65)

    def test_create_access_token_with_scopes(self):
        """Test token creation with scopes."""
        data = {"sub": str(uuid.uuid4()), "scopes": ["read", "write"]}
        token = jwt_module.create_access_token(data)

        payload = jwt.decode(
            token, jwt_module.SECRET_KEY, algorithms=[jwt_module.ALGORITHM]
        )
        assert payload["scopes"] == ["read", "write"]


class TestDecodeToken:
    """Tests for token decoding."""

    def test_decode_token_success(self):
        """Test successful token decoding."""
        user_id = str(uuid.uuid4())
        data = {"sub": user_id, "scopes": ["read"]}
        token = jwt_module.create_access_token(data)

        token_data = jwt_module.decode_token(token)

        assert token_data.sub == user_id
        assert token_data.scopes == ["read"]

    def test_decode_token_missing_sub(self):
        """Test decoding token without sub field."""
        # Create token without sub
        token = jwt.encode(
            {"exp": datetime.now(UTC) + timedelta(hours=1)},
            jwt_module.SECRET_KEY,
            algorithm=jwt_module.ALGORITHM,
        )

        with pytest.raises(HTTPException) as exc_info:
            jwt_module.decode_token(token)

        assert exc_info.value.status_code == 401

    def test_decode_token_invalid(self):
        """Test decoding invalid token."""
        with pytest.raises(HTTPException) as exc_info:
            jwt_module.decode_token("invalid.token.here")

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == "Invalid authentication credentials"

    def test_decode_token_expired(self):
        """Test decoding expired token."""
        data = {"sub": str(uuid.uuid4())}
        expires_delta = timedelta(seconds=-10)  # Already expired
        token = jwt_module.create_access_token(data, expires_delta=expires_delta)

        with pytest.raises(HTTPException) as exc_info:
            jwt_module.decode_token(token)

        assert exc_info.value.status_code == 401

    def test_decode_token_with_refresh_flag(self):
        """Test decoding token with refresh flag."""
        data = {"sub": str(uuid.uuid4()), "refresh": True}
        token = jwt_module.create_access_token(data)

        token_data = jwt_module.decode_token(token)

        assert token_data.refresh is True


class TestGetCurrentUser:
    """Tests for get_current_user function."""

    @pytest.fixture
    def mock_user(self):
        """Create a mock user."""
        user = MagicMock()
        user.id = uuid.uuid4()
        user.username = "testuser"
        user.email = "test@example.com"
        user.is_active = True
        user.account_id = str(uuid.uuid4())
        return user

    def test_get_current_user_with_jwt(self, mock_user):
        """Test getting current user with valid JWT."""
        user_id = str(mock_user.id)
        token = jwt_module.create_access_token({"sub": user_id})

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_user
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            result = jwt_module.get_current_user(token, db=mock_session)

            assert result == mock_user

    def test_get_current_user_with_api_key(self, mock_user):
        """Test getting current user with API key (no dots)."""
        api_key = "abcdef123456"  # No dots, looks like API key

        mock_api_key = MagicMock()
        mock_api_key.user_id = mock_user.id
        mock_api_key.is_expired = False

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_user
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            with patch.object(
                jwt_module.crud_api_key, "get_by_key", return_value=mock_api_key
            ):
                result = jwt_module.get_current_user(api_key, db=mock_session)

                assert result == mock_user

    def test_get_current_user_api_key_expired(self, mock_user):
        """Test failure when API key is expired."""
        api_key = "abcdef123456"

        mock_api_key = MagicMock()
        mock_api_key.user_id = mock_user.id
        mock_api_key.is_expired = True
        mock_api_key.expires_at = datetime.now(UTC) - timedelta(days=1)

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_get_db.return_value = iter([mock_session])

            with patch.object(
                jwt_module.crud_api_key, "get_by_key", return_value=mock_api_key
            ):
                with pytest.raises(HTTPException) as exc_info:
                    jwt_module.get_current_user(api_key, db=mock_session)

                assert exc_info.value.status_code == 401
                assert "expired" in exc_info.value.detail

    def test_get_current_user_inactive_user(self, mock_user):
        """Test failure when user is inactive."""
        mock_user.is_active = False
        user_id = str(mock_user.id)
        token = jwt_module.create_access_token({"sub": user_id})

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_user
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            with pytest.raises(HTTPException) as exc_info:
                jwt_module.get_current_user(token, db=mock_session)

            assert exc_info.value.status_code == 401
            # The actual implementation wraps errors in "Authentication error"
            assert (
                "error" in exc_info.value.detail.lower()
                or "Inactive" in exc_info.value.detail
            )

    def test_get_current_user_not_found(self):
        """Test failure when user doesn't exist."""
        user_id = str(uuid.uuid4())
        token = jwt_module.create_access_token({"sub": user_id})

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = None
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            with pytest.raises(HTTPException) as exc_info:
                jwt_module.get_current_user(token, db=mock_session)

            assert exc_info.value.status_code == 401
            # The actual implementation wraps errors in "Authentication error"
            assert (
                "error" in exc_info.value.detail.lower()
                or "not found" in exc_info.value.detail.lower()
            )

    def test_get_current_user_invalid_uuid(self):
        """Test failure when token contains invalid UUID."""
        token = jwt_module.create_access_token({"sub": "not-a-uuid"})
        mock_session = MagicMock()

        with pytest.raises(HTTPException) as exc_info:
            jwt_module.get_current_user(token, db=mock_session)

        assert exc_info.value.status_code == 401
        assert "Invalid user ID" in exc_info.value.detail

    def test_get_current_user_refresh_token_rejected(self, mock_user):
        """Refresh tokens must not authenticate access-token endpoints."""
        user_id = str(mock_user.id)
        # Create a refresh token (with refresh=True)
        token = jwt_module.create_access_token({"sub": user_id, "refresh": True})

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_user
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            # decode_token returns a TokenData model; the refresh flag is read
            # as an attribute, so a refresh token is rejected with 401.
            with pytest.raises(HTTPException) as exc_info:
                jwt_module.get_current_user(token, db=mock_session)
            assert exc_info.value.status_code == 401
            assert "refresh token" in exc_info.value.detail.lower()


class TestGetCurrentActiveUser:
    """Tests for get_current_active_user function."""

    def test_get_current_active_user(self):
        """Test getting current active user."""
        mock_user = MagicMock()
        mock_user.is_active = True

        result = jwt_module.get_current_active_user(current_user=mock_user)

        assert result == mock_user


class TestGetCurrentActiveUserOptional:
    """Tests for get_current_active_user_optional function."""

    def test_returns_none_when_no_token(self):
        """Test returns None when no token is provided."""
        result = jwt_module.get_current_active_user_optional(token=None)

        assert result is None

    def test_returns_user_when_valid_token(self):
        """Test returns user when valid token is provided."""
        mock_user = MagicMock()
        mock_user.is_active = True

        with patch.object(jwt_module, "get_current_user") as mock_get_user:
            mock_get_user.return_value = mock_user

            result = jwt_module.get_current_active_user_optional(token="valid_token")

            assert result == mock_user

    def test_returns_none_when_inactive_user(self):
        """Test returns None when user is inactive."""
        mock_user = MagicMock()
        mock_user.is_active = False

        with patch.object(jwt_module, "get_current_user") as mock_get_user:
            mock_get_user.return_value = mock_user

            result = jwt_module.get_current_active_user_optional(token="valid_token")

            assert result is None

    def test_returns_none_on_authentication_error(self):
        """Test returns None when authentication fails."""
        with patch.object(jwt_module, "get_current_user") as mock_get_user:
            mock_get_user.side_effect = HTTPException(status_code=401, detail="Invalid")

            result = jwt_module.get_current_active_user_optional(token="invalid_token")

            assert result is None


class TestGetUserFromTokenIfValid:
    """Tests for get_user_from_token_if_valid function."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_token(self):
        """Test returns None when no token is provided."""
        result = await jwt_module.get_user_from_token_if_valid(
            token="", db_session=None
        )

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_user_when_valid(self):
        """Test returns user when token is valid."""
        mock_user = MagicMock()
        mock_user.is_active = True

        with patch.object(jwt_module, "crud_api_key") as mock_crud:
            mock_crud.get_by_key.return_value = MagicMock()
            with patch.object(jwt_module, "_authenticate_with_api_key") as mock_auth:
                mock_auth.return_value = mock_user

                result = await jwt_module.get_user_from_token_if_valid(
                    token="validtoken", db_session=MagicMock()
                )

                assert result == mock_user

    @pytest.mark.asyncio
    async def test_returns_none_on_exception(self):
        """Test returns None when exception occurs."""
        with patch.object(jwt_module, "crud_api_key") as mock_crud:
            mock_crud.get_by_key.side_effect = Exception("Some error")

            result = await jwt_module.get_user_from_token_if_valid(
                token="some_token", db_session=MagicMock()
            )

            assert result is None


class TestApiKeyFallback:
    """Tests for API key fallback when JWT fails."""

    @pytest.fixture
    def mock_user(self):
        """Create a mock user."""
        user = MagicMock()
        user.id = uuid.uuid4()
        user.username = "testuser"
        user.is_active = True
        return user

    def test_api_key_used_for_token_without_dots(self, mock_user):
        """Test that API key auth is used for tokens without dots."""
        # Token without dots is treated as API key first
        token = "simpletokenwithoutdots"

        mock_api_key = MagicMock()
        mock_api_key.user_id = mock_user.id
        mock_api_key.name = "test_key"
        mock_api_key.is_expired = False
        mock_api_key.expires_at = None
        mock_api_key.last_used_at = None

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_query = MagicMock()
            mock_query.filter.return_value = mock_query
            mock_query.first.return_value = mock_user
            mock_session.query.return_value = mock_query
            mock_get_db.return_value = iter([mock_session])

            with patch.object(
                jwt_module.crud_api_key, "get_by_key", return_value=mock_api_key
            ):
                result = jwt_module.get_current_user(token, db=mock_session)

                assert result == mock_user

    def test_api_key_not_found_fallback(self):
        """Test failure when API key is not found."""
        # Token without dots - will try API key first
        token = "invalidapikey"

        with patch.object(jwt_module, "get_db_session") as mock_get_db:
            mock_session = MagicMock()
            mock_get_db.return_value = iter([mock_session])

            with patch.object(jwt_module.crud_api_key, "get_by_key", return_value=None):
                with pytest.raises(HTTPException) as exc_info:
                    jwt_module.get_current_user(token, db=mock_session)

                assert exc_info.value.status_code == 401
