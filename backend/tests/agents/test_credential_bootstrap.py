"""Credential-safe runtime bootstrap, including image entrypoint output."""

import os
from pathlib import Path
import subprocess
import sys

from preloop.models import crud  # noqa: F401
from preloop.agents.credential_bootstrap import bootstrap_source


def test_bootstrap_keeps_credentials_out_of_setup_and_masks_agent_output(
    tmp_path: Path,
) -> None:
    """An entrypoint and agent stub can both print env without exporting values."""
    entrypoint = tmp_path / "entrypoint"
    entrypoint.write_text('#!/bin/sh\nenv\nexec /bin/bash "$@"\n')
    entrypoint.chmod(0o700)
    token = "synthetic-bootstrap-credential-123456789"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            bootstrap_source(),
            str(entrypoint),
            "-c",
            'test -n "$OPENAI_API_KEY" || exit 19; env; printf "%s" "$OPENAI_API_KEY"',
        ],
        env={
            "PATH": os.environ["PATH"],
            "OPENAI_API_KEY": token,
            "PRELOOP_DISABLE_TELEMETRY": "true",
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0
    assert token not in result.stdout + result.stderr
    assert "OPENAI_API_KEY=[REDACTED]" in result.stdout
    assert result.stdout.endswith("[REDACTED]")


def test_aider_startup_writes_config_without_echoing() -> None:
    """Startup materializes MCP config without teeing credential headers."""
    from preloop.agents import aider

    source = Path(aider.__file__).read_text()
    assert 'echo "$MCP_CONFIG_JSON" | tee' not in source


def test_shell_agents_mask_before_container_log_capture() -> None:
    """The common Docker boundary wraps each shell harness invocation."""
    from preloop.agents.container import ContainerAgentExecutor

    agent = ContainerAgentExecutor(
        agent_type="example", config={}, image="example/image"
    )
    context = {"execution_id": "example"}
    env = agent._apply_git_credential_env({"EXAMPLE_API_KEY": "synthetic-key"}, context)
    config = {
        "Cmd": ["/bin/bash", "-c", "env"],
        "Env": [f"{k}={v}" for k, v in env.items()],
    }
    agent._guard_docker_launch_payload(config, what="example")
    assert config["Entrypoint"][:2] == ["python3", "-c"]


def test_opencode_file_auth_reduces_tool_environment(tmp_path: Path) -> None:
    """Gateway and MCP file-backed authorization need no inherited token."""
    import json
    from preloop.agents.opencode import OpenCodeAgent

    script = OpenCodeAgent({})._build_opencode_script(
        {
            "execution_id": "example",
            "flow_id": "example",
            "prompt": "Task",
            "model_identifier": "example-model",
            "model_provider": "openai",
            "model_gateway_enabled": True,
            "model_gateway_provider": "openai",
            "model_gateway_url": "https://example.com/openai/v1",
            "model_endpoint": "https://example.com/openai/v1",
        }
    )
    fragment = script.split("# Write OpenCode configuration", 1)[1]
    fragment = (
        "# Write OpenCode configuration"
        + fragment.split("cat > /tmp/opencode-json-log-filter.js", 1)[0]
    )
    fragment = fragment.replace("/workspace", str(tmp_path))
    result = subprocess.run(
        ["bash", "-c", fragment + "\nenv"],
        env={
            "PATH": os.environ["PATH"],
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "OPENAI_API_KEY": "synthetic-gateway-credential",
            "PRELOOP_API_TOKEN": "synthetic-mcp-credential",
            "PRELOOP_MCP_URL": "https://example.com/mcp/v1",
        },
        text=True,
        capture_output=True,
        check=True,
    )
    assert "synthetic-gateway-credential" not in result.stdout
    assert "synthetic-mcp-credential" not in result.stdout
    config = tmp_path / "opencode.json"
    assert config.stat().st_mode & 0o077 == 0
    assert (
        json.loads(config.read_text())["provider"]["openai"]["options"]["apiKey"]
        == "synthetic-gateway-credential"
    )
