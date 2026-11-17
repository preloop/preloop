"""The storage guard covers legacy and bulk execution writers."""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Engine


def test_category_backfill_and_guard(db_engine: Engine) -> None:
    """Upgrade names old failures and protects insert/update; downgrade removes it."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261220_execution_failure_category.py"
    )
    spec = importlib.util.spec_from_file_location("execution_failure", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = f"failure_category_{uuid4().hex}"
    with db_engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        connection.execute(
            text(
                "CREATE TABLE flow_execution (status text, error_message text, failure_category text)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO flow_execution VALUES ('FAILED', 'native_result requires an enabled result_schema', NULL), ('FAILED', 'Agent reported an explicit failure in result.json', 'unknown'), ('RUNNING', NULL, NULL), ('FAILED', 'Execution stopped after detecting a repeated tool loop: native/Bash repeated 20 times with identical arguments.', 'unknown')"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(
            text("SELECT failure_category FROM flow_execution ORDER BY error_message")
        ).scalars().all() == [
            "agent_reported_failure",
            "agent_tool_loop",
            "flow_config",
            None,
        ]
        connection.execute(
            text(
                "INSERT INTO flow_execution VALUES ('FAILED', 'unexpected failure', NULL), ('ABORTED', 'aborted execution', NULL)"
            )
        )
        connection.execute(
            text(
                "UPDATE flow_execution SET status='FAILED', failure_category='unknown' WHERE status='RUNNING'"
            )
        )
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM flow_execution WHERE failure_category IS NULL OR failure_category='unknown'"
                )
            ).scalar()
            == 0
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))
