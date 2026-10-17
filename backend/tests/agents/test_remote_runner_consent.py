"""Runner owner decisions remain effective while the runner is offline."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents import remote_runner as module
from preloop.agents.base import AgentStatus
from preloop.models.crud import owner_consent


@pytest.fixture
def execution() -> SimpleNamespace:
    execution_id = uuid4()
    return SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        status="PENDING",
        start_time=datetime.now(timezone.utc) - timedelta(hours=2),
        end_time=None,
        error_message="Waiting for runner owner",
        agent_session_reference=f"runner:queued:local:{execution_id}",
        resolved_input_prompt="Example prompt",
        result=None,
    )


def _update(_db: Any, *, db_obj: Any, obj_in: Any) -> Any:
    for key, value in obj_in.model_dump(exclude_unset=True).items():
        setattr(db_obj, key, value)
    return db_obj


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["declined", "expired", "cancelled", "consumed"])
async def test_offline_negative_decision_terminates_without_lease(
    execution: SimpleNamespace, decision: str
) -> None:
    executor = module.RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with (
        patch.object(module.crud_flow_execution, "get", return_value=execution),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(owner_consent, "poll_runner_execution", return_value=decision),
        patch.object(module, "lease_job") as lease,
    ):
        status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.FAILED
    assert execution.status == "FAILED"
    assert execution.end_time is not None
    assert "runner owner declined this run" in execution.error_message
    lease.assert_not_called()


@pytest.mark.asyncio
async def test_pending_offline_approval_uses_approval_deadline(
    execution: SimpleNamespace,
) -> None:
    executor = module.RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with (
        patch.object(module.crud_flow_execution, "get", return_value=execution),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(owner_consent, "poll_runner_execution", return_value="pending"),
        patch.object(module, "lease_job") as lease,
    ):
        status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.PENDING
    assert execution.status == "PENDING"
    assert execution.error_message == "Waiting for runner owner"
    lease.assert_not_called()


@pytest.mark.asyncio
async def test_approved_offline_request_resumes_queue_timeout(
    execution: SimpleNamespace,
) -> None:
    executor = module.RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with (
        patch.object(module.crud_flow_execution, "get", return_value=execution),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(owner_consent, "poll_runner_execution", return_value="approved"),
        patch.object(module, "lease_job") as lease,
    ):
        status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.FAILED
    assert "No matching self-hosted runner" in execution.error_message
    lease.assert_not_called()


@pytest.mark.asyncio
async def test_pending_consent_does_not_trigger_harness_fail_fallback(
    execution: SimpleNamespace,
) -> None:
    db = MagicMock()
    executor = module.RemoteRunnerExecutor(
        "codex", {"harness_fallback": "fail"}, db=db, pool="local", account_id=uuid4()
    )

    def lease(*args: Any, **kwargs: Any) -> None:
        kwargs["outcome"].update(
            consent_status="pending", routing_reason="runner_unavailable"
        )
        return None

    payload = {module.HOST_EXEC_LEASE_KEY: {"harness": "codex"}}
    with (
        patch.object(executor, "_lease_payload", return_value=payload),
        patch.object(executor, "_harness_pin", return_value=None),
        patch.object(executor, "_owner_consent_outcome", new_callable=AsyncMock),
        patch.object(module, "workspace_owner_runner_id", return_value=None),
        patch.object(module, "lease_job", side_effect=lease),
        patch.object(
            module, "harness_fallback_mode", return_value=module.HARNESS_FALLBACK_FAIL
        ),
        patch.object(module.crud_flow_execution, "get", return_value=execution),
    ):
        reference = await executor.start({"execution_id": str(execution.id)})
    assert reference.startswith("runner:queued:")
    assert execution.error_message == "Waiting for runner owner"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [OSError("notification unavailable"), TimeoutError()])
async def test_notification_failure_keeps_execution_pending(
    execution: SimpleNamespace, error: Exception
) -> None:
    executor = module.RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    consent = SimpleNamespace(created=True, approval_id=uuid4())
    with (
        patch(
            "preloop.services.owner_consent.notify_owner_consent",
            new_callable=AsyncMock,
            side_effect=error,
        ),
        patch.object(module.crud_flow_execution, "get", return_value=execution),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
    ):
        await executor._owner_consent_outcome(
            {"owner_consent": consent, "consent_status": "pending"}, execution.id
        )
    assert execution.status == "PENDING"
    assert execution.error_message == "Waiting for runner owner"


def test_terminal_decision_audited_once(execution: SimpleNamespace) -> None:
    executor = module.RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with (
        patch.object(module.crud_audit_log, "log_action") as audit,
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
    ):
        executor._fail_owner_consent(execution)
        executor._fail_owner_consent(execution)
    audit.assert_called_once()
    assert audit.call_args.kwargs["status"] == "denied"
