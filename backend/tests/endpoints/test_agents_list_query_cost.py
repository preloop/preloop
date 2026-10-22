"""The agents list ranks a page without aggregating every agent's usage (#1694).

``GET /agents?status=all&limit=100`` took 3.0 to 3.6 s on prod for the
Attention page. The list statement joined ``api_usage`` for every agent of
the account and grouped the result on twenty-odd columns (a JSONB one among
them) before it applied the LIMIT, so the cost followed the account's whole
gateway history rather than the page. Ranking now reads one index-bounded
``max(timestamp)`` per agent and the page's request counts come from one
grouped statement over the page's sessions only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, List
from uuid import uuid4

import pytest

from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage, crud_managed_agent


def _agent(
    db: Session,
    user: Any,
    index: int,
    *,
    seen_minutes_ago: int,
    requests: int = 0,
    request_minutes_ago: int = 0,
) -> models.ManagedAgent:
    now = datetime.now(UTC).replace(tzinfo=None)
    source_id = f"cost-{index}-{uuid4().hex[:8]}"
    session = models.RuntimeSession(
        account_id=user.account_id,
        session_source_type="claude_code",
        session_source_id=source_id,
        started_at=now - timedelta(days=2),
        last_activity_at=now - timedelta(minutes=seen_minutes_ago),
    )
    db.add(session)
    db.flush()
    agent = models.ManagedAgent(
        id=uuid4(),
        account_id=user.account_id,
        runtime_session_id=session.id,
        agent_kind="claude_code",
        session_source_type="claude_code",
        session_source_id=source_id,
        session_reference=None,
        display_name=f"Cost agent {index}",
        enrolled_via="runtime_session_token",
        lifecycle_state="active",
        lifecycle_updated_at=now,
        last_seen_at=now - timedelta(minutes=seen_minutes_ago),
        tags={"team": "a"},
    )
    db.add(agent)
    db.flush()
    for _ in range(requests):
        row = crud_api_usage.log_gateway_request(
            db,
            endpoint="/openai/v1/chat/completions",
            method="POST",
            status_code=200,
            duration=0.1,
            account_id=str(user.account_id),
            runtime_session_id=str(session.id),
            model_alias="model-a",
            provider_name="provider-a",
            prompt_tokens=10,
            completion_tokens=1,
            total_tokens=11,
            estimated_cost=0.001,
        )
        row.timestamp = now - timedelta(minutes=request_minutes_ago)
    # Tool traffic on the same session is not a model request.
    db.add(
        models.ApiUsage(
            account_id=user.account_id,
            runtime_session_id=session.id,
            endpoint="/mcp",
            method="POST",
            status_code=200,
            duration=0.1,
            action_type="mcp_tool",
        )
    )
    db.flush()
    return agent


def test_page_ranks_by_last_request_then_activity(
    db_session: Session, test_user: Any
) -> None:
    # Requests rank an agent by the request time, ahead of later sightings.
    talker = _agent(
        db_session,
        test_user,
        0,
        seen_minutes_ago=600,
        requests=3,
        request_minutes_ago=1,
    )
    quiet_recent = _agent(db_session, test_user, 1, seen_minutes_ago=5)
    quiet_old = _agent(db_session, test_user, 2, seen_minutes_ago=900)
    old_talker = _agent(
        db_session,
        test_user,
        3,
        seen_minutes_ago=2000,
        requests=2,
        request_minutes_ago=60,
    )

    result = crud_managed_agent.list_for_account(
        db_session, account_id=str(test_user.account_id), status="all", limit=100
    )
    order = [item["id"] for item in result["items"]]
    assert order == [
        str(talker.id),
        str(quiet_recent.id),
        str(old_talker.id),
        str(quiet_old.id),
    ]
    assert result["total"] == 4

    page = crud_managed_agent.list_for_account(
        db_session,
        account_id=str(test_user.account_id),
        status="all",
        limit=2,
        offset=2,
    )
    assert [item["id"] for item in page["items"]] == order[2:]


@pytest.mark.parametrize("ready", [False, True])
def test_list_statement_does_not_group_the_account_usage(
    db_session: Session, test_user: Any, ready: bool
) -> None:
    state = db_session.get(models.AgentUsageBackfill, "agents")
    assert state is not None
    state.ready = ready
    db_session.flush()
    for index in range(6):
        _agent(db_session, test_user, index, seen_minutes_ago=index, requests=index % 3)
    statements: List[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        crud_managed_agent.list_for_account(
            db_session, account_id=str(test_user.account_id), status="all", limit=3
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    joined = [
        sql
        for sql in statements
        if "FROM managed_agent" in sql and "GROUP BY managed_agent.id" in sql
    ]
    assert joined == [], joined
    usage_by_session = [
        sql
        for sql in statements
        if "FROM api_usage" in sql
        and "GROUP BY api_usage.runtime_session_id" in sql
        and "api_usage.runtime_session_id IN" in sql
    ]
    if ready:
        assert not any("FROM api_usage" in sql for sql in statements), statements
        assert any("agent_usage_total" in sql for sql in statements), statements
    else:
        assert len(usage_by_session) == 1, statements


def test_principal_usage_is_read_per_principal(
    db_session: Session, test_user: Any
) -> None:
    """Totals and latest model per agent principal, one LATERAL pair each.

    Grouping every listed principal's rows in one statement sorted all of
    them (215k spilled to disk on the founder account) and ran a window over
    the same rows for the latest model: ~0.6 s of the agents list on prod.
    """
    from preloop.models.crud.managed_agent import _usage_aggregates_for_principals

    now = datetime.now(UTC).replace(tzinfo=None)

    def usage(principal_id: str, minutes_ago: int, alias: str, status: int = 200):
        db_session.add(
            models.ApiUsage(
                account_id=test_user.account_id,
                endpoint="/v1/messages",
                method="POST",
                status_code=status,
                duration=0.1,
                action_type="model_gateway",
                runtime_principal_type="claude_code",
                runtime_principal_id=principal_id,
                model_alias=alias,
                provider_name="anthropic",
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
                estimated_cost=0.5,
                timestamp=now - timedelta(minutes=minutes_ago),
            )
        )

    usage("busy", 30, "old-model")
    usage("busy", 5, "new-model")
    usage("busy", 10, "mid-model", status=500)
    db_session.flush()

    statements: List[str] = []

    def record(conn, cursor, statement, *args):
        statements.append(" ".join(statement.split()))

    engine = db_session.get_bind().engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = _usage_aggregates_for_principals(
            db_session,
            account_id=str(test_user.account_id),
            principals=[("claude_code", "busy"), ("claude_code", "idle")],
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    busy = result[("claude_code", "busy")]
    assert busy["total_requests"] == 3
    assert busy["successful_requests"] == 2
    assert busy["failed_requests"] == 1
    assert busy["total_tokens"] == 36
    assert busy["estimated_cost"] == 1.5
    assert busy["latest_model_alias"] == "new-model"
    idle = result[("claude_code", "idle")]
    assert idle["total_requests"] == 0
    assert idle["latest_model_alias"] is None

    usage_reads = [sql for sql in statements if "FROM api_usage" in sql]
    assert len(usage_reads) == 1
    assert "LATERAL" in usage_reads[0]
    assert "row_number" not in usage_reads[0]
