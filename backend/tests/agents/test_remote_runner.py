"""Tests for RemoteRunnerExecutor lease and offline-fail policy."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest

from preloop.agents.factory import create_executor_for_execution
from preloop.agents.codex import CodexAgent
from preloop.agents.images import default_agent_image
from preloop.agents.remote_runner import (
    RemoteRunnerExecutor,
    payload_for_log,
)
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
async def test_fresh_queued_executor_reconstructs_complete_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = uuid4()
    flow_id = uuid4()
    account_id = uuid4()
    flow = SimpleNamespace(
        id=flow_id,
        name="Self-hosted review",
        runner_pool="local",
        account_id=account_id,
        agent_type="codex",
        agent_config={"image": "preloop/codex:latest"},
        allowed_mcp_servers=["github"],
        allowed_mcp_tools=[{"server": "github", "tool": "get_issue"}],
        git_clone_config={"repositories": [{"url": "example/repo"}]},
        custom_commands={"enabled": True, "commands": ["make test"]},
        ai_model=SimpleNamespace(
            model_identifier="gpt-5.4",
            provider_name="openai",
        ),
    )
    execution = SimpleNamespace(
        id=execution_id,
        flow_id=flow_id,
        flow=flow,
        runner_id=None,
        agent_session_reference=f"runner:queued:local:{execution_id}",
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
    monkeypatch.setattr(
        "preloop.agents.remote_runner.create_flow_runtime_token",
        lambda *args, **kwargs: ("fresh-runtime-token", uuid4()),
    )

    executor = create_executor_for_execution(
        "codex",
        {"agent_config": flow.agent_config},
        flow=flow,
        execution=execution,
        db=MagicMock(),
        execution_context=None,
    )
    assert isinstance(executor, RemoteRunnerExecutor)

    status = await executor.get_status(execution.agent_session_reference)

    assert status == AgentStatus.STARTING
    assert len(leased_payloads) == 1
    delayed_payload = leased_payloads[0]
    assert delayed_payload["agent_config"] == flow.agent_config
    assert delayed_payload["prompt"] == execution.resolved_input_prompt
    assert delayed_payload["model_identifier"] == flow.ai_model.model_identifier
    assert delayed_payload["model_provider"] == flow.ai_model.provider_name
    assert delayed_payload["allowed_mcp_servers"] == flow.allowed_mcp_servers
    assert delayed_payload["allowed_mcp_tools"] == flow.allowed_mcp_tools
    assert delayed_payload["git_clone_config"] == flow.git_clone_config
    assert delayed_payload["custom_commands"] == flow.custom_commands
    assert delayed_payload.get("account_api_token") is None
    assert len(pushed_payloads) == 1
    pushed_payload = pushed_payloads[0]
    assert pushed_payload["account_api_token"] == "fresh-runtime-token"
    assert pushed_payload["agent_config"] == flow.agent_config
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


def test_lease_payload_injects_default_image() -> None:
    execution_id = uuid4()
    flow_id = uuid4()
    executor = RemoteRunnerExecutor(
        "opencode", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    payload = executor._lease_payload(
        execution_id=execution_id,
        flow_id=flow_id,
        prompt="review",
        execution_context={"agent_type": "opencode", "agent_config": {}},
    )
    image = default_agent_image("opencode")
    assert image
    assert payload["agent_config"]["image"] == image


def test_lease_payload_keeps_explicit_image() -> None:
    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    payload = executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="review",
        execution_context={
            "agent_type": "codex",
            "agent_config": {"docker_image": "custom/codex:dev"},
        },
    )
    assert payload["agent_config"]["docker_image"] == "custom/codex:dev"
    assert "image" not in payload["agent_config"]


def test_lease_payload_includes_resume_from() -> None:
    prior = uuid4()
    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    payload = executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="continue",
        execution_context={
            "agent_type": "codex",
            "agent_config": {"image": "preloop/codex:latest"},
            "trigger_event_data": {"_resume": {"execution_id": str(prior)}},
        },
    )
    assert payload["resume_from"] == str(prior)


def test_payload_for_log_strips_account_api_token() -> None:
    token = "live-runtime-token-should-never-log"
    redacted = payload_for_log(
        {
            "execution_id": "exec-1",
            "account_api_token": token,
            "agent_config": {"image": "preloop/agent:dev"},
        }
    )
    assert token not in str(redacted)
    assert "account_api_token" not in redacted


@pytest.mark.asyncio
async def test_start_logs_do_not_include_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    token = "super-secret-runtime-token"
    execution_id = uuid4()
    runner = SimpleNamespace(id=uuid4(), pending_job=None)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job",
        lambda *args, **kwargs: runner,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *args, **kwargs: SimpleNamespace(
            id=execution_id,
            runner_id=None,
            agent_session_reference=None,
        ),
    )

    async def fake_push(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr("preloop.agents.remote_runner._push_job", fake_push)
    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with caplog.at_level(logging.DEBUG):
        await executor.start(
            {
                "execution_id": str(execution_id),
                "account_api_token": token,
                "agent_config": {},
            }
        )
    assert token not in caplog.text


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
