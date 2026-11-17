"""A native-result review that stops before reading the repository continues.

The stub ``codex`` stands in for the CLI and its model: the first session
ends with one assistant message and no command (a ``finish_reason: stop``
turn), a later session runs a command first. No model provider is called.
"""

import json
import os
import subprocess
from pathlib import Path

from unittest.mock import patch

import pytest

from preloop.agents.codex import REVIEW_INSPECTION_CONTINUATIONS, CodexAgent
from preloop.utils.execve_limits import PROMPT_FILE_PATH

SCHEMA = {
    "type": "object",
    "required": ["status", "head_sha"],
    "properties": {"status": {"type": "string"}, "head_sha": {"type": "string"}},
}

STUB = r"""#!/bin/bash
# Fake codex: records each prompt, then plays the scripted session.
n=$(( $(cat "$STATE/calls" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE/calls"
cat > "$STATE/prompt-$n"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--output-last-message" ]; then out="$2"; fi
  shift
done
echo '{"type":"turn.started"}'
if [ "$n" -ge "$INSPECT_FROM" ]; then
  git diff "$PRELOOP_REVIEW_BASE_SHA...$PRELOOP_REVIEW_HEAD_SHA" > "$STATE/diff-$n"
  echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","exit_code":0,"command":"git diff $PRELOOP_REVIEW_BASE_SHA...$PRELOOP_REVIEW_HEAD_SHA"}}'
  msg='{"status":"success","head_sha":"real"}'
else
  msg='{"status":"success","head_sha":"invented"}'
fi
printf '{"type":"item.completed","item":{"id":"m","type":"agent_message","text":%s}}\n' \
  "$(printf %s "$msg" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')"
echo '{"type":"turn.completed"}'
printf %s "$msg" > "$out"
"""


def _context(**overrides: object) -> dict:
    context: dict = {
        "prompt": "Review the change",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": {"native_result": True},
        "result_schema": SCHEMA,
        "git_clone_config": {"publication_mode": "read_only"},
        "read_only_head_sha": "a" * 40,
        "read_only_base_sha": "b" * 40,
        "read_only_base_ref": "main",
    }
    context.update(overrides)
    return context


def _run_block(tmp_path: Path, block: str, inspect_from: int) -> tuple[int, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args],
            text=True,
            env={**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"},
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Jane Doe")
    git("config", "user.email", "jane@example.com")
    (repo / "change.txt").write_text("before\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    (repo / "change.txt").write_text("after\n")
    git("commit", "-qam", "Change")
    head = git("rev-parse", "HEAD")
    block = block.replace("a" * 40, head).replace("b" * 40, base)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text(STUB)
    stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    result = tmp_path / "result.json"
    prompt = tmp_path / "prompt"
    prompt.write_text("Review the change")
    log = tmp_path / "attempt.log"
    out_log = tmp_path / "out.log"
    block = (
        block.replace("/workspace/result.json", str(result))
        .replace("/tmp/preloop-native-result-schema.json", str(tmp_path / "schema"))
        .replace("/tmp/preloop-agent-attempt.log", str(log))
        .replace("/tmp/preloop-agent-output.log", str(out_log))
        .replace("/tmp/preloop-review-continuation.txt", str(tmp_path / "cont"))
    )
    block = block.replace(PROMPT_FILE_PATH, str(prompt)).replace(
        "/workspace", str(repo)
    )
    # The first session, exactly as the main exec runs it.
    script = (
        "set -e\nPRELOOP_AS_AGENT=\n"
        f"codex exec --output-last-message {result} < {prompt} > {log}\n"
        "CODEX_EXIT_CODE=0\n" + block + '\necho "EXIT=$CODEX_EXIT_CODE"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bindir}:/usr/bin:/bin",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_REVIEW_BASE_SHA": base,
            "PRELOOP_REVIEW_HEAD_SHA": head,
            "STATE": str(state),
            "INSPECT_FROM": str(inspect_from),
        },
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "EXIT=0" in proc.stdout
    calls = int((state / "calls").read_text())
    return calls, result


def test_review_that_stops_before_inspection_is_continued(tmp_path: Path) -> None:
    agent = CodexAgent({})
    block = agent._review_inspection_continuation_block(
        _context(), event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    calls, result = _run_block(tmp_path, block, inspect_from=2)
    assert calls == 2
    assert json.loads(result.read_text())["head_sha"] == "real"
    prompt = (tmp_path / "state" / "prompt-2").read_text()
    assert prompt.startswith("Review the change")
    assert "bound base_sha:" in prompt and "Bound head_sha:" in prompt
    assert "diff" in prompt and "changed files" in prompt
    assert "take every commit id" not in prompt


def test_continuation_is_bounded_and_keeps_rejectable_result(tmp_path: Path) -> None:
    agent = CodexAgent({})
    block = agent._review_inspection_continuation_block(
        _context(), event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    calls, result = _run_block(tmp_path, block, inspect_from=99)
    assert calls == 1 + REVIEW_INSPECTION_CONTINUATIONS
    # No result is synthesised: the file is the model's own last answer,
    # which the controller's integrity check rejects as review_not_inspected.
    assert json.loads(result.read_text())["head_sha"] == "invented"


def test_inspected_review_is_not_continued(tmp_path: Path) -> None:
    agent = CodexAgent({})
    block = agent._review_inspection_continuation_block(
        _context(), event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    calls, _ = _run_block(tmp_path, block, inspect_from=1)
    assert calls == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"git_clone_config": {}},
        {"agent_config": {}, "result_schema": None},
        {"agent_config": {"native_result": True, "sandbox_type": "read-only"}},
        {
            "git_clone_config": {
                "publication_mode": "read_only",
                "review_integrity": {"require_inspection": False},
            }
        },
    ],
)
def test_other_flows_get_no_continuation(overrides: dict) -> None:
    agent = CodexAgent({})
    assert (
        agent._review_inspection_continuation_block(
            _context(**overrides), event_filter="cat", model="m", sandbox_flags=""
        )
        == ""
    )


def test_review_script_contains_continuation() -> None:
    agent = CodexAgent({})
    with patch.object(agent, "_prepare_init_commands", return_value=""):
        script = agent._build_codex_script(_context())
    assert "PRELOOP_REVIEW_CONTINUATION" in script
