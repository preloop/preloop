"""Tests for the database session module."""

import os
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

import preloop.models.db.session as session_module


EXPECTED_POOL_KWARGS = {
    "pool_size": 20,
    "max_overflow": 40,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
    "pool_timeout": 30,
    "pool_use_lifo": True,
}


@pytest.fixture(autouse=True)  # Apply mock engine automatically to relevant tests
def mock_engine_dependencies(monkeypatch):
    """Mock dependencies used by session_module.get_engine."""
    # Reset global engine cache before each test
    session_module._engine = None
    session_module._session_factory = None

    mock_create = MagicMock()
    mock_engine_instance = MagicMock()
    mock_conn = MagicMock()
    # Simulate the context manager for connect()
    mock_engine_instance.connect.return_value.__enter__.return_value = mock_conn
    mock_create.return_value = mock_engine_instance

    mock_check_pgvector = MagicMock(return_value=True)

    monkeypatch.setattr("preloop.models.db.session.create_engine", mock_create)
    monkeypatch.setattr(
        "preloop.models.db.session.check_pgvector_extension", mock_check_pgvector
    )
    # Prevent actual installation attempt if check returns False
    monkeypatch.setattr(
        "preloop.models.db.session.install_pgvector_extension", MagicMock()
    )

    # Return the mocks for potential use in tests
    return {
        "create_engine": mock_create,
        "engine_instance": mock_engine_instance,
        "connection": mock_conn,
        "check_pgvector": mock_check_pgvector,
    }


# Test cases now implicitly use the mocked dependencies via autouse fixture
def test_get_engine_custom_url(mock_engine_dependencies):
    """Test get_engine with a custom URL."""
    custom_url = "postgresql://user:pass@custom-host/db"
    engine = session_module.get_engine(custom_url)

    # Verify create_engine was called with the custom URL and connection pool parameters
    mock_engine_dependencies["create_engine"].assert_called_once_with(
        custom_url,
        **EXPECTED_POOL_KWARGS,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=30000",
        },
        echo=False,
    )

    # Verify connection test happened (execute called on connection mock)
    mock_conn = mock_engine_dependencies["connection"]
    assert mock_conn.execute.call_count >= 1  # Ensure execute was called
    # Check the arguments of the first call specifically
    first_call_args = mock_conn.execute.call_args_list[0].args
    assert "SELECT 1" in str(first_call_args[0])

    # Verify check_pgvector_extension was called (implies a second execute call)
    mock_engine_dependencies["check_pgvector"].assert_called_once_with(
        mock_engine_dependencies["engine_instance"]
    )

    # Verify the engine instance was returned
    assert engine == mock_engine_dependencies["engine_instance"]


def test_get_engine_from_env(mock_engine_dependencies):
    """Test get_engine uses DATABASE_URL environment variable."""
    env_url = "postgresql://user:pass@env-host/db"

    with patch.dict(os.environ, {"DATABASE_URL": env_url}):
        engine = session_module.get_engine()

        # Verify create_engine was called with the URL from environment and pool parameters
        mock_engine_dependencies["create_engine"].assert_called_once_with(
            env_url,
            **EXPECTED_POOL_KWARGS,
            connect_args={
                "connect_timeout": 10,
                "options": "-c statement_timeout=30000",
            },
            echo=False,
        )

        # Verify connection test happened
        mock_conn = mock_engine_dependencies["connection"]
        assert mock_conn.execute.call_count >= 1  # Ensure execute was called
        first_call_args = mock_conn.execute.call_args_list[0].args
        assert "SELECT 1" in str(first_call_args[0])

        # Verify check_pgvector_extension was called (implies a second execute call)
        mock_engine_dependencies["check_pgvector"].assert_called_once_with(
            mock_engine_dependencies["engine_instance"]
        )

        # Verify the engine instance was returned
        assert engine == mock_engine_dependencies["engine_instance"]


# Add test for default URL case (was missing)
def test_get_engine_default(mock_engine_dependencies):
    """Test get_engine with default URL from env."""
    # Assume DATABASE_URL is set in the environment for default case
    default_url = "postgresql://default:pass@default-host/db"
    with patch.dict(os.environ, {"DATABASE_URL": default_url}):
        engine = session_module.get_engine()  # Call without args

        # Verify create_engine was called with the default URL and pool parameters
        mock_engine_dependencies["create_engine"].assert_called_once_with(
            default_url,
            **EXPECTED_POOL_KWARGS,
            connect_args={
                "connect_timeout": 10,
                "options": "-c statement_timeout=30000",
            },
            echo=False,
        )

        # Verify connection test happened
        mock_conn = mock_engine_dependencies["connection"]
        assert mock_conn.execute.call_count >= 1  # Ensure execute was called
        first_call_args = mock_conn.execute.call_args_list[0].args
        assert "SELECT 1" in str(first_call_args[0])

        # Verify check_pgvector_extension was called (implies a second execute call)
        mock_engine_dependencies["check_pgvector"].assert_called_once_with(
            mock_engine_dependencies["engine_instance"]
        )
        assert engine == mock_engine_dependencies["engine_instance"]


# Add test for error case (missing URL)
def test_get_engine_error_fallback():
    """Test get_engine raises exception if DATABASE_URL is not set."""
    # Reset global engine cache to ensure test starts fresh
    session_module._engine = None
    session_module._session_factory = None

    # Ensure DATABASE_URL is not set
    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(Exception, match="DATABASE_URL not in env"):
            session_module.get_engine()


def test_get_session_factory(mock_engine_dependencies):
    """Test get_session_factory function."""
    # get_engine is mocked by mock_engine_dependencies fixture via autouse
    factory = session_module.get_session_factory()

    # Verify the factory has the correct bind (the mocked engine instance)
    assert factory.kw["bind"] == mock_engine_dependencies["engine_instance"]


def test_get_engine_uses_database_pool_env(mock_engine_dependencies):
    """Database pool settings can be tuned from environment variables."""
    env_url = "postgresql://user:pass@env-host/db"
    with patch.dict(
        os.environ,
        {
            "DATABASE_URL": env_url,
            "DATABASE_POOL_SIZE": "3",
            "DATABASE_MAX_OVERFLOW": "4",
            "DATABASE_POOL_RECYCLE": "300",
            "DATABASE_POOL_TIMEOUT": "7",
        },
    ):
        session_module.get_engine()

    mock_engine_dependencies["create_engine"].assert_called_once_with(
        env_url,
        pool_size=3,
        max_overflow=4,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_timeout=7,
        pool_use_lifo=True,
        connect_args={
            "connect_timeout": 10,
            "options": "-c statement_timeout=30000",
        },
        echo=False,
    )


def test_get_engine_keeps_pool_reset_on_return_default(mock_engine_dependencies):
    """Pooled connections should be rolled back on return before reuse."""
    env_url = "postgresql://user:pass@env-host/db"
    with patch.dict(os.environ, {"DATABASE_URL": env_url}):
        session_module.get_engine()

    _, kwargs = mock_engine_dependencies["create_engine"].call_args
    assert "pool_reset_on_return" not in kwargs


def test_get_db_session():
    """Test get_db_session generator function."""
    mock_session = MagicMock(spec=Session)
    mock_factory = MagicMock(return_value=mock_session)

    with patch(
        "preloop.models.db.session.get_session_factory", return_value=mock_factory
    ):
        # Get the generator
        session_gen = session_module.get_db_session()

        # Get the session from the generator
        session = next(session_gen)

        # Verify the session is the one from our mock
        assert session == mock_session

        # Verify calling close on the generator causes session.close()
        try:
            next(session_gen)
        except StopIteration:
            pass

        mock_session.in_transaction.assert_called_once()
        mock_session.close.assert_called_once()


def test_get_db_session_invalidates_when_rollback_fails():
    """Dead pooled connections are invalidated instead of surfacing close errors."""
    mock_session = MagicMock(spec=Session)
    mock_session.in_transaction.return_value = True
    mock_session.rollback.side_effect = SQLAlchemyError(
        "SSL connection has been closed unexpectedly"
    )
    mock_factory = MagicMock(return_value=mock_session)

    with patch(
        "preloop.models.db.session.get_session_factory", return_value=mock_factory
    ):
        session_gen = session_module.get_db_session()
        assert next(session_gen) == mock_session

        with pytest.raises(StopIteration):
            next(session_gen)

        mock_session.rollback.assert_called_once()
        mock_session.invalidate.assert_called_once()
