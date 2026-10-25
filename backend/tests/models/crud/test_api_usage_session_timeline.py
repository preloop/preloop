"""Tests for per-request runtime session ApiUsage CRUD helpers."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models import models
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


def test_empty_event_ids_returns_no_rows(db_session, create_account) -> None:
    """An explicit empty event_ids filter should match nothing, not everything."""
    account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    db_session.commit()

    _log_gateway_row(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        status_code=200,
        total_tokens=100,
        estimated_cost=0.01,
    )

    assert (
        crud_api_usage.count_session_request_rows(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            event_ids=[],
        )
        == 0
    )
    assert (
        crud_api_usage.list_session_request_rows(
            db_session,
            account_id=account.id,
            runtime_session_id=runtime_session.id,
            event_ids=[],
        )
        == []
    )


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


def test_list_session_request_keys_pages_real_requests_by_keyset(
    db_session: Session, create_account: Callable[..., models.Account]
) -> None:
    """Keys page oldest-first across timestamp ties and skip internal analysis."""
    account = create_account()
    other_account = create_account()
    runtime_session = _create_runtime_session(db_session, account.id)
    other_session = _create_runtime_session(db_session, account.id)
    db_session.commit()
    base = datetime.now(UTC) - timedelta(hours=1)
    real = []
    for offset, meta, counted in [
        (0, None, True),
        (1, {"purpose": "customer.task", "usage_details": {"blob": "x" * 64}}, True),
        (1, {"purpose": "governance_session_digest"}, False),
        (1, None, True),
        (2, {"purpose": "session_title"}, False),
        (3, {}, True),
    ]:
        row = crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.2,
            account_id=str(account.id),
            runtime_session_id=str(runtime_session.id),
            meta_data=meta,
        )
        row.timestamp = base + timedelta(minutes=offset)
        if counted:
            real.append(row)
    for owner, session_id in (
        (account.id, other_session.id),
        (other_account.id, runtime_session.id),
    ):
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.2,
            account_id=str(owner),
            runtime_session_id=str(session_id),
        )
    db_session.commit()
    expected = sorted((r.timestamp, r.id) for r in real)

    keys = []
    cursor = None
    while page := crud_api_usage.list_session_request_keys(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        after=cursor,
        limit=1,
    ):
        assert set(vars(page[0])) == {"id", "timestamp"}
        keys.extend(page)
        cursor = (page[-1].timestamp, page[-1].id)

    assert [(k.timestamp, k.id) for k in keys] == expected
    full = crud_api_usage.list_session_request_rows(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        limit=100,
    )
    assert [k.id for k in keys] == [
        r.id
        for r in full
        if (r.meta_data or {}).get("purpose")
        not in {"governance_session_digest", "session_title"}
    ]
