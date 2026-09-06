import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session
import uuid

from fastapi import FastAPI
from preloop.api.auth.router import router as auth_router
from preloop.models.models.user import User

app = FastAPI()
app.include_router(auth_router, prefix="/auth")
client = TestClient(app)


@pytest.fixture
def db_session_mock():
    from preloop.models.db.session import get_db_session

    db_session = MagicMock(spec=Session)
    mock_execute = MagicMock()
    mock_scalars = MagicMock()
    mock_scalars.first.return_value = None
    mock_execute.scalars.return_value = mock_scalars
    db_session.execute.return_value = mock_execute

    # Mock the query chain for CRUD methods
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None
    mock_query.all.return_value = []
    db_session.query.return_value = mock_query

    app.dependency_overrides[get_db_session] = lambda: db_session
    try:
        yield db_session
    finally:
        app.dependency_overrides.pop(get_db_session, None)


def test_register_user_success(db_session_mock):
    # The registration response carries the new user's identity (id,
    # account_id), so the mocked CRUD layer must hand back rows with ids: a
    # model built against a MagicMock session is never flushed and would
    # otherwise keep id=None.
    account_id = uuid.uuid4()
    user_id = uuid.uuid4()
    with (
        patch("preloop.api.auth.router.complete_new_account_setup_background"),
        patch("preloop.api.auth.router.crud_account") as mock_account,
        patch("preloop.api.auth.router.crud_user") as mock_user_crud,
    ):
        mock_account.create.return_value = MagicMock(id=account_id)
        mock_user_crud.get_by_username.return_value = None
        mock_user_crud.get_by_email.return_value = None
        mock_user_crud.create.return_value = MagicMock(
            id=user_id,
            account_id=account_id,
            username="testuser",
            email="test@example.com",
            full_name="Test User",
            email_verified=False,
        )

        response = client.post(
            "/auth/register",
            json={
                "username": "testuser",
                "email": "test@example.com",
                "password": "password",
                "full_name": "Test User",
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["id"] == str(user_id)
        assert data["account_id"] == str(account_id)
        assert data["username"] == "testuser"
        assert data["email"] == "test@example.com"
        assert data["full_name"] == "Test User"
        assert not data["email_verified"]
        assert data["team_ids"] == []


def test_register_user_username_exists(db_session_mock):
    mock_user = MagicMock(spec=User)
    mock_user.username = "testuser"
    mock_user.email = "test@example.com"

    with patch("preloop.api.auth.router.crud_user") as mock_crud:
        mock_crud.get_by_username.return_value = mock_user

        response = client.post(
            "/auth/register",
            json={
                "username": "testuser",
                "email": "another@example.com",
                "password": "password",
                "full_name": "Test User",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Username already registered"


def test_register_user_email_exists(db_session_mock):
    mock_user = MagicMock(spec=User)
    mock_user.username = "anotheruser"
    mock_user.email = "test@example.com"

    with patch("preloop.api.auth.router.crud_user") as mock_crud:
        mock_crud.get_by_username.return_value = None
        mock_crud.get_by_email.return_value = mock_user

        response = client.post(
            "/auth/register",
            json={
                "username": "testuser",
                "email": "test@example.com",
                "password": "password",
                "full_name": "Test User",
            },
        )
        assert response.status_code == 400
        assert response.json()["detail"] == "Email already registered"


def test_register_disabled(db_session_mock):
    """Test that registration is blocked when REGISTRATION_ENABLED is false."""
    # The computed rule lives in preloop.api.auth.bootstrap (shared with
    # /features), so the effective settings seam is the bootstrap module.
    with patch("preloop.api.auth.bootstrap.settings") as mock_settings:
        # Disable registration; no bootstrap token configured.
        mock_settings.registration_enabled = False
        mock_settings.bootstrap_token = ""

        response = client.post(
            "/auth/register",
            json={
                "username": "testuser",
                "email": "test@example.com",
                "password": "password",
                "full_name": "Test User",
            },
        )

        # Should return 403 Forbidden
        assert response.status_code == 403
        assert "Registration is disabled" in response.json()["detail"]

        # Verify that nothing was written: the rule may probe the users
        # table (zero-users check), but the endpoint must reject before any
        # create/commit happens.
        db_session_mock.add.assert_not_called()
        db_session_mock.commit.assert_not_called()


def test_login_success():
    with patch("preloop.api.auth.router.authenticate_user") as mock_authenticate_user:
        mock_user = MagicMock(spec=User)
        mock_user.id = uuid.uuid4()
        mock_user.username = "testuser"
        mock_user.email = "test@example.com"
        mock_user.hashed_password = "hashed_password"
        mock_authenticate_user.return_value = mock_user

        response = client.post(
            "/auth/token/json",
            json={"username": "testuser", "password": "password"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "access_token" in data
        assert "refresh_token" in data
        assert data["token_type"] == "bearer"


def test_login_failure():
    with patch("preloop.api.auth.router.authenticate_user") as mock_authenticate_user:
        mock_authenticate_user.return_value = None

        response = client.post(
            "/auth/token/json",
            json={"username": "wronguser", "password": "password"},
        )
        assert response.status_code == 401
        assert response.json()["detail"] == "Incorrect username or password"
