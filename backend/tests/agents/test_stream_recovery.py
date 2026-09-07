"""Execute generated CLI invocation/recovery blocks against synthetic CLIs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.gemini import GeminiAgent
from preloop.agents.opencode import OpenCodeAgent
from preloop.agents.stream_recovery import build_stream_recovery_block

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "node", "timeout")),
    reason="generated launch blocks require bash, node and timeout",
)

SID = "12345678-1234-4234-8234-123456789abc"


def _run_generated(
    tmp_path: Path,
    harness: str,
    *,
    message: str = "upstream_disconnect: incomplete chunked read",
    initial_exit: int = 1,
    missing_session: bool = False,
    persistent: bool = False,
    recovered: bool = False,
    truncated: bool = False,
    resume_error: str | None = None,
    omit_result: bool = False,
    raw_error: bool = False,
    write_result: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], Path]:
    agent_class = {
        "codex": CodexAgent,
        "opencode": OpenCodeAgent,
        "gemini": GeminiAgent,
    }[harness]
    agent = agent_class({})
    context = {
        "prompt": "Complete the synthetic task",
        "model_identifier": "test-model",
        "execution_id": "synthetic-execution",
        "completion_nudge_enabled": False,
    }
    published = tmp_path / "published"
    with (
        patch.object(agent, "_prepare_init_commands", return_value=""),
        patch.object(
            agent,
            "_prepare_git_post_execution_commands",
            return_value=f'echo published >> "{published}"',
        ),
    ):
        if harness in ("codex", "opencode"):
            blocks = agent._build_cli_session_blocks(context)
            blocks["pack"] = ""
            with patch.object(agent, "_build_cli_session_blocks", return_value=blocks):
                script = getattr(agent, f"_build_{harness}_script")(context)
        else:
            script = agent._build_gemini_script(context)
    # Execute the real invocation, capture, recovery and publication blocks,
    # excluding unrelated image setup/install/credential provisioning.
    invocation = script.split('echo "PRELOOP_AGENT_EXEC_START"', 1)[1]
    if harness == "opencode":
        filter_body = script.split("<<'JS'\n", 1)[1].split("\nJS", 1)[0]
        (tmp_path / "opencode-json-log-filter.js").write_text(
            filter_body.replace('"/tmp/', f'"{tmp_path}/')
        )
    invocation = invocation.replace("/tmp/", str(tmp_path) + "/")
    invocation = invocation.replace("/workspace/", str(tmp_path) + "/")
    invocation = invocation.replace("sleep $((2 *", "sleep $((0 *")
    (tmp_path / "prompt.txt").write_text(context["prompt"])
    fake = tmp_path / harness
    fake.write_text(
        "#!/usr/bin/env python3\n"
        + f"harness={harness!r}; sid={SID!r}; message={message!r}; first_exit={initial_exit}; missing={missing_session}; persistent={persistent}; recovered={recovered}; truncated={truncated}; resume_error={resume_error!r}; omit_result={omit_result}; raw_error={raw_error}; write_result={write_result!r}\n"
        + """import json, os, pathlib, sys
args = sys.argv[1:]
if '--help' in args:
    print('resume --session --resume'); sys.exit(0)
root = pathlib.Path(os.environ['TEST_ROOT'])
log = root / 'calls'
with log.open('a') as out: out.write(json.dumps(args) + '\\n')
count = len(log.read_text().splitlines())
if count == 1 and write_result is not None:
    (root / 'result.json').write_text(write_result)
failed = persistent or count == 1
if count > 1 and resume_error:
    failed = True
    message = resume_error
if not missing and harness == 'codex':
    sessions = pathlib.Path(os.environ['CODEX_HOME']) / 'sessions'
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / ('rollout-2026-' + sid + '.jsonl')).write_text(json.dumps({'type':'session_meta','payload':{'id':sid,'source':'exec'}}) + '\\n')
    # A newer delegated session must never steal recovery from the parent.
    (sessions / 'rollout-9999-child.jsonl').write_text(json.dumps({'type':'session_meta','payload':{'id':'87654321-1234-4234-8234-123456789abc','source':{'subagent':{}},'forked_from_id':sid}}) + '\\n')
if harness == 'codex':
    print(message if failed else 'FLOW_EXECUTION_SUCCESS')
elif harness == 'opencode':
    if truncated and failed:
        print(json.dumps({'type':'text','sessionID':'ses_parent1234','part':{'type':'text','text':'Partial output'}})); sys.exit(0)
    obj = {'type':'error','error':{'name':'APIError','data':{'message':message}}} if failed else {'type':'text','part':{'type':'text','text':'FLOW_EXECUTION_SUCCESS'}}
    if not missing: obj['sessionID'] = 'ses_parent1234'
    print(json.dumps(obj))
    if not failed or recovered: print(json.dumps({'type':'step_finish','sessionID':'ses_parent1234','part':{'reason':'stop'}}))
else:
    if not missing: print(json.dumps({'type':'init','session_id':sid}))
    if truncated and failed:
        print(json.dumps({'type':'message','role':'assistant','content':'Partial output'})); sys.exit(0)
    if failed:
        if raw_error: print(message, file=sys.stderr)
        else: print(json.dumps({'type':'error','severity':'error','message':message}))
    else: print(json.dumps({'type':'message','role':'assistant','content':'FLOW_EXECUTION_SUCCESS\\n'}))
    if not (omit_result and failed):
        print(json.dumps({'type':'result','status':'error' if failed and not recovered else 'success'}))
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
    }
    completed = subprocess.run(
        ["bash", "-c", "set -e\n" + invocation],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    return completed, calls, published


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
def test_generated_launch_resumes_parent_and_publishes_once(
    tmp_path: Path, harness: str
) -> None:
    result, calls, published = _run_generated(tmp_path, harness)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    expected = "ses_parent1234" if harness == "opencode" else SID
    assert expected in calls[1]
    if harness != "codex":
        assert "Complete the synthetic task" in calls[0]
    assert "Complete the synthetic task" not in calls[1]
    assert published.read_text().splitlines() == ["published"]


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
@pytest.mark.parametrize(
    "scenario", ["auth", "missing_session", "cancelled", "completed"]
)
def test_generated_launch_refuses_unsafe_recovery(
    tmp_path: Path, harness: str, scenario: str
) -> None:
    kwargs = {
        "auth": {"message": "invalid API key"},
        "missing_session": {"missing_session": True},
        "cancelled": {"initial_exit": 130},
        "completed": {"message": "FLOW_EXECUTION_SUCCESS\nupstream_disconnect"},
    }[scenario]
    result, calls, published = _run_generated(tmp_path, harness, **kwargs)
    assert result.returncode != 0
    assert len(calls) == 1
    assert not published.exists()


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
def test_generated_launch_exhausts_two_resumes(tmp_path: Path, harness: str) -> None:
    result, calls, published = _run_generated(tmp_path, harness, persistent=True)
    assert result.returncode != 0
    assert len(calls) == 3
    assert not published.exists()


@pytest.mark.parametrize("harness", ["opencode", "gemini"])
def test_structured_terminal_error_overrides_cli_zero(
    tmp_path: Path, harness: str
) -> None:
    result, calls, published = _run_generated(tmp_path, harness, initial_exit=0)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert published.read_text().splitlines() == ["published"]


@pytest.mark.parametrize("harness", ["opencode", "gemini"])
def test_later_structured_success_does_not_resume(tmp_path: Path, harness: str) -> None:
    result, calls, published = _run_generated(
        tmp_path, harness, initial_exit=0, recovered=True
    )
    assert result.returncode == 0
    assert len(calls) == 1
    assert published.exists()


def test_timeout_kills_term_ignoring_resume(tmp_path: Path) -> None:
    attempt_log = tmp_path / "attempt"
    attempt_log.write_text("upstream_disconnect")
    block = build_stream_recovery_block(
        agent_label="synthetic",
        exit_code_var="RC",
        session_id_expr='"known-session"',
        resume_probe="true",
        resume_command="$PRELOOP_RECOVERY_TIMEOUT bash -c 'trap \"\" TERM; while :; do sleep 1; done'\nRC=$?",
        attempt_log_path=str(attempt_log),
        prompt_path=str(tmp_path / "prompt"),
        timeout_seconds=1,
        kill_after_seconds=1,
        backoff_seconds=0,
    )
    result = subprocess.run(
        ["bash", "-c", "RC=1\n" + block + '\nexit "$RC"'],
        capture_output=True,
        text=True,
        timeout=8,
    )
    assert result.returncode == 137
    assert result.stdout.count("PRELOOP_STREAM_RECOVERY synthetic attempt=") == 1


def test_opencode_final_success_overrides_sticky_prior_error_exit(
    tmp_path: Path,
) -> None:
    result, calls, published = _run_generated(tmp_path, "opencode", recovered=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 1
    assert published.exists()


@pytest.mark.parametrize("harness", ["opencode", "gemini"])
def test_truncated_structured_stream_resumes_instead_of_false_success(
    tmp_path: Path, harness: str
) -> None:
    result, calls, published = _run_generated(tmp_path, harness, truncated=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert published.read_text().splitlines() == ["published"]


@pytest.mark.parametrize("harness", ["opencode", "gemini"])
def test_resume_terminal_failure_does_not_reuse_old_transient_log(
    tmp_path: Path, harness: str
) -> None:
    result, calls, published = _run_generated(
        tmp_path, harness, resume_error="invalid API key"
    )
    assert result.returncode != 0
    assert len(calls) == 2
    assert not published.exists()


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
def test_stale_completion_report_does_not_prevent_resume(
    tmp_path: Path, harness: str
) -> None:
    (tmp_path / "result.json").write_text('{"status":"success"}')
    result, calls, published = _run_generated(tmp_path, harness)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert published.exists()
    assert (tmp_path / "result.json").read_text() == '{"status":"success"}'


def test_explicit_nontransient_gemini_error_at_eof_does_not_become_disconnect(
    tmp_path: Path,
) -> None:
    result, calls, published = _run_generated(
        tmp_path,
        "gemini",
        message="INVALID_ARGUMENT: unsupported model",
        initial_exit=0,
        omit_result=True,
    )
    assert result.returncode != 0
    assert len(calls) == 1
    assert "stream disconnected before completion" not in result.stdout
    assert not published.exists()


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
@pytest.mark.parametrize("status", ["running", "", "partial"])
def test_nonterminal_progress_report_does_not_prevent_resume(
    tmp_path: Path, status: str, harness: str
) -> None:
    (tmp_path / "result.json").write_text(json.dumps({"status": status}))
    result, calls, published = _run_generated(tmp_path, harness)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert published.exists()


def test_native_nonzero_gemini_stderr_keeps_original_failure(tmp_path: Path) -> None:
    result, calls, published = _run_generated(
        tmp_path,
        "gemini",
        message="INVALID_ARGUMENT: unsupported model",
        raw_error=True,
        omit_result=True,
    )
    assert result.returncode != 0
    assert len(calls) == 1
    assert "stream disconnected before completion" not in result.stdout
    assert not published.exists()


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
@pytest.mark.parametrize("existing", [False, True])
def test_current_terminal_report_prevents_resume_even_if_content_is_identical(
    tmp_path: Path, harness: str, existing: bool
) -> None:
    report = '{"status":"success"}'
    if existing:
        (tmp_path / "result.json").write_text(report)
    result, calls, published = _run_generated(tmp_path, harness, write_result=report)
    assert result.returncode != 0
    assert len(calls) == 1
    assert not published.exists()
    assert (tmp_path / "result.json").read_text() == report


@pytest.mark.parametrize("harness", ["codex", "opencode", "gemini"])
def test_current_nonterminal_report_does_not_prevent_resume(
    tmp_path: Path, harness: str
) -> None:
    (tmp_path / "result.json").write_text('{"status":"success"}')
    result, calls, published = _run_generated(
        tmp_path, harness, write_result='{"status":"running"}'
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(calls) == 2
    assert published.exists()
