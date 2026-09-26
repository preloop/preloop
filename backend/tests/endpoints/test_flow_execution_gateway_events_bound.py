"""The execution page's first gateway-events read is small and complete.

The page merges model calls into its timeline and sums their usage for the
summary strip. Without a filter the endpoint returns every log row of the run,
agent log lines included, so a long run spends most of that response on rows
the logs endpoint already serves, and its model calls can fall out of the
``tail`` window altogether.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution_log import FlowExecutionLog
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate


def _execution(db_session, test_user, name="Gateway Events Flow"):
    flow = crud_flow.create(
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
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    db_session.flush()
    return execution


def _row(db_session, execution, *, at, log_type, metadata=None, message=None):
    db_session.add(
        FlowExecutionLog(
            execution_id=execution.id,
            timestamp=at,
            log_type=log_type,
            message=message,
            metadata_=metadata,
        )
    )


def _model_call(index):
    return {
        "api_usage_id": f"usage-{index}",
        "model_alias": "openai/gpt-5",
        "total_tokens": 100,
        "estimated_cost": 0.01,
        "conversation_preview": {"messages": [{"role": "user", "text": "hi"}]},
    }


def _seed(db_session, execution, *, calls, lines_after_each):
    start = datetime(2026, 9, 1, tzinfo=UTC)
    tick = 0
    for index in range(calls):
        _row(
            db_session,
            execution,
            at=start + timedelta(seconds=tick),
            log_type="model_gateway_call",
            metadata=_model_call(index),
        )
        tick += 1
        for line in range(lines_after_each):
            _row(
                db_session,
                execution,
                at=start + timedelta(seconds=tick),
                log_type="agent_log_line",
                metadata={"line": f"line {index}.{line}"},
                message=f"line {index}.{line}",
            )
            tick += 1
    db_session.flush()


def test_model_calls_only_keeps_calls_that_log_lines_would_crowd_out(
    client, db_session, test_user
):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=3, lines_after_each=10)

    unfiltered = client.get(
        f"/api/v1/flows/executions/{execution.id}/gateway-events?tail=5"
    ).json()
    filtered = client.get(
        f"/api/v1/flows/executions/{execution.id}/gateway-events"
        "?tail=5&model_calls_only=true&metadata_only=true"
    ).json()

    # The unfiltered tail is all log lines and misses every model call.
    assert {event["type"] for event in unfiltered["logs"]} == {"agent_log_line"}
    assert unfiltered["has_more"] is True
    # The filtered read has all three calls and says nothing is left.
    assert [event["payload"]["api_usage_id"] for event in filtered["logs"]] == [
        "usage-2",
        "usage-1",
        "usage-0",
    ]
    assert filtered["has_more"] is False
    assert "conversation_preview" not in filtered["logs"][0]["payload"]
    assert filtered["logs"][0]["payload"]["total_tokens"] == 100


def test_has_more_reports_calls_older_than_the_tail(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=4, lines_after_each=1)

    body = client.get(
        f"/api/v1/flows/executions/{execution.id}/gateway-events"
        "?tail=3&model_calls_only=true"
    ).json()

    assert body["has_more"] is True
    # The newest three, in the order the endpoint has always returned.
    assert [event["payload"]["api_usage_id"] for event in body["logs"]] == [
        "usage-3",
        "usage-2",
        "usage-1",
    ]


def test_default_read_is_unchanged_for_existing_callers(client, db_session, test_user):
    execution = _execution(db_session, test_user)
    _seed(db_session, execution, calls=2, lines_after_each=2)

    body = client.get(f"/api/v1/flows/executions/{execution.id}/gateway-events").json()

    assert len(body["logs"]) == 6
    assert body["has_more"] is False
    calls = [e for e in body["logs"] if e["type"] == "model_gateway_call"]
    assert "conversation_preview" in calls[0]["payload"]


def test_tail_must_be_positive(client, db_session, test_user):
    execution = _execution(db_session, test_user)

    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/gateway-events?tail=0"
    )

    assert response.status_code == 422


def test_detail_row_carries_the_flow_name(client, db_session, test_user):
    execution = _execution(db_session, test_user, name="Nightly triage")

    body = client.get(f"/api/v1/flows/executions/{execution.id}").json()

    assert body["flow_name"] == "Nightly triage"
