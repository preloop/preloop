"""Launch contracts for Pi and DeepSeek Harness."""

import json
import subprocess
from unittest.mock import AsyncMock, patch

import pytest

from preloop.agents.factory import create_agent_executor, SUPPORTED_AGENT_TYPES
from preloop.agents.harness import DeepSeekAgent, PiAgent
from preloop.utils.execve_limits import PROMPT_FILE_PATH


@pytest.mark.parametrize("kind,cls", [("pi", PiAgent), ("deepseek", DeepSeekAgent)])
@pytest.mark.asyncio
async def test_gateway_launch_and_identity(kind: str, cls: type) -> None:
    agent = create_agent_executor(kind, {})
    assert kind in SUPPORTED_AGENT_TYPES
    assert isinstance(agent, cls)
    context = {
        "prompt": "untrusted $(touch /tmp/should-not-exist)\n" * 20000,
        "model_gateway_enabled": True,
        "model_gateway_model_alias": "provider/test-model",
        "model_gateway_url": "https://example.com/openai/v1",
        "model_gateway_token": "test-gateway-token",
        "account_api_token": "test-mcp-token",
        "allowed_mcp_servers": ["preloop-mcp"],
    }
    env = await agent._prepare_environment(context)
    assert json.loads(env["PRELOOP_HARNESS_MODEL"])["models"] == [
        {"id": "provider/test-model"}
    ]
    assert env["PRELOOP_MODEL_TOKEN"] == "test-gateway-token"
    mcp = json.loads(env["MCP_CONFIG_JSON"])
    assert (
        mcp["mcpServers"]["preloop-mcp"]["headers"]["Authorization"]
        == "Bearer test-mcp-token"
    )
    script = agent._build_harness_script(context)
    assert "$(touch" not in script
    assert "test-gateway-token" not in script
    assert f"< {PROMPT_FILE_PATH}" in script
    assert 'exit "$PRELOOP_HARNESS_EXIT"' in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
    with patch(
        "preloop.agents.container.ContainerAgentExecutor.start", new_callable=AsyncMock
    ) as start:
        start.return_value = "worker-id"
        assert await agent.start(context) == "worker-id"
        launch = start.call_args.args[0]
        assert launch["_container_command"] == ["/bin/bash"]
        assert launch["_agent_env"]["PRELOOP_HARNESS"] == kind


@pytest.mark.parametrize("cls", [PiAgent, DeepSeekAgent])
@pytest.mark.asyncio
async def test_missing_model_or_credential_fails_before_launch(cls: type) -> None:
    agent = cls({})
    with pytest.raises(ValueError, match="No model"):
        await agent._prepare_environment({})
    with pytest.raises(ValueError, match="credential"):
        await agent._prepare_environment({"model_identifier": "test-model"})


@pytest.mark.asyncio
async def test_custom_provider_and_native_approvals() -> None:
    env = await DeepSeekAgent({})._prepare_environment(
        {
            "model_identifier": "custom-model",
            "model_provider": "custom",
            "model_endpoint": "https://example.com/v1",
            "model_api_key": "test-key",
            "agent_config": {"native_tool_approvals": True},
            "model_parameters": {"max_output_tokens": 512},
        }
    )
    assert env["PRELOOP_NATIVE_APPROVALS"] == "on"
    assert json.loads(env["PRELOOP_HARNESS_MODEL"])["models"][0]["maxTokens"] == 512
    assert (
        json.loads(env["PRELOOP_HARNESS_MODEL"])["baseUrl"] == "https://example.com/v1"
    )


@pytest.mark.parametrize("kind", ["pi", "deepseek"])
@pytest.mark.asyncio
async def test_private_runner_uses_same_bootstrap(kind: str) -> None:
    from preloop.agents.runner_launch import (
        build_runner_launch,
        validate_runner_completion,
    )

    launch = await build_runner_launch(
        {
            "agent_type": kind,
            "prompt": "Write a result",
            "model_gateway_enabled": True,
            "model_gateway_model_alias": "provider/model",
            "model_gateway_token": "test-token",
            "account_api_token": "test-mcp",
            "allowed_mcp_servers": ["preloop-mcp"],
        }
    )
    assert launch["env"]["PRELOOP_HARNESS"] == kind
    assert (
        json.loads(launch["env"]["PRELOOP_HARNESS_MODEL"])["baseUrl"]
        == "${PRELOOP_URL}/openai/v1"
    )
    assert "bootstrap.mjs" in launch["script"]
    status, _, _ = validate_runner_completion(
        {
            "status": "SUCCEEDED",
            "exit_code": 0,
            "completion_protocol": "docker_v1",
            "launch_version": 1,
            "result": {"status": "success"},
        },
        leased_job={"launch_version": 1, "agent_type": kind},
    )
    assert status == "SUCCEEDED"


@pytest.mark.parametrize("cls", [PiAgent, DeepSeekAgent])
@pytest.mark.parametrize("global_non_root", ["false", "true"])
@pytest.mark.asyncio
async def test_kubernetes_harness_runs_directly_as_unprivileged_user(
    cls: type, global_non_root: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from preloop.agents.container import K8S_INNER_SCRIPT_ENV_PREFIX

    monkeypatch.setenv("AGENT_RUN_AS_NON_ROOT", global_non_root)
    agent = cls({})
    agent.use_kubernetes = True
    agent._init_kubernetes_clients = AsyncMock()
    agent._create_kubernetes_job = AsyncMock(return_value="job-id")
    context = {
        "execution_id": "test-execution",
        "flow_id": "test-flow",
        "prompt": "Write a result",
        "model_identifier": "test-model",
        "model_api_key": "test-key",
        "git_clone_config": {
            "enabled": True,
            "repositories": [
                {
                    "repository_url": "https://github.com/example/repo.git",
                    "clone_path": "workspace",
                }
            ],
        },
    }
    assert await agent.start(context) == "job-id"
    job = agent._create_kubernetes_job.call_args.args[0]
    pod = job.spec.template.spec
    container = pod.containers[0]
    assert container.security_context.run_as_user == 10000
    assert container.security_context.run_as_non_root is True
    assert container.security_context.capabilities.drop == ["ALL"]
    assert not container.security_context.capabilities.add
    assert pod.security_context.run_as_group == 10000
    assert pod.security_context.fs_group == 10000
    # CRI creates workingDir as root after fsGroup chown. A clone
    # subdirectory that does not exist yet would be unwritable to UID 10000.
    assert container.working_dir == "/workspace"
    env = {item.name: item.value for item in container.env}
    assert env["HOME"] == "/tmp/preloop-home"
    assert sum(item.name == "HOME" for item in container.env) == 1
    chunks = sorted(
        (name, value)
        for name, value in env.items()
        if name.startswith(K8S_INNER_SCRIPT_ENV_PREFIX) and name[-1].isdigit()
    )
    script = base64.b64decode("".join(value for _, value in chunks)).decode()
    assert "setpriv" not in script
    assert "BASH_EXECUTION_STRING" not in script
    assert "PRELOOP_AGENT_EXEC_START" in script
    subprocess.run(["bash", "-n"], input=script, text=True, check=True)
