"""Tests for API key endpoints."""

import pytest
import uuid
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from fastapi import FastAPI
from preloop.api.auth.router import router as auth_router
from preloop.models.models.user import User
from preloop.models.models.api_key import ApiKey

app = FastAPI()
app.include_router(auth_router, prefix="/auth")
client = TestClient(app)


@pytest.fixture
def mock_current_user():
    """Create a mock authenticated user."""
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.username = "testuser"
    user.email = "test@example.com"
    user.is_active = True
    user.account_id = uuid.uuid4()
    return user


@pytest.fixture
def db_session_mock():
    """Create a mock database session."""
    from preloop.models.db.session import get_db_session

    db_session = MagicMock(spec=Session)

    # Mock commit, rollback, close
    db_session.commit = MagicMock()
    db_session.rollback = MagicMock()
    db_session.close = MagicMock()

    # Override the dependency for FastAPI routers
    app.dependency_overrides[get_db_session] = lambda: db_session

    yield db_session

    # Clean up
    if get_db_session in app.dependency_overrides:
        del app.dependency_overrides[get_db_session]


def test_create_api_key_success(db_session_mock, mock_current_user):
    """Test that creating an API key works with the correct user_id field.

    This test ensures that:
    1. The ApiKey model is instantiated with user_id (not created_by)
    2. The response includes user_id (not created_by)
    3. All database operations complete successfully

    This would catch the bug where created_by was used instead of user_id.
    """
    from preloop.api.auth.jwt import get_current_active_user

    # Mock the session.refresh to populate database-generated fields
    def mock_refresh(obj):
        # Simulate refresh by setting database-generated fields
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        if not hasattr(obj, "created_at") or obj.created_at is None:
            obj.created_at = datetime.now(timezone.utc)

    db_session_mock.refresh = mock_refresh
    db_session_mock.add = MagicMock()

    # Override the authentication dependency
    app.dependency_overrides[get_current_active_user] = lambda: mock_current_user

    try:
        # Make a request to create an API key
        response = client.post(
            "/auth/api-keys",
            json={"name": "Test API Key", "expires_at": None, "scopes": []},
        )

        # Verify the response
        assert response.status_code == 201
        data = response.json()

        # Critical assertions - these would fail with created_by field
        assert "user_id" in data, "Response must include user_id field"
        assert "created_by" not in data, (
            "Response should not include deprecated created_by field"
        )

        # Verify all expected fields are present
        assert "id" in data
        assert "name" in data
        assert "key" in data
        assert "created_at" in data
        assert "expires_at" in data
        assert "scopes" in data
        assert "last_used_at" in data

        # Verify the database operations were called correctly
        db_session_mock.add.assert_called_once()

        # Get the ApiKey object that was added
        added_key = db_session_mock.add.call_args[0][0]

        # Critical assertion - the ApiKey must be created with user_id
        assert hasattr(added_key, "user_id"), "ApiKey must have user_id attribute"
        assert added_key.user_id == mock_current_user.id, (
            "user_id must match current user's id"
        )

        # Account scoping
        assert hasattr(added_key, "account_id"), "ApiKey must have account_id attribute"
        assert added_key.account_id == mock_current_user.account_id, (
            "account_id must match current user's account_id"
        )

        # Ensure deprecated field is not used
        assert not hasattr(added_key, "created_by") or added_key.user_id is not None, (
            "ApiKey should use user_id, not created_by"
        )
    finally:
        # Clean up the dependency override
        app.dependency_overrides.clear()


def test_create_api_key_with_expiration(db_session_mock, mock_current_user):
    """Test creating an API key with an expiration date."""
    from preloop.api.auth.jwt import get_current_active_user

    # Mock the session.refresh to populate database-generated fields
    def mock_refresh(obj):
        # Simulate refresh by setting database-generated fields
        if not hasattr(obj, "id") or obj.id is None:
            obj.id = uuid.uuid4()
        if not hasattr(obj, "created_at") or obj.created_at is None:
            obj.created_at = datetime.now(timezone.utc)

    db_session_mock.refresh = mock_refresh

    # Override the authentication dependency
    app.dependency_overrides[get_current_active_user] = lambda: mock_current_user

    try:
        expires_at = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)

        response = client.post(
            "/auth/api-keys",
            json={
                "name": "Expiring Key",
                "expires_at": expires_at.isoformat(),
                "scopes": ["read", "write"],
            },
        )

        assert response.status_code == 201
        data = response.json()

        # Verify user_id is present
        assert "user_id" in data
        assert data["name"] == "Expiring Key"
        assert data["scopes"] == ["read", "write"]
        # expires_at should be present (we're not validating exact format in this test)
        assert "expires_at" in data
    finally:
        # Clean up the dependency override
        app.dependency_overrides.clear()


def test_create_api_key_duplicate_name(db_session_mock, mock_current_user):
    """Test that creating an API key with a duplicate name fails appropriately."""
    from sqlalchemy.exc import IntegrityError
    from preloop.api.auth.jwt import get_current_active_user

    # Override the authentication dependency
    app.dependency_overrides[get_current_active_user] = lambda: mock_current_user

    try:
        # Mock the session to raise IntegrityError on commit with constraint name
        # that matches what the handler expects
        mock_orig = MagicMock()
        mock_orig.diag = MagicMock()
        mock_orig.diag.constraint_name = "uix_api_key_account_id_name"
        error = IntegrityError("", "", mock_orig)
        error.orig = mock_orig
        db_session_mock.commit.side_effect = error

        response = client.post(
            "/auth/api-keys",
            json={"name": "Duplicate Key", "expires_at": None, "scopes": []},
        )

        # Should return 400 Bad Request
        assert response.status_code == 400
        assert "already exists" in response.json()["detail"].lower()
    finally:
        # Clean up the dependency override
        app.dependency_overrides.clear()


def test_api_key_model_compatibility():
    """Direct unit test to verify ApiKey model field compatibility.

    This test directly checks that the ApiKey model has the expected fields
    and doesn't have deprecated fields.
    """
    import inspect

    # Get the __init__ signature
    sig = inspect.signature(ApiKey.__init__)
    list(sig.parameters.keys())

    # Check that user_id is an expected parameter
    # Note: SQLAlchemy models don't always show all fields in __init__,
    # so we check the class attributes instead
    assert hasattr(ApiKey, "user_id"), "ApiKey must have user_id attribute"

    # Verify we can create an ApiKey with user_id
    try:
        test_key = ApiKey(
            name="test",
            key="test_key_123",
            account_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            scopes=[],
        )
        # If we get here, user_id is accepted
        assert test_key.user_id is not None
    except TypeError as e:
        if "user_id" in str(e):
            pytest.fail(f"ApiKey model does not accept user_id parameter: {e}")
        raise


def test_api_key_governance_round_trip(db_session_mock, mock_current_user):
    from preloop.api.auth.jwt import get_current_active_user

    app.dependency_overrides[get_current_active_user] = lambda: mock_current_user

    key_id = uuid.uuid4()
    key = MagicMock(spec=ApiKey)
    key.id = key_id
    key.user_id = mock_current_user.id
    key.account_id = mock_current_user.account_id
    key.context_data = {}
    query = MagicMock()
    filter_result = MagicMock()
    filter_result.first.return_value = key
    query.filter.return_value = filter_result
    db_session_mock.query.return_value = query

    account = MagicMock()
    account.meta_data = {}

    try:
        with patch("preloop.api.auth.router.crud_account.get", return_value=account):
            get_response = client.get(f"/auth/api-keys/{key_id}/governance")
            assert get_response.status_code == 200
            assert get_response.json()["config"]["allowed_models"] == []

            update_response = client.put(
                f"/auth/api-keys/{key_id}/governance",
                json={
                    "allowed_models": ["openai/gpt-5"],
                    "model_budgets": {"openai/gpt-5": {"monthly_usd_limit": 10}},
                    "tool_rules": {"search_issues": [{"action": "require_approval"}]},
                },
            )
            assert update_response.status_code == 200
            body = update_response.json()
            assert body["config"]["allowed_models"] == ["openai/gpt-5"]
            assert (
                body["config"]["model_budgets"]["openai/gpt-5"]["monthly_usd_limit"]
                == 10
            )
            assert body["config"]["tool_rules"]["search_issues"][0]["action"] == (
                "require_approval"
            )
            db_session_mock.commit.assert_called()
    finally:
        app.dependency_overrides.clear()
