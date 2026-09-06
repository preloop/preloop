"""Native preparation must stay outside cloud credential and Docker paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.agents.runner_launch import prepare_runner_delivery
from preloop.api.endpoints.runners import job_for_runner_replay
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.mark.asyncio
async def test_native_context_lease_and_replay_never_prepare_cloud_secrets(monkeypatch):
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.agent_type = "cursor"
    orchestrator.flow_id = uuid4()
    orchestrator.flow = SimpleNamespace(
        agent_type="cursor",
        agent_config={"host_exec_profile": "cursor-ask"},
        runner_pool="local",
        git_clone_config=None,
        custom_commands=None,
        account_id=uuid4(),
        name="Local question",
    )
    orchestrator.execution_log = SimpleNamespace(id=uuid4())
    orchestrator.trigger_event_data = {}
    orchestrator.ai_model = SimpleNamespace(model_identifier="team-fast")
    mint = MagicMock(side_effect=AssertionError("must not mint credentials"))
    monkeypatch.setattr(orchestrator, "_create_temporary_api_token", mint)
    context = await orchestrator._prepare_execution_context(resolved_prompt="--force")
    assert context["model_identifier"] == "team-fast"
    assert context["agent_config"] == {"host_exec_profile": "cursor-ask"}
    executor = RemoteRunnerExecutor(
        "cursor",
        {},
        db=MagicMock(),
        pool="local",
        account_id=orchestrator.flow.account_id,
    )
    # The payload allowlist drops even accidentally supplied cloud credentials.
    context.update(
        account_api_token="secret",
        model_gateway_token="secret",
        model_api_key="secret",
        allowed_mcp_tools=[{"name": "write"}],
    )
    job = executor._lease_payload(
        execution_id=orchestrator.execution_log.id,
        flow_id=orchestrator.flow_id,
        prompt=context["prompt"],
        execution_context=context,
    )
    assert job["model_identifier"] == "team-fast"
    assert "secret" not in str(job)
    assert "allowed_mcp_tools" not in job
    assert "launch_version" not in job
    hydrate = AsyncMock(side_effect=AssertionError("must not hydrate Docker"))
    monkeypatch.setattr("preloop.agents.runner_launch.hydrate_runner_job", hydrate)
    monkeypatch.setattr(
        "preloop.services.flow_runtime_token.create_flow_runtime_token", mint
    )
    for initial_context in (context, None):
        delivered = await prepare_runner_delivery(MagicMock(), job, initial_context)
        assert delivered == job
        assert "launch" not in delivered
    assert job_for_runner_replay(MagicMock(), pending_job=job, mint_token=True) == job
    mint.assert_not_called()
    hydrate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("git_clone_config", {"enabled": True}), ("custom_commands", {"enabled": True})],
)
async def test_native_unsupported_setup_fails_before_credentials(
    field, value, monkeypatch
):
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.agent_type = "cursor"
    orchestrator.flow = SimpleNamespace(
        agent_type="cursor",
        agent_config={"host_exec_profile": "cursor-ask"},
        runner_pool="local",
        git_clone_config=None,
        custom_commands=None,
    )
    setattr(orchestrator.flow, field, value)
    orchestrator.trigger_event_data = {}
    mint = MagicMock(side_effect=AssertionError("must not mint credentials"))
    monkeypatch.setattr(orchestrator, "_create_temporary_api_token", mint)
    with pytest.raises(ValueError, match="does not support remote"):
        await orchestrator._prepare_execution_context(resolved_prompt="question")
    mint.assert_not_called()
