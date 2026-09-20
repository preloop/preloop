"""Tests for AgentControlExecutor persistent flow dispatch."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.agents.agent_control import (
    AgentControlExecutor,
    parse_control_session_reference,
)
from preloop.agents.base import AgentStatus
from preloop.agents.errors import AgentStartError
from preloop.models.crud.agent_control_command import COMMAND_RESULT_ENVELOPE_KEY
from preloop.services.agent_control_dispatch import AgentControlDispatchError


def _account_id():
    return uuid4()


def _agent(**overrides: Any) -> SimpleNamespace:
    values = {
        "id": uuid4(),
        "account_id": _account_id(),
        "display_name": "Review node",
        "lifecycle_state": "active",
        "agent_kind": "openclaw",
        "session_source_type": "openclaw",
        "session_source_id": "openclaw-example",
        "runtime_session_id": uuid4(),
        "control_last_heartbeat_at": datetime.now(UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _executor(*, agent_id: Any = None, account_id: Any = None, **config: Any):
    account_id = account_id or _account_id()
    target = agent_id or uuid4()
    merged = {
        "execution_path": "persistent",
        "target_agent_id": str(target),
        **config,
    }
    execution_id = uuid4()
    return AgentControlExecutor(
        "codex",
        merged,
        db=MagicMock(),
        account_id=account_id,
        flow=SimpleNamespace(
            timeout_seconds=1800,
            name="Persistent review",
            account_id=account_id,
        ),
        execution=SimpleNamespace(id=execution_id, account_id=account_id),
    )


def _command_record(*, status: str, **overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "status": status,
        "expires_at": datetime.now(UTC) + timedelta(hours=1),
        "created_at": datetime.now(UTC),
        "delivered_at": None,
        "acked_at": None,
        "last_error": None,
        "envelope": {"payload": {"text": "review this"}},
        "account_id": uuid4(),
        "managed_agent_id": uuid4(),
        "command_id": "cmd-example-1",
        "runtime_session_id": uuid4(),
        "kind": "command",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.fixture
def connected_patches(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: True,
    )
    history = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        "preloop.agents.agent_control.create_command_history_session",
        lambda *args, **kwargs: history,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_runtime_session_activity."
        "log_agent_control_message",
        MagicMock(),
    )
    bind = MagicMock()
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.bind_agent_control_command",
        bind,
    )
    return SimpleNamespace(history=history, bind=bind)


@pytest.mark.asyncio
async def test_start_dispatches_and_binds(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent()
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    captured: dict[str, Any] = {}

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        captured.update(kwargs)
        return SimpleNamespace(
            command_id="cmd-example-1",
            local_delivery=True,
            subject=None,
        )

    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )

    prompt = "Review https://github.com/example/repo/pull/1"
    reference = await executor.start(
        {
            "prompt": prompt,
            "execution_id": str(executor.execution.id),
            "flow_id": str(uuid4()),
            "flow_name": "Persistent review",
            "account_id": agent.account_id,
            "timeout_seconds": 1800,
            "trigger_event_data": {
                "source": "github",
                "payload": {
                    "repository": {"full_name": "example/repo"},
                    "ref": "refs/heads/feature",
                },
            },
        }
    )

    assert reference == f"control:{agent.id}:cmd-example-1"
    assert captured["text"] == prompt
    assert captured["start_new_session"] is True
    assert captured["input_mode"] == "text"
    assert captured["session_mode"] == "new"
    assert captured["source"] == "flow_execution"
    metadata = captured["metadata"]
    assert metadata["source"] == "flow_execution"
    assert metadata["repository"] == "example/repo"
    assert metadata["ref"] == "refs/heads/feature"
    assert metadata["timeout_seconds"] == 1800
    connected_patches.bind.assert_called_once()
    bind_kwargs = connected_patches.bind.call_args.kwargs
    assert bind_kwargs["command_id"] == "cmd-example-1"
    assert bind_kwargs["session_reference"] == reference


@pytest.mark.asyncio
async def test_start_missing_target_fails_fast() -> None:
    executor = _executor()
    executor.config.pop("target_agent_id")
    with pytest.raises(AgentStartError, match="missing") as excinfo:
        await executor.start({"prompt": "do work", "account_id": executor.account_id})
    assert excinfo.value.category == "runner_error"


@pytest.mark.asyncio
async def test_start_inactive_agent_fails(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent(lifecycle_state="suspended")
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    with pytest.raises(AgentStartError, match="is not active") as excinfo:
        await executor.start({"prompt": "do work", "account_id": agent.account_id})
    assert excinfo.value.category == "runner_error"


@pytest.mark.asyncio
async def test_start_unsupported_kind_fails(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent(agent_kind="codex", session_source_type="codex")
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    with pytest.raises(AgentStartError, match="not connected") as excinfo:
        await executor.start({"prompt": "do work", "account_id": agent.account_id})
    assert excinfo.value.category == "runner_error"


@pytest.mark.asyncio
async def test_start_without_control_config_fails(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent()
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.agent_has_control_config",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(AgentStartError, match="not connected") as excinfo:
        await executor.start({"prompt": "do work", "account_id": agent.account_id})
    assert excinfo.value.category == "runner_error"


@pytest.mark.asyncio
async def test_start_stale_heartbeat_fails(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent(control_last_heartbeat_at=datetime.now(UTC) - timedelta(minutes=10))
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.control_heartbeat_is_fresh",
        lambda *args, **kwargs: False,
    )
    with pytest.raises(AgentStartError, match="not connected") as excinfo:
        await executor.start({"prompt": "do work", "account_id": agent.account_id})
    assert excinfo.value.category == "runner_error"


def _status_executor(monkeypatch: pytest.MonkeyPatch, record: SimpleNamespace):
    executor = _executor(agent_id=record.managed_agent_id, account_id=record.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.get_by_command_id",
        lambda *args, **kwargs: record,
    )
    return executor, f"control:{record.managed_agent_id}:{record.command_id}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "envelope", "expected"),
    [
        ("pending", {}, AgentStatus.RUNNING),
        ("delivered", {}, AgentStatus.RUNNING),
        ("acked", {}, AgentStatus.RUNNING),
        (
            "acked",
            {
                COMMAND_RESULT_ENVELOPE_KEY: {
                    "status": "completed",
                    "reply_text": "looks good",
                }
            },
            AgentStatus.SUCCEEDED,
        ),
        (
            "acked",
            {COMMAND_RESULT_ENVELOPE_KEY: {"status": "failed", "error": "boom"}},
            AgentStatus.FAILED,
        ),
        ("failed", {}, AgentStatus.FAILED),
        ("expired", {}, AgentStatus.FAILED),
    ],
)
async def test_get_status_mapping(
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    envelope: dict[str, Any],
    expected: AgentStatus,
) -> None:
    record = _command_record(status=status, envelope=envelope or {"payload": {}})
    executor, reference = _status_executor(monkeypatch, record)
    assert await executor.get_status(reference) == expected


@pytest.mark.asyncio
async def test_get_status_pending_past_expiry_is_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
    )
    executor, reference = _status_executor(monkeypatch, record)
    assert await executor.get_status(reference) == AgentStatus.FAILED


@pytest.mark.asyncio
async def test_get_result_uses_reply_text(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _command_record(
        status="acked",
        envelope={
            COMMAND_RESULT_ENVELOPE_KEY: {
                "status": "completed",
                "reply_text": "Review posted on example.com",
            }
        },
    )
    executor, reference = _status_executor(monkeypatch, record)
    result = await executor.get_result(reference)
    assert result.status == AgentStatus.SUCCEEDED
    assert result.output_summary == "Review posted on example.com"


@pytest.mark.asyncio
async def test_stop_sends_interrupt_and_marks_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(status="delivered")
    executor, reference = _status_executor(monkeypatch, record)
    agent = _agent(id=record.managed_agent_id, account_id=record.account_id)
    dispatched: dict[str, Any] = {}

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        dispatched.update(kwargs)
        return SimpleNamespace(command_id="cmd-stop", local_delivery=True, subject=None)

    mark = MagicMock(return_value=record)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.mark_terminal_result",
        mark,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.get",
        lambda *args, **kwargs: SimpleNamespace(
            trigger_event_details={
                "_agent_control": {
                    "managed_agent_id": str(record.managed_agent_id),
                    "command_id": record.command_id,
                    "history_session_id": None,
                }
            }
        ),
    )

    await executor.stop(reference)

    assert dispatched["interrupt"] is True
    assert dispatched["start_new_session"] is False
    assert dispatched["target_session_id"] is None
    assert dispatched["session_mode"] == "current"
    assert dispatched["require_delivery"] is True
    mark.assert_called_once()
    assert mark.call_args.kwargs["failed"] is True
    assert mark.call_args.kwargs["error"] == "stopped"


@pytest.mark.asyncio
async def test_stop_does_not_mark_when_interrupt_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(status="delivered")
    executor, reference = _status_executor(monkeypatch, record)
    agent = _agent(id=record.managed_agent_id, account_id=record.account_id)
    mark = MagicMock(return_value=record)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )

    async def fail_dispatch(*args: Any, **kwargs: Any) -> None:
        raise AgentControlDispatchError(
            "Managed agent command channel is unavailable",
            status_code=503,
        )

    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fail_dispatch,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.mark_terminal_result",
        mark,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.get",
        lambda *args, **kwargs: SimpleNamespace(
            trigger_event_details={
                "_agent_control": {
                    "managed_agent_id": str(record.managed_agent_id),
                    "command_id": record.command_id,
                    "history_session_id": "tracking-uuid-not-a-plugin-id",
                }
            }
        ),
    )

    await executor.stop(reference)

    mark.assert_not_called()
    assert await executor.is_stopped(reference) is False


@pytest.mark.asyncio
async def test_stop_uses_session_reference_when_bind_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(status="delivered")
    executor, reference = _status_executor(monkeypatch, record)
    agent = _agent(id=record.managed_agent_id, account_id=record.account_id)
    dispatched: dict[str, Any] = {}

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        dispatched.update(kwargs)
        return SimpleNamespace(command_id="cmd-stop", local_delivery=True, subject=None)

    mark = MagicMock(return_value=record)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_agent_control_command.mark_terminal_result",
        mark,
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.get",
        lambda *args, **kwargs: SimpleNamespace(trigger_event_details={}),
    )

    await executor.stop(reference)

    assert dispatched["interrupt"] is True
    mark.assert_called_once()


@pytest.mark.asyncio
async def test_start_returns_reference_when_bind_fails(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent()
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )

    async def fake_dispatch(*args: Any, **kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(
            command_id="cmd-bind-fail",
            local_delivery=True,
            subject=None,
        )

    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fake_dispatch,
    )
    connected_patches.bind.side_effect = RuntimeError("bind failed")
    reference = await executor.start(
        {
            "prompt": "Review https://github.com/example/repo/pull/1",
            "execution_id": str(executor.execution.id),
            "account_id": agent.account_id,
        }
    )
    assert reference == f"control:{agent.id}:cmd-bind-fail"


@pytest.mark.asyncio
async def test_start_dispatch_error_includes_cause(
    monkeypatch: pytest.MonkeyPatch, connected_patches
) -> None:
    agent = _agent()
    executor = _executor(agent_id=agent.id, account_id=agent.account_id)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_managed_agent.get_for_account",
        lambda *args, **kwargs: agent,
    )

    async def fail_dispatch(*args: Any, **kwargs: Any) -> None:
        raise AgentControlDispatchError(
            "Managed agent command channel is unavailable",
            status_code=503,
        )

    monkeypatch.setattr(
        "preloop.agents.agent_control.dispatch_operator_message",
        fail_dispatch,
    )
    with pytest.raises(AgentStartError, match="unavailable") as excinfo:
        await executor.start({"prompt": "do work", "account_id": agent.account_id})
    assert "not connected" in str(excinfo.value)
    assert excinfo.value.category == "runner_error"


@pytest.mark.asyncio
async def test_get_result_pending_expiry_explains_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(
        status="pending",
        expires_at=datetime.now(UTC) - timedelta(seconds=5),
        last_error=None,
    )
    executor, reference = _status_executor(monkeypatch, record)
    result = await executor.get_result(reference)
    assert result.status == AgentStatus.FAILED
    assert result.error_message == "Agent Control command expired before delivery"


def test_parse_control_session_reference() -> None:
    managed = uuid4()
    assert parse_control_session_reference(f"control:{managed}:cmd-1") == (
        str(managed),
        "cmd-1",
    )
    with pytest.raises(ValueError):
        parse_control_session_reference("runner:queued:local:x")


@pytest.mark.asyncio
async def test_get_logs_include_lifecycle_and_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = _command_record(
        status="acked",
        delivered_at=datetime.now(UTC),
        acked_at=datetime.now(UTC),
        envelope={
            COMMAND_RESULT_ENVELOPE_KEY: {"status": "completed", "reply_text": "ok"}
        },
    )
    executor, reference = _status_executor(monkeypatch, record)
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_runtime_session_activity."
        "list_for_runtime_session",
        lambda *args, **kwargs: [
            SimpleNamespace(
                timestamp=datetime.now(UTC),
                summary="unrelated operator note",
                activity_type="agent_control_message",
                metadata_={"command_id": "cmd-other"},
            ),
            SimpleNamespace(
                timestamp=datetime.now(UTC),
                summary="wrote review comment",
                activity_type="tool_call",
                metadata_={"command_id": record.command_id},
            ),
        ],
    )
    monkeypatch.setattr(
        "preloop.agents.agent_control.crud_flow_execution.get",
        lambda *args, **kwargs: None,
    )
    lines = await executor.get_logs(reference)
    assert any("queued" in line for line in lines)
    assert any("delivered" in line for line in lines)
    assert any("acked" in line for line in lines)
    assert any("result:" in line for line in lines)
    assert any("wrote review comment" in line for line in lines)
    assert not any("unrelated operator note" in line for line in lines)
    assert all(line.startswith("[agent_control]") for line in lines)
