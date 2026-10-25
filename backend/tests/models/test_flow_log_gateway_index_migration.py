"""The flow log gateway call index migration, and the model that mirrors it."""

import importlib.util
import uuid
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from preloop.models import models
from preloop.models.crud.flow_execution_log import crud_flow_execution_log
from preloop.models.models.base import Base

INDEX = "ix_flow_execution_log_gateway_call_ts"
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261112_flow_log_gateway_idx.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "flow_log_gateway_idx_migration", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_valid(connection):
    return connection.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
        ),
        {"name": INDEX},
    ).scalar()


def test_upgrade_is_idempotent_and_leaves_a_valid_index(db_engine):
    migration = _load_migration()
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        assert _index_valid(connection) is True, "run alembic upgrade head"
        # autocommit_block needs a migration transaction to step out of.
        connection.commit()
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()
        assert _index_valid(connection) is True


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


def test_compaction_walk_uses_the_partial_index(db_session, test_user):
    """The maintenance page query is served by the partial index."""
    flow = models.Flow(
        account_id=test_user.account_id,
        name=f"idx-{uuid.uuid4().hex[:8]}",
        prompt_template="x",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    db_session.execute(
        text(
            "INSERT INTO flow_execution_log (id, execution_id, timestamp, "
            "log_type, metadata) SELECT gen_random_uuid(), :eid, "
            "now() - make_interval(secs => g), CASE WHEN g % 50 = 0 THEN "
            "'model_gateway_call' ELSE 'agent_log_line' END, "
            "jsonb_build_object('line', g) FROM generate_series(1, 20000) g"
        ),
        {"eid": execution.id},
    )
    db_session.execute(text("ANALYZE flow_execution_log"))
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT") and (
            "pg_column_size" in statement
        ):
            plan = conn.exec_driver_sql("EXPLAIN " + statement, parameters)
            statements.append("\n".join(row[0] for row in plan))

    from sqlalchemy import event

    bind = db_session.connection()
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    event.listen(bind, "before_cursor_execute", capture)
    try:
        crud_flow_execution_log.gateway_call_batch(
            db_session, min_stored_bytes=0, limit=200
        )
    finally:
        event.remove(bind, "before_cursor_execute", capture)
    assert statements and INDEX in statements[0]
