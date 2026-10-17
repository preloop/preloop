"""Persistent flows request owner consent before creating agent sessions."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents import agent_control as module
from preloop.agents.base import AgentStatus
from preloop.agents.errors import AgentStartError


@pytest.fixture
def setup() -> tuple[module.AgentControlExecutor, SimpleNamespace, dict[str, Any]]:
    account_id, agent_id, execution_id = uuid4(), uuid4(), uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        account_id=account_id,
        status="RUNNING",
        agent_session_reference=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        account_id=account_id,
        owner_user_id=uuid4(),
        others_may_start_sessions="ask_me",
        lifecycle_state="active",
        agent_kind="openclaw",
    )
    executor = module.AgentControlExecutor(
        "openclaw",
        {"target_agent_id": str(agent_id)},
        db=MagicMock(),
        account_id=account_id,
        execution=execution,
    )
    return (
        executor,
        agent,
        {
            "execution_id": str(execution_id),
            "account_id": str(account_id),
            "prompt": "Example prompt",
        },
    )


def _update(_db: Any, *, db_obj: Any, obj_in: Any) -> Any:
    for name, value in obj_in.model_dump(exclude_unset=True).items():
        setattr(db_obj, name, value)
    return db_obj


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["allow", "deny"])
async def test_flow_modes(setup: Any, mode: str) -> None:
    executor, agent, context = setup
    agent.others_may_start_sessions = mode
    with patch.object(module.crud_audit_log, "log_action"):
        if mode == "deny":
            with pytest.raises(AgentStartError):
                await executor._flow_owner_consent(agent, context, consume=True)
        else:
            assert await executor._flow_owner_consent(agent, context, consume=True)


@pytest.mark.asyncio
async def test_pending_flow_queues_without_dispatch(setup: Any) -> None:
    executor, agent, context = setup
    decision = SimpleNamespace(status="pending", approval_id=uuid4(), created=False)
    with (
        patch.object(executor, "_resolve_target", return_value=(agent, str(agent.id))),
        patch(
            "preloop.services.owner_consent.check_owner_consent", return_value=decision
        ),
        patch.object(
            module.crud_flow_execution, "get", return_value=executor.execution
        ),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(
            module.crud_flow_execution, "admit_runtime_start", return_value=True
        ),
        patch.object(
            module, "dispatch_operator_message", new_callable=AsyncMock
        ) as dispatch,
    ):
        reference = await executor.start(context)
    assert reference == f"control-consent:{agent.id}:{executor.execution.id}"
    assert executor.execution.error_message == "Waiting for agent owner"
    assert executor._consent_queue_context == context
    dispatch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "declined", "expired", "consumed"])
async def test_queue_polls_owner_decision_without_dispatch(
    setup: Any, status: str
) -> None:
    executor, agent, context = setup
    executor._consent_queue_context = context
    reference = f"control-consent:{agent.id}:{executor.execution.id}"
    decision = SimpleNamespace(status=status, approval_id=uuid4(), created=False)
    with (
        patch.object(
            module.crud_flow_execution, "get", return_value=executor.execution
        ),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(
            module.crud_managed_agent, "get_visible_target", return_value=agent
        ),
        patch(
            "preloop.services.owner_consent.check_owner_consent", return_value=decision
        ),
        patch.object(executor, "start", new_callable=AsyncMock) as start,
    ):
        result = await executor.get_status(reference)
    assert result == (
        AgentStatus.PENDING if status == "pending" else AgentStatus.FAILED
    )
    start.assert_not_called()


@pytest.mark.asyncio
async def test_approved_queue_dispatches_once_then_tracks_command(setup: Any) -> None:
    executor, agent, context = setup
    executor._consent_queue_context = context
    reference = f"control-consent:{agent.id}:{executor.execution.id}"
    command_reference = f"control:{agent.id}:{uuid4()}"
    decision = SimpleNamespace(status="approved", approval_id=uuid4(), created=False)
    command = SimpleNamespace(status="pending", expires_at=None)
    with (
        patch.object(
            module.crud_flow_execution, "get", return_value=executor.execution
        ),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(
            module.crud_managed_agent, "get_visible_target", return_value=agent
        ),
        patch(
            "preloop.services.owner_consent.check_owner_consent", return_value=decision
        ),
        patch.object(
            executor, "start", new_callable=AsyncMock, return_value=command_reference
        ) as start,
        patch.object(executor, "_load_command", return_value=command),
        patch.object(
            module.crud_agent_control_command,
            "command_result_payload",
            return_value=None,
        ),
    ):
        assert await executor.get_status(reference) == AgentStatus.STARTING
        assert await executor.get_status(reference) == AgentStatus.RUNNING
    start.assert_awaited_once_with(context)


@pytest.mark.asyncio
async def test_approved_intent_must_be_consumed_once(setup: Any) -> None:
    executor, agent, context = setup
    decision = SimpleNamespace(status="approved", approval_id=uuid4(), created=False)
    with (
        patch(
            "preloop.services.owner_consent.check_owner_consent", return_value=decision
        ),
        patch(
            "preloop.models.crud.owner_consent.consume", side_effect=[True, False]
        ) as consume,
        patch.object(module.crud_audit_log, "log_action"),
    ):
        assert await executor._flow_owner_consent(agent, context, consume=True)
        with pytest.raises(AgentStartError):
            await executor._flow_owner_consent(agent, context, consume=True)
    assert consume.call_count == 2


@pytest.mark.asyncio
async def test_restart_without_launch_credentials_fails_closed(setup: Any) -> None:
    executor, agent, _ = setup
    reference = f"control-consent:{agent.id}:{executor.execution.id}"
    decision = SimpleNamespace(status="approved", approval_id=uuid4(), created=False)
    with (
        patch.object(
            module.crud_flow_execution, "get", return_value=executor.execution
        ),
        patch.object(module.crud_flow_execution, "get_stop_request", return_value=None),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(
            module.crud_managed_agent, "get_visible_target", return_value=agent
        ),
        patch(
            "preloop.services.owner_consent.check_owner_consent", return_value=decision
        ),
        patch.object(
            module, "dispatch_operator_message", new_callable=AsyncMock
        ) as dispatch,
    ):
        assert await executor.get_status(reference) == AgentStatus.FAILED
    assert "temporary runtime credentials" in executor.execution.error_message
    dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_stop_pending_queue_never_interrupts_agent(setup: Any) -> None:
    executor, agent, context = setup
    executor._consent_queue_context = context
    reference = f"control-consent:{agent.id}:{executor.execution.id}"
    with (
        patch.object(
            module.crud_flow_execution, "get", return_value=executor.execution
        ),
        patch.object(module.crud_flow_execution, "update", side_effect=_update),
        patch.object(
            module, "dispatch_operator_message", new_callable=AsyncMock
        ) as dispatch,
    ):
        await executor.stop(reference)
        assert await executor.is_stopped(reference)
    dispatch.assert_not_called()
    assert executor._consent_queue_context is None
