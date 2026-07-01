"""Tests for per-request runtime session ApiUsage CRUD helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from preloop.models.crud import crud_api_usage, crud_runtime_session


def _create_runtime_session(db_session, account_id) -> object:
    return crud_runtime_session.upsert_by_source(
        db_session,
        account_id=account_id,
        session_source_type="claude_code",
        session_source_id=f"timeline-{uuid4()}",
        session_reference="timeline",
        runtime_principal_type="managed_agent",
        runtime_principal_id="agent-timeline",
        runtime_principal_name="Timeline Agent",
        last_activity_at=datetime.now(UTC),
    )


def _log_gateway_row(
    db_session,
    *,
    account_id,
    runtime_session_id,
    status_code: int,
    total_tokens: int,
    estimated_cost: float,
    principal_id: str = "agent-timeline",
    when: datetime | None = None,
):
    row = crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=status_code,
        duration=0.2,
        account_id=str(account_id),
        runtime_session_id=str(runtime_session_id),
        model_alias="gpt-4o",
        provider_name="openai",
        prompt_tokens=total_tokens // 2,
        completion_tokens=total_tokens - total_tokens // 2,
        total_tokens=total_tokens,
        estimated_cost=estimated_cost,
        runtime_principal_type="managed_agent",
        runtime_principal_id=principal_id,
        runtime_principal_name="Timeline Agent",
    )
    if when is not None:
        row.timestamp = when
        db_session.commit()
    return row


def test_list_session_request_rows_orders_oldest_first(
    db_session, create_account
) -> None:
    """Per-request rows should be returned in chronological order."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    first = _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        status_code=200,
        total_tokens=100,
        estimated_cost=0.01,
        when=base,
    )
    second = _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        status_code=500,
        total_tokens=50,
        estimated_cost=0.0,
        when=base + timedelta(minutes=1),
    )

    rows = crud_api_usage.list_session_request_rows(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
    )

    assert [row.id for row in rows] == [first.id, second.id]


def test_count_session_request_rows_honors_failed_only_and_event_ids(
    db_session, create_account
) -> None:
    """Count helpers should mirror list filters."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    success = _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        status_code=200,
        total_tokens=100,
        estimated_cost=0.01,
    )
    failure = _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        status_code=500,
        total_tokens=50,
        estimated_cost=0.0,
    )

    assert (
        crud_api_usage.count_session_request_rows(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
        )
        == 2
    )
    assert (
        crud_api_usage.count_session_request_rows(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            failed_only=True,
        )
        == 1
    )
    assert (
        crud_api_usage.count_session_request_rows(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            event_ids=[str(success.id)],
        )
        == 1
    )
    listed = crud_api_usage.list_session_request_rows(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        failed_only=True,
    )
    assert len(listed) == 1
    assert listed[0].id == failure.id


def test_get_runtime_principal_gateway_averages_excludes_session_and_zeros(
    db_session, create_account
) -> None:
    """Principal averages should exclude a session and return zeros when empty."""
    account = create_account()
    included = _create_runtime_session(db_session, account.id)
    excluded = _create_runtime_session(db_session, account.id)
    db_session.commit()
    start = datetime(2026, 6, 1, tzinfo=UTC)

    _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=included.id,
        status_code=200,
        total_tokens=100,
        estimated_cost=0.10,
        when=start,
    )
    _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=excluded.id,
        status_code=200,
        total_tokens=200,
        estimated_cost=0.20,
        when=start + timedelta(minutes=1),
    )

    averages = crud_api_usage.get_runtime_principal_gateway_averages(
        db_session,
        account_id=str(account.id),
        runtime_principal_id="agent-timeline",
        start=start,
        exclude_runtime_session_id=str(excluded.id),
    )

    assert averages["requests"] == 1
    assert averages["avg_total_tokens_per_request"] == 100.0
    assert averages["avg_cost_per_request"] == 0.10

    empty = crud_api_usage.get_runtime_principal_gateway_averages(
        db_session,
        account_id=str(account.id),
        runtime_principal_id="missing-principal",
        start=start,
    )
    assert empty == {
        "requests": 0,
        "avg_total_tokens_per_request": 0.0,
        "avg_cost_per_request": 0.0,
    }
