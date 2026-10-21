"""Tests for runtime session activity CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from sqlalchemy import event
from sqlalchemy.orm import Session
import pytest

from preloop.models import models
from preloop.models.crud.runtime_session_activity import (
    MAX_AGENT_CONTROL_MESSAGE_SUMMARY_LEN,
    TOOL_CALL_SUCCESS_STATUSES,
    crud_runtime_session_activity,
)
from preloop.models.models.managed_agent import ManagedAgent
from preloop.models.models.runtime_session import RuntimeSession


def test_bounded_gateway_payload_pages_preserve_equal_timestamp_order(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """A byte-limited read never transfers a long captured history at once."""
    account = create_account()
    at = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(runtime_session)
    db_session.flush()
    activities = [
        models.RuntimeSessionActivity(
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            activity_type="model_gateway_call",
            timestamp=at,
            metadata_={"api_usage_id": str(index), "request": "x" * 350},
        )
        for index in range(45)
    ]
    db_session.add_all(activities)
    db_session.flush()
    seen = []
    cursor = None
    for _ in range(45):
        rows, skipped, next_cursor, complete = (
            crud_runtime_session_activity.list_bounded_model_gateway_call_payloads_for_session(
                db_session,
                account_id=account.id,
                runtime_session_id=runtime_session.id,
                start_date=at,
                end_date=at,
                after=cursor,
                max_payload_bytes=1_000,
            )
        )
        assert not skipped
        assert 0 < len(rows) <= 2
        seen.extend(row.id for row in rows)
        if complete:
            break
        assert next_cursor is not None and next_cursor != cursor
        cursor = next_cursor
    assert len(seen) == 45
    assert set(seen) == {activity.id for activity in activities}


def test_bounded_gateway_payload_reports_oversized_capture(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """Oversized evidence is explicit and cannot masquerade as a full page."""
    account = create_account()
    at = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(runtime_session)
    db_session.flush()
    oversized = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        activity_type="model_gateway_call",
        timestamp=at,
        metadata_={"request": "x" * 2_000},
    )
    db_session.add(oversized)
    db_session.flush()
    rows, skipped, cursor, complete = (
        crud_runtime_session_activity.list_bounded_model_gateway_call_payloads_for_session(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            start_date=at,
            end_date=at,
            max_payload_bytes=1_000,
        )
    )
    assert rows == []
    assert skipped == [(oversized.id, at)]
    assert cursor == (at, oversized.id)
    assert not complete


@pytest.mark.parametrize("mutation", ["growth", "timestamp"])
def test_bounded_gateway_payload_rechecks_row_before_transfer(
    db_session: Session,
    create_account: Callable[..., models.Account],
    mutation: str,
) -> None:
    """A changed row cannot cross the connection under a stale size/cursor."""
    account = create_account()
    at = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(runtime_session)
    db_session.flush()
    activity = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        activity_type="model_gateway_call",
        timestamp=at,
        metadata_={"request": "small"},
    )
    db_session.add(activity)
    db_session.flush()
    connection = db_session.connection()
    changed = False

    def grow_after_probe(*args: object) -> None:
        nonlocal changed
        statement = str(args[2])
        if changed or "octet_length" not in statement or "ORDER BY" not in statement:
            return
        changed = True
        values = (
            {"metadata_": {"request": "x" * 5_000}}
            if mutation == "growth"
            else {"timestamp": at + timedelta(seconds=1)}
        )
        db_session.query(models.RuntimeSessionActivity).filter(
            models.RuntimeSessionActivity.id == activity.id
        ).update(values)
        db_session.flush()

    event.listen(connection, "after_cursor_execute", grow_after_probe)
    try:
        rows, skipped, _, complete = (
            crud_runtime_session_activity.list_bounded_model_gateway_call_payloads_for_session(
                db_session,
                account_id=account.id,
                runtime_session_id=runtime_session.id,
                start_date=at,
                end_date=at,
                max_payload_bytes=1_000,
            )
        )
    finally:
        event.remove(connection, "after_cursor_execute", grow_after_probe)
    assert changed
    assert rows == []
    assert skipped == [(activity.id, at)]
    assert not complete


def test_timeline_hook_timestamps_do_not_displace_visible_activity(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """Hook rows stay queryable without consuming the visible timeline limit."""
    account = create_account()
    now = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id="timeline-hooks",
        started_at=now - timedelta(minutes=5),
    )
    db_session.add(runtime_session)
    db_session.flush()
    note_id = str(uuid4())
    note = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        activity_type="agent_control_message",
        timestamp=now - timedelta(minutes=1),
        metadata_={"kind": "operator_note", "note_id": note_id},
    )
    tool = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        activity_type="tool_call",
        timestamp=now,
        tool_name="get_issue",
    )
    hooks = [
        models.RuntimeSessionActivity(
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            activity_type=kind,
            timestamp=now + timedelta(minutes=index + 1),
        )
        for index, kind in enumerate(("user_prompt", "turn_end", "session_end"))
    ]
    db_session.add_all([note, tool, *hooks])
    db_session.flush()
    query = {
        "account_id": str(account.id),
        "runtime_session_id": str(runtime_session.id),
    }

    timeline = crud_runtime_session_activity.list_timeline_rows_for_runtime_session(
        db_session, **query, limit=1
    )
    assert [row.id for row in timeline] == [tool.id]
    focused = crud_runtime_session_activity.list_timeline_rows_for_runtime_session(
        db_session, **query, limit=1, operator_note_id=note_id
    )
    assert [row.id for row in focused] == [tool.id, note.id]
    raw = crud_runtime_session_activity.list_for_runtime_session(db_session, **query)
    assert {row.id for row in raw} == {note.id, tool.id, *(row.id for row in hooks)}


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

    recent = crud_runtime_session_activity.get_recent_tool_calls_by_flow_execution(
        db_session,
        flow_execution_id=execution.id,
        limit=10,
    )
    assert len(recent) == 5
    assert {row.status for row in recent} >= {
        "success",
        "succeeded",
        "failed",
        "refused",
    }


def test_control_message_preserves_only_explicit_author_attribution(
    db_session, create_account
) -> None:
    """Trusted public labels survive while arbitrary auth metadata stays masked."""
    account = create_account()
    run = RuntimeSession(
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id="note-author",
        started_at=datetime.now(UTC),
    )
    db_session.add(run)
    db_session.flush()
    metadata = {
        "author_display": "Untrusted label",
        "authorization": "Bearer sensitive-value",
        "nested": {"api_key": "sensitive-value"},
    }
    untrusted = crud_runtime_session_activity.log_agent_control_message(
        db_session,
        account_id=account.id,
        runtime_session_id=run.id,
        message="Ordinary message.",
        status="delivered",
        metadata=metadata,
    )
    assert untrusted.metadata_["author_display"] == "***REDACTED***"
    trusted = crud_runtime_session_activity.log_agent_control_message(
        db_session,
        account_id=account.id,
        runtime_session_id=run.id,
        message="Stamped note.",
        status="delivered",
        metadata=metadata,
        author_display="Jane Doe",
        author_auth_method="agent",
    )
    assert trusted.metadata_["author_display"] == "Jane Doe"
    assert trusted.metadata_["author_auth_method"] == "agent"
    assert trusted.metadata_["authorization"] == "***REDACTED***"
    assert trusted.metadata_["nested"]["api_key"] == "***REDACTED***"
    assert metadata["author_display"] == "Untrusted label"


def _plan_nodes(node: dict) -> list[dict]:
    nodes = [node]
    for child in node.get("Plans", []):
        nodes.extend(_plan_nodes(child))
    return nodes


def test_timeline_limits_ids_before_reading_metadata(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """The top-N sort never carries metadata, so history is not detoasted (#1689).

    Stripping history keys on every row before the sort read 1.78 GB on a
    prod session with 8,178 rows. The plan's Limit nodes must output ids and
    timestamps only; metadata is projected after the newest ids are chosen.
    """
    from sqlalchemy import event

    account = create_account()
    now = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="claude_code",
        session_source_id="timeline-plan",
        started_at=now - timedelta(minutes=5),
    )
    db_session.add(runtime_session)
    db_session.flush()
    db_session.add_all(
        models.RuntimeSessionActivity(
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            activity_type="model_gateway_call",
            timestamp=now - timedelta(seconds=index),
            metadata_={"model_alias": "m", "messages": ["x" * 100]},
        )
        for index in range(5)
    )
    db_session.flush()

    statements: list[tuple[str, object]] = []
    connection = db_session.connection()

    def capture(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        if "runtime_session_activity" in statement and "metadata" in statement:
            statements.append((statement, parameters))

    event.listen(connection, "before_cursor_execute", capture)
    try:
        rows = crud_runtime_session_activity.list_timeline_rows_for_runtime_session(
            db_session,
            account_id=str(account.id),
            runtime_session_id=str(runtime_session.id),
            limit=2,
        )
    finally:
        event.remove(connection, "before_cursor_execute", capture)

    assert len(rows) == 2
    assert all("messages" not in (row.metadata_ or {}) for row in rows)
    assert len(statements) == 1
    statement, parameters = statements[0]
    cursor = connection.connection.cursor()
    cursor.execute("EXPLAIN (VERBOSE, FORMAT JSON) " + statement, parameters)
    plan = cursor.fetchone()[0][0]["Plan"]
    cursor.close()
    limits = [node for node in _plan_nodes(plan) if node["Node Type"] == "Limit"]
    assert limits, "timeline query must bound rows with LIMIT"
    for node in limits:
        assert not any("metadata" in column for column in node.get("Output", []))
