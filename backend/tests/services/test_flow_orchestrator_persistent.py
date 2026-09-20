"""Orchestrator start and timeout for persistent Agent Control executions."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.agent_control import AgentControlExecutor
from preloop.agents.base import AgentStatus
from preloop.models.crud.agent_control_command import COMMAND_RESULT_ENVELOPE_KEY
from preloop.services.flow_orchestrator import (
    FlowExecutionOrchestrator,
    TimeoutBudget,
)


def _patch_monitor_side_channels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.crud_flow_execution.get_park_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_listen_for_commands",
        AsyncMock(),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_stream_logs_to_nats",
        AsyncMock(),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_capture_result_artifact",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_sync_runtime_tool_activity_metrics",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_cleanup_monitoring",
        AsyncMock(),
    )

    async def fast_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.asyncio.sleep",
        fast_sleep,
    )


def _orchestrator(account_id, execution_id, agent_id, *, timeout_seconds: int = 5):
    nats = AsyncMock()
    nats.is_connected = True
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data={
            "source": "github",
            "payload": {
                "repository": {"full_name": "example/repo"},
                "ref": "refs/heads/feature",
            },
        },
        nats_client=nats,
    )
    orchestrator.flow = SimpleNamespace(
        id=orchestrator.flow_id,
        name="Persistent review",
        account_id=account_id,
        agent_type="codex",
        agent_config={
            "execution_path": "persistent",
            "target_agent_id": str(agent_id),
        },
        timeout_seconds=timeout_seconds,
        runner_pool=None,
    )
    orchestrator.execution_log = SimpleNamespace(
        id=execution_id,
        account_id=account_id,
        trigger_event_details={},
        agent_session_reference=None,
    )
    orchestrator._execution_timeout_budget = lambda: TimeoutBudget(
        seconds=timeout_seconds, source="flow"
    )
    return orchestrator


@pytest.mark.asyncio
async def test_start_and_poll_succeeds_when_command_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_monitor_side_channels(monkeypatch)
    account_id = uuid4()
    execution_id = uuid4()
    agent_id = uuid4()
    command = SimpleNamespace(
        status="pending",
        expires_at=None,
        envelope={"payload": {"text": "review"}},
        last_error=None,
        account_id=account_id,
        managed_agent_id=agent_id,
        command_id="cmd-complete-1",
        runtime_session_id=uuid4(),
        created_at=None,
        delivered_at=None,
        acked_at=None,
        kind="command",
    )
    polls = {"n": 0}

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            command_id=command.command_id,
            local_delivery=True,
            subject=None,
        )

    def fake_get_command(*args: Any, **kwargs: Any) -> SimpleNamespace:
        polls["n"] += 1
        if polls["n"] >= 2:
            command.status = "acked"
            command.envelope = {
                COMMAND_RESULT_ENVELOPE_KEY: {
                    "status": "completed",
                    "reply_text": "Review posted",
                }
            }
        return command

    agent = SimpleNamespace(
        id=agent_id,
        account_id=account_id,
        display_name="Review node",
        lifecycle_state="active",
        agent_kind="openclaw",
        session_source_type="openclaw",
        session_source_id="openclaw-example",
        runtime_session_id=uuid4(),
        control_last_heartbeat_at=None,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.create_command_history_session",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.bind_agent_control_command",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.get_by_command_id",
        fake_get_command,
    )

    orchestrator = _orchestrator(account_id, execution_id, agent_id, timeout_seconds=15)
    context = {
        "agent_type": "codex",
        "agent_config": orchestrator.flow.agent_config,
        "prompt": "Review https://github.com/example/repo/pull/1",
        "execution_id": str(execution_id),
        "flow_id": str(orchestrator.flow_id),
        "flow_name": orchestrator.flow.name,
        "account_id": account_id,
    }
    reference, executor = await orchestrator._start_agent_session(context)
    assert isinstance(executor, AgentControlExecutor)
    assert reference == f"control:{agent_id}:{command.command_id}"

    result = await orchestrator._monitor_agent_execution(reference, executor)
    assert result["status"] == "SUCCEEDED"
    assert result.get("output_summary") == "Review posted"


@pytest.mark.asyncio
async def test_timeout_calls_stop_when_command_never_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_monitor_side_channels(monkeypatch)
    account_id = uuid4()
    execution_id = uuid4()
    agent_id = uuid4()
    command = SimpleNamespace(
        status="pending",
        expires_at=None,
        envelope={"payload": {"text": "review"}},
        last_error=None,
        account_id=account_id,
        managed_agent_id=agent_id,
        command_id="cmd-timeout-1",
        runtime_session_id=uuid4(),
        created_at=None,
        delivered_at=None,
        acked_at=None,
        kind="command",
    )
    stopped: list[str] = []
    original_stop = AgentControlExecutor.stop

    async def tracking_stop(self, session_reference: str) -> None:
        stopped.append(session_reference)
        await original_stop(self, session_reference)

    monkeypatch.setattr(AgentControlExecutor, "stop", tracking_stop)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: SimpleNamespace(
            id=agent_id,
            account_id=account_id,
            display_name="Review node",
            lifecycle_state="active",
            agent_kind="openclaw",
            session_source_type="openclaw",
            session_source_id="openclaw-example",
            runtime_session_id=uuid4(),
            control_last_heartbeat_at=None,
        ),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        AsyncMock(
            return_value=SimpleNamespace(
                command_id=command.command_id,
                local_delivery=True,
                subject=None,
            )
        ),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.create_command_history_session",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.bind_agent_control_command",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.get_by_command_id",
        lambda *args, **kwargs: command,
    )

    def fake_mark(*args: Any, **kwargs: Any) -> SimpleNamespace:
        command.status = "failed"
        command.last_error = kwargs.get("error")
        command.envelope = {
            COMMAND_RESULT_ENVELOPE_KEY: kwargs["result_payload"],
        }
        return command

    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.mark_terminal_result",
        fake_mark,
    )

    orchestrator = _orchestrator(account_id, execution_id, agent_id)
    context = {
        "agent_type": "codex",
        "agent_config": orchestrator.flow.agent_config,
        "prompt": "Review https://github.com/example/repo/pull/1",
        "execution_id": str(execution_id),
        "flow_id": str(orchestrator.flow_id),
        "flow_name": orchestrator.flow.name,
        "account_id": account_id,
    }
    reference, executor = await orchestrator._start_agent_session(context)
    result = await orchestrator._monitor_agent_execution(reference, executor)

    assert result["status"] == "FAILED"
    assert "timed out" in (result["error_message"] or "").lower()
    assert stopped == [reference]
    assert await executor.get_status(reference) == AgentStatus.FAILED
    assert await executor.is_stopped(reference) is True


@pytest.mark.asyncio
async def test_user_stop_reports_success_when_result_wins_the_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stop press racing command_result reports SUCCEEDED, not a timeout.

    mark_terminal_result keeps the first payload, so an interrupt cannot
    overwrite a completed result. The monitor must then fall through to the
    real status instead of re-dispatching interrupt every poll.
    """
    _patch_monitor_side_channels(monkeypatch)
    account_id = uuid4()
    execution_id = uuid4()
    agent_id = uuid4()
    command = SimpleNamespace(
        status="pending",
        expires_at=None,
        envelope={"payload": {"text": "review"}},
        last_error=None,
        account_id=account_id,
        managed_agent_id=agent_id,
        command_id="cmd-stop-race-1",
        runtime_session_id=uuid4(),
        created_at=None,
        delivered_at=None,
        acked_at=None,
        kind="command",
    )
    interrupts: list[str] = []

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        if kwargs.get("interrupt"):
            interrupts.append(str(kwargs.get("session_mode")))
            command.status = "acked"
            command.envelope = {
                COMMAND_RESULT_ENVELOPE_KEY: {
                    "status": "completed",
                    "reply_text": "Review posted",
                }
            }
        return SimpleNamespace(
            command_id=command.command_id,
            local_delivery=True,
            subject=None,
        )

    def fake_mark(*args: Any, **kwargs: Any) -> SimpleNamespace:
        existing = (command.envelope or {}).get(COMMAND_RESULT_ENVELOPE_KEY)
        if existing:
            return command
        command.status = "failed" if kwargs.get("failed") else "acked"
        command.last_error = kwargs.get("error")
        command.envelope = {
            COMMAND_RESULT_ENVELOPE_KEY: kwargs["result_payload"],
        }
        return command

    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: SimpleNamespace(
            id=agent_id,
            account_id=account_id,
            display_name="Review node",
            lifecycle_state="active",
            agent_kind="openclaw",
            session_source_type="openclaw",
            session_source_id="openclaw-example",
            runtime_session_id=uuid4(),
            control_last_heartbeat_at=None,
        ),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.create_command_history_session",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.bind_agent_control_command",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.get_by_command_id",
        lambda *args, **kwargs: command,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.mark_terminal_result",
        fake_mark,
    )

    orchestrator = _orchestrator(account_id, execution_id, agent_id, timeout_seconds=15)
    context = {
        "agent_type": "codex",
        "agent_config": orchestrator.flow.agent_config,
        "prompt": "Review https://github.com/example/repo/pull/1",
        "execution_id": str(execution_id),
        "flow_id": str(orchestrator.flow_id),
        "flow_name": orchestrator.flow.name,
        "account_id": account_id,
    }
    reference, executor = await orchestrator._start_agent_session(context)
    orchestrator._stop_requested.set()
    result = await orchestrator._monitor_agent_execution(reference, executor)

    assert result["status"] == "SUCCEEDED"
    assert result.get("output_summary") == "Review posted"
    assert interrupts == ["current"]
    assert await executor.is_stopped(reference) is False
