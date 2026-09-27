"""Hook linkage and seat usage for runs on host-exec profiles (DB-backed)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from preloop.models import models
from preloop.models.crud import (
    crud_api_usage,
    crud_managed_agent,
    crud_runtime_session,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.schemas.usage_import import UsageIngestRecord
from preloop.services.host_exec import apply_runner_completion_to_execution
from preloop.services.host_exec_usage import summarize_host_exec_usage
from preloop.services.usage_import import ingest_push_records

SESSION_ID = "0f8fad5b-d9cb-469f-a165-70867728950e"
OCCURRED_AT = datetime(2026, 9, 27, 12, 0)


def _host_execution(db_session, account_id, agent_type="copilot"):
    flow = models.Flow(
        account_id=account_id,
        name="PR Reviewer",
        prompt_template="Review",
        agent_type=agent_type,
        agent_config={"host_exec_profile": "copilot-seat"},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id, status="RUNNING")
    db_session.add(execution)
    db_session.flush()
    flow_session = crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="flow_execution",
        session_source_id=str(execution.id),
        runtime_principal_type="flow_execution",
        runtime_principal_id=str(execution.id),
        runtime_principal_name=flow.name,
        started_at=OCCURRED_AT,
    )
    db_session.flush()
    return flow, execution, flow_session


def _agent(db_session, account_id):
    agent = crud_managed_agent.upsert_from_runtime_session(
        db_session,
        account_id=str(account_id),
        runtime_session_id=None,
        session_source_type="desktop_agent",
        session_source_id=f"copilot-{uuid4()}",
        display_name="Copilot CLI",
        agent_kind="copilot_cli",
    )
    db_session.flush()
    return agent


def _ingest(db_session, test_user, agent, **fields):
    record = UsageIngestRecord(
        external_id=fields.pop("external_id", f"evt-{uuid4()}"),
        timestamp=OCCURRED_AT,
        conversation_id=SESSION_ID,
        **fields,
    )
    return ingest_push_records(
        db_session,
        account_id=str(test_user.account_id),
        user_id=str(test_user.id),
        agent=agent,
        records=[record],
        source="copilot_cli",
    )


def _rows(db_session, conversation_id=SESSION_ID):
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.conversation_id == conversation_id)
        .order_by(ApiUsage.timestamp)
        .all()
    )


def test_hook_records_link_to_verified_host_execution(db_session, test_user):
    account_id = test_user.account_id
    flow, execution, flow_session = _host_execution(db_session, account_id)
    agent = _agent(db_session, account_id)

    _ingest(
        db_session,
        test_user,
        agent,
        event_type="session_start",
        flow_execution_id=execution.id,
    )

    (row,) = _rows(db_session)
    assert row.flow_execution_id == execution.id
    assert row.flow_id == flow.id
    hook_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=account_id,
        session_source_type="copilot_cli",
        session_source_id=SESSION_ID,
    )
    assert hook_session.parent_session_id == flow_session.id
    # Hook events are not API requests of the execution.
    assert crud_api_usage.count_by_execution_timeframe(db_session, execution) == 0


def test_hook_records_ignore_foreign_or_non_host_executions(db_session, test_user):
    account_id = test_user.account_id
    agent = _agent(db_session, account_id)
    _, docker_execution, _ = _host_execution(db_session, account_id, "codex")

    _ingest(
        db_session,
        test_user,
        agent,
        event_type="session_start",
        flow_execution_id=docker_execution.id,
    )
    _ingest(
        db_session,
        test_user,
        agent,
        event_type="session_end",
        flow_execution_id=uuid4(),
    )

    rows = _rows(db_session)
    assert len(rows) == 2
    assert all(row.flow_execution_id is None for row in rows)


def test_completion_backlinks_hook_rows_and_records_premium_requests(
    db_session, test_user
):
    account_id = test_user.account_id
    flow, execution, flow_session = _host_execution(db_session, account_id)
    agent = _agent(db_session, account_id)
    # An older CLI pushed these without the execution id.
    _ingest(db_session, test_user, agent, event_type="session_start")
    _ingest(
        db_session,
        test_user,
        agent,
        model="claude-sonnet-4.5",
        input_tokens=120,
        output_tokens=40,
    )
    pending = {
        "completion_protocol": "host_exec",
        "agent_type": "copilot",
        "host_exec_profile": "copilot-seat",
    }
    result = {
        "status": "success",
        "harness": "copilot_cli",
        "session_id": SESSION_ID,
        "model": "claude-sonnet-4.5",
        "premium_requests": 3,
        "gateway_metered": False,
    }
    for _ in range(2):  # A replayed completion must not double count.
        apply_runner_completion_to_execution(
            db_session,
            execution,
            account_id=account_id,
            status="SUCCEEDED",
            error=None,
            result=result,
            message={"status": "SUCCEEDED"},
            pending_job=pending,
        )
    db_session.flush()

    rows = _rows(db_session)
    assert all(row.flow_execution_id == execution.id for row in rows)
    premium = [
        row
        for row in rows
        if (row.meta_data or {}).get("event_type") == "host_exec_result"
    ]
    assert len(premium) == 1
    assert premium[0].cost_source == "subscription"
    assert premium[0].estimated_cost is None
    assert premium[0].action_type == "imported_usage"
    assert premium[0].runtime_session_id == flow_session.id
    hook_session = crud_runtime_session.get_by_source(
        db_session,
        account_id=account_id,
        session_source_type="copilot_cli",
        session_source_id=SESSION_ID,
    )
    assert hook_session.parent_session_id == flow_session.id

    summary = summarize_host_exec_usage(
        db_session, account_id=account_id, execution_id=execution.id
    )
    assert summary["premium_requests"] == 3.0
    assert summary["event_count"] == 2
    assert summary["gateway_metered"] is False
    (session,) = summary["sessions"]
    assert session["conversation_id"] == SESSION_ID
    assert session["event_types"] == {"session_start": 1, "usage": 1}
    assert session["models"] == ["claude-sonnet-4.5"]
    # No gateway rows exist for the run.
    assert (
        crud_api_usage.get_gateway_usage_for_execution(db_session, execution.id)[
            "api_requests"
        ]
        == 0
    )
    assert crud_api_usage.count_by_execution_timeframe(db_session, execution) == 0


def test_completion_ignores_docker_leases_and_bad_premium_values(db_session, test_user):
    account_id = test_user.account_id
    _, execution, _ = _host_execution(db_session, account_id)
    base = {"status": "success", "harness": "copilot_cli", "session_id": SESSION_ID}
    cases = [
        ({"launch_version": 1, "agent_type": "codex"}, {**base, "premium_requests": 2}),
        (
            {"completion_protocol": "host_exec", "agent_type": "copilot"},
            {**base, "premium_requests": -1},
        ),
        (
            {"completion_protocol": "host_exec", "agent_type": "copilot"},
            {**base, "premium_requests": True},
        ),
        (
            {"completion_protocol": "host_exec", "agent_type": "cursor"},
            {**base, "premium_requests": 2},
        ),
    ]
    for pending, result in cases:
        apply_runner_completion_to_execution(
            db_session,
            execution,
            account_id=account_id,
            status="SUCCEEDED",
            error=None,
            result=result,
            message={},
            pending_job=pending,
        )
    db_session.flush()
    summary = summarize_host_exec_usage(
        db_session, account_id=account_id, execution_id=execution.id
    )
    assert summary["premium_requests"] is None
    assert summary["sessions"] == []


def test_charged_hook_rows_keep_their_cost_when_linked(db_session, test_user):
    account_id = test_user.account_id
    _, execution, _ = _host_execution(db_session, account_id)
    agent = _agent(db_session, account_id)
    _ingest(
        db_session,
        test_user,
        agent,
        model="gpt-5",
        charged_cost=Decimal("0.25"),
        flow_execution_id=execution.id,
        external_id="charged-1",
    )
    (row,) = _rows(db_session)
    assert row.flow_execution_id == execution.id
    assert row.estimated_cost == 0.25
    assert row.timestamp.replace(tzinfo=UTC) == OCCURRED_AT.replace(tzinfo=UTC)
