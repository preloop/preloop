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


@pytest.fixture(autouse=True)
def no_pending_stop(monkeypatch):
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )


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

    async def fake_hydrate(db: Any, job: dict[str, Any]) -> dict[str, Any]:
        return {
            **job,
            "account_api_token": "fresh-runtime-token",
            "launch": {"version": 1},
        }

    monkeypatch.setattr(
        "preloop.agents.remote_runner.prepare_runner_delivery", fake_hydrate
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


def test_payload_for_log_omits_credentials() -> None:
    token = "live-runtime-token-should-never-log"
    api_key = "sk-live-openai-key-should-never-log"
    redacted = payload_for_log(
        {
            "execution_id": "exec-1",
            "agent_type": "codex",
            "account_api_token": token,
            "prompt": "do the work",
            "agent_config": {
                "image": "preloop/agent:dev",
                "openai_api_key": api_key,
                "api_key": api_key,
            },
        }
    )
    dumped = str(redacted)
    assert token not in dumped
    assert api_key not in dumped
    assert "account_api_token" not in redacted
    assert "agent_config" not in redacted
    assert redacted["agent_type"] == "codex"
    assert redacted["image"] == "preloop/agent:dev"
    assert redacted["host_exec_profile"] is None


def test_lease_payload_skips_image_for_host_exec() -> None:
    executor = RemoteRunnerExecutor(
        "cursor",
        {},
        db=MagicMock(),
        pool="local",
        account_id=uuid4(),
    )
    payload = executor._lease_payload(
        execution_id=uuid4(),
        flow_id=uuid4(),
        prompt="summarize",
        execution_context={
            "agent_type": "cursor",
            "agent_config": {"host_exec_profile": "cursor-ask"},
            "timeout_seconds": 120,
        },
    )
    assert payload["host_exec_profile"] == "cursor-ask"
    assert payload["agent_type"] == "cursor"
    assert "image" not in payload["agent_config"]
    assert payload["timeout_seconds"] == 120
    assert payload_for_log(payload)["host_exec_profile"] == "cursor-ask"
    assert payload_for_log(payload)["agent_type"] == "cursor"


def test_lease_payload_host_exec_does_not_bypass_environment_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native profile cannot silently ignore a selected Docker environment."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "flow_environment_profiles_file", None)
    executor = RemoteRunnerExecutor(
        "cursor", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with pytest.raises(ValueError, match="environment_profile_not_approved"):
        executor._lease_payload(
            execution_id=uuid4(),
            flow_id=uuid4(),
            prompt="summarize",
            execution_context={
                "agent_type": "cursor",
                "agent_config": {
                    "host_exec_profile": "cursor-ask",
                    "environment_profile": "unapproved",
                },
            },
        )


def test_lease_payload_host_exec_rejects_pull_request() -> None:
    executor = RemoteRunnerExecutor(
        "cursor", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with pytest.raises(ValueError, match="pull requests"):
        executor._lease_payload(
            execution_id=uuid4(),
            flow_id=uuid4(),
            prompt="open a pr",
            execution_context={
                "agent_type": "cursor",
                "agent_config": {"host_exec_profile": "cursor-ask"},
                "git_clone_config": {"create_pull_request": True},
            },
        )


def test_lease_payload_host_exec_rejects_resume() -> None:
    executor = RemoteRunnerExecutor(
        "cursor", {}, db=MagicMock(), pool="local", account_id=uuid4()
    )
    with pytest.raises(ValueError, match="does not resume"):
        executor._lease_payload(
            execution_id=uuid4(),
            flow_id=uuid4(),
            prompt="continue",
            execution_context={
                "agent_type": "cursor",
                "agent_config": {"host_exec_profile": "cursor-ask"},
                "resume_from": str(uuid4()),
            },
        )


def test_lease_payload_keeps_custom_image_without_host_profile() -> None:
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
    assert "host_exec_profile" not in payload


@pytest.mark.asyncio
async def test_start_logs_do_not_include_token(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    token = "super-secret-runtime-token"
    api_key = "sk-live-openai-key-should-never-log"
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
                "agent_type": "codex",
                "agent_config": {"openai_api_key": api_key, "api_key": api_key},
            }
        )
    assert token not in caplog.text
    assert api_key not in caplog.text
    assert f"Leased execution {execution_id}" in caplog.text


def test_factory_rejects_hosted_cursor() -> None:
    flow = SimpleNamespace(runner_pool="server", account_id=uuid4())
    with pytest.raises(ValueError, match="hosted compute"):
        create_executor_for_execution(
            "cursor",
            {"host_exec_profile": "cursor-ask"},
            flow=flow,
            db=MagicMock(),
            execution_context={"agent_config": {"host_exec_profile": "cursor-ask"}},
        )


def test_factory_uses_remote_runner_for_cursor_host_exec() -> None:
    flow = SimpleNamespace(
        runner_pool="office-mac",
        account_id=uuid4(),
        agent_config={"host_exec_profile": "cursor-ask"},
    )
    executor = create_executor_for_execution(
        "cursor",
        {"host_exec_profile": "cursor-ask"},
        flow=flow,
        db=MagicMock(),
        execution_context={"agent_config": {"host_exec_profile": "cursor-ask"}},
    )
    assert isinstance(executor, RemoteRunnerExecutor)
    assert executor.pool == "office-mac"


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


def test_factory_uses_remote_runner_when_online_private_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.services.runner_service import AUTO_RUNNER_POOL

    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool=None),
    )
    monkeypatch.setattr(
        "preloop.services.runner_service.crud_flow_runner.find_matching",
        lambda db, **kwargs: [
            SimpleNamespace(id=uuid4(), status="online", pending_job=None)
        ],
    )
    executor = create_executor_for_execution(
        "codex",
        {"model": "gpt-5.4"},
        flow=flow,
        db=MagicMock(),
        execution_context={},
    )
    assert isinstance(executor, RemoteRunnerExecutor)
    assert executor.pool == AUTO_RUNNER_POOL


def test_factory_keeps_hosted_for_server_sentinel() -> None:
    flow = SimpleNamespace(runner_pool="server", account_id=uuid4())
    executor = create_executor_for_execution(
        "codex",
        {"model": "gpt-5.4"},
        flow=flow,
        db=MagicMock(),
        execution_context={},
    )
    assert isinstance(executor, CodexAgent)


def test_factory_uses_account_default_pool() -> None:
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        account=SimpleNamespace(default_runner_pool="office-mac"),
    )
    executor = create_executor_for_execution(
        "codex",
        {"model": "gpt-5.4"},
        flow=flow,
        db=MagicMock(),
        execution_context={},
    )
    assert isinstance(executor, RemoteRunnerExecutor)
    assert executor.pool == "office-mac"


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_status", ["RUNNING", "SUCCEEDED", "FAILED"])
async def test_stale_queued_reference_follows_persisted_assignment(
    monkeypatch: pytest.MonkeyPatch, execution_status: str
) -> None:
    """An old queue marker must neither lease twice nor overwrite completion."""
    execution_id, runner_id = uuid4(), uuid4()
    execution = SimpleNamespace(
        id=execution_id,
        status=execution_status,
        start_time=datetime.now(timezone.utc) - timedelta(minutes=16),
        agent_session_reference=f"runner:{runner_id}:{execution_id}",
        runner_id=runner_id,
        current_execution_id=None,
        error_message=None,
        end_time=None,
    )
    runner = SimpleNamespace(
        id=runner_id,
        halt_requested=False,
        reported_status=execution_status,
        current_execution_id=execution_id,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_runner.get", lambda *a, **k: runner
    )
    lease = MagicMock()
    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", lease)
    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="auto", account_id=uuid4()
    )
    assert await executor.get_status(
        f"runner:queued:auto:{execution_id}"
    ) == AgentStatus(execution_status)
    assert execution.status == execution_status
    assert execution.error_message is None
    lease.assert_not_called()


@pytest.mark.asyncio
async def test_terminal_execution_ignores_reused_runner_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id, runner_id = uuid4(), uuid4()
    execution = SimpleNamespace(id=execution_id, status="FAILED")
    runner = SimpleNamespace(
        halt_requested=False, reported_status="RUNNING", current_execution_id=uuid4()
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_runner.get", lambda *a, **k: runner
    )
    executor = RemoteRunnerExecutor(
        "codex", {}, db=MagicMock(), pool="auto", account_id=uuid4()
    )
    assert (
        await executor.get_status(f"runner:{runner_id}:{execution_id}")
        == AgentStatus.FAILED
    )
