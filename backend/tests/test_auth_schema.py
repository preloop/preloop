"""Tests for authentication Pydantic schemas."""

from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from preloop.schemas.auth import (
    ApiKeyCreate,
    ApiKeyResponse,
    ApiKeySummary,
    ApiUsageStatistics,
    EmailVerificationRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RefreshRequest,
    Token,
    TokenData,
    User,
    AuthUserCreate,
    UserInDB,
    AuthUserResponse,
    AuthUserUpdate,
)


class TestToken:
    """Test Token schema."""

    def test_create_token(self):
        """Test creating Token with all fields."""
        token = Token(
            access_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            refresh_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...",
            token_type="bearer",
            expires_in=3600,
        )

        assert token.access_token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        assert token.refresh_token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        assert token.token_type == "bearer"
        assert token.expires_in == 3600


class TestTokenData:
    """Test TokenData schema."""

    def test_create_with_defaults(self):
        """Test creating TokenData with default values."""
        token_data = TokenData()

        assert token_data.sub is None
        assert token_data.scopes == []
        assert token_data.exp is None
        assert token_data.refresh is False

    def test_create_with_all_fields(self):
        """Test creating TokenData with all fields."""
        exp_time = datetime.now() + timedelta(hours=1)
        token_data = TokenData(
            sub="user123",
            scopes=["read:issues", "write:issues"],
            exp=exp_time,
            refresh=True,
        )

        assert token_data.sub == "user123"
        assert token_data.scopes == ["read:issues", "write:issues"]
        assert token_data.exp == exp_time
        assert token_data.refresh is True


class TestUser:
    """Test User schema."""

    def test_create_minimal_user(self):
        """Test creating User with minimal fields."""
        user = User(username="testuser")

        assert user.username == "testuser"
        assert user.email is None
        assert user.full_name is None
        assert user.disabled is None
        assert user.email_verified is None

    def test_create_complete_user(self):
        """Test creating User with all fields."""
        user = User(
            username="johndoe",
            email="john@example.com",
            full_name="John Doe",
            disabled=False,
            email_verified=True,
        )

        assert user.username == "johndoe"
        assert user.email == "john@example.com"
        assert user.full_name == "John Doe"
        assert user.disabled is False
        assert user.email_verified is True


class TestUserInDB:
    """Test UserInDB schema."""

    def test_inherits_from_user(self):
        """Test that UserInDB inherits from User."""
        user_in_db = UserInDB(
            username="testuser",
            email="test@example.com",
            hashed_password="$2b$12$...",
        )

        assert isinstance(user_in_db, User)
        assert user_in_db.username == "testuser"
        assert user_in_db.hashed_password == "$2b$12$..."


class TestAuthUserCreate:
    """Test AuthUserCreate schema."""

    def test_create_with_required_fields(self):
        """Test creating AuthUserCreate with required fields."""
        user_create = AuthUserCreate(
            username="newuser",
            email="new@example.com",
            password="securepassword123",
        )

        assert user_create.username == "newuser"
        assert user_create.email == "new@example.com"
        assert user_create.password == "securepassword123"
        assert user_create.full_name is None

    def test_create_with_full_name(self):
        """Test creating AuthUserCreate with optional full_name."""
        user_create = AuthUserCreate(
            username="newuser",
            email="new@example.com",
            password="securepassword123",
            full_name="New User",
        )

        assert user_create.full_name == "New User"

    def test_username_length_validation(self):
        """Test username length validation."""
        # Too short
        with pytest.raises(ValidationError):
            AuthUserCreate(
                username="ab",
                email="test@example.com",
                password="password123",
            )

        # Too long
        with pytest.raises(ValidationError):
            AuthUserCreate(
                username="a" * 51,
                email="test@example.com",
                password="password123",
            )

    def test_username_alphanumeric_validation(self):
        """Test username alphanumeric validation."""
        with pytest.raises(ValidationError) as exc_info:
            AuthUserCreate(
                username="invalid-username",
                email="test@example.com",
                password="password123",
            )

        errors = exc_info.value.errors()
        assert any("alphanumeric" in str(error).lower() for error in errors)

    def test_password_length_validation(self):
        """Test password minimum length validation."""
        with pytest.raises(ValidationError):
            AuthUserCreate(
                username="testuser",
                email="test@example.com",
                password="short",
            )

    def test_email_validation(self):
        """Test email format validation."""
        with pytest.raises(ValidationError):
            AuthUserCreate(
                username="testuser",
                email="invalid-email",
                password="password123",
            )


class TestAuthUserUpdate:
    """Test AuthUserUpdate schema."""

    def test_create_empty_update(self):
        """Test creating empty AuthUserUpdate."""
        update = AuthUserUpdate()

        assert update.full_name is None

    def test_create_with_full_name(self):
        """Test creating AuthUserUpdate with full_name."""
        update = AuthUserUpdate(full_name="Updated Name")

        assert update.full_name == "Updated Name"


class TestAuthUserResponse:
    """Test AuthUserResponse schema."""

    def test_create_user_response(self):
        """Test creating AuthUserResponse."""
        user_id = uuid4()
        account_id = uuid4()
        team_id = uuid4()
        response = AuthUserResponse(
            id=user_id,
            account_id=account_id,
            username="testuser",
            email="test@example.com",
            full_name="Test User",
            email_verified=True,
            team_ids=[team_id],
        )

        assert response.id == user_id
        assert response.account_id == account_id
        assert response.username == "testuser"
        assert response.email == "test@example.com"
        assert response.full_name == "Test User"
        assert response.email_verified is True
        assert response.team_ids == [team_id]

    def test_email_verified_required(self):
        """Test that email_verified is required."""
        with pytest.raises(ValidationError):
            AuthUserResponse(
                id=uuid4(),
                account_id=uuid4(),
                username="testuser",
                email="test@example.com",
                team_ids=[],
            )

    def test_identity_fields_required(self):
        """id, account_id and team_ids are part of the profile contract."""
        with pytest.raises(ValidationError):
            AuthUserResponse(
                username="testuser",
                email="test@example.com",
                email_verified=True,
            )


class TestLoginRequest:
    """Test LoginRequest schema."""

    def test_create_login_request(self):
        """Test creating LoginRequest."""
        request = LoginRequest(username="testuser", password="password123")

        assert request.username == "testuser"
        assert request.password == "password123"


class TestRefreshRequest:
    """Test RefreshRequest schema."""

    def test_create_refresh_request(self):
        """Test creating RefreshRequest."""
        request = RefreshRequest(
            refresh_token="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        )

        assert request.refresh_token == "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."


class TestEmailVerificationRequest:
    """Test EmailVerificationRequest schema."""

    def test_create_email_verification_request(self):
        """Test creating EmailVerificationRequest."""
        request = EmailVerificationRequest(token="verification-token-123")

        assert request.token == "verification-token-123"


class TestPasswordResetRequest:
    """Test PasswordResetRequest schema."""

    def test_create_password_reset_request(self):
        """Test creating PasswordResetRequest."""
        request = PasswordResetRequest(email="user@example.com")

        assert request.email == "user@example.com"

    def test_email_validation(self):
        """Test email validation."""
        with pytest.raises(ValidationError):
            PasswordResetRequest(email="invalid-email")


class TestPasswordResetConfirmRequest:
    """Test PasswordResetConfirmRequest schema."""

    def test_create_password_reset_confirm(self):
        """Test creating PasswordResetConfirmRequest."""
        request = PasswordResetConfirmRequest(
            token="reset-token-123",
            new_password="newsecurepassword",
        )

        assert request.token == "reset-token-123"
        assert request.new_password == "newsecurepassword"

    def test_password_length_validation(self):
        """Test new password length validation."""
        with pytest.raises(ValidationError):
            PasswordResetConfirmRequest(
                token="reset-token-123",
                new_password="short",
            )


class TestPasswordChangeRequest:
    """Test PasswordChangeRequest schema."""

    def test_create_password_change_request(self):
        """Test creating PasswordChangeRequest."""
        request = PasswordChangeRequest(
            current_password="oldpassword123",
            new_password="newpassword123",
        )

        assert request.current_password == "oldpassword123"
        assert request.new_password == "newpassword123"

    def test_new_password_length_validation(self):
        """Test new password length validation."""
        with pytest.raises(ValidationError):
            PasswordChangeRequest(
                current_password="oldpassword123",
                new_password="short",
            )


class TestApiKeyCreate:
    """Test ApiKeyCreate schema."""

    def test_create_with_required_fields(self):
        """Test creating ApiKeyCreate with required fields."""
        api_key = ApiKeyCreate(name="My API Key")

        assert api_key.name == "My API Key"
        assert api_key.expires_at is None
        assert api_key.scopes == []

    def test_create_with_all_fields(self):
        """Test creating ApiKeyCreate with all fields."""
        expires_at = datetime.now() + timedelta(days=30)
        api_key = ApiKeyCreate(
            name="Production Key",
            expires_at=expires_at,
            scopes=["read:issues", "write:issues"],
        )

        assert api_key.name == "Production Key"
        assert api_key.expires_at == expires_at
        assert api_key.scopes == ["read:issues", "write:issues"]

    def test_name_length_validation(self):
        """Test name length validation."""
        # Empty name
        with pytest.raises(ValidationError):
            ApiKeyCreate(name="")

        # Name too long
        with pytest.raises(ValidationError):
            ApiKeyCreate(name="a" * 101)


class TestApiKeyResponse:
    """Test ApiKeyResponse schema."""

    def test_create_api_key_response(self):
        """Test creating ApiKeyResponse with all fields."""
        key_id = uuid4()
        user_id = uuid4()
        created_at = datetime.now()
        expires_at = datetime.now() + timedelta(days=30)
        last_used_at = datetime.now()

        response = ApiKeyResponse(
            id=key_id,
            name="Test Key",
            key="sk_test_...",
            created_at=created_at,
            expires_at=expires_at,
            scopes=["read:issues"],
            user_id=user_id,
            last_used_at=last_used_at,
        )

        assert response.id == key_id
        assert response.name == "Test Key"
        assert response.key == "sk_test_..."
        assert response.created_at == created_at
        assert response.expires_at == expires_at
        assert response.scopes == ["read:issues"]
        assert response.user_id == user_id
        assert response.last_used_at == last_used_at

    def test_create_with_defaults(self):
        """Test creating with optional fields as None."""
        key_id = uuid4()
        user_id = uuid4()
        created_at = datetime.now()

        response = ApiKeyResponse(
            id=key_id,
            name="Test Key",
            key="sk_test_...",
            created_at=created_at,
            user_id=user_id,
        )

        assert response.expires_at is None
        assert response.scopes == []
        assert response.last_used_at is None


class TestApiKeySummary:
    """Test ApiKeySummary schema."""

    def test_create_api_key_summary(self):
        """Test creating ApiKeySummary (without key field)."""
        key_id = uuid4()
        created_at = datetime.now()
        expires_at = datetime.now() + timedelta(days=30)

        summary = ApiKeySummary(
            id=key_id,
            name="Test Key",
            created_at=created_at,
            expires_at=expires_at,
            scopes=["read:issues"],
            last_used_at=None,
        )

        assert summary.id == key_id
        assert summary.name == "Test Key"
        assert summary.created_at == created_at
        assert summary.expires_at == expires_at
        assert summary.scopes == ["read:issues"]
        assert summary.last_used_at is None


class TestApiUsageStatistics:
    """Test ApiUsageStatistics schema."""

    def test_create_usage_statistics(self):
        """Test creating ApiUsageStatistics."""
        stats = ApiUsageStatistics(
            total_requests=1000,
            requests_by_date={"2025-01-15": 450, "2025-01-16": 550},
            issues_created=50,
            issues_updated=75,
            issues_closed=25,
            requests_by_endpoint={
                "/api/issues": 600,
                "/api/comments": 300,
                "/api/projects": 100,
            },
        )

        assert stats.total_requests == 1000
        assert stats.requests_by_date["2025-01-15"] == 450
        assert stats.issues_created == 50
        assert stats.issues_updated == 75
        assert stats.issues_closed == 25
        assert stats.requests_by_endpoint["/api/issues"] == 600

    def test_empty_statistics(self):
        """Test creating empty statistics."""
        stats = ApiUsageStatistics(
            total_requests=0,
            requests_by_date={},
            issues_created=0,
            issues_updated=0,
            issues_closed=0,
            requests_by_endpoint={},
        )

        assert stats.total_requests == 0
        assert len(stats.requests_by_date) == 0
        assert len(stats.requests_by_endpoint) == 0
