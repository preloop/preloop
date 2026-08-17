"""Tests for RemoteRunnerExecutor lease and offline-fail policy."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from preloop.agents.factory import create_executor_for_execution
from preloop.agents.codex import CodexAgent
from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.agents.base import AgentStatus
from preloop.services.runner_service import persistable_job_payload


@pytest.mark.asyncio
async def test_remote_runner_queues_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid4()
    db = MagicMock()
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=None,
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="do work",
        model_output_summary=None,
        result=None,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *args, **kwargs: execution,
    )

    executor = RemoteRunnerExecutor(
        "codex", {}, db=db, pool="local", account_id=uuid4()
    )
    ref = await executor.start({"execution_id": str(execution_id)})
    assert ref.startswith("runner:queued:local:")
    assert execution.agent_session_reference == ref


@pytest.mark.asyncio
async def test_queued_status_fails_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid4()
    started = datetime.now(timezone.utc) - timedelta(minutes=16)
    execution = SimpleNamespace(
        id=execution_id,
        start_time=started,
        status="PENDING",
        error_message=None,
        end_time=None,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:local:{execution_id}",
        resolved_input_prompt=None,
        model_output_summary=None,
        result=None,
    )
    db = MagicMock()
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *args, **kwargs: execution,
    )
    executor = RemoteRunnerExecutor(
        "codex", {}, db=db, pool="local", account_id=uuid4()
    )
    status = await executor.get_status(f"runner:queued:local:{execution_id}")
    assert status == AgentStatus.FAILED
    assert execution.status == "FAILED"
    assert "No matching self-hosted runner" in (execution.error_message or "")


@pytest.mark.asyncio
async def test_queued_status_releases_complete_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid4()
    flow_id = uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=flow_id,
        flow=None,
        runner_id=None,
        agent_session_reference=None,
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="do work",
        model_output_summary=None,
        result=None,
    )
    runner = SimpleNamespace(id=uuid4(), pending_job=None)
    leased_payloads: list[dict[str, Any]] = []
    pushed_payloads: list[dict[str, Any]] = []

    def fake_lease(*args: Any, **kwargs: Any) -> Any:
        payload = dict(kwargs["payload"])
        leased_payloads.append(payload)
        if len(leased_payloads) == 1:
            return None
        runner.pending_job = persistable_job_payload(payload)
        return runner

    async def fake_push(runner_id: UUID, payload: dict[str, Any]) -> None:
        assert runner_id == runner.id
        pushed_payloads.append(dict(payload))

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", fake_lease)
    monkeypatch.setattr("preloop.agents.remote_runner._push_job", fake_push)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *args, **kwargs: execution,
    )

    context = {
        "execution_id": str(execution_id),
        "flow_id": str(flow_id),
        "agent_type": "codex",
        "agent_config": {"image": "preloop/codex:latest"},
        "prompt": "do work",
        "model_identifier": "gpt-5.4",
        "model_provider": "openai",
        "account_api_token": "live-secret",
        "allowed_mcp_servers": ["github"],
        "allowed_mcp_tools": [{"server": "github", "tool": "get_issue"}],
        "git_clone_config": {"repositories": [{"url": "example/repo"}]},
        "custom_commands": {"enabled": True, "commands": ["make test"]},
    }
    executor = RemoteRunnerExecutor(
        "codex",
        context["agent_config"],
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
    )

    ref = await executor.start(context)
    status = await executor.get_status(ref)

    assert status == AgentStatus.STARTING
    delayed_payload = leased_payloads[1]
    for key in (
        "model_identifier",
        "model_provider",
        "allowed_mcp_servers",
        "allowed_mcp_tools",
        "git_clone_config",
        "custom_commands",
    ):
        assert delayed_payload[key] == context[key]
    assert delayed_payload["account_api_token"] == "live-secret"
    assert pushed_payloads == [delayed_payload]
    assert "account_api_token" not in runner.pending_job


def test_factory_uses_remote_runner_when_pool_set() -> None:
    flow = SimpleNamespace(runner_pool="local", account_id=uuid4())
    executor = create_executor_for_execution(
        "codex",
        {"model": "gpt-5.4"},
        flow=flow,
        db=MagicMock(),
        execution_context={},
    )
    assert isinstance(executor, RemoteRunnerExecutor)
    assert executor.pool == "local"


def test_factory_keeps_hosted_executor_without_pool() -> None:
    flow = SimpleNamespace(runner_pool=None, account_id=uuid4())
    executor = create_executor_for_execution(
        "codex",
        {"model": "gpt-5.4"},
        flow=flow,
        db=MagicMock(),
        execution_context={},
    )
    assert isinstance(executor, CodexAgent)
