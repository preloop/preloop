"""Test fixtures for preloop.models."""

import os  # Added import

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


@pytest.fixture(scope="session")
def db_engine():
    """Create a test database engine. Uses DATABASE_URL if set, otherwise raises error."""
    # Read database URL from environment variable
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        raise ValueError(
            "DATABASE_URL environment variable not set. "
            "This is required for tests needing PostgreSQL with PGVector."
        )

    # Use PostgreSQL for tests requiring it (like embedding tests)
    # Assumes the database specified by DATABASE_URL exists and has PGVector enabled.
    engine = create_engine(db_url)

    with engine.connect() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.commit()

    # NOTE: We do NOT drop and recreate tables here!
    # This would affect the production database if DATABASE_URL is set incorrectly.
    # Tests use transaction rollbacks for isolation, so existing tables are fine.
    # If you need to reset the schema, do it manually outside of tests.

    yield engine

    # NOTE: DO NOT drop tables in teardown!
    # Tests use transactions that are rolled back, so no cleanup needed.
    # Dropping tables would affect the actual database if DATABASE_URL is set incorrectly.


@pytest.fixture(scope="function")
def db_session(db_engine):
    """Create a new database session for a test."""
    # Create a new session for each test
    connection = db_engine.connect()
    transaction = connection.begin()

    # Create session factory bound to the connection
    session_factory = sessionmaker(autocommit=False, autoflush=False, bind=connection)
    session = session_factory()

    yield session

    # Roll back the transaction after the test completes
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def create_account(db_session):
    """Create a test account (organization).

    Note: In the multi-user architecture, Account represents an organization.
    To create a user, use create_user() fixture instead.
    """
    import uuid

    from preloop.models.crud import crud_account

    def _create_account(organization_name=None, **kwargs):
        # Generate unique organization name if not provided
        unique_id = str(uuid.uuid4())[:8]
        organization_name = organization_name or f"Test Organization {unique_id}"

        account_data = {
            "organization_name": organization_name,
            "is_active": True,
            **kwargs,
        }
        return crud_account.create(db_session, obj_in=account_data)

    return _create_account


@pytest.fixture
def create_user(db_session, create_account):
    """Create a test user within an account."""
    import uuid

    from preloop.models.crud import crud_user

    def _create_user(username=None, email=None, account=None, **kwargs):
        # Create account if not provided
        if account is None:
            account = create_account()

        # Generate unique username and email if not provided
        unique_id = str(uuid.uuid4())[:8]
        username = username or f"testuser_{unique_id}"
        email = email or f"test_{unique_id}@example.com"

        user_data = {
            "username": username,
            "email": email,
            "account_id": account.id,
            "hashed_password": "hashed_password",
            "is_active": True,
            **kwargs,
        }
        return crud_user.create(db_session, obj_in=user_data)

    return _create_user


@pytest.fixture
def create_tracker(db_session, create_account):
    """Create a test tracker."""
    from preloop.models.crud import crud_tracker

    def _create_tracker(
        account=None, tracker_type="github", name="Test Tracker", **kwargs
    ):
        if account is None:
            account = create_account()

        tracker_data = {
            "name": name,
            "tracker_type": tracker_type,
            "account_id": account.id,
            "api_key": "test_api_key",
            "url": "https://example.com",
            "is_active": True,
            "jira_webhook_id": None,
            "jira_webhook_secret": None,
            **kwargs,
        }
        # Ensure that if tracker_type is jira, url is provided.
        if tracker_data["tracker_type"] == "jira" and not tracker_data.get("url"):
            tracker_data["url"] = "https://jira.example.com"

        return crud_tracker.create(db_session, obj_in=tracker_data)

    return _create_tracker


@pytest.fixture
def create_organization(db_session, create_tracker):
    """Create a test organization."""
    import uuid

    from preloop.models.crud import crud_organization

    def _create_organization(tracker=None, name=None, identifier=None, **kwargs):
        # Generate unique values if not provided
        unique_id = str(uuid.uuid4())[:8]
        name = name or f"Test Organization {unique_id}"
        identifier = identifier or f"test-org-{unique_id}"

        if tracker is None:
            tracker = create_tracker()

        org_data = {
            "name": name,
            "identifier": identifier,
            "tracker_id": tracker.id,
            "is_active": True,
            **kwargs,
        }
        return crud_organization.create(db_session, obj_in=org_data)

    return _create_organization


@pytest.fixture
def create_project(db_session, create_organization):
    """Create a test project."""
    from preloop.models.crud import crud_project

    def _create_project(
        name="Test Project", identifier="test-project", slug=None, **kwargs
    ):
        # Create organization first if not provided
        organization = kwargs.pop("organization", create_organization())

        project_data = {
            "name": name,
            "identifier": identifier,
            "slug": slug,  # Add slug field
            "organization_id": organization.id,
            "is_active": True,
            **kwargs,
        }
        return crud_project.create(db_session, obj_in=project_data)

    return _create_project


@pytest.fixture
def create_issue(db_session, create_project, create_tracker):
    """Create a test issue."""
    import uuid  # Import uuid
    from preloop.models.crud import crud_issue

    def _create_issue(title="Test Issue", description="Test description", **kwargs):
        # Create project and tracker first if not provided
        project = kwargs.pop("project", create_project())
        tracker = kwargs.pop("tracker", create_tracker())

        # Generate default unique values if not provided
        default_key = f"TEST-KEY-{uuid.uuid4().hex[:6]}"
        default_external_id = f"EXT-{uuid.uuid4().hex[:8]}"

        issue_data = {
            "title": title,
            "description": description,
            "status": "open",
            "issue_type": "task",
            "project_id": project.id,
            "tracker_id": tracker.id,
            "key": kwargs.pop("key", default_key),  # Use default or kwargs
            "external_id": kwargs.pop(
                "external_id", default_external_id
            ),  # Use default or kwargs
            **kwargs,  # Include remaining kwargs
        }
        return crud_issue.create(db_session, obj_in=issue_data)

    return _create_issue


@pytest.fixture
def create_comment(db_session, create_issue, create_user):
    """Create a test comment.
    Handles 'issue_id' or 'issue' object passed in kwargs,
    or creates a default issue.
    Handles 'author' (as username string or User object) passed in kwargs,
    or creates a default author user.
    """
    from preloop.models.crud import crud_comment
    from preloop.models.models import User, Issue

    def _create_comment(body="Test comment body", type="issue", **kwargs):
        current_issue: "Issue"

        if "issue_id" in kwargs:
            issue_id = str(kwargs.pop("issue_id"))
            current_issue = db_session.query(Issue).filter(Issue.id == issue_id).one()
        elif "issue" in kwargs:
            current_issue = kwargs.pop("issue")
        else:
            current_issue = create_issue()

        author_obj = None
        if "author" in kwargs:
            author_arg = kwargs.pop("author")
            if isinstance(author_arg, str):  # Username provided
                author_obj = (
                    db_session.query(User).filter(User.username == author_arg).first()
                )
                if not author_obj:
                    raise ValueError(
                        f"Test setup error: author with username '{author_arg}' not found."
                    )
            elif isinstance(author_arg, User):  # User object provided
                author_obj = author_arg

        if not author_obj:
            author_obj = create_user()

        comment_data = {
            "body": body,
            "type": type,
            "issue_id": str(current_issue.id),
            "tracker_id": str(current_issue.tracker_id),
            "author": author_obj.username,
            "external_id": kwargs.pop("external_id", "default-external-id"),
            **kwargs,
        }

        return crud_comment.create(db_session, obj_in=comment_data)

    return _create_comment


@pytest.fixture
def create_embedding_model(db_session):
    """Create a test embedding model."""
    from preloop.models.crud import crud_embedding_model

    def _create_embedding_model(
        name="test-embedding",
        provider="openai",
        version="text-embedding-ada-002",
        dimensions=1536,
        **kwargs,
    ):
        model_data = {
            "name": name,
            "provider": provider,
            "version": version,
            "dimensions": dimensions,
            "is_active": True,
            "meta_data": {},
            **kwargs,
        }
        return crud_embedding_model.create(db_session, obj_in=model_data)

    return _create_embedding_model


@pytest.fixture(autouse=True)
def isolate_system_wide_ai_models(db_session):
    """Remove system-wide (account-less) AI models for the duration of a test.

    ``get_default_active_model`` deliberately falls back to system-wide models
    (``account_id IS NULL``) so that a self-hosted install seeded by
    ``scripts/init_db.py`` resolves a default without per-account setup. That
    fallback is correct product behaviour, but it makes any test that asserts
    "this account resolves to X" depend on whether the database happens to have
    been seeded.

    CI seeds one: the workflow sets ``OPENAI_API_KEY=mock_key`` and runs
    ``init_db.py --force``, which creates a global default named
    ``gpt-5.3-codex``. A local database usually has none, so these tests passed
    locally and failed in CI. Clearing the global scope here makes the
    assertions mean what they say in both environments.

    The deletion happens inside the test's transaction, which ``db_session``
    rolls back, so a seeded database is left untouched.
    """
    from preloop.models.crud import crud_ai_model

    for model_kind in ("llm", "stt", "tts"):
        while True:
            system_model = crud_ai_model.get_default_active_model(
                db=db_session, account_id=None, model_kind=model_kind
            )
            if system_model is None:
                break
            crud_ai_model.remove(db_session, id=system_model.id)

    yield
