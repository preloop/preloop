"""Pytest configuration file for Preloop tests."""

from typing import Generator
import inspect

import pytest
import os
import fastapi
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from preloop.api.app import create_app
from preloop.api.auth import get_current_active_user
from preloop.models.db.session import get_db_session as get_db
from preloop.models.models.user import User
from preloop.models.crud import crud_account, crud_user


async def maybe_await(result):
    """Await the result if it's a coroutine, otherwise return it directly.

    This helper allows tests to work with both sync and async endpoint functions.
    In EE builds, the RBAC decorator wraps sync functions in async wrappers.
    In OSS builds, sync functions remain sync.
    """
    if inspect.iscoroutine(result):
        return await result
    return result


@pytest.fixture(autouse=True)
def mock_openai_client():
    """Mock the OpenAI client to avoid real API calls."""
    with patch("openai.Client") as mock_client:
        mock_embedding = MagicMock()
        mock_embedding.embedding = [0.1] * 1536
        mock_response = MagicMock()
        mock_response.data = [mock_embedding]
        mock_client.return_value.embeddings.create.return_value = mock_response
        yield mock_client


def pytest_configure(config):
    """
    Load environment variables from .env file before tests run.
    """
    # Set TESTING mode to skip external service connections (NATS, MCP, etc.)
    os.environ["TESTING"] = "true"

    # Disable RBAC permission checks during unit tests
    # This ensures tests work consistently regardless of whether the EE RBAC
    # plugin is available (which wraps endpoints in async wrappers)
    os.environ["DISABLE_RBAC"] = "true"
    # Settings is a singleton loaded at import time (often before this hook),
    # so also update the in-memory flag used by auth/plugin code paths.
    from preloop.config import settings

    settings.disable_rbac = True

    # Construct the path to the .env file relative to the conftest.py file
    # Assuming .env is in the project root, and conftest.py is in tests/
    dotenv_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    load_dotenv(dotenv_path)


@pytest.fixture(scope="session")
def db_engine():
    """Create a new database engine for the test session.

    NOTE: This does NOT create tables. The database schema must already exist
    via Alembic migrations (run `alembic upgrade head` before tests).
    Tests use transaction rollbacks for isolation.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise ValueError("DATABASE_URL environment variable not set")

    engine = create_engine(database_url)
    # NOTE: Tables are created via Alembic, not here!
    # This prevents accidental schema recreation during tests.
    yield engine


@pytest.fixture(scope="function")
def db_session(db_engine) -> Generator[Session, None, None]:
    """Create a database session for each test function, and clean up afterwards."""
    from sqlalchemy import event

    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection)

    # Start a nested transaction (savepoint) so that app-level commits are nested
    nested = connection.begin_nested()

    @event.listens_for(session, "after_transaction_end")
    def end_savepoint(session, transaction):
        nonlocal nested
        if not nested.is_active:
            nested = connection.begin_nested()

    # Explicitly flush to ensure roles are visible in this transaction
    session.flush()

    yield session

    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture(scope="function")
def test_user(db_session: Session) -> User:
    """Create and persist a test user for authentication with owner role."""
    from preloop.models.crud import crud_role, crud_user_role

    # Create account (organization) first
    account_data = {
        "organization_name": "Test Organization",
        "is_active": True,
    }
    account = crud_account.create(db_session, obj_in=account_data)

    # Create user linked to the account
    user_data = {
        "account_id": account.id,
        "email": "test@example.com",
        "username": "testuser",
        "full_name": "Test User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    # Assign owner role to test user (has all permissions)
    owner_role = crud_role.get_by_name(db_session, name="owner")
    if owner_role:
        crud_user_role.create(
            db_session,
            obj_in={"user_id": user.id, "role_id": owner_role.id},
        )
        db_session.flush()

    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def test_tracker(db_session: Session, test_user: User):
    """Create a test tracker for testing."""
    from preloop.models.crud import crud_tracker
    from preloop.models.schemas.tracker import TrackerCreate

    tracker_data = TrackerCreate(
        name="Test Tracker",
        tracker_type="github",
        url="https://github.com/test",
        api_key="test_key",
        account_id=test_user.account_id,
        is_active=True,
    )
    tracker = crud_tracker.create(db_session, obj_in=tracker_data.model_dump())
    db_session.flush()
    return tracker


@pytest.fixture(scope="function")
def test_viewer_user(db_session: Session) -> User:
    """Create a test user with viewer role (read-only permissions)."""
    from preloop.models.crud import crud_role, crud_user_role

    account_data = {
        "organization_name": "Test Organization",
        "is_active": True,
    }
    account = crud_account.create(db_session, obj_in=account_data)

    user_data = {
        "account_id": account.id,
        "email": "viewer@example.com",
        "username": "vieweruser",
        "full_name": "Viewer User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    # Assign viewer role
    viewer_role = crud_role.get_by_name(db_session, name="viewer")
    if viewer_role:
        crud_user_role.create(
            db_session,
            obj_in={"user_id": user.id, "role_id": viewer_role.id},
        )
        db_session.flush()

    db_session.refresh(user)
    return user


@pytest.fixture(scope="function")
def test_editor_user(db_session: Session) -> User:
    """Create a test user with editor role (can create and edit)."""
    from preloop.models.crud import crud_role, crud_user_role

    account_data = {
        "organization_name": "Test Organization",
        "is_active": True,
    }
    account = crud_account.create(db_session, obj_in=account_data)

    user_data = {
        "account_id": account.id,
        "email": "editor@example.com",
        "username": "editoruser",
        "full_name": "Editor User",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    # Assign editor role
    editor_role = crud_role.get_by_name(db_session, name="editor")
    if editor_role:
        crud_user_role.create(
            db_session,
            obj_in={"user_id": user.id, "role_id": editor_role.id},
        )
        db_session.flush()

    db_session.refresh(user)
    return user


def assign_role_to_user(db_session: Session, user: User, role_name: str) -> None:
    """Helper function to assign a role to a user.

    Args:
        db_session: Database session
        user: User to assign role to
        role_name: Name of the role (e.g., 'owner', 'admin', 'editor', 'viewer')
    """
    from preloop.models.crud import crud_role, crud_user_role

    role = crud_role.get_by_name(db_session, name=role_name)
    if role:
        # Check if user already has this role
        existing = crud_user_role.get_by_user_and_role(
            db_session, user_id=user.id, role_id=role.id
        )
        if not existing:
            crud_user_role.create(
                db_session,
                obj_in={"user_id": user.id, "role_id": role.id},
            )
            db_session.flush()


@pytest.fixture(scope="function")
def app(db_session: Session, test_user: User) -> Generator[fastapi.FastAPI, None, None]:
    """Create a FastAPI app for testing with dependency overrides."""

    def override_get_current_active_user():
        return test_user

    app = create_app()
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = override_get_current_active_user
    yield app


@pytest.fixture(scope="function")
def client(app: fastapi.FastAPI) -> Generator[TestClient, None, None]:
    """Create a test client for the FastAPI app."""
    with TestClient(app) as client:
        yield client
