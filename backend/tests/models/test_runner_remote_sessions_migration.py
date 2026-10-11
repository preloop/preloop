"""Migration ``20261011_runner_remote_sessions`` (#1482), down and up."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models import models

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261011_runner_remote_sessions.py"
)
INDEXES = {
    "ix_runner_remote_sessions_id",
    "ix_runner_remote_sessions_runtime_session_id",
    "ix_runner_remote_sessions_runner_state",
    "ix_runner_remote_sessions_account_state",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "runner_remote_sessions_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_downgrade_then_upgrade_matches_the_model(db_session):
    migration = _load_migration()
    connection = db_session.connection()
    with Operations.context(MigrationContext.configure(connection)):
        migration.downgrade()
        assert "runner_remote_sessions" not in inspect(connection).get_table_names()
        migration.upgrade()

    inspector = inspect(connection)
    columns = {c["name"] for c in inspector.get_columns("runner_remote_sessions")}
    model = models.RunnerRemoteSession
    assert columns == {column.name for column in model.__table__.columns}
    indexes = {i["name"] for i in inspector.get_indexes("runner_remote_sessions")}
    assert INDEXES <= indexes
    foreign = {
        fk["referred_table"]: fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("runner_remote_sessions")
    }
    assert foreign == {
        "account": "CASCADE",
        "runtime_session": "SET NULL",
        "flow_runner": "CASCADE",
        "user": "SET NULL",
    }
