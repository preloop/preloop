"""Native preparation must stay outside cloud credential and Docker paths."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.agents.runner_launch import prepare_runner_delivery
from preloop.api.endpoints.runners import job_for_runner_replay
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


def _patch_delivery_lookup(monkeypatch, flow, execution):
    """Serve the execution and flow rows the host delivery path reloads."""
    flow.allowed_mcp_tools = getattr(flow, "allowed_mcp_tools", None)
    flow.allowed_mcp_servers = getattr(flow, "allowed_mcp_servers", None)
    row = SimpleNamespace(id=execution.id, flow_id=uuid4(), trigger_event_details={})
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow_execution.get",
        MagicMock(return_value=row),
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow.get",
        MagicMock(return_value=flow),
    )


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
    orchestrator.flow.agent_config = {
        "host_exec_profile": "cursor-ask",
        "cursor_model": "grok-4.7-high",
    }
    pinned = await orchestrator._prepare_execution_context(resolved_prompt="--force")
    assert pinned["model_identifier"] == "grok-4.7-high"
    orchestrator.flow.agent_config = {"host_exec_profile": "cursor-ask"}
    orchestrator.ai_model = None
    automatic = await orchestrator._prepare_execution_context(resolved_prompt="--force")
    assert automatic["model_identifier"] is None
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
    _patch_delivery_lookup(monkeypatch, orchestrator.flow, orchestrator.execution_log)
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
    [
        ("git_clone_config", {"enabled": True, "setup_commands": ["make deps"]}),
        ("custom_commands", {"enabled": True}),
    ],
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
    with pytest.raises(ValueError, match="does not (support|run) remote"):
        await orchestrator._prepare_execution_context(resolved_prompt="question")
    mint.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [None, {"nonce": "n-1", "version": 1}],
)
async def test_prepare_rejects_isolated_publication_mode(snapshot, monkeypatch):
    """Isolated publication fails before credentials, with or without a snapshot."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.agent_type = "cursor"
    orchestrator.flow_id = uuid4()
    orchestrator.flow = SimpleNamespace(
        agent_type="cursor",
        agent_config={"host_exec_profile": "cursor-ask"},
        runner_pool="local",
        git_clone_config={"publication_mode": "isolated"},
        custom_commands=None,
        account_id=uuid4(),
        name="Local question",
    )
    result = None if snapshot is None else {"_private_publication": snapshot}
    orchestrator.execution_log = SimpleNamespace(id=uuid4(), result=result)
    orchestrator.trigger_event_data = {}
    orchestrator.ai_model = None
    orchestrator.db = MagicMock()
    mint = MagicMock(side_effect=AssertionError("must not mint credentials"))
    monkeypatch.setattr(orchestrator, "_create_temporary_api_token", mint)
    with pytest.raises(ValueError, match="isolated publication|pull request"):
        await orchestrator._prepare_execution_context(resolved_prompt="question")
    mint.assert_not_called()


@pytest.mark.asyncio
async def test_copilot_context_and_lease_use_local_login_only(monkeypatch):
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.agent_type = "copilot"
    orchestrator.flow_id = uuid4()
    orchestrator.flow = SimpleNamespace(
        agent_type="copilot",
        agent_config={
            "host_exec_profile": "copilot-seat",
            # A Cursor alias must not leak into a Copilot lease.
            "cursor_model": "composer-2.5",
            "copilot_model": "team-default",
        },
        runner_pool="local",
        git_clone_config=None,
        custom_commands=None,
        account_id=uuid4(),
        name="Local review",
    )
    orchestrator.execution_log = SimpleNamespace(id=uuid4())
    orchestrator.trigger_event_data = {}
    orchestrator.ai_model = SimpleNamespace(model_identifier="catalog-model")
    mint = MagicMock(side_effect=AssertionError("must not mint credentials"))
    monkeypatch.setattr(orchestrator, "_create_temporary_api_token", mint)
    context = await orchestrator._prepare_execution_context(resolved_prompt="review")
    assert context["agent_type"] == "copilot"
    assert context["model_identifier"] == "team-default"
    assert context["agent_config"] == {"host_exec_profile": "copilot-seat"}
    orchestrator.flow.agent_config = {"host_exec_profile": "copilot-seat"}
    fallback = await orchestrator._prepare_execution_context(resolved_prompt="review")
    assert fallback["model_identifier"] == "catalog-model"
    executor = RemoteRunnerExecutor(
        "copilot",
        {},
        db=MagicMock(),
        pool="local",
        account_id=orchestrator.flow.account_id,
    )
    context.update(
        account_api_token="secret",
        model_gateway_token="secret",
        allowed_mcp_tools=[{"name": "write"}],
    )
    job = executor._lease_payload(
        execution_id=orchestrator.execution_log.id,
        flow_id=orchestrator.flow_id,
        prompt=context["prompt"],
        execution_context=context,
    )
    assert job["agent_type"] == "copilot"
    assert job["completion_protocol"] == "host_exec"
    assert job["host_exec_profile"] == "copilot-seat"
    assert "secret" not in str(job)
    assert "launch_version" not in job
    hydrate = AsyncMock(side_effect=AssertionError("must not hydrate Docker"))
    monkeypatch.setattr("preloop.agents.runner_launch.hydrate_runner_job", hydrate)
    _patch_delivery_lookup(monkeypatch, orchestrator.flow, orchestrator.execution_log)
    assert await prepare_runner_delivery(MagicMock(), job, context) == job
    mint.assert_not_called()
    hydrate.assert_not_called()


@pytest.mark.asyncio
async def test_copilot_without_profile_fails_before_credentials(monkeypatch):
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.agent_type = "copilot"
    orchestrator.flow = SimpleNamespace(
        agent_type="copilot",
        agent_config={},
        runner_pool="local",
        git_clone_config=None,
        custom_commands=None,
    )
    orchestrator.trigger_event_data = {}
    mint = MagicMock(side_effect=AssertionError("must not mint credentials"))
    monkeypatch.setattr(orchestrator, "_create_temporary_api_token", mint)
    with pytest.raises(ValueError, match="copilot requires"):
        await orchestrator._prepare_execution_context(resolved_prompt="review")
    mint.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_runner_delivery_rejects_unknown_native_agent_type():
    job = {
        "completion_protocol": "host_exec",
        "agent_type": "codex",
        "host_exec_profile": "copilot-seat",
        "execution_id": str(uuid4()),
    }
    delivered = await prepare_runner_delivery(MagicMock(), job, None)
    assert "launch_error" in delivered
