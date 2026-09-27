"""The execution page reads host-exec hook sessions and seat usage."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from preloop.models.crud import crud_api_usage, crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.host_exec_usage import host_exec_premium_fingerprint


def _execution(db_session, account_id):
    flow = crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Host Sessions Flow",
            prompt_template="Review",
            trigger_event_source="github",
            trigger_event_types=["test"],
            agent_type="copilot",
            agent_config={"host_exec_profile": "copilot-seat"},
            allowed_mcp_servers=[],
            allowed_mcp_tools=[],
            account_id=account_id,
        ),
        account_id=account_id,
    )
    execution = crud_flow_execution.create(
        db_session, FlowExecutionCreate(flow_id=flow.id, status="SUCCEEDED")
    )
    db_session.flush()
    return flow, execution


def test_host_sessions_reports_hook_events_and_premium_requests(
    client, db_session, test_user
):
    flow, execution = _execution(db_session, test_user.account_id)
    for event_type in ("session_start", "session_end"):
        crud_api_usage.log_imported_usage_event(
            db_session,
            account_id=str(test_user.account_id),
            timestamp=datetime(2026, 9, 27, 12, 0),
            model_alias=None,
            source="copilot_cli",
            conversation_id="copilot-session-1",
            flow_id=flow.id,
            flow_execution_id=execution.id,
            meta_data={"event_type": event_type},
            commit=False,
        )
    crud_api_usage.log_imported_usage_event(
        db_session,
        account_id=str(test_user.account_id),
        timestamp=datetime(2026, 9, 27, 12, 5),
        model_alias="claude-sonnet-4.5",
        source="copilot_cli",
        cost_source="subscription",
        conversation_id="copilot-session-1",
        flow_id=flow.id,
        flow_execution_id=execution.id,
        import_fingerprint=host_exec_premium_fingerprint(execution.id),
        meta_data={"event_type": "host_exec_result", "premium_requests": 2},
        commit=False,
    )
    db_session.flush()

    response = client.get(f"/api/v1/flows/executions/{execution.id}/host-sessions")

    assert response.status_code == 200
    body = response.json()
    assert body["premium_requests"] == 2
    assert body["event_count"] == 2
    assert body["gateway_metered"] is False
    (session,) = body["sessions"]
    assert session["conversation_id"] == "copilot-session-1"
    assert session["event_types"] == {"session_start": 1, "session_end": 1}


def test_host_sessions_is_empty_for_other_runs_and_404_for_unknown(
    client, db_session, test_user
):
    _, execution = _execution(db_session, test_user.account_id)
    body = client.get(f"/api/v1/flows/executions/{execution.id}/host-sessions").json()
    assert body["sessions"] == [] and body["premium_requests"] is None
    missing = client.get(f"/api/v1/flows/executions/{uuid4()}/host-sessions")
    assert missing.status_code == 404
