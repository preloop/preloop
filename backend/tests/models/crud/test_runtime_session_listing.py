"""Runtime session listing preserves attribution while bounding DB results."""

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.runtime_session import (
    _latest_gateway_usage_for_sessions,
    crud_runtime_session,
)


@pytest.fixture
def session_listing_data(db_session: Session, create_account: Any) -> dict[str, Any]:
    account = create_account()
    other_account = create_account()
    now = datetime.now(UTC).replace(tzinfo=None)
    flow = models.Flow(
        account_id=account.id,
        name="Example flow",
        prompt_template="Example",
        agent_config={},
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(flow_id=flow.id)
    db_session.add(execution)
    db_session.flush()
    sessions = {}
    for name in ("direct", "legacy", "empty", "other"):
        session = models.RuntimeSession(
            account_id=other_account.id if name == "other" else account.id,
            session_source_type="flow_execution" if name == "legacy" else "custom",
            session_source_id=str(execution.id) if name == "legacy" else name,
            started_at=now - timedelta(hours=2),
            last_activity_at=now - timedelta(hours=1),
        )
        db_session.add(session)
        sessions[name] = session
    db_session.flush()

    def usage(name: str | None, **kwargs: Any) -> models.ApiUsage:
        row = models.ApiUsage(
            account_id=kwargs.pop("account_id", account.id),
            runtime_session_id=sessions[name].id if name else None,
            endpoint="/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="model_gateway",
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            estimated_cost=0.01,
            timestamp=kwargs.pop("timestamp", now - timedelta(minutes=5)),
            **kwargs,
        )
        db_session.add(row)
        return row

    usage("direct", model_alias="z-old", timestamp=now - timedelta(minutes=10))
    usage("direct", model_alias="a-latest", cache_read_tokens=4)
    usage(None, flow_execution_id=execution.id, flow_id=flow.id)
    # Both attribution paths match: this row must only count once.
    usage("legacy", flow_execution_id=execution.id, flow_id=flow.id)
    # Distinct session attribution retains the historical OR-join semantics.
    usage(
        "direct",
        flow_execution_id=execution.id,
        flow_id=flow.id,
        timestamp=now - timedelta(minutes=7),
    )
    usage("direct", meta_data={"purpose": "session_title"})
    usage("direct", meta_data={"purpose": "replay_validation"})
    usage("direct", timestamp=now - timedelta(days=5))
    usage("other", account_id=other_account.id)
    db_session.flush()
    return {
        "account": account,
        "other_account": other_account,
        "now": now,
        "sessions": sessions,
        "usage": usage,
    }


def test_listing_preserves_legacy_attribution_and_pagination(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    kwargs = {
        "account_id": str(data["account"].id),
        "start_date": data["now"] - timedelta(days=1),
        "end_date": data["now"],
    }
    result = crud_runtime_session.list_account_sessions(db_session, **kwargs)
    items = {item["id"]: item for item in result["items"]}
    assert result["total"] == 3
    direct = items[str(data["sessions"]["direct"].id)]
    legacy = items[str(data["sessions"]["legacy"].id)]
    empty = items[str(data["sessions"]["empty"].id)]
    assert direct["total_requests"] == legacy["total_requests"] == 3
    assert direct["total_tokens"] == legacy["total_tokens"] == 45
    assert direct["cache_read_tokens"] == 4
    assert direct["latest_model_alias"] == "a-latest"
    assert empty["total_requests"] == 0
    page = crud_runtime_session.list_account_sessions(
        db_session, **kwargs, limit=1, offset=2
    )
    assert page["total"] == 3
    assert page["items"][0]["id"] == empty["id"]


def test_listing_does_not_aggregate_usage_owned_by_another_account(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    data["usage"]("direct", account_id=data["other_account"].id)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        start_date=data["now"] - timedelta(days=1),
        end_date=data["now"],
    )
    direct = next(
        item for item in result["items"] if item["session_source_id"] == "direct"
    )
    assert direct["total_requests"] == 3


def test_latest_lookup_transfers_only_one_row_per_session(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    timestamp = data["now"] - timedelta(seconds=1)
    for _ in range(50):
        data["usage"]("direct", timestamp=timestamp, model_alias="tied")
    data["usage"](
        "direct",
        id=UUID(int=(1 << 128) - 1),
        timestamp=timestamp,
        model_alias="tie-winner",
    )
    data["usage"](
        "direct",
        timestamp=data["now"],
        model_alias="internal",
        meta_data={"purpose": "session_title"},
    )
    db_session.flush()
    fetched_row_counts = []

    def record_rows(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        fetched_row_counts.append(cursor.rowcount)

    event.listen(db_session.connection(), "after_cursor_execute", record_rows)
    try:
        latest = _latest_gateway_usage_for_sessions(
            db_session,
            account_id=str(data["account"].id),
            runtime_session_ids=[
                str(data["sessions"][name].id) for name in ("direct", "legacy")
            ],
        )
    finally:
        event.remove(db_session.connection(), "after_cursor_execute", record_rows)
    assert len(latest) == 2
    assert fetched_row_counts == [2]

    assert latest[str(data["sessions"]["direct"].id)].model_alias == "tie-winner"


def test_minimum_requests_is_account_scoped(
    db_session: Session, session_listing_data: dict[str, Any]
) -> None:
    data = session_listing_data
    data["usage"]("empty", account_id=data["other_account"].id)
    db_session.flush()
    result = crud_runtime_session.list_account_sessions(
        db_session,
        account_id=str(data["account"].id),
        min_requests=1,
    )
    assert {item["session_source_id"] for item in result["items"]} == {
        data["sessions"]["direct"].session_source_id,
        data["sessions"]["legacy"].session_source_id,
    }
