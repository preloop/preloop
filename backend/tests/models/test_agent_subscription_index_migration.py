"""Principal subscription index builds are restartable without changing other queues."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError


def test_subscription_principal_index_upgrade_repair_and_downgrade(
    db_engine: Engine,
) -> None:
    """Exercise concurrent DDL and invalid-index repair in an isolated schema."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261108_agent_subscription_principal_idx.py"
    )
    spec = importlib.util.spec_from_file_location(
        "subscription_principal_migration", path
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "subscription_principal_test_" + uuid4().hex
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    "CREATE TABLE api_usage ("
                    "id uuid, account_id uuid, timestamp timestamptz, "
                    "runtime_principal_type text, runtime_principal_id text, "
                    "action_type text, cost_source text, segment_index integer)"
                )
            )
            connection.execute(
                text(
                    "CREATE INDEX ix_api_usage_gateway_subscription "
                    "ON api_usage (account_id)"
                )
            )
            for invalid in (False, True):
                if invalid:
                    connection.execute(text(f"DROP INDEX {migration.NAME}"))
                    connection.execute(
                        text("INSERT INTO api_usage (segment_index) VALUES (1), (1)")
                    )
                    # A failed concurrent unique build leaves an invalid index,
                    # without requiring privileged writes to PostgreSQL catalogs.
                    connection.commit()
                    with context.autocommit_block(), pytest.raises(IntegrityError):
                        connection.execute(
                            text(
                                f"CREATE UNIQUE INDEX CONCURRENTLY {migration.NAME} "
                                "ON api_usage (segment_index)"
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
                        {"name": migration.NAME},
                    ).scalar_one()
                    is True
                )
            connection.commit()
            with context.begin_transaction(), Operations.context(context):
                migration.downgrade()
            assert (
                connection.execute(
                    text("SELECT to_regclass(:name)"), {"name": migration.NAME}
                ).scalar_one()
                is None
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('ix_api_usage_gateway_subscription')")
                ).scalar_one()
                is not None
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
