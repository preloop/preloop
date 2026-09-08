"""Tests for invitation schemas."""

from datetime import datetime
from uuid import uuid4

import pytest

from preloop.schemas.invitation import (
    InvitationAccept,
    InvitationCreate,
    InvitationListResponse,
    InvitationPublicInfo,
    InvitationResponse,
)


class TestInvitationCreate:
    """Test InvitationCreate schema."""

    def test_valid_minimal(self):
        """Minimal valid invitation create."""
        data = InvitationCreate(email="user@example.com")
        assert data.email == "user@example.com"
        assert data.role_ids == []
        assert data.team_ids == []

    def test_with_role_and_team_ids(self):
        """Invitation create with role and team IDs."""
        role_id = uuid4()
        team_id = uuid4()
        data = InvitationCreate(
            email="user@example.com",
            role_ids=[role_id],
            team_ids=[team_id],
        )
        assert data.role_ids == [role_id]
        assert data.team_ids == [team_id]

    def test_invalid_email_raises(self):
        """Invalid email raises validation error."""
        with pytest.raises(ValueError):
            InvitationCreate(email="not-an-email")


class TestInvitationAccept:
    """Test InvitationAccept schema."""

    def test_valid(self):
        """Valid accept schema."""
        data = InvitationAccept(
            token="abc123",
            username="newuser",
            password="securepass123",
        )
        assert data.token == "abc123"
        assert data.username == "newuser"
        assert data.password == "securepass123"
        assert data.full_name is None

    def test_with_full_name(self):
        """Accept with optional full_name."""
        data = InvitationAccept(
            token="t",
            username="user",
            password="password123",
            full_name="John Doe",
        )
        assert data.full_name == "John Doe"

    def test_username_too_short_raises(self):
        """Username min length enforced."""
        with pytest.raises(ValueError):
            InvitationAccept(
                token="t",
                username="ab",
                password="password123",
            )

    def test_password_too_short_raises(self):
        """Password min length enforced."""
        with pytest.raises(ValueError):
            InvitationAccept(
                token="t",
                username="user",
                password="short",
            )

    def test_password_too_long_raises(self):
        """Passwords longer than bcrypt's 72-byte limit are rejected."""
        with pytest.raises(ValueError):
            InvitationAccept(
                token="t",
                username="user",
                password="a" * 73,
            )


class TestInvitationResponse:
    """Test InvitationResponse schema."""

    def test_serialize_uuids(self):
        """UUIDs serialize to strings in model_dump."""
        inv_id = uuid4()
        acc_id = uuid4()
        data = InvitationResponse(
            id=inv_id,
            account_id=acc_id,
            email="u@ex.com",
            token="t",
            status="pending",
            role_ids=None,
            team_ids=None,
            invited_by=None,
            created_at=datetime.now(),
            accepted_at=None,
            expires_at=datetime.now(),
        )
        dumped = data.model_dump()
        assert dumped["id"] == str(inv_id)
        assert dumped["account_id"] == str(acc_id)


class TestInvitationListResponse:
    """Test InvitationListResponse schema."""

    def test_valid(self):
        """Valid list response."""
        data = InvitationListResponse(
            invitations=[],
            total=0,
            skip=0,
            limit=10,
        )
        assert data.total == 0
        assert data.skip == 0
        assert data.limit == 10


class TestInvitationPublicInfo:
    """Test InvitationPublicInfo schema."""

    def test_valid(self):
        """Valid public info."""
        data = InvitationPublicInfo(
            email="u@ex.com",
            organization_name="Org",
            expires_at=datetime.now(),
            is_valid=True,
        )
        assert data.email == "u@ex.com"
        assert data.is_valid is True
        assert data.error_message is None
        assert data.role_names == []
        assert data.team_names == []
