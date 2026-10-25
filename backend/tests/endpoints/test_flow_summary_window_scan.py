"""A windowed flows list does not sum every gateway row a flow ever made.

``/flows/summary?stats_since=`` prints runs, failures, spend and tokens for
the window. The lifetime spend and tokens were summed over all of a flow's
``api_usage`` rows only to be shadowed by the window's figures, and that
scan grows with every run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, List

from sqlalchemy import event

from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _flow_with_usage(db_session: Any, test_user: Any) -> Any:
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Busy flow",
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
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    crud_api_usage.log_gateway_request(
        db_session,
        endpoint="/openai/v1/responses",
        method="POST",
        status_code=200,
        duration=0.1,
        user_id=test_user.id,
        account_id=test_user.account_id,
        flow_id=flow.id,
        flow_execution_id=execution.id,
        model_alias="openai/gpt-5.5",
        provider_name="openai",
        prompt_tokens=10,
        completion_tokens=5,
        total_tokens=15,
        estimated_cost=0.25,
    )
    db_session.commit()
    return flow


def _lifetime_usage_scans(statements: List[str]) -> List[str]:
    return [
        sql
        for sql in statements
        if "FROM api_usage" in sql and "GROUP BY api_usage.flow_id" in sql
    ]


def _record(db_session: Any, statements: List[str]):
    def record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        statements.append(" ".join(statement.split()))

    return record


def test_windowed_summary_skips_the_lifetime_usage_scan(
    client, db_session, test_user
) -> None:
    flow = _flow_with_usage(db_session, test_user)
    since = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    statements: List[str] = []
    record = _record(db_session, statements)
    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        response = client.get(
            "/api/v1/flows/summary",
            params={"include_stats": "true", "stats_since": since},
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 200
    row = next(item for item in response.json() if item["id"] == str(flow.id))
    stats = row["execution_stats"]
    # The window's figures are still there, from the per-execution read.
    assert stats["runs"] == 1
    assert stats["cost"] == 0.25
    assert stats["token_usage"]["total_tokens"] == 15
    assert stats["estimated_cost"] is None
    assert _lifetime_usage_scans(statements) == []


def test_unwindowed_stats_keep_the_lifetime_figures(db_session, test_user) -> None:
    flow = _flow_with_usage(db_session, test_user)

    (stat,) = crud_flow_execution.get_execution_stats_for_flows(db_session, [flow.id])

    assert stat.estimated_cost == 0.25
    assert stat.token_usage["total_tokens"] == 15
