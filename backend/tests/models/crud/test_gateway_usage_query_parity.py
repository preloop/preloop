"""Parity tests for the aggregation-first gateway usage query shapes.

The session breakdown aggregates ``api_usage`` by (session, model) before it
joins the descriptive runtime-session/agent/flow tables, and the daily
timeseries groups before it orders. Both must still equal raw per-row
accounting. These tests pin that equivalence across the cases the query shape
is easy to get wrong: empty windows, UTC day boundaries, tenant isolation,
priced/unpriced/errored rows, replay exclusions, native vs legacy attribution,
per-user filters, ordering, limits, cache splits, and the 1:many agent join.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import (
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_managed_agent,
    crud_runtime_session,
)
from preloop.models.crud.api_usage import REPLAY_VALIDATION_PURPOSE
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate

BASE = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
ONE_DAY = timedelta(days=1)
_WINDOW_START = BASE - timedelta(days=2)
_WINDOW_END = BASE + timedelta(days=10)


def _create_session(db, account_id, **overrides):
    """Create (or fetch) a runtime session with stable test defaults."""
    data = {
        "account_id": account_id,
        "session_source_type": "claude_code",
        "session_source_id": f"src-{uuid4()}",
        "session_reference": None,
        "runtime_principal_type": "managed_agent",
        "runtime_principal_id": "agent-1",
        "runtime_principal_name": "Agent One",
        "started_at": BASE,
        "last_activity_at": BASE,
    }
    data.update(overrides)
    return crud_runtime_session.upsert_by_source(db, **data)


def _create_flow_execution(db, account_id, *, agent_session_reference=None):
    flow = crud_flow.create(
        db=db,
        flow_in=FlowCreate(
            name=f"Parity Flow {uuid4()}",
            prompt_template="Test",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="codex",
            agent_config={},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account_id,
        ),
        account_id=account_id,
    )
    execution = crud_flow_execution.create(
        db,
        FlowExecutionCreate(
            flow_id=flow.id,
            status="SUCCEEDED",
            agent_session_reference=agent_session_reference,
        ),
    )
    return flow, execution


def _log(
    db,
    account_id,
    *,
    when,
    runtime_session_id=None,
    flow_execution_id=None,
    flow_id=None,
    model_alias="gpt-4o",
    provider_name="openai",
    status_code=200,
    prompt_tokens=60,
    completion_tokens=40,
    total_tokens=100,
    estimated_cost=0.01,
    cache_read_tokens=None,
    cache_creation_tokens=None,
    meta_data=None,
    is_retry=None,
    runtime_principal_id=None,
    runtime_principal_type=None,
):
    """Log one gateway row and pin its timestamp for deterministic windows."""
    row = crud_api_usage.log_gateway_request(
        db,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=status_code,
        duration=0.1,
        account_id=str(account_id),
        runtime_session_id=(str(runtime_session_id) if runtime_session_id else None),
        flow_execution_id=(str(flow_execution_id) if flow_execution_id else None),
        flow_id=str(flow_id) if flow_id else None,
        model_alias=model_alias,
        provider_name=provider_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        cache_read_tokens=cache_read_tokens,
        cache_creation_tokens=cache_creation_tokens,
        meta_data=meta_data,
        is_retry=is_retry,
        runtime_principal_type=runtime_principal_type,
        runtime_principal_id=runtime_principal_id,
        runtime_principal_name=(
            f"Principal {runtime_principal_id}" if runtime_principal_id else None
        ),
    )
    row.timestamp = when
    db.flush()
    return row


def _by_session(db, account_id, **kwargs):
    kwargs.setdefault("start_date", _WINDOW_START)
    kwargs.setdefault("end_date", _WINDOW_END)
    return crud_api_usage.get_gateway_usage_by_session(
        db, account_id=str(account_id), **kwargs
    )


def _timeseries(db, account_id, **kwargs):
    kwargs.setdefault("start_date", _WINDOW_START)
    kwargs.setdefault("end_date", _WINDOW_END)
    return crud_api_usage.get_gateway_usage_timeseries(
        db, account_id=str(account_id), **kwargs
    )


def test_empty_window_returns_no_rows(db_session, create_account) -> None:
    """Both shapes return an empty list when nothing falls in the window."""
    account = create_account()

    assert _by_session(db_session, account.id) == []
    assert _timeseries(db_session, account.id) == []


def test_session_breakdown_aggregates_all_rows_before_limit(
    db_session, create_account
) -> None:
    """A limit of one must still sum every row of the winning session."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    for index in range(40):
        _log(
            db_session,
            account.id,
            when=BASE + timedelta(seconds=index),
            runtime_session_id=session.id,
            total_tokens=10,
            prompt_tokens=6,
            completion_tokens=4,
            estimated_cost=0.25,
        )

    rows = _by_session(db_session, account.id, limit=1)

    assert len(rows) == 1
    assert rows[0]["request_count"] == 40
    assert rows[0]["total_tokens"] == 400
    assert rows[0]["estimated_cost"] == 10.0


def test_timeseries_utc_day_boundaries_and_partial_days(
    db_session, create_account
) -> None:
    """Rows split on UTC midnight into whole and partial day buckets."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=datetime(2026, 6, 1, 23, 59, 59, 999999, tzinfo=UTC),
        runtime_session_id=session.id,
        total_tokens=10,
    )
    _log(
        db_session,
        account.id,
        when=datetime(2026, 6, 2, 0, 0, 0, tzinfo=UTC),
        runtime_session_id=session.id,
        total_tokens=20,
    )
    _log(
        db_session,
        account.id,
        when=datetime(2026, 6, 2, 23, 59, 59, tzinfo=UTC),
        runtime_session_id=session.id,
        total_tokens=30,
    )

    days = _timeseries(
        db_session,
        account.id,
        start_date=datetime(2026, 6, 1, tzinfo=UTC),
        end_date=datetime(2026, 6, 3, tzinfo=UTC),
    )

    assert [day["date"] for day in days] == ["2026-06-01", "2026-06-02"]
    assert [day["request_count"] for day in days] == [1, 2]
    assert [day["total_tokens"] for day in days] == [10, 50]


def test_session_breakdown_isolates_tenants(db_session, create_account) -> None:
    """One account's usage never appears in another account's breakdown."""
    account_a = create_account()
    account_b = create_account()
    session_a = _create_session(db_session, account_a.id)
    session_b = _create_session(db_session, account_b.id)
    db_session.flush()
    _log(
        db_session,
        account_a.id,
        when=BASE,
        runtime_session_id=session_a.id,
        total_tokens=11,
        estimated_cost=0.11,
    )
    _log(
        db_session,
        account_b.id,
        when=BASE,
        runtime_session_id=session_b.id,
        total_tokens=99,
        estimated_cost=0.99,
    )

    rows = _by_session(db_session, account_a.id)

    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 11
    assert rows[0]["estimated_cost"] == 0.11


def test_session_breakdown_priced_unpriced_and_errored(
    db_session, create_account
) -> None:
    """Errored and unpriced rows count volume but never invent cost."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session.id,
        status_code=200,
        total_tokens=100,
        estimated_cost=0.05,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=1),
        runtime_session_id=session.id,
        status_code=500,
        total_tokens=200,
        estimated_cost=None,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=2),
        runtime_session_id=session.id,
        status_code=429,
        total_tokens=50,
        estimated_cost=0.0,
    )

    rows = _by_session(db_session, account.id)

    assert len(rows) == 1
    assert rows[0]["request_count"] == 3
    assert rows[0]["total_tokens"] == 350
    assert rows[0]["estimated_cost"] == 0.05


def test_replay_validation_rows_excluded_from_both_shapes(
    db_session, create_account
) -> None:
    """Replay-validation traffic is not user-facing usage in either shape."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session.id,
        total_tokens=100,
        estimated_cost=0.1,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=1),
        runtime_session_id=session.id,
        total_tokens=999,
        estimated_cost=9.99,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )

    rows = _by_session(db_session, account.id)
    days = _timeseries(db_session, account.id)

    assert rows[0]["request_count"] == 1
    assert rows[0]["total_tokens"] == 100
    assert days[0]["request_count"] == 1
    assert days[0]["total_tokens"] == 100


def test_session_breakdown_counts_retries(db_session, create_account) -> None:
    """Retries consume real tokens, so these shapes count them by default."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session.id,
        total_tokens=100,
        estimated_cost=0.125,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(seconds=1),
        runtime_session_id=session.id,
        total_tokens=100,
        estimated_cost=0.125,
        is_retry=True,
    )

    rows = _by_session(db_session, account.id)
    days = _timeseries(db_session, account.id)

    assert rows[0]["request_count"] == 2
    assert rows[0]["total_tokens"] == 200
    assert days[0]["request_count"] == 2
    assert days[0]["total_tokens"] == 200


def test_native_and_legacy_session_attribution(db_session, create_account) -> None:
    """Session rows keep native runtime-session and legacy execution shapes."""
    account = create_account()
    native = _create_session(
        db_session,
        account.id,
        session_source_type="claude_code",
        session_source_id="native-source",
        runtime_principal_id="native-agent",
    )
    flow, execution = _create_flow_execution(
        db_session, account.id, agent_session_reference="legacy-ref"
    )
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=2),
        runtime_session_id=native.id,
        total_tokens=100,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=1),
        flow_execution_id=execution.id,
        flow_id=flow.id,
        total_tokens=40,
    )

    rows = _by_session(db_session, account.id)

    by_type = {row["session_source_type"]: row for row in rows}
    assert set(by_type) == {"claude_code", "flow_execution"}
    assert by_type["claude_code"]["runtime_session_id"] == str(native.id)
    assert by_type["claude_code"]["session_source_id"] == "native-source"
    legacy = by_type["flow_execution"]
    assert legacy["runtime_session_id"] is None
    assert legacy["flow_execution_id"] == str(execution.id)
    assert legacy["session_source_id"] == str(execution.id)
    assert legacy["session_reference"] == "legacy-ref"
    assert legacy["flow_name"] == flow.name


def test_session_breakdown_per_user_filter(db_session, create_account) -> None:
    """The per-user window only aggregates the requested principal."""
    account = create_account()
    session_one = _create_session(
        db_session,
        account.id,
        runtime_principal_id="agent-1",
        runtime_principal_name="Agent One",
    )
    session_two = _create_session(
        db_session,
        account.id,
        runtime_principal_id="agent-2",
        runtime_principal_name="Agent Two",
    )
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session_one.id,
        runtime_principal_id="agent-1",
        total_tokens=100,
    )
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session_two.id,
        runtime_principal_id="agent-2",
        total_tokens=200,
    )

    rows = _by_session(db_session, account.id, runtime_principal_id="agent-2")

    assert len(rows) == 1
    assert rows[0]["runtime_principal_id"] == "agent-2"
    assert rows[0]["total_tokens"] == 200


def test_session_breakdown_ordering_and_limit(db_session, create_account) -> None:
    """Rows order by last request descending, then request count descending."""
    account = create_account()
    older = _create_session(db_session, account.id, session_source_id="older")
    newer = _create_session(db_session, account.id, session_source_id="newer")
    tie = _create_session(db_session, account.id, session_source_id="tie")
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=older.id,
        total_tokens=10,
    )
    for _ in range(2):
        _log(
            db_session,
            account.id,
            when=BASE + timedelta(minutes=1),
            runtime_session_id=tie.id,
            total_tokens=10,
        )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=2),
        runtime_session_id=newer.id,
        total_tokens=10,
    )

    rows = _by_session(db_session, account.id, limit=2)

    assert [row["session_source_id"] for row in rows] == ["newer", "tie"]


def test_session_breakdown_matches_raw_accounting(db_session, create_account) -> None:
    """Every returned figure equals the raw rows summed in Python."""
    account = create_account()
    session = _create_session(db_session, account.id, session_source_id="accounted")
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session.id,
        model_alias="model-a",
        prompt_tokens=100,
        completion_tokens=50,
        total_tokens=150,
        estimated_cost=0.125,
        cache_read_tokens=30,
        cache_creation_tokens=0,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=1),
        runtime_session_id=session.id,
        model_alias="model-a",
        prompt_tokens=40,
        completion_tokens=10,
        total_tokens=50,
        estimated_cost=0.25,
        cache_read_tokens=None,
        cache_creation_tokens=None,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(minutes=2),
        runtime_session_id=session.id,
        model_alias="model-b",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.5,
    )

    rows = _by_session(db_session, account.id)
    by_model = {row["model_alias"]: row for row in rows}

    assert set(by_model) == {"model-a", "model-b"}
    model_a = by_model["model-a"]
    assert model_a["request_count"] == 2
    assert model_a["prompt_tokens"] == 140
    assert model_a["completion_tokens"] == 60
    assert model_a["total_tokens"] == 200
    assert model_a["estimated_cost"] == 0.375
    assert model_a["cache_read_tokens"] == 30
    assert model_a["cache_write_tokens"] == 0
    # Only the cache-covered row (prompt 100) contributes covered input.
    assert model_a["uncached_input_tokens"] == 70
    model_b = by_model["model-b"]
    assert model_b["request_count"] == 1
    assert model_b["total_tokens"] == 15
    assert model_b["cache_read_tokens"] == 0


def test_timeseries_matches_raw_accounting(db_session, create_account) -> None:
    """Daily buckets equal the raw per-day sums and stay date-ordered."""
    account = create_account()
    session = _create_session(db_session, account.id)
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(hours=1),
        runtime_session_id=session.id,
        total_tokens=100,
        estimated_cost=0.125,
    )
    _log(
        db_session,
        account.id,
        when=BASE + timedelta(hours=2),
        runtime_session_id=session.id,
        total_tokens=50,
        estimated_cost=0.25,
    )
    _log(
        db_session,
        account.id,
        when=BASE + ONE_DAY + timedelta(hours=1),
        runtime_session_id=session.id,
        total_tokens=25,
        estimated_cost=0.5,
    )

    days = _timeseries(db_session, account.id)

    assert days == [
        {
            "date": "2026-06-01",
            "request_count": 2,
            "total_tokens": 150,
            "input_tokens": 120,
            "output_tokens": 80,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost": 0.375,
        },
        {
            "date": "2026-06-02",
            "request_count": 1,
            "total_tokens": 25,
            "input_tokens": 60,
            "output_tokens": 40,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "estimated_cost": 0.5,
        },
    ]


def test_multiple_agents_keep_full_session_totals(db_session, create_account) -> None:
    """Each agent bound to a session reports the session's full totals."""
    account = create_account()
    session = _create_session(
        db_session,
        account.id,
        session_source_type="claude_code",
        session_source_id="shared-source",
    )
    first = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=account.id,
        runtime_session_id=session.id,
        session_source_type="claude_code",
        session_source_id="shared-source",
        display_name="First Agent",
    )
    second = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=account.id,
        runtime_session_id=session.id,
        session_source_type="claude_code",
        session_source_id="shared-source-2",
        display_name="Second Agent",
    )
    db_session.flush()
    _log(
        db_session,
        account.id,
        when=BASE,
        runtime_session_id=session.id,
        total_tokens=120,
        estimated_cost=0.12,
    )

    rows = _by_session(db_session, account.id)

    assert len(rows) == 2
    assert {row["agent_id"] for row in rows} == {str(first.id), str(second.id)}
    assert {row["agent_name"] for row in rows} == {"First Agent", "Second Agent"}
    assert all(row["request_count"] == 1 for row in rows)
    assert all(row["total_tokens"] == 120 for row in rows)
    assert all(row["estimated_cost"] == 0.12 for row in rows)
