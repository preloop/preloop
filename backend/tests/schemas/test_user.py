"""Tests for user schemas."""

from datetime import datetime
from uuid import uuid4

import pytest

from preloop.schemas.user import (
    AdminUserCreate,
    AdminUserResponse,
    AdminUserUpdate,
    UserBase,
    UserListResponse,
    UserPasswordUpdate,
    UserSummary,
)


class TestUserBase:
    """Test UserBase schema."""

    def test_valid(self):
        """Valid user base."""
        data = UserBase(
            username="testuser",
            email="test@example.com",
            full_name="Test User",
        )
        assert data.username == "testuser"
        assert data.email == "test@example.com"
        assert data.full_name == "Test User"

    def test_full_name_optional(self):
        """Full name is optional."""
        data = UserBase(username="user", email="u@ex.com")
        assert data.full_name is None


class TestAdminUserCreate:
    """Test AdminUserCreate schema."""

    def test_valid(self):
        """Valid admin user create."""
        data = AdminUserCreate(
            username="adminuser",
            email="admin@example.com",
            password="securepass123",
        )
        assert data.username == "adminuser"
        assert data.password == "securepass123"
        assert data.user_source == "local"
        assert data.is_active is True

    def test_username_alphanumeric_valid(self):
        """Username with alphanumeric and underscore is valid."""
        data = AdminUserCreate(
            username="user_123",
            email="u@ex.com",
            password="password123",
        )
        assert data.username == "user_123"

    def test_username_invalid_chars_raises(self):
        """Username with invalid chars raises."""
        with pytest.raises(ValueError, match="alphanumeric"):
            AdminUserCreate(
                username="user-with-dash",
                email="u@ex.com",
                password="password123",
            )

    def test_password_max_length_raises(self):
        """Passwords longer than bcrypt's 72-byte limit are rejected."""
        with pytest.raises(ValueError):
            AdminUserCreate(
                username="adminuser",
                email="admin@example.com",
                password="a" * 73,
            )


class TestAdminUserUpdate:
    """Test AdminUserUpdate schema."""

    def test_partial_update(self):
        """Partial update with optional fields."""
        data = AdminUserUpdate(email="new@example.com")
        assert data.email == "new@example.com"
        assert data.full_name is None
        assert data.is_active is None


class TestUserPasswordUpdate:
    """Test UserPasswordUpdate schema."""

    def test_valid(self):
        """Valid password update."""
        data = UserPasswordUpdate(
            current_password="oldpass",
            new_password="newpass123",
        )
        assert data.current_password == "oldpass"
        assert data.new_password == "newpass123"

    def test_new_password_min_length(self):
        """New password min length enforced."""
        with pytest.raises(ValueError):
            UserPasswordUpdate(
                current_password="old",
                new_password="short",
            )

    def test_new_password_max_length(self):
        """New passwords longer than bcrypt's 72-byte limit are rejected."""
        with pytest.raises(ValueError):
            UserPasswordUpdate(
                current_password="oldpass",
                new_password="a" * 73,
            )

    def test_current_password_may_exceed_bcrypt_limit(self):
        """current_password is a submitted secret, not a new one; do not 422 it."""
        data = UserPasswordUpdate(
            current_password="a" * 80,
            new_password="newpass123",
        )
        assert data.current_password == "a" * 80


class TestAdminUserResponse:
    """Test AdminUserResponse schema."""

    def test_serialize_uuids(self):
        """UUIDs serialize to strings."""
        uid = uuid4()
        acc_id = uuid4()
        data = AdminUserResponse(
            id=uid,
            account_id=acc_id,
            username="user",
            email="u@ex.com",
            email_verified=True,
            full_name=None,
            is_active=True,
            user_source="local",
            oauth_provider=None,
            last_login=None,
            created_at=datetime.now(),
            updated_at=datetime.now(),
            roles=None,
            inherited_roles=None,
        )
        dumped = data.model_dump()
        assert dumped["id"] == str(uid)
        assert dumped["account_id"] == str(acc_id)


class TestUserSummary:
    """Test UserSummary schema."""

    def test_valid(self):
        """Valid user summary."""
        uid = uuid4()
        data = UserSummary(
            id=uid,
            username="user",
            email="u@ex.com",
            full_name="User",
            is_active=True,
        )
        assert data.username == "user"
        dumped = data.model_dump()
        assert dumped["id"] == str(uid)


class TestUserListResponse:
    """Test UserListResponse schema."""

    def test_valid(self):
        """Valid list response."""
        data = UserListResponse(
            users=[],
            total=0,
            skip=0,
            limit=10,
        )
        assert data.total == 0
        assert data.limit == 10
