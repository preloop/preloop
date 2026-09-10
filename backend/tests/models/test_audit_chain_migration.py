"""Run the audit chain migration down and up again on real Postgres.

`test_alembic_single_head.py` only walks the revision graph and the test
database is built from ORM metadata, so neither one ever executes this DDL.
This does, inside the test transaction, and then diffs what came back against
the ORM definitions.
"""

import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from preloop.models.models.account_signing_key import AccountSigningKey
from preloop.models.models.audit_chain import AuditChainCheckpoint, AuditChainState
from preloop.models.models.record_signature import RecordSignature

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20260910_audit_chain_signing.py"
)

NEW_TABLES = {
    "audit_chain_state": AuditChainState,
    "audit_chain_checkpoint": AuditChainCheckpoint,
    "account_signing_key": AccountSigningKey,
    "record_signature": RecordSignature,
}

CHAIN_COLUMNS = {"chain_seq", "prev_hash", "row_hash", "sealed_at"}


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "audit_chain_signing_migration", MIGRATION_PATH
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


def test_downgrade_removes_every_table_and_column_it_added(db_session):
    migration = _load_migration()
    inspector = inspect(db_session.connection())
    for table in NEW_TABLES:
        assert inspector.has_table(table)

    with _operations(db_session):
        migration.downgrade()

    inspector = inspect(db_session.connection())
    for table in NEW_TABLES:
        assert not inspector.has_table(table)
    assert not CHAIN_COLUMNS & _column_names(db_session, "audit_log")


def test_downgrade_leaves_the_audit_rows_themselves_alone(db_session, test_user):
    """Reversing tamper evidence must not delete the records it covered."""
    from preloop.models.models.audit_log import AuditLog

    row = AuditLog(
        account_id=test_user.account_id,
        action="permission_check",
        resource_type="tool",
        resource_id="send_payment",
        status="success",
    )
    db_session.add(row)
    db_session.flush()
    row_id = row.id
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()

    surviving = db_session.connection().exec_driver_sql(
        "SELECT count(*) FROM audit_log WHERE id = %(id)s", {"id": str(row_id)}
    )
    assert surviving.scalar() == 1


def test_a_full_cycle_rebuilds_exactly_the_orm_shape(db_session):
    migration = _load_migration()
    expected = {
        table: {column.name for column in model.__table__.columns}
        for table, model in NEW_TABLES.items()
    }

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    for table, columns in expected.items():
        assert _column_names(db_session, table) == columns
    assert CHAIN_COLUMNS <= _column_names(db_session, "audit_log")


def test_the_chain_columns_come_back_nullable(db_session):
    """Every row written before the chain existed has no position in it."""
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    columns = {
        column["name"]: column
        for column in inspect(db_session.connection()).get_columns("audit_log")
    }
    for name in CHAIN_COLUMNS:
        assert columns[name]["nullable"] is True


def test_the_upgrade_recreates_the_one_active_key_index(db_session):
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    indexes = {
        index["name"]: index
        for index in inspect(db_session.connection()).get_indexes("account_signing_key")
    }
    active = indexes["uq_account_signing_key_active"]
    assert active["unique"] is True
    assert active["column_names"] == ["account_id"]
    # Partial: rotation must not be blocked by the keys it retired.
    assert "retired_at IS NULL" in str(active.get("dialect_options", {}))


def test_the_upgrade_recreates_the_chain_walk_index(db_session):
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    indexes = {
        index["name"]: index
        for index in inspect(db_session.connection()).get_indexes("audit_log")
    }
    walk = indexes["ix_audit_log_account_chain_seq"]
    assert walk["column_names"] == ["account_id", "chain_seq"]


def test_one_signature_per_record_survives_the_cycle(db_session):
    migration = _load_migration()

    with _operations(db_session):
        migration.downgrade()
        migration.upgrade()

    indexes = {
        index["name"]: index
        for index in inspect(db_session.connection()).get_indexes("record_signature")
    }
    subject = indexes["uq_record_signature_subject"]
    assert subject["unique"] is True
    assert subject["column_names"] == ["account_id", "payload_type", "subject_id"]
