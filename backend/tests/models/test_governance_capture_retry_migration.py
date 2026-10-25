"""Capture-retry index builds are restartable without changing other queues."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError


def test_capture_retry_index_upgrade_repair_and_downgrade(db_engine: Engine) -> None:
    """Exercise concurrent DDL and invalid-index repair in an isolated schema."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261107_governance_capture_retry.py"
    )
    spec = importlib.util.spec_from_file_location("capture_retry_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "capture_retry_test_" + uuid4().hex
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    "CREATE TABLE governance_session_digest ("
                    "id uuid, account_id uuid, window_end timestamptz, "
                    "runtime_session_id uuid, segment_index integer, digest jsonb)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_governance_session_digest_retry_queue "
                    "ON governance_session_digest (account_id)"
                )
            )
            for invalid in (False, True):
                if invalid:
                    connection.execute(text(f"DROP INDEX {migration.INDEX}"))
                    connection.execute(
                        text(
                            "INSERT INTO governance_session_digest (segment_index) VALUES (1), (1)"
                        )
                    )
                    # A failed concurrent unique build leaves an invalid index,
                    # without requiring privileged writes to PostgreSQL catalogs.
                    connection.commit()
                    with context.autocommit_block(), pytest.raises(IntegrityError):
                        connection.execute(
                            text(
                                f"CREATE UNIQUE INDEX CONCURRENTLY {migration.INDEX} "
                                "ON governance_session_digest (segment_index)"
                            )
                        )
                connection.commit()
                context = MigrationContext.configure(connection)
                with context.begin_transaction(), Operations.context(context):
                    migration.upgrade()
                    migration.upgrade()
                assert (
                    connection.execute(
                        text(
                            "SELECT indisvalid FROM pg_index "
                            "WHERE indexrelid = to_regclass(:name)"
                        ),
                        {"name": migration.INDEX},
                    ).scalar_one()
                    is True
                )
            connection.commit()
            with context.begin_transaction(), Operations.context(context):
                migration.downgrade()
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": migration.INDEX}
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text(
                        "SELECT to_regclass('ix_governance_session_digest_retry_queue')"
                    )
                ).scalar_one()
                is not None
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
