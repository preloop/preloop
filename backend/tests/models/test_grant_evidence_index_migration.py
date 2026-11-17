"""Exercise grant evidence indexes against a disposable PostgreSQL schema."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.schema import CreateIndex

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261217_grant_evidence_idx.py"
)


def load_migration() -> ModuleType:
    """Load the migration module from its file.

    Returns:
        The imported migration module.
    """
    spec = importlib.util.spec_from_file_location("grant_evidence", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_connection(db_engine: Engine) -> Iterator[Connection]:
    """Yield a connection whose search_path is a throwaway schema.

    Args:
        db_engine: Test database engine.

    Yields:
        An autocommit connection on the disposable schema.
    """
    schema = f"grant_evidence_test_{uuid4().hex}"
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    "CREATE TABLE audit_log (id uuid, account_id uuid, timestamp timestamptz, details jsonb)"
                )
            )
            yield connection
        finally:
            connection.commit()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            connection.commit()


def run_migration(connection: Connection, direction: str) -> None:
    """Run the migration's upgrade or downgrade on ``connection``.

    Args:
        connection: Connection to migrate.
        direction: ``"upgrade"`` or ``"downgrade"``.
    """
    connection.commit()
    context = MigrationContext.configure(connection)
    with context.begin_transaction(), Operations.context(context):
        getattr(load_migration(), direction)()


def index_defs(connection: Connection) -> dict[str, tuple[bool, str]]:
    """Index name to (valid, definition) in the current schema.

    Args:
        connection: Connection on the disposable schema.

    Returns:
        Mapping of index names to validity and definition.
    """
    return {
        row.relname: (row.indisvalid, row.definition)
        for row in connection.execute(
            text(
                "SELECT c.relname, i.indisvalid, pg_get_indexdef(c.oid) AS definition "
                "FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema()"
            )
        )
    }


def test_upgrade_is_idempotent_and_reversible(
    isolated_connection: Connection,
) -> None:
    """Upgrade builds valid partial indexes; downgrade removes them."""
    connection = isolated_connection
    run_migration(connection, "upgrade")
    defs = index_defs(connection)
    assert set(defs) == {name for name, _ in load_migration().INDEXES}
    assert all(valid and "WHERE" in definition for valid, definition in defs.values())
    run_migration(connection, "upgrade")
    assert index_defs(connection) == defs
    run_migration(connection, "downgrade")
    assert index_defs(connection) == {}


def test_upgrade_rebuilds_invalid_index(isolated_connection: Connection) -> None:
    """An interrupted concurrent build is dropped and rebuilt."""
    connection = isolated_connection
    run_migration(connection, "upgrade")
    connection.execute(
        text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid IN "
            "(SELECT c.oid FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = current_schema())"
        )
    )
    run_migration(connection, "upgrade")
    assert all(valid for valid, _ in index_defs(connection).values())


def test_model_metadata_declares_unsealed_index() -> None:
    """The model carries the same partial index the migration builds."""
    definitions = {
        index.name: str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in models.AuditLog.__table__.indexes
    }
    for name, _ in load_migration().INDEXES:
        assert "account_id" in definitions[name]
        assert "WHERE" in definitions[name]
