"""Generated wrappers install the validated CLI before restoring one session."""

import pytest
from pathlib import Path
from preloop.agents.codex import CodexAgent
from preloop.agents.opencode import OpenCodeAgent


@pytest.mark.parametrize(
    "harness,session",
    [("codex", "0f0e1d2c-3b4a-4568-8778-aabbccddeeff"), ("opencode", "ses_ab12cd34")],
)
def test_generated_native_restore_runs_once_after_install(
    harness: str, session: str
) -> None:
    agent = CodexAgent({}) if harness == "codex" else OpenCodeAgent({})
    context = {
        "prompt": "repair",
        "codex_model": "fixture",
        "opencode_model": "fixture",
        "execution_id": "exec",
        "flow_name": "fixture",
        "trigger_event_data": {
            "_session_thread_id": "thread",
            "_resume": {
                "thread_id": "thread",
                "cli_session": {"agent_type": harness, "session_id": session},
            },
        },
    }
    script = getattr(agent, f"_build_{harness}_script")(context)
    restore = "python3 /tmp/preloop-native-session.py restore"
    assert script.count(restore) == 1
    assert script.index("npm install") < script.index(restore)
    assert "opencode-ai@latest" not in script


@pytest.mark.parametrize("harness", ["codex", "opencode"])
@pytest.mark.parametrize("backend", ["docker", "kubernetes"])
@pytest.mark.asyncio
async def test_execution_wrapper_enters_first_checkout_after_init(
    harness: str, backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shlex
    import subprocess
    from unittest.mock import AsyncMock, MagicMock, patch

    agent = CodexAgent({}) if harness == "codex" else OpenCodeAgent({})
    first = tmp_path / "first checkout"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    init = f"cd {shlex.quote(str(second))}"
    monkeypatch.setattr(agent, "_prepare_init_commands", lambda _: init)
    context = {
        "prompt": "repair",
        "codex_model": "fixture",
        "opencode_model": "fixture",
        "execution_id": "exec",
        "flow_id": "flow",
        "flow_name": "fixture",
        "git_clone_config": {
            "enabled": True,
            "repositories": [{"clone_path": str(first)}, {"clone_path": str(second)}],
        },
    }
    if backend == "kubernetes":
        with patch(
            "preloop.agents.container.ContainerAgentExecutor._start_kubernetes_pod",
            AsyncMock(return_value="job"),
        ) as parent:
            await agent._start_kubernetes_pod(context)
            script = parent.call_args.args[0]["_container_args"][-1]
    else:
        container = MagicMock(id="container", start=AsyncMock())
        docker = MagicMock(
            images=MagicMock(inspect=AsyncMock()),
            containers=MagicMock(create=AsyncMock(return_value=container)),
        )
        monkeypatch.setattr(agent, "_get_docker_client", AsyncMock(return_value=docker))
        monkeypatch.setattr(agent, "_prepare_environment", AsyncMock(return_value={}))
        await agent._start_docker_container(context)
        config = docker.containers.create.call_args.kwargs["config"]
        assert config["WorkingDir"] == str(first)
        script = config["Cmd"][-1]
    # Execute the actual generated init/cwd fragment to catch clone/setup cwd drift.
    fragment = script.split(
        "# Run initialization commands (git clone, custom commands) if any\n", 1
    )[1].split("# Restore a prior CLI session", 1)[0]
    result = subprocess.run(
        ["bash", "-c", fragment + "\npwd"], check=True, capture_output=True, text=True
    )
    assert result.stdout.strip() == str(first)
    if harness == "opencode":
        assert "cat > /workspace/opencode.json" in script


def test_opencode_version_override_must_be_exact() -> None:
    context = {
        "prompt": "repair",
        "opencode_model": "fixture",
        "agent_config": {"opencode_cli_version": "latest"},
    }
    with pytest.raises(ValueError, match="exact release"):
        OpenCodeAgent({})._build_opencode_script(context)
