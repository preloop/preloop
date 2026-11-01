"""Private Docker launch, redelivery and completion contracts (no live services)."""

import base64
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.utils.execve_limits import PROMPT_ENV_PREFIX, PROMPT_FILE_PATH

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
    # The prompt is not in the script any more: it travels as base64 chunks
    # in the launch environment and the script decodes them into a file, so
    # neither the script nor any one variable can cross MAX_ARG_STRLEN.
    assert "Implement a focused fix" not in launch["script"]
    assert PROMPT_FILE_PATH in launch["script"]
    chunks = int(launch["env"][f"{PROMPT_ENV_PREFIX}CHUNKS"])
    delivered = base64.b64decode(
        "".join(launch["env"][f"{PROMPT_ENV_PREFIX}{i}"] for i in range(chunks))
    ).decode()
    assert delivered.startswith("Implement a focused fix")
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
async def test_runner_launch_uses_the_public_url_not_the_cluster_gateway(monkeypatch):
    """A runner is outside the cluster, so it gets the public gateway origin.

    The resolved runtime carries the in-cluster gateway Service, which is
    correct for agent Jobs and unreachable for a self-hosted runner. The
    launch replaces it with the runner's own control-plane origin.
    """
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.setenv(
        "PRELOOP_MODEL_GATEWAY_URL_K8S",
        "http://release-preloop-gateway:80/openai/v1",
    )
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "model_identifier": "example-model",
        "model_gateway_enabled": True,
        "model_gateway_provider": "preloop",
        "model_gateway_model_alias": "gateway-alias",
        "model_gateway_url": "http://release-preloop-gateway:80/openai/v1",
        "account_api_token": "mcp-secret",
        "prompt": "Implement a focused fix",
        "execution_id": str(uuid4()),
        "flow_id": str(uuid4()),
    }

    launch = await build_runner_launch(context)

    assert "${PRELOOP_URL}/openai/v1" in launch["script"]
    assert "release-preloop-gateway" not in launch["script"]


@pytest.mark.asyncio
async def test_private_launch_delivers_evidence_capability_not_workspace():
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "model_identifier": "example-model",
        "account_api_token": "mcp-secret",
        "prompt": "Audit the release",
        "execution_id": str(uuid4()),
        "flow_id": str(uuid4()),
        "evidence_env": {
            "PRELOOP_EVIDENCE_URL": (
                "https://example.com/api/v1/flows/executions/x/artifacts"
            ),
            "PRELOOP_EVIDENCE_PUT_TOKEN": "evidence-secret",
            "PRELOOP_EVIDENCE_MAX_BYTES": "33554432",
            "PRELOOP_EVIDENCE_EXPANDED_MAX_BYTES": "2147483648",
        },
    }
    launch = await build_runner_launch(context)
    assert launch["env"]["PRELOOP_EVIDENCE_PUT_TOKEN"] == "evidence-secret"
    assert "PRELOOP_CHECKPOINT_PUT_TOKEN" not in launch["env"]
    assert "checkpoint-client.py evidence" in launch["script"]
    assert "evidence-secret" not in launch["script"]


@pytest.mark.asyncio
async def test_unsupported_harness_fails_explicitly():
    with pytest.raises(ValueError, match="Unsupported private Docker harness"):
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


@pytest.mark.asyncio
async def test_redelivery_uses_resolved_prompt_when_lease_omits_it(monkeypatch):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    execution = SimpleNamespace(
        id=uuid4(),
        flow_id=uuid4(),
        trigger_event_details={},
        resolved_input_prompt="stored resolved prompt",
    )
    monkeypatch.setattr(
        "preloop.agents.runner_launch.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_get_flow_details", lambda self, **kwargs: None
    )
    captured = {}

    async def prepare(self, *, resolved_prompt):
        captured["prompt"] = resolved_prompt
        return {"account_api_token": "fresh-token"}

    monkeypatch.setattr(
        FlowExecutionOrchestrator, "_prepare_execution_context", prepare
    )
    build = AsyncMock(
        return_value={"version": 1, "script": "shared bootstrap", "env": {}}
    )
    monkeypatch.setattr("preloop.agents.runner_launch.build_runner_launch", build)
    await hydrate_runner_job(
        MagicMock(),
        {
            "launch_version": 1,
            "execution_id": str(execution.id),
            "agent_type": "codex",
        },
    )
    assert captured["prompt"] == "stored resolved prompt"


@pytest.mark.asyncio
async def test_build_runner_launch_checks_payload_before_return(monkeypatch):
    from preloop.agents.codex import CodexAgent
    from preloop.utils.execve_limits import (
        LaunchPayloadTooLargeError,
        MAX_LAUNCH_STRING_BYTES,
    )

    original = CodexAgent._prepare_environment

    async def bloated(self, context):
        env = await original(self, context)
        env["BLOATED"] = "x" * MAX_LAUNCH_STRING_BYTES
        return env

    monkeypatch.setattr(CodexAgent, "_prepare_environment", bloated)
    with pytest.raises(LaunchPayloadTooLargeError, match="private runner launch"):
        await build_runner_launch(
            {
                "agent_type": "codex",
                "agent_config": {},
                "prompt": "short",
                "execution_id": str(uuid4()),
                "flow_id": str(uuid4()),
            }
        )


@pytest.mark.asyncio
async def test_prepare_delivers_named_launch_payload_error(monkeypatch):
    from preloop.agents.runner_launch import prepare_runner_delivery
    from preloop.utils.execve_limits import LaunchPayloadTooLargeError

    monkeypatch.setattr(
        "preloop.agents.runner_launch.build_runner_launch",
        AsyncMock(
            side_effect=LaunchPayloadTooLargeError(
                "Cannot start private runner launch: launch payload exceeds "
                "the execve string limit."
            )
        ),
    )
    delivered = await prepare_runner_delivery(
        MagicMock(), {"execution_id": str(uuid4()), "launch_version": 1}, {}
    )
    assert "launch payload exceeds" in delivered["launch_error"]
    assert "launch" not in delivered


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


def test_completion_strips_forged_evidence_upload_from_agent_json():
    status, error, result = validate_runner_completion(
        {
            "status": "SUCCEEDED",
            "launch_version": 1,
            "completion_protocol": "docker_v1",
            "exit_code": 0,
            "result": {"status": "success", "evidence_upload": "uploaded"},
            "evidence_upload": "failed",
        },
        leased_job={"launch_version": 1, "agent_type": "codex"},
    )
    assert status == "SUCCEEDED"
    assert error is None
    assert result == {"status": "success"}


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


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["codex", "opencode"])
@pytest.mark.parametrize(
    "config, expected",
    [
        ({"image": "example.com/team/agent:v1"}, "example.com/team/agent:v1"),
        ({"docker_image": "example.com/team/legacy:v2"}, "example.com/team/legacy:v2"),
        (
            {"image": "example.com/team/agent:v1", "docker_image": "ignored:v2"},
            "example.com/team/agent:v1",
        ),
        (
            {"image": "", "docker_image": "example.com/team/legacy:v2"},
            "example.com/team/legacy:v2",
        ),
        (
            {"image": "  ", "docker_image": " example.com/team/legacy:v2 "},
            "example.com/team/legacy:v2",
        ),
        (
            {"image": 123, "docker_image": "example.com/team/legacy:v2"},
            "example.com/team/legacy:v2",
        ),
        ({}, None),
    ],
)
async def test_private_launch_reports_effective_image(
    agent_type: str,
    config: dict,
    expected: str | None,
) -> None:
    from preloop.agents.images import default_agent_image

    launch = await build_runner_launch(
        {
            "agent_type": agent_type,
            "agent_config": config,
            "prompt": "Check the repository",
            "model_identifier": "example-model",
            "execution_id": str(uuid4()),
            "flow_id": str(uuid4()),
        }
    )
    assert (
        f"Agent image reference: {expected or default_agent_image(agent_type)}"
        in launch["script"]
    )


_REVIEW_PROMPT = (
    "Review the checked-out pull request. Do not edit files. "
    "Report findings with file and line, severity and a suggested fix.\n"
) * 1000  # ~118 KB, far above the 3-4 KB prompts that failed in the field


def _review_context(agent_type: str, **extra):
    context = {
        "agent_type": agent_type,
        "agent_config": {},
        "model_identifier": "qwen2.5-coder",
        "model_provider": "openai",
        "model_endpoint": "http://192.0.2.10:11434/v1",
        "account_api_token": "mcp-secret",
        "prompt": _REVIEW_PROMPT,
        "execution_id": str(uuid4()),
        "flow_id": str(uuid4()),
    }
    context.update(extra)
    return context


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["codex", "opencode"])
@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"allowed_mcp_servers": ["preloop-mcp"], "allowed_mcp_tools": []},
        {
            "git_clone_config": {
                "enabled": True,
                "repositories": [
                    {"repository_url": "https://github.com/example/repo.git"}
                ],
            }
        },
        {"custom_commands": {"enabled": True, "commands": ["echo ready"]}},
    ],
    ids=["no-optional-blocks", "mcp", "git-clone", "setup-commands"],
)
async def test_review_launch_fits_the_runner_cli_limits(agent_type, extra):
    """A large prompt and each optional block alone stay inside the CLI caps.

    The CLI rejects an empty script or one above 96 KiB, and any environment
    value above 96 KiB. The prompt travels as chunked environment variables,
    so its size must not move the script.
    """
    from preloop.agents.runner_launch import MAX_RUNNER_LAUNCH_STRING_BYTES

    launch = await build_runner_launch(_review_context(agent_type, **extra))
    script = launch["script"]
    assert script.strip()
    assert len(script.encode()) <= MAX_RUNNER_LAUNCH_STRING_BYTES
    assert _REVIEW_PROMPT[:200] not in script
    for value in launch["env"].values():
        assert len(value.encode()) <= MAX_RUNNER_LAUNCH_STRING_BYTES
    small = await build_runner_launch(
        _review_context(agent_type, prompt="Review the PR.", **extra)
    )
    # Only the chunk reassembly lines grow with the prompt.
    assert len(script) - len(small["script"]) < 1024


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "env", "match"),
    [
        ("   \n", {}, "empty bootstrap script"),
        ("x" * (96 * 1024 + 1), {}, "bootstrap script is 98305 bytes"),
        ("echo ok", {"BIG_VALUE": "y" * (96 * 1024 + 5)}, "BIG_VALUE is 98309"),
    ],
)
async def test_launch_the_cli_would_refuse_is_a_named_launch_error(
    monkeypatch, script, env, match
):
    """The control plane names the problem instead of the CLI's generic refusal."""
    from preloop.agents.codex import CodexAgent
    from preloop.agents.runner_launch import (
        RunnerLaunchRejectedError,
        prepare_runner_delivery,
    )

    original = CodexAgent._prepare_environment

    async def with_env(self, context):
        prepared = await original(self, context)
        prepared.update(env)
        return prepared

    monkeypatch.setattr(CodexAgent, "_build_codex_script", lambda self, c: script)
    monkeypatch.setattr(CodexAgent, "_prepare_environment", with_env)
    with pytest.raises(RunnerLaunchRejectedError, match=match):
        await build_runner_launch(_review_context("codex"))
    delivered = await prepare_runner_delivery(
        MagicMock(),
        {"execution_id": str(uuid4()), "launch_version": 1},
        _review_context("codex"),
    )
    assert "launch" not in delivered
    assert match.split(" is ")[0] in delivered["launch_error"]
