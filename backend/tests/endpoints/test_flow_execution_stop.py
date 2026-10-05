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
    # An operator's stop carries no automatic reason, but it is recorded as
    # a durable, manual stop request.
    assert execution.stop_reason is None
    assert execution.stop_source == "manual"
    assert execution.stop_requested_at is not None
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
    # The request is durable whatever its source; the source says it was
    # not a kill switch.
    assert execution.stop_requested_at is not None
    # Never admitted, no runtime: nothing can be running.
    assert execution.stop_confirmed_at is not None
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


def _runtime(*, stop_error=None, stopped=True):
    runtime = MagicMock()
    runtime.get_logs = AsyncMock(return_value=[])
    runtime.stop = AsyncMock(side_effect=stop_error)
    runtime.is_stopped = AsyncMock(return_value=stopped)
    return runtime


def test_operator_stop_is_durable_and_audited(client, db_session, flow, test_user):
    """The API's stop records the request, confirms termination, and audits."""
    from preloop.models.models.audit_log import AuditLog

    execution = _execution(db_session, flow, "RUNNING")
    execution.agent_session_reference = "agent-docker-1"
    execution.launch_requested_at = execution.start_time
    db_session.commit()
    runtime = _runtime(stopped=True)

    with patch("preloop.agents.codex.CodexAgent", return_value=runtime):
        assert _stop(client, execution) == {"status": "stopped"}

    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.stop_source == "manual"
    assert execution.stop_requested_at is not None
    assert execution.stop_confirmed_at is not None
    assert execution.stop_requested_at <= execution.stop_confirmed_at
    runtime.stop.assert_awaited_once_with("agent-docker-1")
    runtime.is_stopped.assert_awaited_once_with("agent-docker-1")

    rows = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "flow_execution_stop_requested",
            AuditLog.resource_id == str(execution.id),
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id
    assert rows[0].status == "success"
    assert rows[0].details["status_before"] == "RUNNING"
    assert rows[0].details["termination_confirmed"] is True

    # The schema exposes both timestamps so a client can tell them apart.
    body = client.get(f"/api/v1/flows/executions/{execution.id}").json()
    assert body["stop_requested_at"] is not None
    assert body["stop_confirmed_at"] is not None


def test_failed_teardown_leaves_the_stop_unconfirmed(client, db_session, flow):
    execution = _execution(db_session, flow, "RUNNING")
    execution.agent_session_reference = "agent-k8s-1"
    execution.launch_requested_at = execution.start_time
    db_session.commit()
    runtime = _runtime(stop_error=RuntimeError("apiserver unreachable"))

    with patch("preloop.agents.codex.CodexAgent", return_value=runtime):
        assert _stop(client, execution) == {"status": "stopped"}

    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.stop_requested_at is not None
    assert execution.stop_confirmed_at is None
    assert "runtime teardown failed" in execution.stop_reason
    assert "apiserver unreachable" in execution.stop_reason
    runtime.is_stopped.assert_not_awaited()


def test_accepted_kubernetes_deletion_is_not_confirmation(client, db_session, flow):
    """``stop()`` returned (deletion accepted) but the pods are still there."""
    execution = _execution(db_session, flow, "RUNNING")
    execution.agent_session_reference = "agent-k8s-2"
    execution.launch_requested_at = execution.start_time
    db_session.commit()
    runtime = _runtime(stopped=False)

    with patch("preloop.agents.codex.CodexAgent", return_value=runtime):
        assert _stop(client, execution) == {"status": "stopped"}

    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.stop_requested_at is not None
    assert execution.stop_confirmed_at is None
    # Pending, not failed: no reason to record.
    assert execution.stop_reason is None

    # Deletion completes; the monitor (or the recovery pass resuming it)
    # confirms through the shared helper.
    crud_flow_execution.confirm_stop(db_session, execution_id=execution.id)
    db_session.refresh(execution)
    assert execution.stop_confirmed_at is not None


def test_stop_of_an_admitted_launch_without_runtime_is_unconfirmed(
    client, db_session, flow
):
    """STARTING, admitted, no reference yet: the runtime may be coming up."""
    execution = _execution(db_session, flow, "STARTING")
    execution.launch_requested_at = execution.start_time
    db_session.commit()

    assert _stop(client, execution) == {"status": "stopped"}

    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.stop_requested_at is not None
    assert execution.stop_confirmed_at is None
    assert "no runtime reference" in execution.stop_reason


@pytest.mark.parametrize("status", ["STOPPED", "FAILED", "SUCCEEDED", "CANCELLED"])
def test_admission_refuses_a_terminal_row_and_leaves_it(db_session, flow, status):
    execution = _execution(db_session, flow, status)
    db_session.commit()

    assert (
        crud_flow_execution.admit_runtime_start(db_session, execution_id=execution.id)
        is False
    )
    db_session.refresh(execution)
    assert execution.status == status
    assert execution.launch_requested_at is None


def test_admission_refuses_a_stop_request(client, db_session, flow):
    execution = _execution(db_session, flow, "STARTING")
    db_session.commit()
    assert _stop(client, execution) == {"status": "stopped"}

    assert (
        crud_flow_execution.admit_runtime_start(db_session, execution_id=execution.id)
        is False
    )
    db_session.refresh(execution)
    assert execution.status == "STOPPED"


def test_launch_status_never_replaces_a_stop(client, db_session, flow):
    execution = _execution(db_session, flow, "INITIALIZING")
    db_session.commit()
    assert _stop(client, execution) == {"status": "stopped"}

    for status in ("INITIALIZING", "STARTING", "RUNNING"):
        assert (
            crud_flow_execution.claim_live_status(
                db_session, execution_id=execution.id, status=status
            )
            is False
        )
    db_session.commit()
    db_session.refresh(execution)
    assert execution.status == "STOPPED"
    assert execution.error_message == "Manually stopped by user"
