"""The executions list states the numbers the execution page states.

Staging showed the same finished run as "0 tool calls, $0.03" in the table
and "16 tool calls, $0.08" on its own page. The table printed the two rollup
columns the orchestrator writes once; the page shows the aggregation behind
``ExecutionMetricsService`` plus the tool calls the MCP server recorded. Two
numbers for one run is worse than either number alone, so the list projects
the same aggregate.

The flows list has the same disease one level up: runs counted from a sample
of recent executions, spend from a per-range endpoint, both under one "in the
last 30d" heading. ``stats_since`` answers both from the same window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from preloop.api.endpoints import flows
from preloop.models import schemas
from preloop.models.crud import (
    crud_api_usage,
    crud_flow,
    crud_flow_execution,
    crud_runtime_session,
    crud_runtime_session_activity,
)
from preloop.models.crud.api_usage import REPLAY_VALIDATION_PURPOSE
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.execution_metrics import ExecutionMetricsService

from tests.conftest import maybe_await


def _create_flow(db_session, test_user, name="Totals Flow"):
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


def _gateway_row(
    db_session, test_user, execution_id, cost, flow_id=None, meta_data=None
):
    return crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        flow_execution_id=str(execution_id),
        flow_id=str(flow_id) if flow_id else None,
        model_alias="openai/gpt-5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=cost,
        meta_data=meta_data,
    )


def _tool_calls(db_session, test_user, execution, count):
    session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=test_user.account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        session_reference=f"totals-{execution.id}",
        last_activity_at=datetime.now(UTC),
    )
    for index in range(count):
        crud_runtime_session_activity.log_tool_call(
            db_session,
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            flow_execution_id=execution.id,
            server_name="preloop",
            tool_name=f"tool_{index}",
            status="success",
            commit=False,
        )
    db_session.flush()


@pytest.mark.asyncio
async def test_list_row_states_the_execution_page_numbers(db_session, test_user):
    """One finished run, three views, one pair of numbers."""
    flow = _create_flow(db_session, test_user)
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    # The rollups as the orchestrator left them: tool calls it never saw, and
    # a cost written before the usage rows were priced.
    execution.tool_calls_count = 0
    execution.estimated_cost = 0.03
    db_session.flush()

    _tool_calls(db_session, test_user, execution, 16)
    _gateway_row(db_session, test_user, execution.id, 0.05)
    _gateway_row(db_session, test_user, execution.id, 0.03)

    listed = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    row = schemas.FlowExecutionListResponse.model_validate(listed[0])

    detail = await maybe_await(
        flows.read_flow_execution(
            db=db_session, execution_id=execution.id, current_user=test_user
        )
    )
    detail_row = schemas.FlowExecutionResponse.model_validate(detail)

    metrics = ExecutionMetricsService(db_session).get_execution_metrics(
        str(execution.id)
    )

    assert row.tool_calls_count == 16
    assert row.estimated_cost == pytest.approx(0.08)
    assert detail_row.tool_calls_count == row.tool_calls_count
    assert float(detail_row.estimated_cost) == pytest.approx(float(row.estimated_cost))
    # The page shows the larger of the row and /metrics, so the row has to be
    # at least what /metrics reports for the numbers to hold still.
    assert row.tool_calls_count >= metrics["tool_calls"]
    assert float(row.estimated_cost) == pytest.approx(metrics["estimated_cost"])


@pytest.mark.asyncio
async def test_projection_does_not_write_the_rollup_columns(db_session, test_user):
    """The response is a read model; the stored rollup keeps its own writer."""
    flow = _create_flow(db_session, test_user, name="Rollup Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    execution.estimated_cost = 0.03
    db_session.flush()
    _gateway_row(db_session, test_user, execution.id, 0.08)

    listed = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    assert float(listed[0].estimated_cost) == pytest.approx(0.08)

    db_session.commit()
    db_session.expire_all()
    stored = crud_flow_execution.get(db_session, id=execution.id)
    assert float(stored.estimated_cost) == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_run_without_gateway_usage_keeps_its_stored_cost(db_session, test_user):
    """No attributed usage means the rollup is still the best answer."""
    flow = _create_flow(db_session, test_user, name="No Usage Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    execution.tool_calls_count = 4
    execution.estimated_cost = 0.02
    db_session.flush()

    listed = await maybe_await(
        flows.read_flow_executions(
            db=db_session, flow_id=flow.id, current_user=test_user
        )
    )
    row = schemas.FlowExecutionListResponse.model_validate(listed[0])
    assert row.tool_calls_count == 4
    assert float(row.estimated_cost) == pytest.approx(0.02)


@pytest.mark.asyncio
async def test_flow_stats_window_answers_runs_failed_and_cost(db_session, test_user):
    """Runs, failures and spend for one window, from one query each."""
    flow = _create_flow(db_session, test_user, name="Window Flow")
    now = datetime.now(UTC)

    recent = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    failed = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="FAILED")
    )
    old = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    old.start_time = (now - timedelta(days=60)).replace(tzinfo=None)
    db_session.flush()

    _gateway_row(db_session, test_user, recent.id, 0.25, flow_id=flow.id)
    _gateway_row(db_session, test_user, failed.id, 0.08, flow_id=flow.id)
    # Replay-validation traffic is Preloop checking itself, not the flow's
    # spend. The Overview usage summary and the per-execution aggregation
    # both drop it, so this window has to drop it too or the same period
    # reads $0.33 in one place and $9.33 in another.
    _gateway_row(
        db_session,
        test_user,
        recent.id,
        9.00,
        flow_id=flow.id,
        meta_data={"purpose": REPLAY_VALIDATION_PURPOSE},
    )

    since = now - timedelta(days=30)
    result = await maybe_await(
        flows.read_flows(db=db_session, stats_since=since, current_user=test_user)
    )
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["runs"] == 2
    assert stats["failed"] == 1
    assert stats["cost"] == pytest.approx(0.33)
    assert stats["last_run_at"] is not None
    # The lifetime numbers other views read keep their meaning.
    assert stats["total_execs"] == 3
    assert stats["estimated_cost"] == pytest.approx(0.33)


@pytest.mark.asyncio
async def test_flow_stats_window_failed_counts_terminal_failures(db_session, test_user):
    """TIMEOUT/ERROR/TIMED_OUT are failures, the same as FAILED.

    The classifier that stamps ``failure_category`` already treats those
    four statuses as the failure set. Counting only ``FAILED`` would let a
    wall-clock timeout read as a quiet success in the flows-list window.
    """
    flow = _create_flow(db_session, test_user, name="Terminal Failures Flow")
    for status in ("FAILED", "ERROR", "TIMEOUT", "TIMED_OUT", "STOPPED", "SUCCEEDED"):
        crud_flow_execution.create(
            db_session, FlowExecutionCreate(flow_id=flow.id, status=status)
        )

    since = datetime.now(UTC) - timedelta(days=30)
    result = await maybe_await(
        flows.read_flows(db=db_session, stats_since=since, current_user=test_user)
    )
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["runs"] == 6
    assert stats["failed"] == 4


@pytest.mark.asyncio
async def test_flow_stats_window_cost_follows_execution_start_time(
    db_session, test_user
):
    """Window spend uses the run's start_time, not the usage timestamp.

    If this filtered ``ApiUsage.timestamp`` again, the old run's fresh
    usage row would inflate the 30d spend and the in-window run's
    backdated usage would vanish — two numbers for one period.
    """
    flow = _create_flow(db_session, test_user, name="Aligned Window Flow")
    now = datetime.now(UTC)

    recent = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    old = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    old.start_time = (now - timedelta(days=60)).replace(tzinfo=None)
    db_session.flush()

    in_window_usage = _gateway_row(
        db_session, test_user, recent.id, 0.25, flow_id=flow.id
    )
    in_window_usage.timestamp = (now - timedelta(days=40)).replace(tzinfo=None)
    _gateway_row(db_session, test_user, old.id, 1.00, flow_id=flow.id)
    db_session.flush()

    since = now - timedelta(days=30)
    result = await maybe_await(
        flows.read_flows(db=db_session, stats_since=since, current_user=test_user)
    )
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["runs"] == 1
    assert stats["cost"] == pytest.approx(0.25)
    assert stats["estimated_cost"] == pytest.approx(1.25)


@pytest.mark.asyncio
async def test_flow_stats_without_a_window_are_unchanged(db_session, test_user):
    """No stats_since, no window keys: the agents view still sees lifetime."""
    flow = _create_flow(db_session, test_user, name="Lifetime Flow")
    crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )

    result = await maybe_await(flows.read_flows(db=db_session, current_user=test_user))
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["total_execs"] == 1
    assert "runs" not in stats


@pytest.mark.asyncio
async def test_flow_detail_carries_the_execution_count(db_session, test_user):
    """The detail page needs the total to offer "View all N executions"."""
    flow = _create_flow(db_session, test_user, name="Detail Stats Flow")
    for _ in range(3):
        crud_flow_execution.create(
            db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
        )

    detail = await maybe_await(
        flows.read_flow(db=db_session, flow_id=flow.id, current_user=test_user)
    )

    assert detail.execution_stats["total_execs"] == 3
    assert detail.execution_stats["running_execs"] == 0


@pytest.mark.asyncio
async def test_flow_stats_window_carries_token_split_beside_cost(db_session, test_user):
    """The flows list states tokens before cost over the same window as cost."""
    now = datetime.now(UTC)
    flow = _create_flow(db_session, test_user, name="Window Tokens Flow")
    recent = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    old = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    old.start_time = (now - timedelta(days=60)).replace(tzinfo=None)
    db_session.flush()

    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(recent.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=1_000,
        completion_tokens=200,
        total_tokens=1_200,
        cache_read_tokens=600,
        cache_creation_tokens=100,
        estimated_cost=0.05,
    )
    # Outside the window: it must not move the tokens the row states.
    _gateway_row(db_session, test_user, old.id, 0.02, flow_id=flow.id)

    since = now - timedelta(days=30)
    result = await maybe_await(
        flows.read_flows(db=db_session, stats_since=since, current_user=test_user)
    )
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["token_usage"]["input_tokens"] == 1_000
    assert stats["token_usage"]["output_tokens"] == 200
    assert stats["token_usage"]["total_tokens"] == 1_200
    assert stats["token_usage"]["cache_read_tokens"] == 600
    assert stats["token_usage"]["cache_write_tokens"] == 100
    assert stats["token_usage"]["uncached_input_tokens"] == 300
    assert stats["cost"] == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_flow_stats_window_without_runs_has_null_token_usage(
    db_session, test_user
):
    """A flow with no run in the window states no tokens, not zero tokens."""
    _create_flow(db_session, test_user, name="Quiet Window Flow")

    since = datetime.now(UTC) + timedelta(days=1)
    result = await maybe_await(
        flows.read_flows(db=db_session, stats_since=since, current_user=test_user)
    )
    stats = [item.execution_stats for item in result][0]

    assert stats["token_usage"] is None


@pytest.mark.asyncio
async def test_flow_stats_without_a_window_carry_lifetime_tokens(db_session, test_user):
    """All-time spend comes with the all-time volume that earned it.

    The agents list asks for flows with no window and prints tokens before
    cost. Without this the row showed a dollar figure beside a permanently
    empty tokens cell, which reads as "nothing measured" on traffic that was
    measured.
    """
    flow = _create_flow(db_session, test_user, name="Lifetime Tokens Flow")
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/anthropic/v1/messages",
        method="POST",
        status_code=200,
        duration=0.2,
        account_id=str(test_user.account_id),
        flow_id=str(flow.id),
        flow_execution_id=str(execution.id),
        model_alias="anthropic/claude-sonnet-4",
        provider_name="anthropic",
        prompt_tokens=800,
        completion_tokens=100,
        total_tokens=900,
        cache_read_tokens=500,
        cache_creation_tokens=50,
        estimated_cost=0.04,
    )

    result = await maybe_await(flows.read_flows(db=db_session, current_user=test_user))
    stats = {
        str(item.id): item.execution_stats
        for item in result
        if str(item.id) == str(flow.id)
    }[str(flow.id)]

    assert stats["estimated_cost"] == pytest.approx(0.04)
    assert stats["token_usage"]["input_tokens"] == 800
    assert stats["token_usage"]["output_tokens"] == 100
    assert stats["token_usage"]["cache_read_tokens"] == 500
    assert stats["token_usage"]["cache_write_tokens"] == 50
    # 800 prompt tokens, 550 of them accounted for by the cache.
    assert stats["token_usage"]["uncached_input_tokens"] == 250

    detail = await maybe_await(
        flows.read_flow(db=db_session, flow_id=flow.id, current_user=test_user)
    )
    assert detail.execution_stats["token_usage"]["total_tokens"] == 900
