"""Tests for runtime session activity CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models import models
from preloop.models.crud.runtime_session_activity import (
    MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN,
    TOOL_CALL_SUCCESS_STATUSES,
    crud_runtime_session_activity,
)
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession


def test_log_agent_control_message_touches_managed_agent(
    db_session,
    create_account,
) -> None:
    """Operator control messages should refresh managed-agent presence."""
    account = create_account()
    principal_type = "openclaw"
    principal_id = "octavia-control"
    now = datetime.now(UTC)
    stale_seen_at = now - timedelta(hours=2)

    runtime_session = RuntimeSession(
        id=uuid4(),
        account_id=account.id,
        session_source_type=principal_type,
        session_source_id="workspace-1",
        session_reference="workspace-1",
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
        started_at=now,
        last_activity_at=stale_seen_at,
    )
    managed_agent = ManagedAgent(
        id=uuid4(),
        account_id=account.id,
        runtime_session_id=None,
        agent_kind=principal_type,
        session_source_type=principal_type,
        session_source_id=principal_id,
        display_name="Octavia",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=stale_seen_at,
    )
    db_session.add_all([runtime_session, managed_agent])
    db_session.commit()

    activity_timestamp = now + timedelta(minutes=5)
    crud_runtime_session_activity.log_agent_control_message(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        message="pause current task",
        status="sent",
        timestamp=activity_timestamp,
    )

    db_session.refresh(runtime_session)
    db_session.refresh(managed_agent)

    assert runtime_session.last_activity_at.replace(tzinfo=UTC) == activity_timestamp
    assert managed_agent.runtime_session_id == runtime_session.id
    assert managed_agent.last_seen_at.replace(tzinfo=UTC) == activity_timestamp


def test_log_agent_control_message_truncates_long_summary() -> None:
    """Audit summaries should cap oversized operator message text."""
    long_message = "x" * (MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN + 50)
    truncated = long_message[:MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN]
    assert len(truncated) == MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN


def test_tool_call_aggregates_accept_mixed_outcome_vocabulary(
    db_session,
    create_account,
) -> None:
    """Legacy success and new succeeded both count; NULL is neither."""
    account = create_account()
    principal_type = "openclaw"
    principal_id = "agg-vocab"
    now = datetime.now(UTC)
    runtime_session = RuntimeSession(
        id=uuid4(),
        account_id=account.id,
        session_source_type=principal_type,
        session_source_id="workspace-agg",
        session_reference="workspace-agg",
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
        started_at=now,
        last_activity_at=now,
    )
    flow = models.Flow(
        account_id=account.id,
        name="Agg vocab flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add_all([runtime_session, flow])
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()

    statuses = ("success", "succeeded", "refused", "failed", None)
    for status in statuses:
        crud_runtime_session_activity.log_tool_call(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            flow_execution_id=execution.id,
            server_name="preloop-mcp",
            tool_name="get_issue",
            status=status,  # type: ignore[arg-type]
            summary=None if status in TOOL_CALL_SUCCESS_STATUSES else "x",
            timestamp=now,
            commit=False,
        )
    db_session.flush()

    server_summary = crud_runtime_session_activity.get_server_summary_for_principal(
        db_session,
        account_id=str(account.id),
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
    )
    assert len(server_summary) == 1
    assert server_summary[0]["call_count"] == 5
    assert server_summary[0]["successful_calls"] == 2
    # refused + failed; NULL is neither success nor failure
    assert server_summary[0]["failed_calls"] == 2

    tool_summary = crud_runtime_session_activity.get_tool_summary_for_principal(
        db_session,
        account_id=str(account.id),
        runtime_principal_type=principal_type,
        runtime_principal_id=principal_id,
    )
    assert tool_summary[0]["successful_calls"] == 2
    assert tool_summary[0]["failed_calls"] == 2

    account_summary = crud_runtime_session_activity.get_tool_summary_for_account(
        db_session,
        account_id=str(account.id),
    )
    assert account_summary[0]["successful_calls"] == 2
    assert account_summary[0]["failed_calls"] == 2

    recent = crud_runtime_session_activity.get_recent_successful_tool_calls_by_flow_execution(
        db_session,
        flow_execution_id=execution.id,
        limit=10,
    )
    assert len(recent) == 2
    assert {row.status for row in recent} == {"success", "succeeded"}
