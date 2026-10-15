"""The admin activity index migration, and the models that mirror it.

The admin accounts list needs ``ix_event_account_timestamp`` (latest event
per account) and ``ix_api_usage_gateway_spend`` (trailing gateway spend per
account). Two ways to lose them silently are pinned:

* a CONCURRENTLY build that failed part way leaves an INVALID index; the
  migration runner retries, and ``IF NOT EXISTS`` alone would skip the
  rebuild and commit a revision whose index the planner never uses;
* an index the model metadata does not declare is one the next
  ``alembic revision --autogenerate`` proposes to drop.
"""

import importlib.util
from pathlib import Path

import pytest

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from preloop.models.models.base import Base

INDEXES = ("ix_event_account_timestamp", "ix_api_usage_gateway_spend")
MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "preloop"
    / "models"
    / "alembic"
    / "versions"
    / "20261013_admin_activity_indexes.py"
)


def _load_migration():
    """Import the migration module by path (its name starts with digits)."""
    spec = importlib.util.spec_from_file_location(
        "admin_activity_indexes_migration", MIGRATION_PATH
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


def _index_valid(connection, index):
    return connection.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname = :name"
        ),
        {"name": index},
    ).scalar()


# Only the event index is rebuilt here: both go through the same loop in
# upgrade(), and in CI an earlier test can leave a transaction holding a lock
# on api_usage, which makes DROP INDEX CONCURRENTLY wait forever (job timed
# out on the first run of this PR).
@pytest.mark.parametrize("index", ["ix_event_account_timestamp"])
def test_upgrade_rebuilds_an_invalid_leftover_index(db_engine, index):
    """A retried upgrade must not keep a half-built index.

    CREATE/DROP INDEX CONCURRENTLY cannot run inside a transaction, so this
    uses its own autocommit connection instead of the rolled-back test
    session. It leaves the index valid, as it found it.
    """
    migration = _load_migration()
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        assert _index_valid(connection, index) is True, "run alembic upgrade head"
        # What an interrupted CONCURRENTLY build leaves behind.
        connection.execute(
            text(
                "UPDATE pg_index SET indisvalid = false "
                "WHERE indexrelid = CAST(:name AS regclass)"
            ),
            {"name": index},
        )
        try:
            assert _index_valid(connection, index) is False
            _upgrade(migration, connection)
            assert _index_valid(connection, index) is True
        finally:
            # Never leave the shared test schema with a broken index, even
            # when the assertion above fails.
            with db_engine.connect().execution_options(
                isolation_level="AUTOCOMMIT"
            ) as repair:
                if _index_valid(repair, index) is not True:
                    repair.execute(text(f"REINDEX INDEX {index}"))


def test_upgrade_is_a_no_op_on_valid_indexes(db_engine):
    migration = _load_migration()
    query = text("SELECT CAST(:name AS regclass)::oid")
    with db_engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        before = [connection.execute(query, {"name": i}).scalar() for i in INDEXES]
        _upgrade(migration, connection)
        after = [connection.execute(query, {"name": i}).scalar() for i in INDEXES]
    assert after == before


def test_model_metadata_declares_the_indexes(db_session):
    """Autogenerate must not see the migrated index as one to drop."""
    context = MigrationContext.configure(db_session.connection())
    diffs = compare_metadata(context, Base.metadata)

    def mentions_index(diff) -> bool:
        items = diff if isinstance(diff, list) else [diff]
        return any(
            getattr(item[1], "name", None) in INDEXES
            for item in items
            if isinstance(item, tuple) and len(item) > 1
        )

    assert [diff for diff in diffs if mentions_index(diff)] == []


def _seed_usage_and_events(db_session):
    """Three tenants among many noisy neighbours, then fresh statistics.

    Most rows belong to other accounts or fall outside the 30-day window, so
    the planner's choice reflects the real table shape rather than a tiny
    table where any index looks equally cheap.
    """
    from preloop.models.crud import crud_account

    accounts = [
        crud_account.create(db_session, obj_in={"organization_name": f"idx-{n}"})
        for n in range(3)
    ]
    ids = ",".join(f"'{account.id}'::uuid" for account in accounts)
    db_session.execute(
        text(
            "INSERT INTO api_usage (id, account_id, endpoint, method, status_code, "
            "duration, action_type, estimated_cost, timestamp, meta_data) "
            "SELECT gen_random_uuid(), "
            f"CASE WHEN g % 50 = 0 THEN (ARRAY[{ids}])[1 + g % 3] END, "
            "'/v1/messages', 'POST', 200, 1.0, "
            "CASE WHEN g % 4 = 0 THEN 'mcp_tool' ELSE 'model_gateway' END, "
            "0.01, now() - make_interval(hours => g % 2400), "
            "jsonb_build_object('client_request_id', md5(g::text)) "
            "FROM generate_series(1, 20000) g"
        )
    )
    db_session.execute(
        text(
            "INSERT INTO event (id, account_id, event_type, timestamp) "
            "SELECT gen_random_uuid(), "
            f"CASE WHEN g % 50 = 0 THEN (ARRAY[{ids}])[1 + g % 3] END, "
            "'page_view', now() - make_interval(mins => g) "
            "FROM generate_series(1, 20000) g"
        )
    )
    db_session.execute(text("ANALYZE api_usage"))
    db_session.execute(text("ANALYZE event"))
    return accounts


def _explain(db_session, statement) -> str:
    compiled = statement.compile(
        dialect=db_session.get_bind().dialect,
        compile_kwargs={"literal_binds": True},
    )
    # Tiny test tables would otherwise always seq scan.
    db_session.execute(text("SET LOCAL enable_seqscan = off"))
    db_session.execute(text("SET LOCAL enable_bitmapscan = off"))
    return "\n".join(row[0] for row in db_session.execute(text(f"EXPLAIN {compiled}")))


def test_gateway_spend_is_an_index_only_scan(db_session):
    """The spend sum must be served by the covering partial index alone.

    ``ix_api_usage_gateway_spend`` is only usable while its WHERE clause
    matches ``exclude_replay_usage_condition`` exactly, and it avoids the heap
    (and the wide meta_data column) only while it covers estimated_cost.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func, select

    from preloop.models.crud.api_usage import exclude_replay_usage_condition
    from preloop.models.models import ApiUsage

    accounts = _seed_usage_and_events(db_session)
    # Narrower non-covering indexes match account, action and time without
    # covering estimated_cost. On a table whose visibility map is cold, those
    # are cheaper than an index-only scan, so the planner never touches the
    # covering indexes. Hide them for this EXPLAIN; the drops roll back with
    # the test transaction.
    for index in (
        "ix_api_usage_account_action_ts",
        "ix_api_usage_account_principal_id_ts",
    ):
        db_session.execute(text(f"DROP INDEX {index}"))
    query = (
        select(ApiUsage.account_id, func.sum(ApiUsage.estimated_cost))
        .where(
            ApiUsage.account_id.in_([accounts[0].id]),
            ApiUsage.action_type == "model_gateway",
            ApiUsage.timestamp >= datetime.now(timezone.utc) - timedelta(days=30),
            exclude_replay_usage_condition(),
        )
        .group_by(ApiUsage.account_id)
    )
    plan = _explain(db_session, query)
    # ix_api_usage_gateway_totals (20261014) has the same predicate and also
    # covers estimated_cost; either covering index keeps it off the heap.
    assert any(
        f"Index Only Scan using {index}" in plan
        for index in ("ix_api_usage_gateway_spend", "ix_api_usage_gateway_totals")
    ), plan


def test_latest_event_per_account_uses_the_composite_index(db_session):
    """The admin last-activity value is one backward probe per account."""
    from sqlalchemy import func, select

    from preloop.models.models import Account, Event

    accounts = _seed_usage_and_events(db_session)
    last_activity = (
        select(func.max(Event.timestamp))
        .where(Event.account_id == Account.id)
        .correlate(Account)
        .scalar_subquery()
    )
    query = select(Account.id, last_activity).where(
        Account.id.in_([account.id for account in accounts])
    )
    plan = _explain(db_session, query)
    assert "ix_event_account_timestamp" in plan, plan


def test_single_column_account_index_is_replaced(db_engine):
    """ix_event_account_id is redundant once the composite leads with it."""
    with db_engine.connect() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT indexname FROM pg_indexes WHERE tablename = 'event'")
            )
        }
    assert "ix_event_account_timestamp" in names
    assert "ix_event_account_id" not in names
