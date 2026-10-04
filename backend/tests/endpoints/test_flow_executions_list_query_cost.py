"""The executions list and the flows stats must cost what their page costs.

Issue #1197: ``GET /flows/executions?limit=3`` took 8-15 s on an account with
a few hundred executions. Two statements behind every list request could not
use an index and read whole tables instead: the models-used aggregate matched
``coalesce(cast(api_usage.flow_execution_id), <session source id>)`` (every
gateway row of every account), and the resume chain rollup filtered on an
unindexed JSONB path (every trigger payload of the account). The windowed
spend of ``/flows?stats_since=`` hash-joined the window's executions against a
sequential scan of ``api_usage``.

Three properties are pinned here, none of them on wall-clock time:

* plan shape: with sequential scans disabled, no statement a list or stats
  request emits still needs one on the large tables (a statement that has no
  usable index keeps its Seq Scan even then);
* statement count: bounded, and the same for a page of 3 and a page of 25;
* parity: the rewritten aggregates return exactly what the old statements
  did on a fixture that exercises every attribution rule.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List

import pytest
from sqlalchemy import String, and_, case, cast, event, func, or_, select, text

from preloop.api.endpoints import flows
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
)
from preloop.models.crud.api_usage import (
    cache_split_columns,
    cache_split_from_row,
    exclude_replay_usage_condition,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.runtime_session import RuntimeSession
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

from tests.conftest import maybe_await

#: Tables whose size is not bounded by an account's page. A Seq Scan on any
#: of these in a list request is the regression this file guards.
LARGE_TABLES = (
    "api_usage",
    "flow_execution",
    "flow_execution_log",
    "runtime_session",
    "runtime_session_activity",
)


def _create_flow(db_session, test_user, name: str):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=test_user.account_id,
        ),
        account_id=test_user.account_id,
    )


def _execution(db_session, flow, *, started: datetime, resume_root=None, tokens=0):
    details: Dict[str, Any] = {"_subject": {"text": "PR #1", "url": None}}
    if resume_root is not None:
        details["_resume"] = {"resume_root": str(resume_root)}
    execution = crud_flow_execution.create(
        db_session,
        FlowExecutionCreate(
            flow_id=flow.id, status="SUCCEEDED", trigger_event_details=details
        ),
    )
    execution.start_time = started
    execution.total_tokens = tokens
    execution.estimated_cost = tokens / 10000
    db_session.flush()
    return execution


def _usage(db_session, test_user, *, alias, execution=None, session=None, **extra):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        account_id=str(test_user.account_id),
        flow_execution_id=str(execution.id) if execution is not None else None,
        flow_id=str(execution.flow_id) if execution is not None else None,
        runtime_session_id=str(session.id) if session is not None else None,
        model_alias=alias,
        provider_name="provider-" + (alias or "none"),
        prompt_tokens=100,
        completion_tokens=10,
        total_tokens=110,
        estimated_cost=extra.pop("estimated_cost", 0.002),
        **extra,
    )


@pytest.fixture
def seeded(db_session, test_user):
    """Two flows, a resume chain of depth 4, every attribution rule.

    - direct usage on several aliases (ordering by request count);
    - usage reached only through the execution's runtime session;
    - a row with both a flow_execution_id and that session (direct wins);
    - replay-validation and alias-less rows (excluded);
    - an unpriced row (NULL cost stays distinguishable);
    - an execution with no usage at all, inside and outside the window.
    """
    now = datetime.now(UTC).replace(tzinfo=None)
    flow_a = _create_flow(db_session, test_user, "Query cost A")
    flow_b = _create_flow(db_session, test_user, "Query cost B")
    executions = []
    for index in range(30):
        executions.append(
            _execution(
                db_session,
                flow_a if index % 3 else flow_b,
                started=now - timedelta(days=index * 2),
                tokens=1000 + index,
            )
        )
    publisher = executions[0]
    chain = [
        _execution(
            db_session,
            flow_a,
            started=now + timedelta(minutes=depth),
            resume_root=publisher.id,
            tokens=100 * depth,
        )
        for depth in range(1, 4)
    ]
    for index, execution in enumerate(executions[:20]):
        for _ in range(1 + index % 3):
            _usage(db_session, test_user, alias="model-a", execution=execution)
        if index % 2:
            _usage(db_session, test_user, alias="model-b", execution=execution)
        if index % 4 == 0:
            _usage(
                db_session,
                test_user,
                alias="model-a",
                execution=execution,
                meta_data={"purpose": "replay_validation"},
            )
        if index % 5 == 0:
            _usage(db_session, test_user, alias=None, execution=execution)
        if index == 7:
            _usage(
                db_session,
                test_user,
                alias="model-c",
                execution=execution,
                estimated_cost=None,
            )
    for index, execution in enumerate(executions[10:25]):
        session = crud_runtime_session.upsert_by_source(
            db_session,
            account_id=test_user.account_id,
            session_source_type="flow_execution",
            session_source_id=str(execution.id),
            started_at=now,
        )
        _usage(db_session, test_user, alias="model-s", session=session)
        _usage(db_session, test_user, alias="model-s", session=session)
        if index % 2:
            # Carries both attributions: counted once, for its own execution.
            _usage(
                db_session,
                test_user,
                alias="model-both",
                execution=executions[0],
                session=session,
            )
    db_session.flush()
    _seed_neighbour_noise(db_session)
    return {
        "flows": [flow_a, flow_b],
        "executions": executions + chain,
        "since": now - timedelta(days=30),
    }


def _seed_neighbour_noise(db_session) -> None:
    """Other tenants' rows, so a whole-table read costs what it does live.

    A test-sized table is cheapest to read whole, so without these the plan
    tests would measure the fixture, not the query. Generated server side and
    analyzed inside the test transaction (both roll back with it).
    """
    neighbour = crud_account.create(
        db_session,
        obj_in={"organization_name": "Query cost neighbour", "is_active": True},
    )
    db_session.execute(
        text(
            """
            WITH account AS (SELECT CAST(:account_id AS uuid) AS id),
            flow AS (
                INSERT INTO flow (id, account_id, name, prompt_template,
                                  agent_type, agent_config, allowed_mcp_servers,
                                  allowed_mcp_tools, is_preset, is_enabled)
                SELECT gen_random_uuid(), account.id, 'Neighbour flow ' || g,
                       'x', 'codex', '{}', '[]', '[]', false, true
                FROM account, generate_series(1, 30) g
                RETURNING id, account_id
            ), executions AS (
                INSERT INTO flow_execution (id, flow_id, status, start_time,
                                            created_at, updated_at,
                                            trigger_event_details)
                SELECT gen_random_uuid(), flow.id, 'SUCCEEDED',
                       now() - g * interval '1 minute', now(), now(),
                       jsonb_build_object('payload', repeat('x', 2000))
                FROM flow, generate_series(1, 100) g
                RETURNING id
            ), sessions AS (
                INSERT INTO runtime_session (id, account_id,
                                             session_source_type,
                                             session_source_id, started_at)
                SELECT gen_random_uuid(), account.id, 'claude_code',
                       'noise-' || g, now()
                FROM account, generate_series(1, 3000) g
                RETURNING id
            )
            INSERT INTO api_usage (id, account_id, runtime_session_id,
                                   flow_execution_id, endpoint, method,
                                   status_code, duration, action_type,
                                   model_alias, provider_name, estimated_cost,
                                   meta_data)
            SELECT gen_random_uuid(), (SELECT id FROM account),
                   (SELECT id FROM sessions LIMIT 1 OFFSET g % 3000),
                   CASE WHEN g % 2 = 0
                        THEN (SELECT id FROM executions LIMIT 1 OFFSET g % 3000)
                   END,
                   '/v1/messages', 'POST', 200, 0.1, 'model_gateway',
                   'noise-model', 'noise', 0.001,
                   jsonb_build_object('purpose', 'chat')
            FROM generate_series(1, 20000) g
            """
        ),
        {"account_id": str(neighbour.id)},
    )
    for table in ("api_usage", "flow_execution", "runtime_session", "flow"):
        db_session.execute(text(f"ANALYZE {table}"))


# Reference implementations: the statements as they were before #1197,
# kept verbatim so the parity tests compare against the real old answer.


def _old_models_used(db, execution_ids) -> Dict[str, List[Dict[str, Any]]]:
    ids = [str(execution_id) for execution_id in execution_ids if execution_id]
    session_execution_id = case(
        (
            and_(
                RuntimeSession.session_source_type == "flow_execution",
                RuntimeSession.session_source_id.isnot(None),
            ),
            RuntimeSession.session_source_id,
        ),
        else_=None,
    )
    execution_key = func.coalesce(
        cast(ApiUsage.flow_execution_id, String), session_execution_id
    )
    rows = (
        db.query(
            execution_key.label("execution_id"),
            ApiUsage.model_alias.label("model_alias"),
            func.max(ApiUsage.provider_name).label("provider_name"),
            func.count(ApiUsage.id).label("request_count"),
        )
        .outerjoin(RuntimeSession, ApiUsage.runtime_session_id == RuntimeSession.id)
        .filter(
            ApiUsage.action_type == "model_gateway",
            ApiUsage.model_alias.isnot(None),
            execution_key.in_(ids),
            exclude_replay_usage_condition(),
        )
        .group_by(execution_key, ApiUsage.model_alias)
        .order_by(
            execution_key, func.count(ApiUsage.id).desc(), ApiUsage.model_alias.asc()
        )
        .all()
    )
    result: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row.execution_id), []).append(
            {
                "model_alias": row.model_alias,
                "provider_name": row.provider_name,
                "request_count": int(row.request_count or 0),
            }
        )
    return result


def _old_chain_totals(db, account_id, roots, root_texts):
    resume_root_col = models.FlowExecution.trigger_event_details["_resume"][
        "resume_root"
    ].astext
    chain_key = func.coalesce(resume_root_col, cast(models.FlowExecution.id, String))
    return db.execute(
        select(
            chain_key.label("chain_root"),
            func.coalesce(func.sum(models.FlowExecution.total_tokens), 0),
            func.coalesce(func.sum(models.FlowExecution.estimated_cost), 0),
            func.count(models.FlowExecution.id),
        )
        .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
        .where(
            models.Flow.account_id == account_id,
            or_(
                models.FlowExecution.id.in_(list(roots)),
                resume_root_col.in_(root_texts),
            ),
        )
        .group_by(chain_key)
    ).all()


def _old_window_cost(db, flow_ids, start_date):
    fe = models.FlowExecution
    rows = (
        db.query(
            fe.flow_id,
            func.coalesce(func.sum(ApiUsage.estimated_cost), 0.0).label(
                "estimated_cost"
            ),
            func.coalesce(func.sum(ApiUsage.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(ApiUsage.completion_tokens), 0).label(
                "completion_tokens"
            ),
            func.coalesce(func.sum(ApiUsage.total_tokens), 0).label("total_tokens"),
            *cache_split_columns(),
        )
        .join(fe, ApiUsage.flow_execution_id == fe.id)
        .filter(
            fe.flow_id.in_(flow_ids),
            fe.start_time >= start_date,
            ApiUsage.action_type == "model_gateway",
            exclude_replay_usage_condition(),
        )
        .group_by(fe.flow_id)
        .all()
    )
    return {
        str(row.flow_id): (
            float(row.estimated_cost or 0.0),
            int(row.prompt_tokens),
            int(row.completion_tokens),
            int(row.total_tokens),
            cache_split_from_row(row),
        )
        for row in rows
    }


def test_models_used_matches_the_old_aggregate(db_session, test_user, seeded):
    ids = [execution.id for execution in seeded["executions"]]
    # A non-UUID id matched nothing before and must match nothing now.
    ids_with_junk = ids + ["not-a-uuid"]

    expected = _old_models_used(db_session, ids_with_junk)
    assert expected, "fixture must produce attributed usage"
    assert (
        crud_api_usage.get_models_used_for_executions(db_session, ids_with_junk)
        == expected
    )
    assert (
        crud_api_usage.get_models_used_for_executions(
            db_session, ids_with_junk, account_id=test_user.account_id
        )
        == expected
    )


def _chain_members(db, account_id, roots, root_texts):
    """Chain membership, unaggregated, by the original JSONB-path lookup."""
    resume_root_col = models.FlowExecution.trigger_event_details["_resume"][
        "resume_root"
    ].astext
    chain_key = func.coalesce(resume_root_col, cast(models.FlowExecution.id, String))
    return db.execute(
        select(chain_key, models.FlowExecution.id, models.FlowExecution.total_tokens)
        .join(models.Flow, models.Flow.id == models.FlowExecution.flow_id)
        .where(
            models.Flow.account_id == account_id,
            or_(
                models.FlowExecution.id.in_(list(roots)),
                resume_root_col.in_(root_texts),
            ),
        )
    ).all()


def test_resume_chain_totals_sum_each_members_displayed_figure(
    db_session, test_user, seeded
):
    """Chain totals equal the sum of the per-run figures the list shows (#1275).

    The reference is independent of the statement under test: membership by
    the JSONB path, each member's figure from ``get_execution_totals`` (the
    list's own projection, replay traffic excluded, stored rollup fallback).
    """
    from preloop.services.execution_metrics import get_execution_totals

    roots = {execution.id for execution in seeded["executions"]}
    root_texts = [str(root) for root in roots]
    # Membership agrees with the original aggregate.
    old_members = {
        str(key): int(members)
        for key, _, _, members in _old_chain_totals(
            db_session, test_user.account_id, roots, root_texts
        )
    }
    assert any(count == 4 for count in old_members.values())

    member_rows = _chain_members(db_session, test_user.account_id, roots, root_texts)
    per_run = get_execution_totals(db_session, [row[1] for row in member_rows])
    expected: Dict[str, List[Any]] = {}
    for chain_root, execution_id, stored_tokens in member_rows:
        total = per_run[str(execution_id)]
        usage = total["token_usage"]
        bucket = expected.setdefault(str(chain_root), [0, 0.0, 0, 0])
        bucket[0] += usage["total_tokens"] if usage else int(stored_tokens or 0)
        bucket[1] += float(total["estimated_cost"] or 0)
        bucket[2] += 1
        if total["has_gateway_usage"] and total["estimated_cost"] is None:
            bucket[3] += 1

    actual = {
        str(key): (int(tokens), float(cost), int(members), int(unpriced))
        for key, tokens, cost, members, unpriced in (
            crud_flow_execution.get_resume_chain_cost_totals(
                db_session,
                account_id=test_user.account_id,
                roots=list(roots),
                root_texts=root_texts,
            )
        )
    }
    assert {key: row[2] for key, row in actual.items()} == old_members
    assert set(actual) == set(expected)
    for key, (tokens, cost, members, unpriced) in actual.items():
        exp_tokens, exp_cost, exp_members, exp_unpriced = expected[key]
        assert tokens == exp_tokens
        assert cost == pytest.approx(exp_cost)
        assert members == exp_members
        assert unpriced == exp_unpriced


def test_window_stats_match_the_old_aggregate(db_session, seeded):
    flow_ids = [flow.id for flow in seeded["flows"]]
    since = seeded["since"]
    expected = _old_window_cost(db_session, flow_ids, since)
    assert len(expected) == 2

    stats = {
        str(row.flow_id): row
        for row in crud_flow_execution.get_execution_stats_for_flows(
            db_session, flow_ids, start_date=since
        )
    }
    for flow_id, (cost, prompt, completion, total, cache) in expected.items():
        row = stats[flow_id]
        assert row.cost == pytest.approx(cost)
        assert row.token_usage["prompt_tokens"] == prompt
        assert row.token_usage["completion_tokens"] == completion
        assert row.token_usage["total_tokens"] == total
        for key, value in cache.items():
            assert row.token_usage[key] == value


class _Statements:
    def __init__(self, db_session):
        self.engine = db_session.get_bind()
        self.rows: List[tuple] = []

    def __enter__(self):
        event.listen(self.engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, "before_cursor_execute", self._record)

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            self.rows.append((statement, parameters))


def _unbounded_scans_in(plan: str, *, paged: bool = False) -> List[str]:
    """Scan nodes in ``plan`` that read a large table without a condition.

    A ``Seq Scan`` on a large table, or an index scan with no ``Index Cond``
    (a full walk of an index, which is what the planner falls back to when
    sequential scans are disabled but no index matches the predicate). With
    ``paged`` an ordered index walk is allowed: under a LIMIT it stops at the
    page.
    """
    lines = plan.splitlines()
    found = []
    for index, line in enumerate(lines):
        node = line.strip().removeprefix("->").strip()
        if " Scan " not in f" {node} " and not node.startswith("Seq Scan"):
            continue
        if node.startswith("Bitmap Heap Scan"):
            # Bounded by its Bitmap Index Scan children, checked on their own.
            continue
        target = node.split(" on ", 1)[1].split()[0] if " on " in node else ""
        if not any(
            target == table or target.startswith(prefix)
            for table in LARGE_TABLES
            for prefix in (f"ix_{table}", f"{table}_pkey", f"uq_{table}")
        ):
            continue
        if node.startswith("Seq Scan"):
            found.append(node)
            continue
        indent = len(line) - len(line.lstrip())
        block = []
        for follow in lines[index + 1 :]:
            if len(follow) - len(
                follow.lstrip()
            ) <= indent or follow.strip().startswith("->"):
                break
            block.append(follow)
        if any("Index Cond" in detail for detail in block):
            continue
        if paged and not node.startswith("Bitmap"):
            # Walking an index in ORDER BY order under a LIMIT reads the page,
            # not the table: that is how the list query finds its rows.
            continue
        found.append(node)
    return found


def _sequential_scans(db_session, statements) -> List[str]:
    """Large-table reads a statement cannot bound with an index.

    ``enable_seqscan = off`` only penalises sequential scans, so a statement
    that still reads a large table without an index condition has no index
    that serves its predicate. That is exactly the property that makes a
    list request cost the whole table instead of its page.
    """
    found = []
    connection = db_session.connection()
    raw = connection.connection.driver_connection
    with raw.cursor() as cursor:
        # No hint against index walks is needed: the noise rows from the
        # fixture make a whole-table read the expensive choice it is in
        # production, so the planner only picks one when nothing else exists.
        cursor.execute("SET LOCAL enable_seqscan = off")
        try:
            for statement, parameters in statements:
                cursor.execute("EXPLAIN " + statement, parameters)
                plan = "\n".join(row[0] for row in cursor.fetchall())
                paged = " LIMIT " in f" {statement.upper()} "
                for node in _unbounded_scans_in(plan, paged=paged):
                    found.append(f"{node} :: {' '.join(statement.split())[:160]}")
        finally:
            cursor.execute("SET LOCAL enable_seqscan = on")
    return found


def test_unbounded_scan_detector():
    """The detector itself: conditioned index scans pass, the rest do not."""
    plan = """HashAggregate
  ->  Seq Scan on api_usage  (cost=0.00..1.00 rows=1 width=1)
        Filter: (model_alias IS NOT NULL)
  ->  Index Scan using ix_api_usage_timestamp on api_usage  (cost=0.1..9 rows=1)
        Filter: (x = 1)
  ->  Bitmap Heap Scan on flow_execution  (cost=1..2 rows=1 width=1)
        Recheck Cond: (id = ANY ('{}'::uuid[]))
        ->  Bitmap Index Scan on flow_execution_pkey  (cost=0..1 rows=1 width=0)
              Index Cond: (id = ANY ('{}'::uuid[]))
  ->  Seq Scan on flow  (cost=0.00..1.00 rows=1 width=1)"""
    assert _unbounded_scans_in(plan) == [
        "Seq Scan on api_usage  (cost=0.00..1.00 rows=1 width=1)",
        "Index Scan using ix_api_usage_timestamp on api_usage  (cost=0.1..9 rows=1)",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [3, 25])
async def test_executions_list_uses_indexes_and_bounded_statements(
    db_session, test_user, seeded, limit
):
    with _Statements(db_session) as log:
        rows = await maybe_await(
            flows.read_flow_executions(
                db=db_session, limit=limit, current_user=test_user
            )
        )
    assert len(rows) == limit
    # Statement count does not follow the page: aggregation is per page,
    # never per row.
    assert len(log.rows) <= 9
    assert _sequential_scans(db_session, log.rows) == []


@pytest.mark.asyncio
async def test_execution_detail_uses_indexes(db_session, test_user, seeded):
    execution = seeded["executions"][-1]
    with _Statements(db_session) as log:
        await maybe_await(
            flows.read_flow_execution(
                db=db_session, execution_id=execution.id, current_user=test_user
            )
        )
    scans = [
        scan
        for scan in _sequential_scans(db_session, log.rows)
        # The detail page's own log/metrics reads are out of scope here.
        if "model_alias" in scan or "resume_root" in scan
    ]
    assert scans == []


@pytest.mark.asyncio
async def test_flows_stats_window_uses_indexes(db_session, test_user, seeded):
    with _Statements(db_session) as log:
        await maybe_await(
            flows.read_flows(
                db=db_session, stats_since=seeded["since"], current_user=test_user
            )
        )
    scans = [
        scan
        for scan in _sequential_scans(db_session, log.rows)
        if "api_usage" in scan.split(" :: ")[0]
    ]
    assert scans == []


def test_resume_root_expression_matches_the_index(db_session):
    """The query spells the expression the migration indexed, byte for byte.

    An expression index is only used for a query that repeats its
    expression, so a drift between the two silently brings the scan back.
    """
    indexdef = db_session.execute(
        text(
            "SELECT pg_get_indexdef(c.oid) FROM pg_class c "
            "WHERE c.relname = 'ix_flow_execution_resume_root'"
        )
    ).scalar()
    assert indexdef is not None, "run alembic upgrade head"
    assert "'_resume'::text" in indexdef and "'resume_root'::text" in indexdef
