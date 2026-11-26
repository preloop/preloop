"""Tests for runtime session activity CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Callable
from uuid import uuid4

from sqlalchemy import event, text
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


def test_activity_pages_keep_complete_turns_and_focus_old_matches(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """Dense transcript rows cannot starve tools or an old search hit (#2422/#2423)."""
    account = create_account()
    at = datetime.now(UTC) - timedelta(days=1)
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="codex_cli",
        session_source_id="example-large-session",
        started_at=at,
    )
    db_session.add(session)
    db_session.flush()
    rows = []
    for turn in range(300):
        for position in range(18):
            tool = position % 3 == 1
            row = models.RuntimeSessionActivity(
                account_id=account.id,
                runtime_session_id=session.id,
                timestamp=at + timedelta(minutes=turn, seconds=position),
                activity_type="tool_call" if tool else "transcript_message",
                tool_name="read_file" if tool else None,
                summary=("Example captured text. " * 24),
                metadata_={
                    "role": "user" if position == 0 else "assistant",
                    "arguments": {"path": "example.py"},
                }
                if tool
                else {"role": "user" if position == 0 else "assistant"},
            )
            rows.append(row)
    db_session.add_all(rows)
    db_session.flush()
    kwargs = {"account_id": str(account.id), "runtime_session_id": str(session.id)}
    import time

    started = time.perf_counter()
    baseline = crud_runtime_session_activity.list_timeline_rows_for_runtime_session(
        db_session, **kwargs, limit=100
    )
    print(
        f"baseline: {len(rows)} rows, 300 turns; {len(baseline)} rows in "
        f"{(time.perf_counter() - started) * 1000:.1f} ms"
    )
    page = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, limit=20
    )
    assert len(page["rows"]) == 360
    assert sum(row.activity_type == "tool_call" for row in page["rows"]) == 120
    assert page["has_more"]
    older = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, limit=20, before=page["next_before"]
    )
    assert not ({row.id for row in page["rows"]} & {row.id for row in older["rows"]})
    focused = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, limit=20, around=str(rows[18 * 7 + 2].id)
    )
    assert rows[18 * 7 + 2].id in {row.id for row in focused["rows"]}
    assert rows[18 * 7 + 1].id in {row.id for row in focused["rows"]}
    assert len(focused["rows"]) <= 360


def test_transcript_turn_pages_preserve_same_timestamp_message_order(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """A pushed batch can give every prompt and reply the same timestamp."""
    account = create_account()
    at = datetime.now(UTC)
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(session)
    db_session.flush()
    rows = [
        models.RuntimeSessionActivity(
            account_id=account.id,
            runtime_session_id=session.id,
            timestamp=at,
            activity_type="transcript_message",
            summary=f"Message {index}",
            metadata_={
                "role": "user" if index % 2 == 0 else "assistant",
                "external_id": "example-batch",
                "message_index": index,
            },
        )
        for index in range(8)
    ]
    db_session.add_all(rows)
    db_session.flush()
    kwargs = {
        "account_id": str(account.id),
        "runtime_session_id": str(session.id),
        "limit": 2,
    }
    page = crud_runtime_session_activity.list_timeline_turn_page(db_session, **kwargs)
    assert [row.summary for row in page["rows"]] == [
        f"Message {i}" for i in (7, 6, 5, 4)
    ]
    earlier = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, before=page["next_before"]
    )
    assert [row.summary for row in earlier["rows"]] == [
        f"Message {i}" for i in (3, 2, 1, 0)
    ]
    assert not earlier["has_more"]
    one_turn = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **{**kwargs, "limit": 1}, after=earlier["next_after"]
    )
    assert [row.summary for row in one_turn["rows"]] == ["Message 5", "Message 4"]
    final_turn = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **{**kwargs, "limit": 1}, after=one_turn["next_after"]
    )
    assert [row.summary for row in final_turn["rows"]] == ["Message 7", "Message 6"]
    assert not final_turn["has_newer"]


@pytest.mark.parametrize("same_timestamp", [False, True])
@pytest.mark.parametrize(("row_count", "focus_index"), [(2100, 5), (6200, 2200)])
def test_focus_survives_an_oversized_turn_and_respects_history(
    db_session: Session,
    create_account: Callable[..., models.Account],
    same_timestamp: bool,
    row_count: int,
    focus_index: int,
) -> None:
    """A safety cap cannot hide the search target or disclose expired history."""
    account = create_account()
    at = datetime.now(UTC) - timedelta(days=1)
    session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(session)
    db_session.flush()
    rows = [
        models.RuntimeSessionActivity(
            account_id=account.id,
            runtime_session_id=session.id,
            timestamp=at if same_timestamp else at + timedelta(seconds=index),
            activity_type="transcript_message",
            summary=f"Example row {index}",
            metadata_={
                "role": "user" if index == 0 else "assistant",
                "external_id": "example-batch",
                "message_index": index,
            },
        )
        for index in range(row_count)
    ]
    db_session.add_all(rows)
    db_session.flush()
    kwargs = {
        "account_id": str(account.id),
        "runtime_session_id": str(session.id),
        "limit": 20,
    }
    focused = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, around=str(rows[focus_index].id)
    )
    assert rows[focus_index].id in {row.id for row in focused["rows"]}
    assert len(focused["rows"]) <= 2000
    assert focused["partial_turn"]
    assert focused["has_more"] is (focus_index > 1000)
    if not focused["has_more"]:
        assert focused["next_before"] is None
    assert focused["has_newer"]
    seen = {row.id for row in focused["rows"]}
    for direction, flag, cursor in (
        ("before", "has_more", "next_before"),
        ("after", "has_newer", "next_after"),
    ):
        page = focused
        while page[flag]:
            page = crud_runtime_session_activity.list_timeline_turn_page(
                db_session, **kwargs, **{direction: page[cursor]}
            )
            page_ids = {row.id for row in page["rows"]}
            assert page_ids, "An advertised cursor must make progress"
            assert len(page_ids) <= 2000
            assert not seen & page_ids
            seen.update(page_ids)
    assert seen == {row.id for row in rows}
    latest = crud_runtime_session_activity.list_timeline_turn_page(db_session, **kwargs)
    older = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, before=latest["next_before"]
    )
    recovered = crud_runtime_session_activity.list_timeline_turn_page(
        db_session, **kwargs, after=older["next_after"]
    )
    assert {row.id for row in recovered["rows"]} == {row.id for row in latest["rows"]}
    for method in (
        crud_runtime_session_activity.list_timeline_turn_page,
        crud_runtime_session_activity.gateway_offset_around,
    ):
        focus_arg = (
            {"around": str(rows[5].id)}
            if method.__name__ == "list_timeline_turn_page"
            else {"target": str(rows[5].id)}
        )
        with pytest.raises(ValueError, match="not found"):
            method(
                db_session, **kwargs, **focus_arg, start_date=at + timedelta(hours=1)
            )


@pytest.mark.parametrize("newest_first", [False, True])
def test_bounded_gateway_payload_pages_preserve_equal_timestamp_order(
    db_session: Session,
    create_account: Callable[..., models.Account],
    newest_first: bool,
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
                newest_first=newest_first,
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
    assert seen == sorted(seen, reverse=newest_first)
    assert complete


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
        # Writers go through the ORM, which keeps metadata_bytes current.
        if mutation == "growth":
            activity.metadata_ = {"request": "x" * 5_000}
        else:
            activity.timestamp = at + timedelta(seconds=1)
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


def _gateway_row(
    db_session: Session, account: models.Account, metadata: dict
) -> tuple[models.RuntimeSessionActivity, datetime]:
    at = datetime.now(UTC)
    runtime_session = models.RuntimeSession(
        account_id=account.id,
        session_source_type="test",
        session_source_id=str(uuid4()),
        started_at=at,
    )
    db_session.add(runtime_session)
    db_session.flush()
    row = models.RuntimeSessionActivity(
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        activity_type="model_gateway_call",
        timestamp=at,
        metadata_=metadata,
    )
    db_session.add(row)
    db_session.flush()
    return row, at


def _text_bytes(db_session: Session, row: models.RuntimeSessionActivity) -> int:
    return db_session.execute(
        text(
            "SELECT octet_length(metadata::text) FROM runtime_session_activity "
            "WHERE id = :id"
        ),
        {"id": row.id},
    ).scalar_one()


def test_metadata_bytes_matches_postgres_text_length_on_insert_and_update(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """The stored size equals what octet_length(metadata::text) reported."""
    payload = {
        "request": {"messages": [{"role": "user", "content": 'héllo ✓ \n"q"'}]},
        "tokens": 12,
        "ratio": 0.5,
        "flag": True,
        "none": None,
    }
    row, _ = _gateway_row(db_session, create_account(), payload)
    assert row.metadata_bytes == _text_bytes(db_session, row)
    row.metadata_ = {**payload, "response": "y" * 900}
    db_session.flush()
    assert row.metadata_bytes == _text_bytes(db_session, row)
    row.status = "success"
    db_session.flush()
    assert row.metadata_bytes == _text_bytes(db_session, row)


def test_bounded_gateway_payload_reads_the_stored_size(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """The budget comes from metadata_bytes, not from serialising the value."""
    row, at = _gateway_row(db_session, create_account(), {"request": "small"})
    db_session.execute(
        text(
            "UPDATE runtime_session_activity SET metadata_bytes = 5000 WHERE id = :id"
        ),
        {"id": row.id},
    )
    rows, skipped, _, _ = (
        crud_runtime_session_activity.list_bounded_model_gateway_call_payloads_for_session(
            db_session,
            account_id=row.account_id,
            runtime_session_id=row.runtime_session_id,
            start_date=at,
            end_date=at,
            max_payload_bytes=1_000,
        )
    )
    assert rows == []
    assert skipped == [(row.id, at)]


def test_bounded_gateway_payload_measures_rows_without_a_stored_size(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """Rows written before the column existed still respect the budget."""
    big, at = _gateway_row(db_session, create_account(), {"request": "x" * 2_000})
    db_session.execute(
        text(
            "UPDATE runtime_session_activity SET metadata_bytes = NULL WHERE id = :id"
        ),
        {"id": big.id},
    )
    rows, skipped, _, complete = (
        crud_runtime_session_activity.list_bounded_model_gateway_call_payloads_for_session(
            db_session,
            account_id=big.account_id,
            runtime_session_id=big.runtime_session_id,
            start_date=at,
            end_date=at,
            max_payload_bytes=1_000,
        )
    )
    assert rows == []
    assert skipped == [(big.id, at)]
    assert not complete
