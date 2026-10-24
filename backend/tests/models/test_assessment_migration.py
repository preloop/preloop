"""The assessment migration upgrades and downgrades cleanly (#2016)."""

import importlib.util
from collections.abc import Generator
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, inspect, text

TABLES = {"assessment", "issue_cost_pull_request_issue"}
PR_COLUMNS = {"origin", "author_id", "head_sha", "state"}


def _load() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261110_assessment_pr_identity.py"
    )
    spec = importlib.util.spec_from_file_location("assessment_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


@pytest.fixture
def schema_connection(db_engine: Engine) -> Generator[Connection, None, None]:
    """An isolated schema holding only the tables the migration references."""
    schema = "assessment_" + uuid4().hex
    with db_engine.connect() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET search_path TO {schema}"))
        for table in ("account", "issue", "flow_execution"):
            connection.execute(text(f"CREATE TABLE {table}(id uuid PRIMARY KEY)"))
        connection.execute(
            text("CREATE TABLE issue_cost_pull_request(id uuid PRIMARY KEY)")
        )
        connection.commit()
        try:
            yield connection
        finally:
            connection.rollback()
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
            # The SET was committed; the pooled connection must not keep it.
            connection.execute(text("RESET search_path"))
            connection.commit()


def _run(connection: Connection, step: str) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        getattr(_load(), step)()
    connection.commit()


def test_parent_is_the_previous_head() -> None:
    assert _load().down_revision == "20261109_agent_usage_totals"


def test_upgrade_then_downgrade(schema_connection: Connection) -> None:
    _run(schema_connection, "upgrade")
    inspector = inspect(schema_connection)
    assert TABLES <= set(inspector.get_table_names())
    columns = {c["name"] for c in inspector.get_columns("issue_cost_pull_request")}
    assert PR_COLUMNS <= columns
    indexes = {index["name"]: index for index in inspector.get_indexes("assessment")}
    issue_index = indexes["ix_assessment_account_issue_assessed"]
    assert issue_index["column_names"] == ["account_id", "issue_id", "assessed_at"]
    assert issue_index["column_sorting"] == {"assessed_at": ("desc",)}
    pr_index = indexes["ix_assessment_account_pull_request_assessed"]
    assert pr_index["column_names"] == [
        "account_id",
        "issue_cost_pull_request_id",
        "assessed_at",
    ]

    _run(schema_connection, "downgrade")
    inspector = inspect(schema_connection)
    assert not TABLES & set(inspector.get_table_names())
    columns = {c["name"] for c in inspector.get_columns("issue_cost_pull_request")}
    assert not PR_COLUMNS & columns
