"""Migration 20261112_activity_meta_bytes adds a column and moves no data."""

import ast
import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.orm import Session

PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop/models/alembic/versions/20261112_activity_metadata_bytes.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("activity_meta_bytes_mig", PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(db_session: Session) -> set:
    return {
        c["name"]
        for c in inspect(db_session.connection()).get_columns(
            "runtime_session_activity"
        )
    }


def test_migration_is_schema_only() -> None:
    """The backfill runs after the deploy; the migration must not touch rows."""
    tree = ast.parse(PATH.read_text())
    assert not [n for n in ast.walk(tree) if isinstance(n, (ast.For, ast.While))]
    source = PATH.read_text().upper()
    assert "UPDATE " not in source.replace("UPDATE\n", "")
    assert "OP.EXECUTE" not in source


def test_migration_down_and_up(db_session: Session) -> None:
    migration = _load()
    assert "metadata_bytes" in _columns(db_session)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        assert "metadata_bytes" not in _columns(db_session)
        migration.upgrade()
    assert "metadata_bytes" in _columns(db_session)
