"""Private Docker launch, redelivery and completion contracts (no live services)."""

import base64
import json
from types import SimpleNamespace
from pathlib import Path
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
        FlowExecutionOrchestrator, "_get_flow_details", lambda self, **kwargs: None
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
    message["completion_protocol"] = "docker_v1"
    assert (
        validate_runner_completion(
            message, leased_job={"launch_version": 1, "agent_type": "codex"}
        )[0]
        == "FAILED"
    )


@pytest.mark.parametrize(
    "result", [{"status": "success", "tests": ["pytest"]}, {"verdict": "fail"}]
)
def test_completed_report_preserved(result):
    assert validate_runner_completion(
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "completion_protocol": "docker_v1",
            "exit_code": 0,
            "result": result,
        },
        leased_job={"launch_version": 1, "agent_type": "codex"},
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
        lambda self, **kwargs: setattr(self, "flow", flow),
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


@pytest.mark.asyncio
async def test_shared_launch_prepares_clone_setup_and_git_credentials(caplog):
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "model_provider": "openai",
        "prompt": "Implement a fix",
        "execution_id": str(uuid4()),
        "git_clone_config": {
            "enabled": True,
            "repositories": [
                {
                    "repository_url": "https://github.com/example/project.git",
                    "clone_path": "/workspace/project",
                    "branch": "main",
                    "tracker_id": "tracker",
                }
            ],
        },
        "git_credentials_map": {
            "tracker": {"token": "git-secret", "tracker_type": "github"}
        },
        "custom_commands": {"enabled": True, "commands": ["echo repository-setup"]},
    }
    with caplog.at_level("DEBUG"):
        launch = await build_runner_launch(context)
    assert "git clone" in launch["script"]
    assert "/workspace/project" in launch["script"]
    assert "repository-setup" in launch["script"]
    assert "git-secret" in launch["env"].values()
    assert "git-secret" not in launch["script"]
    assert "git-secret" not in caplog.text


def test_flow_refresh_bypasses_cached_definition_and_model():
    from preloop.models.crud import crud_flow

    db = MagicMock()
    crud_flow.get(db, id=uuid4(), refresh=True)
    query = db.query.return_value.filter.return_value
    query.populate_existing.assert_called_once_with()
    query.populate_existing.return_value.options.assert_called_once()


@pytest.mark.parametrize(
    "leased_job,protocol",
    [
        ({}, "docker_v1"),
        ({"launch_version": 2, "agent_type": "codex"}, "docker_v1"),
        ({"launch_version": 1, "agent_type": "unknown"}, "docker_v1"),
        ({"launch_version": 1, "agent_type": "codex"}, "host_exec"),
        ({"launch_version": 1, "agent_type": "codex"}, None),
    ],
)
def test_completion_cannot_select_its_own_protocol(leased_job, protocol):
    message = {
        "status": "SUCCEEDED",
        "launch_version": 1,
        "exit_code": 0,
        "completion_protocol": protocol,
        "result": {"status": "success"},
    }
    assert validate_runner_completion(message, leased_job=leased_job)[0] == "FAILED"


_COMPLETION_CONTRACT = json.loads(
    (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "runner_completion_vocabulary.json"
    ).read_text()
)


@pytest.mark.parametrize(
    "case", _COMPLETION_CONTRACT["cases"], ids=lambda case: case["name"]
)
@pytest.mark.parametrize("exit_code", [0, 2])
@pytest.mark.parametrize("claimed_status", ["SUCCEEDED", "FAILED"])
def test_shared_completion_cases_preserve_reports_without_promoting_failure(
    case: dict, exit_code: int, claimed_status: str
) -> None:
    from preloop.agents.runner_launch import LAUNCH_VERSION
    from preloop.services.flow_orchestrator import _result_artifact_confirmation

    assert _COMPLETION_CONTRACT["version"] == LAUNCH_VERSION
    assert _COMPLETION_CONTRACT["protocol"] == "docker_v1"
    assert _result_artifact_confirmation(case["result"]) == case["confirmation"]
    status, _, result = validate_runner_completion(
        {
            "status": claimed_status,
            "exit_code": exit_code,
            "completion_protocol": "docker_v1",
            "launch_version": 1,
            "result": case["result"],
        },
        leased_job={"launch_version": 1, "agent_type": "codex"},
    )
    assert (status == "SUCCEEDED") == (
        claimed_status == "SUCCEEDED"
        and exit_code == 0
        and case["confirmation"] == "success"
    )
    assert result == case["result"]


@pytest.mark.asyncio
async def test_queued_isolated_launch_keeps_restored_pinned_git_configuration(
    monkeypatch,
):
    """Raw queued flow config cannot replace restored authority or read lease."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    execution = SimpleNamespace(
        id=uuid4(),
        flow_id=uuid4(),
        trigger_event_details={},
        resolved_input_prompt="work",
    )
    monkeypatch.setattr(
        "preloop.agents.runner_launch.crud_flow_execution.get",
        lambda *args, **kwargs: execution,
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_flow_details", lambda *args, **kwargs: None
    )
    pinned = {"target_branch": "preloop/bound", "source_branch": "trusted-main"}
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_prepare_execution_context",
        AsyncMock(
            return_value={
                "git_clone_config": pinned,
                "git_credentials_map": {
                    "tracker": {"token": "read-only", "permission": "read"}
                },
            }
        ),
    )
    build = AsyncMock(return_value={"version": 1, "script": "bootstrap", "env": {}})
    monkeypatch.setattr("preloop.agents.runner_launch.build_runner_launch", build)
    await hydrate_runner_job(
        MagicMock(),
        {
            "launch_version": 1,
            "execution_id": str(execution.id),
            "publication": {"version": 1},
            "git_clone_config": {"target_branch": "raw-unbound"},
        },
    )
    assert build.await_args.args[0]["git_clone_config"] == pinned
    assert (
        build.await_args.args[0]["git_credentials_map"]["tracker"]["permission"]
        == "read"
    )
