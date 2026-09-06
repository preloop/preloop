"""Private Docker launch, redelivery and completion contracts (no live services)."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.runner_launch import (
    build_runner_launch,
    hydrate_runner_job,
    validate_runner_completion,
)
from preloop.services.runner_service import persistable_job_payload


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["codex", "opencode"])
async def test_shared_launch_has_model_mcp_prompt_and_no_script_secrets(
    agent_type, caplog
):
    context = {
        "agent_type": agent_type,
        "agent_config": {},
        "model_identifier": "example-model",
        "model_provider": "openai",
        "model_gateway_enabled": True,
        "model_gateway_provider": "preloop",
        "model_gateway_token": "model-secret",
        "model_gateway_model_alias": "gateway-alias",
        "account_api_token": "mcp-secret",
        "allowed_mcp_servers": ["preloop-mcp"],
        "allowed_mcp_tools": [{"name": "get_issue"}],
        "prompt": "Implement a focused fix",
        "execution_id": str(uuid4()),
        "flow_id": str(uuid4()),
    }
    with caplog.at_level("DEBUG"):
        launch = await build_runner_launch(context)
    assert launch["version"] == 1
    assert "gateway-alias" in launch["script"]
    assert "${PRELOOP_URL}/openai/v1" in launch["script"]
    prompt_fragment = (
        "Implement a focused fix"
        if agent_type == "codex"
        else base64.b64encode(b"Implement a focused fix").decode()[:24]
    )
    assert prompt_fragment in launch["script"]
    assert "/workspace/result.json" in launch["script"]
    assert "PRELOOP_AGENT_EXEC_START" in launch["script"]
    assert launch["env"]["PRELOOP_API_TOKEN"] == "mcp-secret"
    assert launch["env"]["PRELOOP_MODEL_GATEWAY_TOKEN"] == "model-secret"
    for secret in ("model-secret", "mcp-secret"):
        assert secret not in launch["script"]
        assert secret not in caplog.text
    stored = persistable_job_payload(
        {"launch_version": 1, "launch": launch, "account_api_token": "mcp-secret"}
    )
    assert stored == {"launch_version": 1}
    assert context["prompt"] == "Implement a focused fix"


@pytest.mark.asyncio
async def test_unsupported_harness_fails_explicitly():
    with pytest.raises(ValueError, match="only codex and opencode"):
        await build_runner_launch({"agent_type": "unknown"})


@pytest.mark.asyncio
async def test_redelivery_rebuilds_context_with_stored_prompt_and_trigger(monkeypatch):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    execution = SimpleNamespace(
        id=uuid4(),
        flow_id=uuid4(),
        trigger_event_details={
            "payload": {"issue": {"number": 7}},
            "_resume": {"thread_id": "thread"},
        },
        resolved_input_prompt="stored resolved prompt",
    )
    monkeypatch.setattr(
        "preloop.agents.runner_launch.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    captured = {}
    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_flow_details", lambda self: None
    )

    async def prepare(self, *, resolved_prompt):
        captured["trigger"] = self.trigger_event_data
        captured["prompt"] = resolved_prompt
        return {
            "account_api_token": "fresh-token",
            "model_gateway_token": "fresh-token",
            "git_credentials_map": {"tracker": "fresh-git"},
            "agent_config": {"model": "changed"},
        }

    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_prepare_execution_context", prepare
    )
    build = AsyncMock(
        return_value={"version": 1, "script": "shared bootstrap", "env": {}}
    )
    monkeypatch.setattr("preloop.agents.runner_launch.build_runner_launch", build)
    stored = {
        "launch_version": 1,
        "execution_id": str(execution.id),
        "prompt": "leased prompt",
        "agent_type": "codex",
        "agent_config": {"image": "example/image:1"},
    }
    first = await hydrate_runner_job(MagicMock(), stored)
    second = await hydrate_runner_job(MagicMock(), stored)
    assert captured == {
        "trigger": execution.trigger_event_details,
        "prompt": "leased prompt",
    }
    assert build.await_count == 2
    context = build.call_args.args[0]
    assert context["git_credentials_map"] == {"tracker": "fresh-git"}
    assert context["agent_config"] == stored["agent_config"]
    assert first["account_api_token"] == second["account_api_token"] == "fresh-token"
    assert "launch" not in stored
    assert "fresh-token" not in json.dumps(persistable_job_payload(first))


@pytest.mark.parametrize(
    "message",
    [
        {"status": "SUCCEEDED"},
        {"status": "SUCCEEDED", "launch_version": 1, "exit_code": 0},
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "exit_code": 1,
            "result": {"status": "success"},
        },
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "exit_code": False,
            "result": {"status": "success"},
        },
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "exit_code": 0,
            "result": {"status": "partial"},
        },
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "exit_code": 0,
            "result": {"status": "failure"},
        },
        {"status": "RUNNING"},
    ],
)
def test_false_completion_rejected(message):
    assert validate_runner_completion(message)[0] == "FAILED"


@pytest.mark.parametrize(
    "result", [{"status": "success", "tests": ["pytest"]}, {"verdict": "fail"}]
)
def test_completed_report_preserved(result):
    assert validate_runner_completion(
        {"status": "SUCCEEDED", "launch_version": 1, "exit_code": 0, "result": result}
    ) == ("SUCCEEDED", None, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("delayed", [False, True])
async def test_prepare_failure_is_delivered_without_leaking_exception(
    monkeypatch, delayed
):
    from preloop.agents.runner_launch import prepare_runner_delivery

    target = "hydrate_runner_job" if delayed else "build_runner_launch"
    monkeypatch.setattr(
        "preloop.agents.runner_launch." + target,
        AsyncMock(side_effect=RuntimeError("upstream secret-token")),
    )
    job = {"execution_id": str(uuid4()), "launch_version": 1}
    delivered = await prepare_runner_delivery(MagicMock(), job, None if delayed else {})
    assert "launch_error" in delivered
    assert "secret-token" not in json.dumps(delivered)
    assert "launch" not in delivered


@pytest.mark.asyncio
async def test_flow_model_edit_rejects_replay_before_minting_credentials(monkeypatch):
    from preloop.agents.runner_launch import (
        flow_launch_fingerprint,
        prepare_runner_delivery,
    )
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    flow = SimpleNamespace(agent_type="codex", agent_config={}, ai_model_id=uuid4())
    execution = SimpleNamespace(id=uuid4(), flow_id=uuid4(), trigger_event_details={})
    fingerprint = flow_launch_fingerprint(flow)
    flow.ai_model_id = uuid4()
    monkeypatch.setattr(
        "preloop.agents.runner_launch.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_get_flow_details",
        lambda self: setattr(self, "flow", flow),
    )
    prepare = AsyncMock()
    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_prepare_execution_context", prepare
    )
    delivered = await prepare_runner_delivery(
        MagicMock(),
        {
            "launch_version": 1,
            "execution_id": str(execution.id),
            "flow_launch_fingerprint": fingerprint,
        },
    )
    assert "configuration changed" in delivered["launch_error"]
    prepare.assert_not_awaited()
