"""Migration 20261011_harness_inventory upgrades and downgrades cleanly."""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect
from sqlalchemy.orm import Session


def _load():
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261011_harness_inventory.py"
    )
    spec = importlib.util.spec_from_file_location("harness_inventory_mig", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _columns(db_session: Session) -> set:
    return {
        c["name"] for c in inspect(db_session.connection()).get_columns("flow_runner")
    }


def test_harness_inventory_migration_down_and_up(db_session: Session) -> None:
    migration = _load()
    assert {"harness_inventory", "harness_inventory_updated_at"} <= _columns(db_session)
    with Operations.context(MigrationContext.configure(db_session.connection())):
        migration.downgrade()
        assert "harness_inventory" not in _columns(db_session)
        assert "harness_inventory_updated_at" not in _columns(db_session)
        migration.upgrade()
    columns = {
        c["name"]: c
        for c in inspect(db_session.connection()).get_columns("flow_runner")
    }
    assert columns["harness_inventory"]["nullable"] is True
    assert columns["harness_inventory_updated_at"]["nullable"] is True
