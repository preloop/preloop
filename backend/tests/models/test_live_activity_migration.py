"""A failed concurrent index build must not commit the feature tables."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from preloop.models.migration_runtime import sqlstate_of

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261031_live_activity.py"
)


def test_index_timeout_can_be_retried_without_partial_tables(
    db_engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the helper's supported finite timeout pair; the short original
    # blocking-DDL setting must still be restored after failure and success.
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", "2")
    spec = spec_from_file_location("live_activity_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"activity_migration_{uuid4().hex}"
    # Session GUCs belong only to this test; never return them to the suite pool.
    engine = create_engine(db_engine.url, poolclass=NullPool)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as admin:
        admin.execute(text(f'CREATE SCHEMA "{schema}"'))
        try:
            with engine.connect() as connection, engine.connect() as writer:
                for conn in (connection, writer):
                    conn.execute(text(f'SET search_path TO "{schema}"'))
                    conn.commit()
                for statement in (
                    "CREATE TABLE account (id uuid PRIMARY KEY)",
                    'CREATE TABLE "user" (id uuid PRIMARY KEY)',
                    "CREATE TABLE runtime_session_activity "
                    "(timestamp timestamptz, account_id uuid, activity_type text)",
                    "CREATE TABLE approval_request (resolved_at timestamptz)",
                ):
                    connection.execute(text(statement))
                connection.execute(text("SET lock_timeout = '200ms'"))
                connection.commit()
                # A real uncommitted write makes CREATE INDEX CONCURRENTLY wait.
                writer.execute(
                    text(
                        "INSERT INTO runtime_session_activity (activity_type) "
                        "VALUES ('user_prompt')"
                    )
                )
                context = MigrationContext.configure(connection)
                with pytest.raises(OperationalError) as failure:
                    with context.begin_transaction(), Operations.context(context):
                        migration.upgrade()
                assert sqlstate_of(failure.value) == "55P03"
                writer.rollback()
                assert (
                    connection.execute(
                        text("SELECT to_regclass('activity_rollup')")
                    ).scalar()
                    is None
                )
                connection.rollback()
                # Retry must repair the invalid index and create the tables once.
                with context.begin_transaction(), Operations.context(context):
                    migration.upgrade()
                assert (
                    connection.execute(
                        text(
                            "SELECT count(*) FROM pg_index i JOIN pg_class c "
                            "ON c.oid = i.indexrelid WHERE c.relnamespace = "
                            "current_schema()::regnamespace AND i.indisvalid "
                            "AND c.relname IN ('ix_rsa_account_turn_events', "
                            "'ix_approval_request_resolved_at')"
                        )
                    ).scalar()
                    == 2
                )
                assert (
                    connection.execute(
                        text("SELECT to_regclass('activity_rollup') IS NOT NULL")
                    ).scalar()
                    is True
                )
                assert connection.execute(text("SHOW lock_timeout")).scalar() == "200ms"
                connection.rollback()
        finally:
            admin.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
