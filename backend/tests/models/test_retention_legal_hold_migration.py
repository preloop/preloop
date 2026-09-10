"""Run the retention migration's downgrade/upgrade cycle on real Postgres.

`test_alembic_single_head.py` only walks the revision graph, and the test
database is built from ORM metadata, so neither one ever executes this
migration's DDL. This one does, in the test transaction: down then up, then
diff the rebuilt table and the added columns against the ORM definition.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models.models.approval_request import ApprovalRequest
from preloop.models.models.flow_artifact import FlowArtifact
from preloop.models.models.flow_execution import FlowExecution
from preloop.models.models.legal_hold import LegalHold

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260910_retention_legal_hold.py"
)

FLAGGED = {
    "flow_execution": FlowExecution,
    "approval_request": ApprovalRequest,
    "flow_artifact": FlowArtifact,
}


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "retention_legal_hold_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(db_session):
    """An Alembic operations context bound to the test transaction."""
    context = MigrationContext.configure(db_session.connection())
    return Operations.context(context)


def _column_names(db_session, table: str) -> set[str]:
    return {
        column["name"] for column in inspect(db_session.connection()).get_columns(table)
    }


def test_downgrade_removes_the_table_and_every_flag(db_session):
    migration = _load_migration()
    assert inspect(db_session.connection()).has_table("legal_hold")

    with _operations(db_session):
        migration.downgrade()

    assert not inspect(db_session.connection()).has_table("legal_hold")
    for table in FLAGGED:
        assert "legal_hold" not in _column_names(db_session, table)


def test_upgrade_after_downgrade_rebuilds_the_orm_shape(db_session):
    """A full down/up cycle leaves exactly the columns the ORM expects."""
    migration = _load_migration()
    expected = {column.name for column in LegalHold.__table__.columns}

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    assert _column_names(db_session, "legal_hold") == expected
    for table, model in FLAGGED.items():
        assert "legal_hold" in _column_names(db_session, table)
        assert model.__table__.columns["legal_hold"].nullable is False


def test_the_flag_defaults_to_false_on_existing_rows(db_session, test_user):
    """An upgrade must not freeze the records an account already has."""
    from preloop.models import models

    migration = _load_migration()
    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    flow = models.Flow(
        name="migration-default",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="COMPLETED")
    db_session.add(execution)
    db_session.flush()

    assert execution.legal_hold is False


def test_upgrade_recreates_the_one_active_hold_per_resource_index(db_session):
    """The partial unique index is what stops two active holds on one row."""
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    indexes = {
        index["name"]: index
        for index in inspect(db_session.connection()).get_indexes("legal_hold")
    }
    active = indexes["uq_legal_hold_active_resource"]
    assert active["unique"] is True
    assert active["column_names"] == ["account_id", "resource_type", "resource_id"]
    # Partial: a released hold must not block holding the same record again.
    assert "released_at IS NULL" in str(active.get("dialect_options", {}))


def test_upgrade_keeps_the_cascade_that_stops_orphan_holds(db_session):
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    rules = {
        tuple(fk["constrained_columns"]): fk["options"].get("ondelete")
        for fk in inspect(db_session.connection()).get_foreign_keys("legal_hold")
    }

    assert rules[("account_id",)] == "CASCADE"
    # A deleted operator must not take the hold they placed with them: the
    # record of who froze the data is the point.
    assert rules[("placed_by_user_id",)] == "SET NULL"
    assert rules[("released_by_user_id",)] == "SET NULL"
