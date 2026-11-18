"""Which ways of reading the bound diff the inspection check accepts.

The table below is the contract: every form a careful reader accepts as
reading the whole bound patch counts, every form that prints a summary, part
of the patch, another range, or whose success the exit code does not show
does not. The same table drives the in-container check (through the real
continuation block and a stub ``codex`` that runs each command in a git
repository) and the controller's evidence counter, and the two must agree.
No model provider is called.
"""

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from preloop.agents.codex import (
    INSPECTION_FORMS,
    REVIEW_CONTINUATION_PROMPT,
    REVIEW_INSPECTION_CONTINUATIONS,
    CodexAgent,
)
from preloop.services.review_inspection import command_reads_change
from preloop.utils.execve_limits import PROMPT_FILE_PATH

HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = BASE
FILES = ["README.md"]

#: (command, accepted). ``{B}``, ``{H}`` and ``{M}`` are the bound base, head
#: and merge base; ``{b}`` and ``{h}`` their seven-digit abbreviations.
FORMS: list[tuple[str, bool]] = [
    # The bound range in each spelling.
    ("git diff {B}...{H}", True),
    ("git diff {B}..{H}", True),
    ("git diff {B} {H}", True),
    ("git diff {M}...{H}", True),
    ("git diff {b}...{h}", True),
    ('git diff "$PRELOOP_REVIEW_BASE_SHA...$PRELOOP_REVIEW_HEAD_SHA"', True),
    # Options that keep the whole patch.
    ("git diff --find-renames --find-copies --unified=80 {B}..{H}", True),
    ("git diff --no-ext-diff --find-renames --find-copies {B}...{H}", True),
    ("git diff -M -C -U5 --no-color --no-textconv -p {B}...{H}", True),
    ("git diff -M50% --histogram {B}...{H}", True),
    ("git --no-pager diff {B}...{H}", True),
    # A pathspec naming the changed files, or the whole tree.
    ("git diff {B}...{H} -- README.md", True),
    ("git diff {B}...{H} -- .", True),
    # Chains whose exit code shows the read succeeded.
    ("cd {R} && git diff {B}...{H}", True),
    ("git diff --stat {B}...{H}; echo ===; git diff {B}...{H}", True),
    ("git rev-parse HEAD 2>/dev/null | head -1; git diff {B}...{H}", True),
    ("false; git diff {B}...{H}", True),
    ("git diff {B}...{H} 2>&1", True),
    ("git diff {B}...{H} && echo done", True),
    # Reading a changed file.
    ("cat README.md", True),
    ("nl -ba README.md", True),
    ("sed -n '1,200p' README.md", True),
    ("git show {H}:README.md", True),
    ("echo ---; cat README.md", True),
    # Summaries, not the patch.
    ("git diff --stat {B}...{H}", False),
    ("git diff --name-only {B}...{H}", False),
    ("git diff --name-status {B}...{H}", False),
    ("git diff --numstat {B}...{H}", False),
    ("git diff --quiet {B}...{H}", False),
    ("git diff --check {B}...{H}", False),
    ("git diff -w {B}...{H}", False),
    # Another range, or HEAD that the agent could have moved.
    ("git diff {B}...HEAD", False),
    ("git diff {H}...{B}", False),
    ("git diff {H}..{B}", False),
    ("git diff {B}...{M}", False),
    ("git diff {B}", False),
    ("git diff {B}...{H}~1", False),
    ("git diff 'HEAD~1'", False),
    # A pathspec that names no changed file, a glob.
    ("git diff {B}...{H} -- calculator.py", False),
    ("git diff {B}...{H} -- '*.py'", False),
    # Output discarded or cut, or success not shown by the exit code.
    ("git diff {B}...{H} | head -5", False),
    ("git diff {B}...{H} | wc -l", False),
    ("git diff {B}...{H} | cat", False),
    ("git diff {B}...{H} > /dev/null", False),
    ("git diff {B}...{H} > /tmp/patch", False),
    ("git diff {B}...{H}; echo done", False),
    ("cat README.md; echo ---", False),
    ("git diff {B}...{H} || true", False),
    ("true || git diff {B}...{H}", False),
    ("git diff {B}...{H} &", False),
    ("cat README.md 2 >/dev/null", False),
    ("git diff {B}...{H} 2>", False),
    ("(git diff {B}...{H})", False),
    ("git diff $(git merge-base {B} {H})...{H}", False),
    ("if true; then git diff {B}...{H}; fi", False),
    ("cd sub; git diff {B}...{H} -- README.md", False),
    ("cat -A README.md | sed -n '1,3p'", False),
    # Shell state that changes what a later diff prints.
    ("export GIT_EXTERNAL_DIFF=true; git diff {B}...{H}", False),
    ("GIT_EXTERNAL_DIFF=true git diff {B}...{H}", False),
    ("git config diff.external true; git diff {B}...{H}", False),
    ("git -c diff.external=true diff {B}...{H}", False),
    ("echo x | tee README.md; cat README.md", False),
    ("command exec >/dev/null; git diff {B}...{H}", False),
    ("builtin export GIT_EXTERNAL_DIFF=true; git diff {B}...{H}", False),
    ("env GIT_EXTERNAL_DIFF=true git diff {B}...{H}", False),
    ("env git config diff.external true; git diff {B}...{H}", False),
    ("git -C {R} config diff.external true; git diff {B}...{H}", False),
    ("git --no-pager config diff.external true; git diff {B}...{H}", False),
    ("timeout 5 git diff {B}...{H}", False),
    ("true || cd sub && cat keep.txt", False),
    ("true || cd sub && git diff {B}...{H} -- README.md", False),
    # Text that only mentions a diff.
    ("echo 'git diff {B}...{H}'", False),
    ("git log -1 {B}..{H}", False),
    ("ls && git rev-parse HEAD", False),
]


def _fill(command: str, base: str, head: str, merge_base: str, root: str) -> str:
    return (
        command.replace("{B}", base)
        .replace("{H}", head)
        .replace("{M}", merge_base)
        .replace("{b}", base[:7])
        .replace("{h}", head[:7])
        .replace("{R}", root)
    )


@pytest.mark.parametrize("command,accepted", FORMS)
def test_form(command: str, accepted: bool) -> None:
    """Each form is decided from its text, wrapped the way Codex runs it."""
    text = _fill(command, BASE, HEAD, MERGE_BASE, "/workspace")
    for wrapped in (text, "/bin/bash -lc " + shlex.quote(text)):
        assert (
            command_reads_change(
                wrapped,
                base=BASE,
                head=HEAD,
                files=FILES,
                root="/workspace",
                merge_base=MERGE_BASE,
            )
            is accepted
        )


def test_two_dot_needs_the_merge_base() -> None:
    """``B..H`` is the bound patch only when the bound base is the merge base."""
    other = "c" * 40
    kwargs: dict[str, Any] = {"base": BASE, "head": HEAD, "files": FILES}
    assert command_reads_change(f"git diff {BASE}..{HEAD}", **kwargs) is False
    assert command_reads_change(f"git diff {BASE} {HEAD}", **kwargs) is False
    assert command_reads_change(f"git diff {BASE}...{HEAD}", **kwargs) is True
    with_merge_base = {**kwargs, "merge_base": other}
    assert command_reads_change(f"git diff {other}..{HEAD}", **with_merge_base)
    assert command_reads_change(f"git diff {other}...{HEAD}", **with_merge_base)
    assert not command_reads_change(f"git diff {BASE}..{HEAD}", **with_merge_base)


def test_cd_after_or_may_not_have_run() -> None:
    """``true || cd sub`` skips the ``cd``; a relative read is from the root."""
    kwargs: dict[str, Any] = {
        "base": BASE,
        "head": HEAD,
        "files": ["sub/README.md"],
        "root": "/workspace",
    }
    assert command_reads_change("cd sub && cat README.md", **kwargs)
    assert not command_reads_change("true || cd sub && cat README.md", **kwargs)
    assert not command_reads_change(
        f"true || cd sub && git diff {BASE}...{HEAD} -- README.md", **kwargs
    )


def test_short_abbreviation_does_not_count() -> None:
    """Six hex digits can name another commit; seven is Git's own minimum."""
    assert not command_reads_change(
        f"git diff {BASE[:6]}...{HEAD[:6]}", base=BASE, head=HEAD, files=FILES
    )


# ---------------------------------------------------------------------------
# The in-container check, run for real, and the controller on the same input.
# ---------------------------------------------------------------------------

#: Fake codex: records each prompt, runs the scripted commands in the
#: checkout for real, reports them as Codex does, then answers.
STUB = """#!{python}
import json, os, shlex, subprocess, sys
state = os.environ["STATE"]
calls = os.path.join(state, "calls")
n = int(open(calls).read()) + 1 if os.path.exists(calls) else 1
open(calls, "w").write(str(n))
open(os.path.join(state, "prompt-%d" % n), "w").write(sys.stdin.read())
out = sys.argv[sys.argv.index("--output-last-message") + 1]
print(json.dumps({{"type": "turn.started"}}))
commands = json.loads(os.environ["COMMANDS"]) if n == 1 else []
codes = []
for index, command in enumerate(commands):
    proc = subprocess.run(
        ["bash", "-c", command], capture_output=True, text=True
    )
    print(json.dumps({{"type": "item.completed", "item": {{
        "id": "item_%d" % index, "type": "command_execution",
        "command": "/bin/bash -lc " + shlex.quote(command),
        "aggregated_output": proc.stdout + proc.stderr,
        "exit_code": proc.returncode, "status": "completed"}}}}))
    codes.append(proc.returncode)
open(os.path.join(state, "codes-%d" % n), "w").write(json.dumps(codes))
answer = json.dumps({{"status": "success", "head_sha": os.environ["HEAD_SHA"]}})
print(json.dumps({{"type": "item.completed",
                  "item": {{"id": "m", "type": "agent_message", "text": answer}}}}))
print(json.dumps({{"type": "turn.completed",
                  "usage": {{"input_tokens": 1, "output_tokens": 1}}}}))
open(out, "w").write(answer)
"""

SCHEMA = {
    "type": "object",
    "required": ["status", "head_sha"],
    "properties": {"status": {"type": "string"}, "head_sha": {"type": "string"}},
}


def _context() -> dict[str, Any]:
    return {
        "prompt": "Review the change",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": {"native_result": True},
        "result_schema": SCHEMA,
        "git_clone_config": {"publication_mode": "read_only"},
        "read_only_head_sha": HEAD,
        "read_only_base_sha": BASE,
        "read_only_base_ref": "main",
    }


@pytest.fixture(scope="module")
def checkout(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    """A repository whose bound change edits README.md only."""
    repo = tmp_path_factory.mktemp("checkout") / "repo"
    repo.mkdir()
    env = {**os.environ, "PRELOOP_DISABLE_TELEMETRY": "true"}

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True, env=env
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Jane Doe")
    git("config", "user.email", "jane@example.com")
    (repo / "README.md").write_text("# Example\n\nBefore.\n")
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "keep.txt").write_text("unchanged\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    (repo / "README.md").write_text("# Example\n\nAfter.\n")
    git("commit", "-qam", "Change")
    head = git("rev-parse", "HEAD")
    return {"repo": str(repo), "base": base, "head": head}


def _container_accepts(
    tmp_path: Path, checkout: dict[str, str], commands: list[str]
) -> tuple[int, Path]:
    """Run the real continuation block; return codex calls and the state dir."""
    repo, base, head = checkout["repo"], checkout["base"], checkout["head"]
    block = CodexAgent({})._review_inspection_continuation_block(
        _context(), event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text(STUB.format(python=sys.executable))
    stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    result = tmp_path / "result.json"
    prompt = tmp_path / "prompt"
    prompt.write_text("Review the change")
    log = tmp_path / "attempt.log"
    block = (
        block.replace(HEAD, head)
        .replace(BASE, base)
        .replace("/workspace/.preloop-native-result.json", str(result))
        .replace("/workspace/result.json", str(result))
        .replace("/tmp/preloop-agent-attempt.log", str(log))
        .replace("/tmp/preloop-agent-output.log", str(tmp_path / "out.log"))
        .replace(PROMPT_FILE_PATH, str(prompt))
        .replace("/workspace", repo)
    )
    script = (
        "set -e\nPRELOOP_AS_AGENT=\n"
        f"codex exec --output-last-message {result} < {prompt} > {log}\n"
        "CODEX_EXIT_CODE=0\n" + block + '\necho "EXIT=$CODEX_EXIT_CODE"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_REVIEW_BASE_SHA": base,
            "PRELOOP_REVIEW_HEAD_SHA": head,
            "STATE": str(state),
            "HEAD_SHA": head,
            "COMMANDS": json.dumps(commands),
        },
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "EXIT=0" in proc.stdout, proc.stderr
    return int((state / "calls").read_text()), state


def _controller_accepts(
    checkout: dict[str, str], commands: list[str], exit_codes: list[int]
) -> bool:
    """The controller's decision from stored evidence of the same commands."""
    from preloop.services.review_diff import compute_diff
    from preloop.services.review_integrity import ObservationCounter

    repo = checkout["repo"]
    diff = compute_diff(repo, checkout["base"], checkout["head"])["diff"]
    counter = ObservationCounter(
        binding={"base_sha": checkout["base"], "head_sha": checkout["head"]},
        diff=diff,
        root=repo,
    )
    for index, (command, code) in enumerate(zip(commands, exit_codes, strict=False)):
        counter.add(
            {
                "observations": [
                    {
                        "source": "native",
                        "kind": "command",
                        "event": "item.completed",
                        "item_id": str(index),
                        "exit_code": code,
                        "value": "/bin/bash -lc " + shlex.quote(command),
                    }
                ]
            }
        )
    return counter.inspected()


@pytest.mark.parametrize("command,accepted", FORMS)
def test_container_and_controller_agree(
    tmp_path: Path, checkout: dict[str, str], command: str, accepted: bool
) -> None:
    """The real check in the shell and the controller decide each form alike."""
    repo, base, head = checkout["repo"], checkout["base"], checkout["head"]
    text = (
        _fill(command, base, head, base, repo)
        .replace("/tmp/patch", "patch.out")
        # Keep the shared checkout's configuration intact; still a write.
        .replace("config diff.external", "config --file scratch.cfg diff.external")
    )
    calls, state = _container_accepts(tmp_path, checkout, [text])
    in_container = calls == 1
    codes = json.loads((state / "codes-1").read_text())
    assert in_container is _controller_accepts(checkout, [text], codes)
    if accepted:
        assert in_container, text
    elif codes == [0]:
        # A rejected form that ran fine is continued. One that failed is
        # rejected for its exit code, which the table does not test.
        assert calls == 1 + REVIEW_INSPECTION_CONTINUATIONS, text
        continuation = (state / "prompt-2").read_text()
        assert "--stat" in continuation and "`HEAD` does not count" in continuation


def test_budget_case_is_not_continued(tmp_path: Path, checkout: dict[str, str]) -> None:
    """A long session that read the diff with harmless options is accepted.

    Shaped after a run that ran 20-odd commands, wrote a complete answer,
    and was still continued twice until its token budget ran out.
    """
    base, head = checkout["base"], checkout["head"]
    commands = [
        "pwd; find . -maxdepth 3 -type d -name .git -print",
        "ls -la",
        "git branch -a -vv; git show-ref",
        f"git log --graph --decorate --oneline --all -n 30; git show -s {head}",
        "git rev-parse --show-toplevel; git rev-parse HEAD; git status --short",
        f"git diff --stat {base}..{head}; git diff --name-status {base}..{head}",
        f"git log --format=fuller --stat {base}..{head}",
        "nl -ba calculator.py",
        f"git diff --find-renames --find-copies --unified=80 {base}..{head}",
        f"git diff --check {base}..{head}; git diff --numstat {base}..{head}",
        "git ls-files; git status --porcelain=v1",
    ]
    calls, _ = _container_accepts(tmp_path, checkout, commands)
    assert calls == 1
    assert _controller_accepts(checkout, commands, [0] * len(commands))


def test_summary_only_session_is_still_continued(
    tmp_path: Path, checkout: dict[str, str]
) -> None:
    """A session that only looked at the shape of the change still fails."""
    base, head = checkout["base"], checkout["head"]
    commands = [
        f"git diff --stat {base}...{head}",
        f"git diff --name-only {base}...{head}",
        "git diff HEAD~1...HEAD | head -5",
        f"echo 'git diff {base}...{head}'",
        "cat calculator.py",
    ]
    calls, _ = _container_accepts(tmp_path, checkout, commands)
    assert calls == 1 + REVIEW_INSPECTION_CONTINUATIONS
    assert not _controller_accepts(checkout, commands, [0] * len(commands))


def test_continuation_prompt_names_accepted_and_rejected_forms() -> None:
    """The continued model is told what counts, so it does not repeat itself."""
    text = REVIEW_CONTINUATION_PROMPT.format(
        base=BASE, head=HEAD, base_ref="main", result_bindings=""
    )
    assert f"`git diff {BASE}...{HEAD}` as its own command" in text
    for rejected in ("--stat", "--name-only", "--name-status", "--numstat"):
        assert rejected in text
    assert "`HEAD` does not count" in text
    assert "piping" in text and "`;`" in text
    assert INSPECTION_FORMS.format(base=BASE, head=HEAD) in text
    # The first form the prompt names is one the check accepts.
    assert command_reads_change(
        f"git diff {BASE}...{HEAD}", base=BASE, head=HEAD, files=FILES
    )
