"""The resume-root index migration, and the model that mirrors it.

Issue #1197: the chain cost rollup on the executions list needs
``ix_flow_execution_resume_root``. Two ways to lose it silently are pinned:

* a CONCURRENTLY build that failed part way leaves an INVALID index; the
  migration runner retries, and ``IF NOT EXISTS`` alone would skip the
  rebuild and commit a revision whose index the planner never uses;
* an index the model metadata does not declare is one the next
  ``alembic revision --autogenerate`` proposes to drop.
"""

import importlib.util
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from preloop.models.models.base import Base

INDEX = "ix_flow_execution_resume_root"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261003_flow_execution_resume_root_idx.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "flow_execution_resume_root_idx_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade(migration, connection) -> None:
    """Run ``upgrade()`` the way the migration runner does, per revision.

    ``autocommit_block`` needs a migration transaction to step out of, so the
    connection's implicit one is closed and the context opens its own.
    """
    connection.commit()
    context = MigrationContext.configure(connection)
    with context.begin_transaction(), Operations.context(context):
        migration.upgrade()


def _index_valid(connection):
    return connection.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
        ),
        {"name": INDEX},
    ).scalar()


def test_upgrade_rebuilds_an_invalid_leftover_index(db_engine):
    """A retried upgrade must not keep a half-built index.

    CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction, so this
    uses its own autocommit connection instead of the rolled-back test
    session. It leaves the index valid, as it found it.
    """
    migration = _load_migration()
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        assert _index_valid(connection) is True, "run alembic upgrade head"
        # What an interrupted CONCURRENTLY build leaves behind.
        connection.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = CAST(:name AS regclass)"
            ),
            {"name": INDEX},
        )
        try:
            assert _index_valid(connection) is False
            _upgrade(migration, connection)
            assert _index_valid(connection) is True
        finally:
            # Never leave the shared test schema with a broken index, even
            # when the assertion above fails.
            with db_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as repair:
                if _index_valid(repair) is not True:
                    repair.execute(text(f"REINDEX INDEX {INDEX}"))


def test_upgrade_is_a_no_op_on_a_valid_index(db_engine):
    migration = _load_migration()
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        oid_before = connection.execute(
            text("SELECT CAST(:name AS regclass)::oid"), {"name": INDEX}
        ).scalar()
        _upgrade(migration, connection)
        oid_after = connection.execute(
            text("SELECT CAST(:name AS regclass)::oid"), {"name": INDEX}
        ).scalar()
    assert oid_after == oid_before


def test_model_metadata_declares_the_index(db_session):
    """Autogenerate must not see the migrated index as one to drop."""
    context = MigrationContext.configure(db_session.connection())
    diffs = compare_metadata(context, Base.metadata)

    def mentions_index(diff) -> bool:
        items = diff if isinstance(diff, list) else [diff]
        return any(
            getattr(item[1], "name", None) == INDEX
            for item in items
            if isinstance(item, tuple) and len(item) > 1
        )

    assert [diff for diff in diffs if mentions_index(diff)] == []
