"""Tests for last_login update on authentication."""

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone
import uuid

from preloop.api.auth.router import authenticate_user
from preloop.models.models.user import User


@pytest.fixture
def mock_user():
    """Create a mock user."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = "testuser"
    user.email = "test@example.com"
    user.hashed_password = "$2b$12$test_hashed_password"
    user.is_active = True
    user.last_login = None  # Never logged in before
    return user


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    db = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    db.close = MagicMock()
    return db


@pytest.mark.asyncio
async def test_authenticate_user_updates_last_login(mock_user, mock_db_session):
    """Test that authenticating a user updates their last_login timestamp."""
    with (
        patch("preloop.api.auth.router.get_db_session") as mock_get_db,
        patch("preloop.api.auth.router.crud_user") as mock_crud_user,
        patch("preloop.api.auth.router.crud_audit_log"),
        patch("preloop.api.auth.router.verify_password") as mock_verify_password,
    ):
        # Setup mocks
        mock_get_db.return_value = iter([mock_db_session])
        mock_crud_user.get_by_username.return_value = mock_user
        mock_verify_password.return_value = True

        # Authenticate user
        result = await authenticate_user("testuser", "password123", db=mock_db_session)

        # Verify user was returned
        assert result == mock_user

        # Verify last_login was updated
        assert mock_user.last_login is not None
        assert isinstance(mock_user.last_login, datetime)

        # Verify it was set to a recent time (within last 5 seconds)
        now = datetime.now(timezone.utc)
        time_diff = (now - mock_user.last_login).total_seconds()
        assert time_diff < 5, (
            f"last_login should be recent, but was {time_diff} seconds ago"
        )

        # Verify database was committed
        mock_db_session.commit.assert_called_once()
        mock_db_session.refresh.assert_called_once_with(mock_user)


@pytest.mark.asyncio
async def test_authenticate_user_writes_login_audit_event(mock_user, mock_db_session):
    """Test that a successful login writes a 'user.login' audit event.

    This audit row is what makes the 7-day-return launch metric queryable,
    since last_login is overwritten in place and keeps no history.
    """
    with (
        patch("preloop.api.auth.router.get_db_session") as mock_get_db,
        patch("preloop.api.auth.router.crud_user") as mock_crud_user,
        patch("preloop.api.auth.router.crud_audit_log") as mock_crud_audit_log,
        patch("preloop.api.auth.router.verify_password") as mock_verify_password,
    ):
        mock_get_db.return_value = iter([mock_db_session])
        mock_crud_user.get_by_username.return_value = mock_user
        mock_verify_password.return_value = True

        result = await authenticate_user("testuser", "password123", db=mock_db_session)

        assert result == mock_user

        # The login audit event was written via the shared audit CRUD helper.
        mock_crud_audit_log.log_action.assert_called_once()
        _, kwargs = mock_crud_audit_log.log_action.call_args
        assert kwargs["action"] == "user.login"
        assert kwargs["status"] == "success"
        assert kwargs["user_id"] == mock_user.id
        assert kwargs["account_id"] == mock_user.account_id


@pytest.mark.asyncio
async def test_authenticate_user_does_not_update_last_login_on_wrong_password(
    mock_user, mock_db_session
):
    """Test that last_login is not updated when password is incorrect."""
    original_last_login = mock_user.last_login

    with (
        patch("preloop.api.auth.router.get_db_session") as mock_get_db,
        patch("preloop.api.auth.router.crud_user") as mock_crud_user,
        patch("preloop.api.auth.router.verify_password") as mock_verify_password,
    ):
        # Setup mocks
        mock_get_db.return_value = iter([mock_db_session])
        mock_crud_user.get_by_username.return_value = mock_user
        mock_verify_password.return_value = False  # Wrong password

        # Authenticate user
        result = await authenticate_user(
            "testuser", "wrong_password", db=mock_db_session
        )

        # Verify None was returned (authentication failed)
        assert result is None

        # Verify last_login was NOT updated
        assert mock_user.last_login == original_last_login

        # Verify database was NOT committed
        mock_db_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_authenticate_user_does_not_update_last_login_when_inactive(
    mock_user, mock_db_session
):
    """Test that last_login is not updated when user is inactive."""
    original_last_login = mock_user.last_login
    mock_user.is_active = False  # User is inactive

    with (
        patch("preloop.api.auth.router.get_db_session") as mock_get_db,
        patch("preloop.api.auth.router.crud_user") as mock_crud_user,
        patch("preloop.api.auth.router.verify_password") as mock_verify_password,
    ):
        # Setup mocks
        mock_get_db.return_value = iter([mock_db_session])
        mock_crud_user.get_by_username.return_value = mock_user
        mock_verify_password.return_value = True

        # Authenticate user
        result = await authenticate_user("testuser", "password123", db=mock_db_session)

        # Verify None was returned (authentication failed)
        assert result is None

        # Verify last_login was NOT updated
        assert mock_user.last_login == original_last_login

        # Verify database was NOT committed
        mock_db_session.commit.assert_not_called()


@pytest.mark.asyncio
async def test_authenticate_user_updates_last_login_on_subsequent_logins(
    mock_user, mock_db_session
):
    """Test that last_login is updated on each login."""
    # Set a previous last_login
    previous_login = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    mock_user.last_login = previous_login

    with (
        patch("preloop.api.auth.router.get_db_session") as mock_get_db,
        patch("preloop.api.auth.router.crud_user") as mock_crud_user,
        patch("preloop.api.auth.router.crud_audit_log"),
        patch("preloop.api.auth.router.verify_password") as mock_verify_password,
    ):
        # Setup mocks
        mock_get_db.return_value = iter([mock_db_session])
        mock_crud_user.get_by_username.return_value = mock_user
        mock_verify_password.return_value = True

        # Authenticate user
        result = await authenticate_user("testuser", "password123", db=mock_db_session)

        # Verify user was returned
        assert result == mock_user

        # Verify last_login was updated to a more recent time
        assert mock_user.last_login > previous_login

        # Verify it was set to current time
        now = datetime.now(timezone.utc)
        time_diff = (now - mock_user.last_login).total_seconds()
        assert time_diff < 5, "last_login should be updated to current time"

        # Verify database was committed
        mock_db_session.commit.assert_called_once()
