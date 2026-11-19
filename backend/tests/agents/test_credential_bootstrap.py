"""Credential-safe runtime bootstrap, including image entrypoint output."""

import os
from pathlib import Path
import subprocess
import sys
import time

from preloop.models import crud  # noqa: F401
from preloop.agents.credential_bootstrap import bootstrap_source
from preloop.services.checkpoint_runtime import checkpoint_shell

_TOKEN = "synthetic-bootstrap-credential-123456789"


def _run_bootstrap(script: str, **extra_env: str) -> tuple[int, str, float]:
    """Run the real wrapper over ``script``.

    Args:
        script: Bash text handed to the wrapped shell.
        **extra_env: Variables added to the wrapper environment.

    Returns:
        The exit code, the combined output and the wall-clock seconds taken.
    """
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-c", bootstrap_source(), "/bin/bash", "-c", script],
        env={
            "PATH": os.environ["PATH"],
            "OPENAI_API_KEY": _TOKEN,
            "PRELOOP_DISABLE_TELEMETRY": "true",
            **extra_env,
        },
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode, result.stdout, time.monotonic() - started


def test_standalone_bootstrap_needs_only_stdlib(tmp_path: Path) -> None:
    """Agent images have neither server packages nor deployment keys."""
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            bootstrap_source(),
            "/bin/bash",
            "-c",
            'printf "%s" "$OPENAI_API_KEY"',
        ],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "OPENAI_API_KEY": _TOKEN,
            "PRELOOP_DISABLE_TELEMETRY": "true",
        },
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert result.stdout == "[REDACTED]"
    assert _TOKEN not in result.stderr


def _gone(pid: int) -> bool:
    """Report whether ``pid`` ends within two seconds.

    A signalled process that its new parent has not reaped yet still exists
    as a zombie, so the state is checked rather than mere existence.
    """
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        state = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if not state or state.startswith("Z"):
            return True
        time.sleep(0.05)
    return False


def test_bootstrap_exits_with_the_script_despite_background_process() -> None:
    """A background process holding the output pipe must not delay the exit."""
    code, output, elapsed = _run_bootstrap(
        'sleep 30 &\necho "pid=$!"\necho "key=$OPENAI_API_KEY"\nexit 7\n'
    )
    assert elapsed < 5, f"wrapper waited {elapsed:.1f}s for a background process"
    assert code == 7
    assert _TOKEN not in output
    assert "key=[REDACTED]\n" in output
    pid = int(output.split("pid=", 1)[1].split()[0])
    assert _gone(pid), "the leftover background process was not signalled"


def test_bootstrap_exits_with_the_script_despite_checkpoint_loop(
    tmp_path: Path,
) -> None:
    """The real checkpoint loop must not keep the wrapper alive after exit."""
    client = tmp_path / "client.py"
    client.write_text("import sys\nprint('PRELOOP_CHECKPOINT stub', sys.argv[1])\n")
    shell = checkpoint_shell({"checkpoint_env": {"present": "1"}}).replace(
        "/tmp/preloop-checkpoint-client.py", str(client)
    )
    code, output, elapsed = _run_bootstrap(
        shell
        + '\n_preloop_start_checkpoint_loop\nsleep 1\necho "key=$OPENAI_API_KEY"\n'
        + "exit 7\n",
        PRELOOP_CHECKPOINT_INTERVAL="30",
    )
    assert elapsed < 5, f"wrapper waited {elapsed:.1f}s for the checkpoint loop"
    assert code == 7
    assert _TOKEN not in output
    assert "key=[REDACTED]\n" in output
    assert "PRELOOP_CHECKPOINT stub capture\n" in output


def test_checkpoint_trap_leaves_no_process_on_the_output_pipe(
    tmp_path: Path,
) -> None:
    """Run directly under bash, the loop's sleep must end with the script."""
    client = tmp_path / "client.py"
    client.write_text("import sys\nprint('PRELOOP_CHECKPOINT stub', sys.argv[1])\n")
    shell = checkpoint_shell({"checkpoint_env": {"present": "1"}}).replace(
        "/tmp/preloop-checkpoint-client.py", str(client)
    )
    started = time.monotonic()
    result = subprocess.run(
        ["bash", "-c", shell + "\n_preloop_start_checkpoint_loop\nsleep 1\nexit 7\n"],
        env={"PATH": os.environ["PATH"], "PRELOOP_CHECKPOINT_INTERVAL": "30"},
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"the output pipe stayed open for {elapsed:.1f}s"
    assert result.returncode == 7
    assert result.stdout == "PRELOOP_CHECKPOINT stub capture\n"
    assert "Terminated" not in result.stderr


def test_checkpoint_loop_still_captures_on_each_interval(tmp_path: Path) -> None:
    """The loop keeps capturing while the agent runs."""
    client = tmp_path / "client.py"
    client.write_text("import sys\nprint('PRELOOP_CHECKPOINT stub', sys.argv[1])\n")
    shell = checkpoint_shell({"checkpoint_env": {"present": "1"}}).replace(
        "/tmp/preloop-checkpoint-client.py", str(client)
    )
    result = subprocess.run(
        ["bash", "-c", shell + "\n_preloop_start_checkpoint_loop\nsleep 3.5\n"],
        env={"PATH": os.environ["PATH"], "PRELOOP_CHECKPOINT_INTERVAL": "1"},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.count("PRELOOP_CHECKPOINT stub capture\n") >= 3


def test_bootstrap_keeps_and_masks_trailing_partial_line() -> None:
    """Unterminated output written just before exit is delivered and masked."""
    code, output, elapsed = _run_bootstrap(
        'sleep 30 &\nprintf "tail=%s" "$OPENAI_API_KEY"\nexit 3\n'
    )
    assert elapsed < 5
    assert code == 3
    assert output == "tail=[REDACTED]"


def test_bootstrap_masks_credential_split_across_reads() -> None:
    """A credential written in two pieces around a pause is still masked."""
    code, output, elapsed = _run_bootstrap(
        "sleep 30 &\n"
        'printf "a %s" "${OPENAI_API_KEY:0:17}"\nsleep 0.5\n'
        'printf "%s b\\n" "${OPENAI_API_KEY:17}"\nexit 0\n'
    )
    assert elapsed < 5
    assert code == 0
    assert output == "a [REDACTED] b\n"


def test_bootstrap_delivers_large_output_written_before_exit() -> None:
    """Output larger than the pipe buffer is not truncated at exit."""
    code, output, elapsed = _run_bootstrap(
        "sleep 30 &\n"
        "head -c 300000 /dev/zero | tr '\\0' x\n"
        'printf "\\nend=%s" "$OPENAI_API_KEY"\nexit 5\n'
    )
    assert elapsed < 5
    assert code == 5
    assert output == "x" * 300000 + "\nend=[REDACTED]"


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


def test_bootstrap_withholds_unfinished_credential_from_leftover_writer() -> None:
    """A credential a leftover process was still writing is not shown in part."""
    code, output, elapsed = _run_bootstrap(
        '(printf "a %s" "${OPENAI_API_KEY:0:17}"; sleep 30) &\nsleep 0.5\nexit 0\n'
    )
    assert elapsed < 5
    assert code == 0
    assert output == "a [REDACTED]"


def test_stream_cut_matches_finish_without_an_unfinished_candidate() -> None:
    """Abandoning the source changes nothing unless a candidate is unfinished."""
    from preloop.utils.execution_credentials import (
        CredentialMasker,
        CredentialStream,
    )

    masker = CredentialMasker.from_values([_TOKEN])
    for text in (
        b"tests pass",
        b"ends with s",
        b"sy",
        b"key=" + _TOKEN.encode(),
        _TOKEN.encode() + b" then " + _TOKEN.encode(),
        b"synt" + b"x" * 60,
    ):
        finished, cut = CredentialStream(masker), CredentialStream(masker)
        expected = finished.feed(text) + finished.finish()
        assert cut.feed(text) + cut.cut() == expected, text
    stream = CredentialStream(masker)
    head = stream.feed(b"log " + _TOKEN.encode() + b" and " + _TOKEN.encode()[:20])
    assert head + stream.cut() == b"log [REDACTED] and [REDACTED]"
    assert stream.pending == b""
