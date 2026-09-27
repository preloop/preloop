"""The stop command is idempotent and shared with automatic stops (#1032).

``POST /flows/executions/{id}/command {"command": "stop"}`` and the pull
request lifecycle stops in ``flow_trigger_service`` run the same code,
``preloop.services.flow_execution_stop.stop_execution``. These tests pin the
parts both rely on: an execution that already ended is left exactly as it
was, a second stop changes nothing, and an automatic stop records why.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services.flow_execution_stop import stop_execution
from tests.bound_session import bound_session_factory


@pytest.fixture(autouse=True)
def _module_sessions(db_session, monkeypatch):
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: bound_session_factory(db_session),
    )


@pytest.fixture(autouse=True)
def send_command():
    """The stop command goes to NATS best effort; capture it instead."""
    with (
        patch(
            "preloop.sync.services.event_bus.get_nats_client",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
            new_callable=AsyncMock,
        ) as mock,
    ):
        yield mock


@pytest.fixture
def flow(db_session, test_user):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Pull Request Reviewer",
            prompt_template="review the pull request",
            agent_type="codex",
            agent_config={},
        ),
        account_id=test_user.account_id,
    )


def _execution(db_session, flow, status):
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            trigger_event_details={"source": "github", "payload": {}},
        ),
    )
    db_session.flush()
    return execution


def _stop(client, execution):
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/command",
        json={"command": "stop", "payload": {}},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
def test_stopping_a_finished_execution_changes_nothing(
    client, db_session, flow, send_command, status
):
    execution = _execution(db_session, flow, status)
    execution.error_message = "kept" if status == "FAILED" else None
    db_session.commit()

    assert _stop(client, execution) == {
        "status": "not_running",
        "execution_status": status,
    }

    db_session.refresh(execution)
    assert execution.status == status
    assert execution.end_time is None
    assert execution.stop_reason is None
    assert execution.stop_source is None
    assert execution.error_message == ("kept" if status == "FAILED" else None)
    send_command.assert_not_awaited()


def test_a_second_stop_is_idempotent(client, db_session, flow, send_command):
    execution = _execution(db_session, flow, "RUNNING")
    db_session.commit()

    assert _stop(client, execution) == {"status": "stopped"}
    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.error_message == "Manually stopped by user"
    # An operator's stop carries no automatic reason.
    assert execution.stop_reason is None
    assert execution.stop_source is None
    first_end = execution.end_time
    assert first_end is not None
    assert send_command.await_count == 1

    assert _stop(client, execution) == {"status": "stopped"}
    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.end_time == first_end
    assert send_command.await_count == 1


def test_unknown_execution_is_not_found(client):
    response = client.post(
        "/api/v1/flows/executions/00000000-0000-4000-8000-000000000000/command",
        json={"command": "stop", "payload": {}},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_automatic_stop_records_why(db_session, flow, test_user, send_command):
    execution = _execution(db_session, flow, "PENDING")
    db_session.commit()
    reason = "Stopped because pull request example-org/widgets#12 was merged"

    outcome = await stop_execution(
        db_session,
        execution,
        account_id=test_user.account_id,
        error_message=reason,
        stop_reason=reason,
        stop_source="pr_merged",
    )

    assert outcome.stopped is True
    assert outcome.status == "STOPPED"
    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.stop_reason == reason
    assert execution.stop_source == "pr_merged"
    assert execution.error_message == reason
    # Not a kill-switch request: that path reports an account halt.
    assert execution.stop_requested_at is None
    send_command.assert_awaited_once()

    again = await stop_execution(
        db_session,
        execution,
        account_id=test_user.account_id,
        error_message="Stopped because pull request example-org/widgets#12 was closed",
        stop_reason="closed",
        stop_source="pr_closed",
    )
    assert again.stopped is False
    db_session.refresh(execution)
    assert execution.stop_source == "pr_merged"
    assert execution.stop_reason == reason


@pytest.mark.asyncio
async def test_run_that_ends_during_teardown_keeps_its_result(
    db_session, flow, test_user, send_command
):
    execution = _execution(db_session, flow, "RUNNING")
    execution.agent_session_reference = "container-abc"
    db_session.commit()

    async def finish_while_stopping(*_args, **_kwargs):
        execution.status = "SUCCEEDED"
        db_session.commit()

    with patch(
        "preloop.services.flow_execution_stop._tear_down_runtime",
        new=finish_while_stopping,
    ):
        outcome = await stop_execution(
            db_session, execution, account_id=test_user.account_id
        )

    assert outcome.stopped is False
    assert outcome.status == "SUCCEEDED"
    db_session.refresh(execution)
    assert execution.status == "SUCCEEDED"
    assert execution.stop_source is None
    send_command.assert_not_awaited()
