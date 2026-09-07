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


@pytest.mark.parametrize(
    "harness,version", [("codex", "0.153.4"), ("opencode", "1.18.29")]
)
def test_approved_image_skips_install_and_checks_pinned_harness(
    harness: str,
    version: str,
) -> None:
    from preloop.services.flow_environment import EnvironmentProfile

    agent = CodexAgent({}) if harness == "codex" else OpenCodeAgent({})
    agent.environment_profile = EnvironmentProfile(
        image="example.com/project@sha256:" + "a" * 64,
        harness=harness,
    )
    context = {
        "prompt": "repair",
        "execution_id": "exec",
        "flow_name": "test",
        "model_identifier": "fixture",
        "trigger_event_data": {
            "_session_thread_id": "thread",
            "_resume": {
                "thread_id": "thread",
                "cli_session": {
                    "agent_type": harness,
                    "session_id": "0f0e1d2c-3b4a-4568-8778-aabbccddeeff"
                    if harness == "codex"
                    else "ses_ab12cd34",
                },
            },
        },
    }
    script = getattr(agent, f"_build_{harness}_script")(context)
    assert "npm install -g" not in script
    assert "environment_harness_version_mismatch" in script
    assert version in script
    assert script.index("environment_harness_version_mismatch") < script.index(
        "python3 /tmp/preloop-native-session.py restore"
    )


@pytest.mark.parametrize(
    "harness,session_id,resume_flag",
    [
        (
            "codex",
            "0f0e1d2c-3b4a-4568-8778-aabbccddeeff",
            "resume $PRELOOP_CLI_SESSION_ID",
        ),
        ("opencode", "ses_ab12cd34", "--session $PRELOOP_CLI_SESSION_ID"),
    ],
)
def test_native_resume_argv_is_explicit_and_never_latest_session(
    harness: str, session_id: str, resume_flag: str
) -> None:
    """Native resume argv names one explicit session, never a newest selector."""
    agent = CodexAgent({}) if harness == "codex" else OpenCodeAgent({})
    model_key = "codex_model" if harness == "codex" else "opencode_model"
    context = {
        "prompt": "repair",
        model_key: "fixture",
        "execution_id": "exec",
        "flow_name": "fixture",
        "trigger_event_data": {
            "_session_thread_id": "thread",
            "_resume": {
                "thread_id": "thread",
                "cli_session": {"agent_type": harness, "session_id": session_id},
            },
        },
    }
    resumed = getattr(agent, f"_build_{harness}_script")(context)
    assert resumed.count(resume_flag) == 1
    assert f"PRELOOP_CLI_SESSION_ID='{session_id}'" in resumed
    for selector in ("--continue", "--last", "resume --last"):
        assert selector not in resumed
    cold_context = {
        "prompt": "repair",
        model_key: "fixture",
        "execution_id": "exec",
        "flow_name": "fixture",
        "trigger_event_data": {},
    }
    cold = getattr(agent, f"_build_{harness}_script")(cold_context)
    # A cold start carries no explicit session id and the resume block stays
    # inert: the flag is only emitted once a real restore marked the env var.
    assert "PRELOOP_CLI_SESSION_ID=''" in cold
    assert session_id not in cold
    for selector in ("--continue", "--last", "resume --last"):
        assert selector not in cold
