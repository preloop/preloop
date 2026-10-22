"""Stored usage bootstrap is resumable and transactional with source writes."""

import importlib.util
from collections.abc import Generator
from types import ModuleType
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from preloop.models.backfills import AGENT_USAGE_TOTALS_BATCH, run_pending_backfills

MigrationFixture = tuple[str, UUID, ModuleType]


@pytest.fixture
def totals_schema(db_engine: Engine) -> Generator[MigrationFixture, None, None]:
    """Install the frozen migration in an isolated, minimal source schema."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261109_agent_usage_totals.py"
    )
    spec = importlib.util.spec_from_file_location("usage_totals_migration", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    schema = "usage_totals_" + uuid4().hex
    account = uuid4()
    with db_engine.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        connection.execute(text("CREATE TABLE account(id uuid PRIMARY KEY)"))
        connection.execute(text("INSERT INTO account VALUES(:id)"), {"id": account})
        connection.execute(
            text("""
            CREATE TABLE api_usage (
                id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid,
                runtime_principal_type varchar(64), runtime_principal_id varchar(255),
                runtime_session_id uuid, action_type text, status_code integer DEFAULT 200,
                prompt_tokens integer, completion_tokens integer, total_tokens integer,
                cache_read_tokens integer, cache_creation_tokens integer,
                estimated_cost double precision, cost_source text, meta_data jsonb,
                timestamp timestamp DEFAULT now(), model_alias varchar(255), provider_name varchar(255)
            )
        """)
        )
        # Historical requests predate the trigger. More than one batch tests
        # cursor progress and the completeness gate across commits.
        connection.execute(
            text("""
            INSERT INTO api_usage(account_id, runtime_principal_type, runtime_principal_id,
                                  action_type, estimated_cost, cost_source, meta_data)
            SELECT :account, 'codex', 'example-agent', 'model_gateway', 0.1,
                   'subscription', jsonb_build_object('api_equivalent_cost', 0.2)
            FROM generate_series(1, 501)
        """),
            {"account": account},
        )
        connection.execute(text(migration.DDL))
    try:
        yield schema, account, migration
    finally:
        with db_engine.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_backfill_resumes_without_double_counting_and_captures_live_writes(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """A resumed batch includes concurrent inserts once and gates partial data."""
    schema, account, migration = totals_schema
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        assert connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one() is False
        assert (
            connection.execute(
                text("SELECT sum(request_count) FROM agent_usage_total")
            ).scalar_one()
            == 500
        )
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        connection.execute(
            text("""
            INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,action_type,estimated_cost)
            VALUES(:account,'codex','example-agent','model_gateway',0.3)
        """),
            {"account": account},
        )
        # DDL can be repeated after an interrupted migration without erasing
        # the ledger or the progress cursor.
        connection.execute(text(migration.DDL))
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass
        # Replaying from the beginning must not add any request twice.
        connection.execute(
            text("UPDATE agent_usage_backfill SET last_usage_id=NULL, ready=false")
        )
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass
        row = connection.execute(
            text(
                "SELECT sum(request_count),sum(estimated_cost),sum(sub_equivalent) FROM agent_usage_total"
            )
        ).one()
        assert row[0] == 502
        assert float(row[1]) == pytest.approx(50.4)
        assert float(row[2]) == pytest.approx(100.2)
        assert (
            connection.execute(
                text("SELECT ready FROM agent_usage_backfill")
            ).scalar_one()
            is True
        )


def test_concurrent_writers_do_not_lose_counter_increments(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """Writers updating the same counter serialize their atomic increments."""
    schema, account, migration = totals_schema
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass

    def write(_: int) -> None:
        with db_engine.begin() as connection:
            connection.execute(text(f"SET LOCAL search_path TO {schema}"))
            connection.execute(
                text("""
                INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,action_type,estimated_cost)
                SELECT :account,'codex','example-agent','model_gateway',0.01 FROM generate_series(1,10)
            """),
                {"account": account},
            )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(write, range(8)))
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        assert (
            connection.execute(
                text("SELECT sum(request_count) FROM agent_usage_total")
            ).scalar_one()
            == 581
        )
        assert float(
            connection.execute(
                text("SELECT sum(estimated_cost) FROM agent_usage_total")
            ).scalar_one()
        ) == pytest.approx(50.9)


def test_actual_upgrade_and_downgrade_preserve_usage(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """Exercise Alembic's commit boundary and remove only derived data."""
    schema, account, migration = totals_schema
    with db_engine.connect() as connection:
        connection.execute(text(f"SET search_path TO {schema}"))
        connection.commit()
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()
        # The schema hook only registers the backfill; history is not scanned.
        assert connection.execute(
            text("SELECT ready, last_usage_id FROM agent_usage_backfill")
        ).one() == (False, None)
        assert (
            connection.execute(
                text("SELECT count(*) FROM agent_usage_counted")
            ).scalar_one()
            == 0
        )
        connection.commit()
        with Session(bind=connection) as db:
            results = run_pending_backfills(db)
        assert [(r.name, r.ready, r.batches) for r in results] == [("agents", True, 3)]
        assert (
            connection.execute(
                text("SELECT ready FROM agent_usage_backfill")
            ).scalar_one()
            is True
        )
        connection.commit()
        with context.begin_transaction(), Operations.context(context):
            migration.downgrade()
        assert (
            connection.execute(text("SELECT count(*) FROM api_usage")).scalar_one()
            == 501
        )
        assert (
            connection.execute(
                text("SELECT to_regclass('agent_usage_total')")
            ).scalar_one()
            is None
        )

        connection.execute(text("RESET search_path"))
        connection.commit()


@pytest.mark.parametrize("action_type", ["other", None])
def test_reclassifying_latest_request_restores_previous_model(
    db_engine: Engine, totals_schema: MigrationFixture, action_type: str | None
) -> None:
    """Removing a request from gateway usage also repairs latest metadata."""
    schema, account, migration = totals_schema
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass
        usage_id = connection.execute(
            text("""
            INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,
                action_type,estimated_cost,timestamp,model_alias)
            VALUES(:account,'codex','example-agent','model_gateway',1,now()+interval '1 day','newest')
            RETURNING id
            """),
            {"account": account},
        ).scalar_one()
        connection.execute(
            text("UPDATE api_usage SET action_type=:action_type WHERE id=:id"),
            {"id": usage_id, "action_type": action_type},
        )
        row = connection.execute(
            text(
                "SELECT request_count,latest_model_alias,last_usage_id FROM agent_usage_total"
            )
        ).one()
        assert row.request_count == 501
        assert row.latest_model_alias is None
        assert row.last_usage_id != usage_id


def test_bulk_writes_update_each_scope_once(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """Bulk import/reprice/delete must not rewrite one counter per request."""
    schema, account, migration = totals_schema
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass
        connection.execute(
            text("""
            CREATE TABLE counter_writes(id serial);
            CREATE FUNCTION count_counter_write() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN INSERT INTO counter_writes DEFAULT VALUES; RETURN NULL; END $$;
            CREATE TRIGGER count_counter_write AFTER INSERT OR UPDATE ON agent_usage_total
            FOR EACH ROW EXECUTE FUNCTION count_counter_write();
            INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,
                action_type,estimated_cost)
            SELECT account_id,'codex','bulk','model_gateway',0.25
            FROM api_usage LIMIT 1
        """)
        )
        connection.execute(text("TRUNCATE counter_writes"))
        connection.execute(
            text("""
            INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,
                action_type,estimated_cost)
            SELECT :account,'codex','bulk','model_gateway',0.25 FROM generate_series(1,2000)
        """),
            {"account": account},
        )
        assert (
            connection.execute(text("SELECT count(*) FROM counter_writes")).scalar_one()
            == 1
        )
        connection.execute(text("TRUNCATE counter_writes"))
        connection.execute(
            text(
                "UPDATE api_usage SET estimated_cost=0.5 WHERE runtime_principal_id='bulk'"
            )
        )
        assert (
            connection.execute(text("SELECT count(*) FROM counter_writes")).scalar_one()
            <= 3
        )
        row = connection.execute(
            text(
                "SELECT request_count,estimated_cost FROM agent_usage_total WHERE principal_id='bulk'"
            )
        ).one()
        assert row.request_count == 2001
        assert float(row.estimated_cost) == 1000.5
        connection.execute(text("TRUNCATE counter_writes"))
        connection.execute(
            text("DELETE FROM api_usage WHERE runtime_principal_id='bulk'")
        )
        assert (
            connection.execute(text("SELECT count(*) FROM counter_writes")).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM agent_usage_total WHERE principal_id='bulk'")
            ).scalar_one()
            == 0
        )


def test_bulk_writes_keep_scopes_and_latest_metadata_separate(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """One statement aggregates and repairs two scopes independently."""
    schema, account, migration = totals_schema
    with db_engine.begin() as connection:
        connection.execute(text(f"SET LOCAL search_path TO {schema}"))
        while not connection.execute(text(AGENT_USAGE_TOTALS_BATCH)).scalar_one():
            pass
        rows = connection.execute(
            text("""
            INSERT INTO api_usage(account_id,runtime_principal_type,runtime_principal_id,
                action_type,estimated_cost,timestamp,model_alias)
            SELECT :account,'codex',scope,'model_gateway',cost,
                timestamp '2026-01-01' + age * interval '1 day',alias
            FROM (VALUES ('a',1,0,'a-old'),('a',2,1,'a-new'),
                         ('b',3,0,'b-old'),('b',4,1,'b-new'))
                 AS v(scope,cost,age,alias)
            RETURNING id,model_alias
        """),
            {"account": account},
        ).all()
        ids = {row.model_alias: row.id for row in rows}

        def assert_totals(costs: dict[str, int], suffix: str, count: int) -> None:
            totals = connection.execute(
                text("""
                SELECT principal_id,request_count,estimated_cost,last_usage_id,latest_model_alias
                FROM agent_usage_total WHERE principal_id IN ('a','b')
            """)
            ).all()
            assert len(totals) == 2
            for row in totals:
                assert row.request_count == count
                assert row.estimated_cost == costs[row.principal_id]
                alias = f"{row.principal_id}-{suffix}"
                assert row.last_usage_id == ids[alias]
                assert row.latest_model_alias == alias

        assert_totals({"a": 3, "b": 7}, "new", 2)
        connection.execute(
            text(
                "UPDATE api_usage SET estimated_cost=estimated_cost*2 WHERE runtime_principal_id IN ('a','b')"
            )
        )
        assert_totals({"a": 6, "b": 14}, "new", 2)
        connection.execute(
            text("DELETE FROM api_usage WHERE model_alias IN ('a-new','b-new')")
        )
        assert_totals({"a": 2, "b": 6}, "old", 1)


def test_upgrade_keeps_a_completed_backfill_complete(
    db_engine: Engine, totals_schema: MigrationFixture
) -> None:
    """Re-applying the revision on a finished database must not rescan history."""
    schema, _account, migration = totals_schema
    with db_engine.connect() as connection:
        connection.execute(text(f"SET search_path TO {schema}"))
        connection.commit()
        with Session(bind=connection) as db:
            assert run_pending_backfills(db)[0].ready is True
        before = connection.execute(
            text("SELECT last_usage_id FROM agent_usage_backfill")
        ).scalar_one()
        connection.commit()
        context = MigrationContext.configure(connection)
        with context.begin_transaction(), Operations.context(context):
            migration.upgrade()
        assert connection.execute(
            text("SELECT ready, last_usage_id FROM agent_usage_backfill")
        ).one() == (True, before)
        connection.commit()
        with Session(bind=connection) as db:
            assert run_pending_backfills(db) == []
        connection.execute(text("RESET search_path"))
        connection.commit()


def _totals(connection: object, schema: str) -> list[tuple[object, ...]]:
    """Every stored total, minus surrogate keys and bookkeeping timestamps."""
    return connection.execute(  # type: ignore[attr-defined]
        text(f"""
        SELECT account_id, principal_type, principal_id, session_id,
               request_count, success_count, error_count, prompt_tokens,
               completion_tokens, total_tokens, cache_read_tokens,
               cache_write_tokens, covered_prompt_tokens, estimated_cost,
               sub_requests, sub_prompt, sub_completion, sub_total, sub_covered,
               sub_equivalent, last_request_at, last_usage_id,
               latest_model_alias, latest_provider_name
        FROM {schema}.agent_usage_total
        ORDER BY principal_id, session_id NULLS FIRST
        """)
    ).all()


_SYNTHETIC_USAGE = """
INSERT INTO api_usage(id, account_id, runtime_principal_type, runtime_principal_id,
    runtime_session_id, action_type, status_code, prompt_tokens, completion_tokens,
    total_tokens, cache_read_tokens, cache_creation_tokens, estimated_cost,
    cost_source, meta_data, timestamp, model_alias, provider_name)
SELECT md5('row' || g)::uuid, :account, 'codex', 'agent-' || (g % 3),
    CASE WHEN g % 4 = 0 THEN NULL ELSE md5('session' || (g % 5))::uuid END,
    CASE WHEN g % 11 = 0 THEN 'other' ELSE 'model_gateway' END,
    CASE WHEN g % 7 = 0 THEN 500 ELSE 200 END,
    g, g * 2, g * 3, g % 13, g % 17, g / 100.0,
    CASE WHEN g % 2 = 0 THEN 'subscription' ELSE 'api' END,
    jsonb_build_object('api_equivalent_cost', g / 50.0),
    timestamp '2026-10-01' + g * interval '1 minute',
    'model-' || (g % 4), 'provider-' || (g % 2)
FROM generate_series(1, 1203) g
"""


def test_runner_produces_the_same_totals_as_live_capture(db_engine: Engine) -> None:
    """History counted by the runner equals the same rows counted by triggers.

    One schema receives the synthetic requests after the revision is
    installed (the triggers count them as they arrive); the other receives
    them before (the post-deploy runner counts them). Both must store
    identical totals.
    """
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions/20261109_agent_usage_totals.py"
    )
    spec = importlib.util.spec_from_file_location("usage_totals_parity", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    account = uuid4()
    live, backfilled = ("usage_live_" + uuid4().hex, "usage_runner_" + uuid4().hex)
    source = """
        CREATE TABLE account(id uuid PRIMARY KEY);
        CREATE TABLE api_usage (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(), account_id uuid,
            runtime_principal_type varchar(64), runtime_principal_id varchar(255),
            runtime_session_id uuid, action_type text, status_code integer DEFAULT 200,
            prompt_tokens integer, completion_tokens integer, total_tokens integer,
            cache_read_tokens integer, cache_creation_tokens integer,
            estimated_cost double precision, cost_source text, meta_data jsonb,
            timestamp timestamp DEFAULT now(), model_alias varchar(255),
            provider_name varchar(255)
        )
    """
    try:
        for schema, history_first in ((live, False), (backfilled, True)):
            with db_engine.begin() as connection:
                connection.execute(text(f"CREATE SCHEMA {schema}"))
                connection.execute(text(f"SET LOCAL search_path TO {schema}"))
                connection.execute(text(source))
                connection.execute(
                    text("INSERT INTO account VALUES(:id)"), {"id": account}
                )
                if history_first:
                    connection.execute(text(_SYNTHETIC_USAGE), {"account": account})
                connection.execute(text(migration.DDL))
                if not history_first:
                    connection.execute(text(_SYNTHETIC_USAGE), {"account": account})
        with db_engine.connect() as connection:
            connection.execute(text(f"SET search_path TO {backfilled}"))
            connection.commit()
            with Session(bind=connection) as db:
                (result,) = run_pending_backfills(db)
            assert result.ready and result.batches > 1 and not result.errors
            connection.execute(text("RESET search_path"))
            connection.commit()
        with db_engine.connect() as connection:
            expected = _totals(connection, live)
            assert len(expected) > 3
            assert _totals(connection, backfilled) == expected
    finally:
        with db_engine.begin() as connection:
            for schema in (live, backfilled):
                connection.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
