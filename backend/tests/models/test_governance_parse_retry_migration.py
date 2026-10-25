"""Parse-retry index builds are restartable and preserve the original queue."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text


def test_parse_retry_index_upgrade_repair_and_downgrade(db_engine: Engine) -> None:
    """Exercise concurrent DDL in an isolated schema, including invalid repair."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261103_governance_parse_retry.py"
    )
    spec = importlib.util.spec_from_file_location("parse_retry_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "parse_retry_test_" + uuid4().hex
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
                    connection.execute(
                        text(
                            "UPDATE pg_index SET indisvalid = false "
                            "WHERE indexrelid = to_regclass(:name)"
                        ),
                        {"name": migration.INDEX},
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
