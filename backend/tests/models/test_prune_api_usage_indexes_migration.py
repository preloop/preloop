"""The api_usage index prune keeps the model and the migration in agreement."""

import importlib.util
from pathlib import Path
from types import ModuleType
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from preloop.models.models.api_usage import ApiUsage

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261110_prune_api_usage_indexes.py"
)


def _migration() -> ModuleType:
    spec = importlib.util.spec_from_file_location("prune_api_usage_idx", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_model_metadata_no_longer_declares_dropped_indexes() -> None:
    """Every index the migration drops is absent from ApiUsage metadata."""
    migration = _migration()
    declared = {index.name for index in ApiUsage.__table__.indexes}
    assert set(migration.DROPPED) == {
        "ix_api_usage_id",
        "ix_api_usage_account_id",
        "ix_api_usage_imported_parent_conv",
    }
    assert declared.isdisjoint(migration.DROPPED), declared & set(migration.DROPPED)
    # The kept neighbours stay declared.
    assert {
        "ix_api_usage_user_id",
        "ix_api_usage_account_action_ts",
        "ix_api_usage_imported_conversation",
        "ix_api_usage_account_upstream_request_id",
    } <= declared
    assert ApiUsage.__table__.c.id.primary_key
    assert not ApiUsage.__table__.c.id.index


def _indexes(connection, schema: str) -> dict[str, bool]:
    rows = connection.execute(
        text(
            "SELECT c.relname, i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relnamespace = to_regnamespace(:schema)"
        ),
        {"schema": schema},
    )
    return {name: valid for name, valid in rows}


# Written out independently of the migration so a wrong DEFINITIONS entry
# (a missing column or predicate) fails the downgrade check.
EXPECTED_DEFINITIONS = {
    "ix_api_usage_id": "CREATE INDEX ix_api_usage_id ON {schema}.api_usage "
    "USING btree (id)",
    "ix_api_usage_account_id": "CREATE INDEX ix_api_usage_account_id "
    "ON {schema}.api_usage USING btree (account_id)",
    "ix_api_usage_imported_parent_conv": "CREATE INDEX "
    "ix_api_usage_imported_parent_conv ON {schema}.api_usage USING btree "
    "(account_id, parent_conversation_id) WHERE ((action_type = "
    "'imported_usage'::text) AND (parent_conversation_id IS NOT NULL))",
}


def _definitions(connection, schema: str) -> dict[str, str]:
    rows = connection.execute(
        text(
            "SELECT c.relname, pg_get_indexdef(c.oid) FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relnamespace = to_regnamespace(:schema) "
            "AND c.relname <> 'api_usage_pkey'"
        ),
        {"schema": schema},
    )
    return {name: definition for name, definition in rows}


def test_prune_upgrade_and_downgrade(db_engine: Engine) -> None:
    """Upgrade drops the three indexes and keeps the pkey; downgrade restores them."""
    migration = _migration()
    schema = "prune_api_usage_test_" + uuid4().hex
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        try:
            connection.execute(text(f"SET search_path TO {schema}"))
            connection.execute(
                text(
                    "CREATE TABLE api_usage (id uuid PRIMARY KEY, account_id uuid, "
                    "action_type text, parent_conversation_id text)"
                )
            )
            for name, definition in migration.DEFINITIONS.items():
                connection.execute(text(f"CREATE INDEX {name} {definition}"))
            connection.commit()
            context = MigrationContext.configure(connection)
            with context.begin_transaction(), Operations.context(context):
                migration.upgrade()
                migration.upgrade()  # idempotent
            assert _indexes(connection, schema) == {"api_usage_pkey": True}

            # A cancelled concurrent rebuild leaves an invalid index of the
            # same name; IF NOT EXISTS alone would keep it. A failed unique
            # concurrent build produces one without catalog writes.
            account = "00000000-0000-0000-0000-000000000001"
            connection.execute(
                text(
                    "INSERT INTO api_usage (id, account_id) VALUES "
                    "(gen_random_uuid(), :a), (gen_random_uuid(), :a)"
                ),
                {"a": account},
            )
            connection.commit()
            with context.autocommit_block(), pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "CREATE UNIQUE INDEX CONCURRENTLY ix_api_usage_account_id "
                        "ON api_usage (account_id)"
                    )
                )
            connection.rollback()
            assert _indexes(connection, schema)["ix_api_usage_account_id"] is False

            with context.begin_transaction(), Operations.context(context):
                migration.downgrade()
            assert _indexes(connection, schema) == {
                "api_usage_pkey": True,
                **{name: True for name in migration.DROPPED},
            }
            assert _definitions(connection, schema) == {
                name: definition.format(schema=schema)
                for name, definition in EXPECTED_DEFINITIONS.items()
            }
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()
