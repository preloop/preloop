"""Database session management."""

import os
from typing import AsyncGenerator, Generator, Optional
from contextlib import asynccontextmanager

from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker

from .vector_types import check_pgvector_extension, install_pgvector_extension

# Global engine instance to be reused across the application
_engine = None
_session_factory = None
_async_engine = None
_async_session_factory = None
_health_engine = None


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning(f"Invalid {name}={value!r}; using default {default}")
        return default


def _database_pool_kwargs() -> dict:
    """Return shared SQLAlchemy pool settings for sync and async engines."""
    return {
        "pool_size": _env_int("DATABASE_POOL_SIZE", 20),
        "max_overflow": _env_int("DATABASE_MAX_OVERFLOW", 40),
        "pool_pre_ping": True,
        # Keep pooled connections younger than typical proxy/LB idle timeouts.
        "pool_recycle": _env_int("DATABASE_POOL_RECYCLE", 1800),
        "pool_timeout": _env_int("DATABASE_POOL_TIMEOUT", 30),
        # Prefer recently used connections so older idle connections are recycled
        # instead of being kept alive indefinitely in FIFO order.
        "pool_use_lifo": True,
    }


def _safe_close_db_session(db: Session) -> None:
    """Rollback and close a sync session, invalidating dead connections quietly."""
    try:
        if db.in_transaction():
            db.rollback()
    except SQLAlchemyError as exc:
        logger.warning(f"Database session rollback failed during close: {exc}")
        db.invalidate()
        return
    finally:
        try:
            db.close()
        except SQLAlchemyError as exc:
            logger.warning(f"Database session close failed: {exc}")
            db.invalidate()


async def _safe_close_async_db_session(session: AsyncSession) -> None:
    """Rollback and close an async session, invalidating dead connections quietly."""
    try:
        if session.in_transaction():
            await session.rollback()
    except SQLAlchemyError as exc:
        logger.warning(f"Async database session rollback failed during close: {exc}")
        await session.invalidate()
        return
    finally:
        try:
            await session.close()
        except SQLAlchemyError as exc:
            logger.warning(f"Async database session close failed: {exc}")
            await session.invalidate()


def get_engine(database_url: Optional[str] = None):
    """Create or retrieve SQLAlchemy engine for PostgreSQL with pgvector."""
    global _engine

    # Return cached engine if it exists
    if _engine is not None:
        return _engine

    url = database_url or os.getenv("DATABASE_URL")

    if not url:
        raise Exception("DATABASE_URL not in env")

    try:
        # Configure connection pool with proper limits, recycling, and timeouts
        _engine = create_engine(
            url,
            **_database_pool_kwargs(),
            connect_args={
                "connect_timeout": 10,  # Timeout for establishing new connections
                "options": "-c statement_timeout=30000",  # 30s query timeout (prevents stuck queries)
            },
            echo=False,  # Set to True for SQL query debugging
        )

        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))

        if not check_pgvector_extension(_engine):
            install_pgvector_extension(_engine)

        logger.debug(f"Connected to database using {url}")
        return _engine
    except (ImportError, SQLAlchemyError) as e:
        logger.error(f"Database connection failed: {e}")
        _engine = None  # Reset on failure
        raise Exception(f"Database connection failed: {e}")


def get_session_factory(engine=None):
    """Get or create session factory for database."""
    global _session_factory, _engine

    # Return cached session factory if it exists
    if _session_factory is not None and engine is None:
        return _session_factory

    # Use provided engine or get the global engine
    engine = engine or get_engine()

    # Create and cache the session factory
    _session_factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    return _session_factory


def get_db_session() -> Generator[Session, None, None]:
    """Get a database session."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        yield db
    finally:
        _safe_close_db_session(db)


def get_health_engine(database_url: Optional[str] = None) -> Engine:
    """Return a tiny dedicated engine used only by health checks.

    Health probes must report whether Postgres is reachable, not whether the
    request pool happens to be saturated. Sharing the main pool made the
    readiness probe fail exactly when the app was busiest, marking every pod
    NotReady at once and turning a load spike into an outage.

    This engine keeps a single connection, never overflows, and fails fast so
    a probe can never sit for the full 30s request-pool timeout.
    """
    global _health_engine

    if _health_engine is not None:
        return _health_engine

    url = database_url or os.getenv("DATABASE_URL")
    if not url:
        raise Exception("DATABASE_URL not in env")

    _health_engine = create_engine(
        url,
        pool_size=1,
        max_overflow=0,
        pool_pre_ping=True,
        pool_recycle=_env_int("DATABASE_POOL_RECYCLE", 1800),
        # Fail fast: a probe should time out well inside its own deadline.
        pool_timeout=_env_int("DATABASE_HEALTH_POOL_TIMEOUT", 3),
        connect_args={
            "connect_timeout": 3,
            "options": "-c statement_timeout=3000",
        },
        echo=False,
    )
    return _health_engine


def get_async_engine(database_url: Optional[str] = None) -> AsyncEngine:
    """Create or retrieve async SQLAlchemy engine for PostgreSQL with pgvector."""
    global _async_engine

    # Return cached engine if it exists
    if _async_engine is not None:
        return _async_engine

    url = database_url or os.getenv("DATABASE_URL")

    if not url:
        raise Exception("DATABASE_URL not in env")

    # Convert psycopg to asyncpg for async operations
    if url.startswith("postgresql+psycopg://"):
        url = url.replace("postgresql+psycopg://", "postgresql+asyncpg://")
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://")

    try:
        # Configure connection pool with proper limits, recycling, and timeouts
        _async_engine = create_async_engine(
            url,
            **_database_pool_kwargs(),
            connect_args={
                "timeout": 10,  # Connection timeout for asyncpg
                "command_timeout": 30,  # Query timeout for asyncpg
            },
            echo=False,  # Set to True for SQL query debugging
        )

        logger.debug(f"Connected to async database using {url}")
        return _async_engine
    except (ImportError, SQLAlchemyError) as e:
        logger.error(f"Async database connection failed: {e}")
        _async_engine = None  # Reset on failure
        raise Exception(f"Async database connection failed: {e}")


def get_async_session_factory(
    engine: Optional[AsyncEngine] = None,
) -> async_sessionmaker:
    """Get or create async session factory for database."""
    global _async_session_factory, _async_engine

    # Return cached session factory if it exists
    if _async_session_factory is not None and engine is None:
        return _async_session_factory

    # Use provided engine or get the global engine
    engine = engine or get_async_engine()

    # Create and cache the session factory
    _async_session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )
    return _async_session_factory


@asynccontextmanager
async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session context manager.

    Usage:
        async with get_async_db_session() as db:
            result = await db.execute(query)

    Note: Always rollback any uncommitted transaction before closing to prevent
    "idle in transaction" connections that can exhaust the connection pool.
    """
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        try:
            yield session
        except Exception:
            # Rollback on any exception to clean up the transaction
            await session.rollback()
            raise
        finally:
            await _safe_close_async_db_session(session)
