"""The work item migration creates and drops its tables cleanly (#2016)."""

import importlib.util
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, inspect, text

TABLES = {"work_item", "work_item_pr", "assessment"}


def _load() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261110_work_item.py"
    )
    spec = importlib.util.spec_from_file_location("work_item_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def schema_connection(db_engine: Engine) -> Generator[Connection, None, None]:
    """An isolated schema holding only the tables the migration references."""
    schema = "work_item_" + uuid4().hex
    with db_engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}"))
        for table in (
            "account",
            "issue",
            "flow_execution",
            "flow_thread",
            '"user"',
            "issue_cost_pull_request",
        ):
            connection.execute(text(f"CREATE TABLE {table}(id uuid PRIMARY KEY)"))
        connection.commit()
        try:
            yield connection
        finally:
            connection.rollback()
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            connection.commit()


def _run(connection: Connection, step: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(_load(), step)()
    connection.commit()


def test_parent_is_the_previous_head() -> None:
    assert _load().down_revision == "20261109_agent_usage_totals"


def test_upgrade_creates_tables_indexes_and_downgrade_drops_them(
    schema_connection: Connection,
) -> None:
    _run(schema_connection, "upgrade")
    inspector = inspect(schema_connection)
    assert TABLES <= set(inspector.get_table_names())
    work_item_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("work_item")
    }
    assert work_item_indexes["ix_work_item_account_state"] == ["account_id", "state"]
    assert work_item_indexes["ix_work_item_flow_thread"] == ["flow_thread_id"]
    pr_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("work_item_pr")
    }
    assert pr_indexes["ix_work_item_pr_repo_state"] == ["repo", "state"]
    work_item_columns = {c["name"] for c in inspector.get_columns("work_item")}
    assert work_item_columns.isdisjoint({"head_sha", "cost", "merged_at"})
    foreign = {
        fk["referred_table"]: fk["constrained_columns"]
        for fk in inspector.get_foreign_keys("work_item")
    }
    assert foreign["assessment"] == ["latest_assessment_id"]
    assert foreign["flow_thread"] == ["flow_thread_id"]

    _run(schema_connection, "downgrade")
    assert TABLES.isdisjoint(inspect(schema_connection).get_table_names())

    # Up again after down: the downgrade left nothing behind.
    _run(schema_connection, "upgrade")
    assert TABLES <= set(inspect(schema_connection).get_table_names())
