"""Execute generated Codex capture and recovery shell against synthetic rollouts."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from preloop.agents.codex import CodexAgent

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "python3", "timeout")),
    reason="generated Codex capture/recovery requires bash, python3 and timeout",
)

PARENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CHILD = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OTHER_ROOT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def _context(**extra: object) -> dict[str, object]:
    context: dict[str, object] = {
        "prompt": "Complete the synthetic task",
        "codex_model": "gpt-5.4",
        "execution_id": "synthetic-execution",
        "flow_name": "synthetic-flow",
        "completion_nudge_enabled": False,
    }
    context.update(extra)
    return context


def _write_rollout(
    root: Path,
    name: str,
    session_id: str,
    *,
    source: object = "exec",
    forked_from_id: str | None = None,
) -> None:
    payload: dict[str, object] = {"id": session_id, "source": source}
    if forked_from_id is not None:
        payload["forked_from_id"] = forked_from_id
    (root / name).write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n"
    )


def _generated_script() -> str:
    return CodexAgent({})._build_codex_script(_context())


def _capture_python(script: str) -> str:
    start = script.index("<<'PRELOOP_CODEX_CAPTURE_PY'")
    start = script.index("\n", start) + 1
    end = script.index("\nPRELOOP_CODEX_CAPTURE_PY\n", start)
    return script[start:end]


def _run_capture(tmp_path: Path, expected_id: str = "") -> str:
    script = _generated_script()
    python = _capture_python(script)
    sessions = tmp_path / "sessions"
    result = subprocess.run(
        ["python3", "-", str(sessions), expected_id],
        input=python,
        capture_output=True,
        text=True,
        timeout=10,
        env={
            **os.environ,
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "TESTING": "true",
            "DISABLE_PROPRIETARY_PLUGINS": "true",
        },
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _run_capture_shell(
    tmp_path: Path,
    *,
    restored_id: str = "",
) -> subprocess.CompletedProcess[str]:
    script = _generated_script()
    match = re.search(
        r"# Capture one explicit parent CLI conversation.*?"
        r'echo "PRELOOP_AGENT_SESSION codex \$_pl_codex_sid"\nfi\n',
        script,
        flags=re.S,
    )
    assert match is not None
    env = {
        **os.environ,
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "TESTING": "true",
        "DISABLE_PROPRIETARY_PLUGINS": "true",
    }
    if restored_id:
        env["PRELOOP_CLI_SESSION_RESTORED"] = "1"
        env["PRELOOP_CLI_SESSION_ID"] = restored_id
    return subprocess.run(
        ["bash", "-c", "set -e\n" + match.group(0)],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )


def _run_generated_recovery(
    tmp_path: Path,
    *,
    message: str = "upstream_disconnect: incomplete chunked read",
    initial_exit: int = 1,
    write_sessions: str = "parent_child",
    persistent: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path]:
    agent = CodexAgent({})
    published = tmp_path / "published"
    with (
        patch.object(agent, "_prepare_init_commands", return_value=""),
        patch.object(
            agent,
            "_prepare_git_post_execution_commands",
            # Expand after script path rewriting so Linux /tmp fixtures are
            # not substituted a second time inside the publication path.
            return_value='echo published >> "$TEST_ROOT/published"',
        ),
    ):
        blocks = agent._build_cli_session_blocks(_context())
        blocks["pack"] = ""
        with patch.object(agent, "_build_cli_session_blocks", return_value=blocks):
            script = agent._build_codex_script(_context())
    invocation = script.split('echo "PRELOOP_AGENT_EXEC_START"', 1)[1]
    invocation = invocation.replace("/tmp/", str(tmp_path) + "/")
    invocation = invocation.replace("/workspace/", str(tmp_path) + "/")
    invocation = invocation.replace("sleep $((2 *", "sleep $((0 *")
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        + (f"parent, child, other = {PARENT!r}, {CHILD!r}, {OTHER_ROOT!r}\n")
        + f"message={message!r}; first_exit={initial_exit}; "
        + f"write_sessions={write_sessions!r}; persistent={persistent!r}\n"
        + r"""
import json, os, pathlib, sys
args = sys.argv[1:]
if '--help' in args:
    print('resume --session --resume')
    sys.exit(0)
root = pathlib.Path(os.environ['TEST_ROOT'])
log = root / 'calls'
with log.open('a') as out:
    out.write(json.dumps(args) + '\n')
count = len(log.read_text().splitlines())
failed = persistent or count == 1
sessions = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions'
sessions.mkdir(parents=True, exist_ok=True)
if write_sessions == 'parent_child' and count == 1:
    (sessions / ('rollout-2020-' + parent + '.jsonl')).write_text(
        json.dumps({'type':'session_meta','payload':{'id':parent,'source':'exec'}}) + '\n'
    )
    (sessions / ('rollout-9999-' + child + '.jsonl')).write_text(
        json.dumps({
            'type':'session_meta',
            'payload':{
                'id':child,
                'source':{'subagent':{'thread_spawn':{'parent_thread_id':parent}}},
                'forked_from_id':parent,
            },
        }) + '\n'
    )
elif write_sessions == 'ambiguous' and count == 1:
    (sessions / ('rollout-2020-' + parent + '.jsonl')).write_text(
        json.dumps({'type':'session_meta','payload':{'id':parent,'source':'exec'}}) + '\n'
    )
    (sessions / ('rollout-2021-' + other + '.jsonl')).write_text(
        json.dumps({'type':'session_meta','payload':{'id':other,'source':'exec'}}) + '\n'
    )
print(message if failed else 'FLOW_EXECUTION_SUCCESS')
sys.exit(first_exit if failed else 0)
"""
    )
    fake.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
        "TEST_ROOT": str(tmp_path),
        "CODEX_HOME": str(tmp_path / "codex-home"),
        "PRELOOP_DISABLE_TELEMETRY": "true",
        "TESTING": "true",
        "DISABLE_PROPRIETARY_PLUGINS": "true",
    }
    completed = subprocess.run(
        ["bash", "-c", "set -e\n" + invocation],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    calls_path = tmp_path / "calls"
    calls = (
        [json.loads(line) for line in calls_path.read_text().splitlines()]
        if calls_path.exists()
        else []
    )
    return completed, calls, published


def test_generated_capture_ignores_newer_child(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_rollout(sessions, f"rollout-2020-{PARENT}.jsonl", PARENT)
    _write_rollout(
        sessions,
        f"rollout-9999-{CHILD}.jsonl",
        CHILD,
        source={"subagent": {"thread_spawn": {"parent_thread_id": PARENT}}},
        forked_from_id=PARENT,
    )
    assert _run_capture(tmp_path) == PARENT
    home_sessions = tmp_path / "codex-home" / "sessions"
    home_sessions.mkdir(parents=True)
    for path in sessions.iterdir():
        (home_sessions / path.name).write_text(path.read_text())
    result = _run_capture_shell(tmp_path)
    assert result.returncode == 0, result.stderr
    assert f"PRELOOP_AGENT_SESSION codex {PARENT}" in result.stdout
    assert CHILD not in result.stdout


def test_generated_capture_refuses_ambiguous_roots(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_rollout(sessions, f"rollout-2020-{PARENT}.jsonl", PARENT)
    _write_rollout(sessions, f"rollout-2021-{OTHER_ROOT}.jsonl", OTHER_ROOT)
    assert _run_capture(tmp_path) == ""
    home_sessions = tmp_path / "codex-home" / "sessions"
    home_sessions.mkdir(parents=True)
    for path in sessions.iterdir():
        (home_sessions / path.name).write_text(path.read_text())
    result = _run_capture_shell(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "PRELOOP_AGENT_SESSION" not in result.stdout


def test_generated_capture_preserves_explicit_parent(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_rollout(sessions, f"rollout-2020-{PARENT}.jsonl", PARENT)
    _write_rollout(sessions, f"rollout-2021-{OTHER_ROOT}.jsonl", OTHER_ROOT)
    assert _run_capture(tmp_path, PARENT) == PARENT
    assert _run_capture(tmp_path, CHILD) == ""
    home_sessions = tmp_path / "codex-home" / "sessions"
    home_sessions.mkdir(parents=True)
    for path in sessions.iterdir():
        (home_sessions / path.name).write_text(path.read_text())
    result = _run_capture_shell(tmp_path, restored_id=PARENT)
    assert result.returncode == 0, result.stderr
    assert f"PRELOOP_AGENT_SESSION codex {PARENT}" in result.stdout


def test_generated_capture_missing_id_is_empty(tmp_path: Path) -> None:
    (tmp_path / "sessions").mkdir()
    assert _run_capture(tmp_path) == ""
    (tmp_path / "codex-home" / "sessions").mkdir(parents=True)
    result = _run_capture_shell(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "PRELOOP_AGENT_SESSION" not in result.stdout


def test_transient_error_resumes_same_parent_and_publishes_once(tmp_path: Path) -> None:
    result, calls, published = _run_generated_recovery(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert "resume" in calls[1]
    assert PARENT in calls[1]
    assert CHILD not in calls[1]
    assert "Complete the synthetic task" not in " ".join(calls[1])
    assert published.read_text().splitlines() == ["published"]
    assert result.stdout.count("PRELOOP_STREAM_RECOVERY codex attempt=") == 1


def test_ambiguous_roots_do_not_resume(tmp_path: Path) -> None:
    result, calls, published = _run_generated_recovery(
        tmp_path, write_sessions="ambiguous"
    )
    assert result.returncode != 0
    assert len(calls) == 1
    assert "resume" not in calls[0]
    assert "PRELOOP_STREAM_RECOVERY_UNAVAILABLE codex" in result.stdout
    assert not published.exists()


def test_missing_id_does_not_cold_restart(tmp_path: Path) -> None:
    result, calls, published = _run_generated_recovery(
        tmp_path, write_sessions="missing"
    )
    assert result.returncode != 0
    assert len(calls) == 1
    assert "resume" not in calls[0]
    assert "PRELOOP_STREAM_RECOVERY_UNAVAILABLE codex" in result.stdout
    assert not published.exists()


def test_cancellation_is_not_retried(tmp_path: Path) -> None:
    result, calls, published = _run_generated_recovery(tmp_path, initial_exit=130)
    assert result.returncode == 130
    assert len(calls) == 1
    assert "PRELOOP_STREAM_RECOVERY codex attempt=" not in result.stdout
    assert not published.exists()


def test_auth_failure_is_not_retried(tmp_path: Path) -> None:
    result, calls, published = _run_generated_recovery(
        tmp_path, message="invalid API key"
    )
    assert result.returncode != 0
    assert len(calls) == 1
    assert not published.exists()
