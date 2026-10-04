"""Exercise audit index lifecycle without touching the app's audit records."""

import importlib.util
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261004_audit_group_lookup_indexes.py"
)


def load_migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "audit_lookup_indexes", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def isolated_audit_connection(db_engine: Engine) -> Iterator[Connection]:
    """Migration DDL uses its own disposable schema, never shared app tables."""
    schema = f"audit_index_test_{uuid4().hex}"
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text("CREATE TABLE audit_log (account_id uuid, details jsonb)")
            )
            connection.execute(
                text(
                    'INSERT INTO audit_log VALUES (\'00000000-0000-0000-0000-000000000001\', \'{"correlation_id":"example-call","approval_id":"example-approval"}\')'
                )
            )
            yield connection
        finally:
            # Alembic restores READ COMMITTED after its autocommit block;
            # explicitly commit cleanup so the pooled connection cannot keep
            # a search_path that points at the discarded schema.
            connection.commit()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            connection.commit()


def run_migration(connection: Connection, direction: str) -> None:
    connection.commit()
    context = MigrationContext.configure(connection)
    with context.begin_transaction(), Operations.context(context):
        getattr(load_migration(), direction)()


def index_state(connection: Connection) -> dict[str, Any]:
    return {
        row.relname: (row.oid, row.indisvalid, row.definition)
        for row in connection.execute(
            text(
                "SELECT c.relname, c.oid, i.indisvalid, pg_get_indexdef(c.oid) AS definition FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema()"
            )
        )
    }


def test_upgrade_downgrade_and_retry_preserve_rows(
    isolated_audit_connection: Connection,
) -> None:
    connection = isolated_audit_connection
    run_migration(connection, "upgrade")
    before = index_state(connection)
    expected = {name for name, _ in load_migration().INDEXES}
    assert set(before) == expected
    assert all(state[1] for state in before.values())
    for name, expression in load_migration().INDEXES:
        assert "account_id" in before[name][2]
        assert expression.split("'")[1] in before[name][2]
        assert "IS NOT NULL" in before[name][2]
    run_migration(connection, "upgrade")
    assert index_state(connection) == before
    run_migration(connection, "downgrade")
    assert index_state(connection) == {}
    assert connection.scalar(text("SELECT count(*) FROM audit_log")) == 1
    run_migration(connection, "upgrade")
    assert set(index_state(connection)) == expected
    assert all(state[1] for state in index_state(connection).values())


def test_upgrade_rebuilds_invalid_indexes(
    isolated_audit_connection: Connection,
) -> None:
    connection = isolated_audit_connection
    run_migration(connection, "upgrade")
    connection.execute(
        text(
            "UPDATE pg_index SET indisvalid = false WHERE indexrelid IN (SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = current_schema())"
        )
    )
    assert not any(state[1] for state in index_state(connection).values())
    run_migration(connection, "upgrade")
    assert all(state[1] for state in index_state(connection).values())


def test_model_metadata_matches_lookup_expressions() -> None:
    definitions = {
        index.name: str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        for index in models.AuditLog.__table__.indexes
    }
    for name, expression in load_migration().INDEXES:
        sql = definitions[name]
        assert f"account_id, {expression}" in sql
        assert f"WHERE {expression} IS NOT NULL" in sql
