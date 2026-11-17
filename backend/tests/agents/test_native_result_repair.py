"""A native final answer that does not match the result schema is asked for again.

Unit tests for the stdlib checker, and shell tests that run the generated
continuation blocks with a stub ``codex`` standing in for the CLI and its
model. No model provider is called.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from jsonschema import Draft202012Validator

from preloop.agents.codex import (
    MAX_EXTRA_SESSIONS,
    NATIVE_RESULT_REPAIR_CONTINUATIONS,
    CodexAgent,
)
from preloop.agents.container import RUN_DEADLINE_EPOCH_CONTEXT_KEY
from preloop.services.flow_result_schema import validate_result_document
from preloop.services.native_result_repair import (
    repair_paths,
    repair_prompt,
    schema_errors,
)
from preloop.utils.execve_limits import PROMPT_FILE_PATH

SHA = {"type": "string", "pattern": "^[0-9a-f]{40}$"}
TEXT = {"type": "string", "pattern": "\\S"}
#: The shape of a strict read-only review schema: every field required, no
#: extra properties, enums for status and verdict.
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "base_sha",
        "findings",
        "head_sha",
        "review",
        "status",
        "success",
        "verdict",
    ],
    "properties": {
        "base_sha": SHA,
        "head_sha": SHA,
        "review": TEXT,
        "status": {"type": "string", "enum": ["success"]},
        "success": {"type": "boolean"},
        "verdict": {"type": "string", "enum": ["pass", "changes_requested", "error"]},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["problem", "evidence", "line"],
                "properties": {
                    "problem": TEXT,
                    "evidence": TEXT,
                    "line": {"type": ["integer", "null"], "minimum": 1},
                },
            },
        },
    },
}


def review(**overrides: Any) -> dict[str, Any]:
    """A complete, valid review document."""
    document: dict[str, Any] = {
        "base_sha": "b" * 40,
        "head_sha": "a" * 40,
        "review": "divide() raises on zero; the README says it returns 0.0.",
        "status": "success",
        "success": True,
        "verdict": "changes_requested",
        "findings": [{"problem": "Wrong claim", "evidence": "calc.py:3", "line": 3}],
    }
    document.update(overrides)
    return {key: value for key, value in document.items() if value is not ...}


INVALID = [
    review(findings=...),
    review(verdict="approve"),
    review(success="yes"),
    review(head_sha="HEAD"),
    review(review=" "),
    review(extra=1),
    review(findings=[{"problem": "x", "evidence": "y", "line": 0}]),
    review(findings=[{"problem": "x", "evidence": "y", "line": True}]),
    review(findings=[{"problem": "x", "line": None}]),
    review(findings="none"),
    [],
]


@pytest.mark.parametrize("document", INVALID)
def test_checker_finds_the_same_paths_as_the_controller(document: Any) -> None:
    expected = sorted(
        {
            error["path"]
            for error in validate_result_document(
                document if isinstance(document, dict) else {"_": document}, SCHEMA
            )["errors"]
        }
    )
    if not isinstance(document, dict):
        assert schema_errors(document, SCHEMA)
        return
    assert sorted({error["path"] for error in schema_errors(document, SCHEMA)}) == (
        expected
    )
    assert list(Draft202012Validator(SCHEMA).iter_errors(document))


def test_checker_accepts_a_valid_review() -> None:
    assert schema_errors(review(), SCHEMA) == []
    assert schema_errors(review(findings=[]), SCHEMA) == []
    assert repair_prompt(json.dumps(review()), SCHEMA, "Review") == ""


KEYWORDS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "minProperties": 2,
    "maxProperties": 3,
    "properties": {
        "tags": {"type": "array", "uniqueItems": True},
        "score": {"type": "number", "multipleOf": 0.5},
        "count": {"type": "integer", "multipleOf": 2},
    },
}


@pytest.mark.parametrize(
    "document",
    [
        {"tags": ["a", "a"], "score": 1.0},
        {"tags": ["a"], "score": 1.25},
        {"tags": ["a"], "count": 3},
        {"tags": ["a"]},
        {"tags": ["a"], "score": 1.0, "count": 2, "other": 1},
    ],
)
def test_controller_keywords_are_checked(document: dict[str, Any]) -> None:
    expected = {
        error["path"]
        for error in validate_result_document(document, KEYWORDS_SCHEMA)["errors"]
    }
    assert expected
    assert {e["path"] for e in schema_errors(document, KEYWORDS_SCHEMA)} == expected


def test_keywords_accept_a_valid_document() -> None:
    document = {"tags": ["a", "b"], "score": 1.5, "count": 4}
    assert schema_errors(document, KEYWORDS_SCHEMA) == []


def test_large_invalid_value_is_shortened_in_the_prompt() -> None:
    answer = json.dumps(review(verdict="x" * 100_000))
    errors = schema_errors(json.loads(answer), SCHEMA)
    assert all(len(error["message"]) < 400 for error in errors)
    prompt = repair_prompt(answer, SCHEMA, "Review")
    # The previous answer is capped separately; the error list stays small.
    error_section = prompt.split("Your previous final answer, verbatim:")[0]
    assert len(error_section) < 2_000


def test_missing_field_is_named_by_its_path() -> None:
    errors = schema_errors(review(findings=...), SCHEMA)
    assert errors == [
        {"path": "$.findings", "message": "'findings' is a required property"}
    ]


def test_checker_follows_local_references_and_any_of() -> None:
    schema = {
        "$defs": {"verdict": {"type": "string", "enum": ["pass", "error"]}},
        "type": "object",
        "required": ["verdict", "note"],
        "properties": {
            "verdict": {"$ref": "#/$defs/verdict"},
            "note": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
    }
    assert schema_errors({"verdict": "pass", "note": None}, schema) == []
    paths = [e["path"] for e in schema_errors({"verdict": "x", "note": 1}, schema)]
    assert paths == ["$.verdict", "$.note"]


@pytest.mark.parametrize(
    "raw,message",
    [(None, "No final answer"), ("", "No final answer"), ("{not json", "Not valid")],
)
def test_missing_or_unparseable_answer_is_repaired(raw: Any, message: str) -> None:
    prompt = repair_prompt(raw, SCHEMA, "Review the change")
    assert prompt.startswith("Review the change")
    assert f"- $: {message}" in prompt
    assert repair_paths(raw, SCHEMA) == ["$"]


def test_repair_prompt_names_errors_and_quotes_the_answer() -> None:
    answer = json.dumps(review(findings=..., verdict="approve"))
    prompt = repair_prompt(answer, SCHEMA, "Review the change")
    assert "- $.findings: 'findings' is a required property" in prompt
    assert "- $.verdict: 'approve' is not one of" in prompt
    assert answer in prompt
    assert "same review" in prompt
    # The schema is attached to the session and already in the instructions;
    # the repair text does not repeat it.
    assert '"additionalProperties"' not in prompt
    assert repair_paths(answer, SCHEMA) == ["$.findings", "$.verdict"]


# --- shell: the generated blocks with a stub codex -------------------------

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
  git diff "$PRELOOP_REVIEW_BASE_SHA...$PRELOOP_REVIEW_HEAD_SHA" > /dev/null
  echo '{"type":"item.completed","item":{"id":"i1","type":"command_execution","exit_code":0,"command":"git diff $PRELOOP_REVIEW_BASE_SHA...$PRELOOP_REVIEW_HEAD_SHA"}}'
fi
msg=$(cat "$ANSWERS/$n" 2>/dev/null || cat "$ANSWERS/last")
echo '{"type":"turn.completed"}'
printf %s "$msg" > "$out"
"""


def _context(**overrides: Any) -> dict[str, Any]:
    context: dict[str, Any] = {
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


def _blocks(context: dict[str, Any]) -> str:
    agent = CodexAgent({})
    kwargs = {"event_filter": "cat", "model": "m", "sandbox_flags": "--yolo"}
    return agent._review_inspection_continuation_block(
        context, **kwargs
    ) + agent._native_result_repair_block(context, **kwargs)


def _run(
    tmp_path: Path,
    block: str,
    answers: list[Any],
    *,
    inspect_from: int = 1,
) -> tuple[int, Path, str]:
    """Run the first session and ``block``; return calls, answer and log."""
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
    (repo / "calc.py").write_text("before\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    (repo / "calc.py").write_text("after\n")
    git("commit", "-qam", "Change")
    head = git("rev-parse", "HEAD")
    answers_dir = tmp_path / "answers"
    answers_dir.mkdir()
    for index, answer in enumerate(answers, start=1):
        text = answer if isinstance(answer, str) else json.dumps(answer)
        text = text.replace("a" * 40, head).replace("b" * 40, base)
        (answers_dir / str(index)).write_text(text)
    (answers_dir / "last").write_text((answers_dir / str(len(answers))).read_text())
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "codex"
    stub.write_text(STUB)
    stub.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    result = tmp_path / "result.json"
    schema = tmp_path / "schema"
    schema.write_text(json.dumps(SCHEMA))
    prompt = tmp_path / "prompt"
    prompt.write_text("Review the change")
    log = tmp_path / "attempt.log"
    out_log = tmp_path / "out.log"
    block = block.replace("a" * 40, head).replace("b" * 40, base)
    block = (
        block.replace("/workspace/.preloop-native-result.json", str(result))
        .replace("/workspace/result.json", str(result))
        .replace("/tmp/preloop-native-result-schema.json", str(schema))
        .replace("/tmp/preloop-agent-attempt.log", str(log))
        .replace("/tmp/preloop-agent-output.log", str(out_log))
    )
    block = block.replace(PROMPT_FILE_PATH, str(prompt)).replace(
        "/workspace", str(repo)
    )
    script = (
        "set -e\nPRELOOP_AS_AGENT=\n"
        f"codex exec --output-last-message {result} < {prompt} > {log}\n"
        "CODEX_EXIT_CODE=0\n" + block + '\necho "EXIT=$CODEX_EXIT_CODE"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={
            # The block runs python3; slim images keep it in /usr/local/bin.
            "PATH": f"{bindir}:{Path(sys.executable).parent}:/usr/bin:/bin",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "PRELOOP_REVIEW_BASE_SHA": base,
            "PRELOOP_REVIEW_HEAD_SHA": head,
            "STATE": str(state),
            "ANSWERS": str(answers_dir),
            "INSPECT_FROM": str(inspect_from),
        },
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "EXIT=0" in proc.stdout
    calls = int((state / "calls").read_text())
    return calls, result, out_log.read_text() if out_log.exists() else ""


def _markers(log: str) -> list[dict[str, Any]]:
    prefix = "PRELOOP_REVIEW_CONTINUATION "
    return [
        json.loads(line[len(prefix) :])
        for line in log.splitlines()
        if line.startswith(prefix)
    ]


def test_invalid_answer_gets_one_repair_naming_the_errors(tmp_path: Path) -> None:
    calls, result, log = _run(
        tmp_path, _blocks(_context()), [review(findings=...), review()]
    )
    assert calls == 2
    assert "findings" in json.loads(result.read_text())
    prompt = (tmp_path / "state" / "prompt-2").read_text()
    assert prompt.startswith("Review the change")
    assert "- $.findings: 'findings' is a required property" in prompt
    assert '"verdict": "changes_requested"' in prompt
    assert _markers(log) == [
        {"attempt": 1, "reason": "result_schema_invalid", "paths": ["$.findings"]}
    ]


def test_repairs_are_bounded_and_keep_the_last_answer(tmp_path: Path) -> None:
    calls, result, log = _run(
        tmp_path, _blocks(_context()), [review(verdict="approve")]
    )
    assert calls == 1 + NATIVE_RESULT_REPAIR_CONTINUATIONS
    # Nothing is synthesised: the controller sees the model's last answer and
    # fails the run with result_schema_invalid as before.
    assert json.loads(result.read_text())["verdict"] == "approve"
    assert [m["attempt"] for m in _markers(log)] == [1, 2]


def test_no_repair_without_room_in_the_run_budget(tmp_path: Path) -> None:
    context = _context(**{RUN_DEADLINE_EPOCH_CONTEXT_KEY: int(time.time()) + 25})
    calls, _, log = _run(tmp_path, _blocks(context), [review(findings=...)])
    assert calls == 1
    assert _markers(log) == []


def test_repair_session_is_cut_at_the_run_budget() -> None:
    context = _context(**{RUN_DEADLINE_EPOCH_CONTEXT_KEY: 2_000_000_000})
    block = CodexAgent({})._native_result_repair_block(
        context, event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    assert "_pl_run_deadline=2000000000" in block
    assert "timeout -k 5 $(_pl_repair_left)" in block


#: A final answer that is a progress note: valid JSON, none of the schema's
#: required properties.
PROGRESS_NOTE: dict[str, Any] = {
    "": {"Content": "Fetching the full contiguous diff before review."}
}
COMBINED = "uninspected_invalid_result"
UNINSPECTED = "no_repository_inspection"


def _reasons(log: str) -> list[str]:
    return [marker["reason"] for marker in _markers(log)]


def test_uninspected_progress_note_gets_one_combined_continuation(
    tmp_path: Path,
) -> None:
    # Two sessions end without reading the change, the third ends on a
    # progress note. One more session is told both things and does both.
    calls, result, log = _run(
        tmp_path,
        _blocks(_context()),
        [review(), review(), PROGRESS_NOTE, review()],
        inspect_from=4,
    )
    assert calls == 4
    assert "findings" in json.loads(result.read_text())
    markers = _markers(log)
    assert [m["reason"] for m in markers] == [UNINSPECTED, UNINSPECTED, COMBINED]
    assert markers[2]["attempt"] == 1
    assert "$.findings" in markers[2]["paths"]
    prompt = (tmp_path / "state" / "prompt-4").read_text()
    assert prompt.startswith("Review the change")
    assert "not complete" in prompt
    assert "has not been inspected" in prompt
    assert "git diff " in prompt
    assert "- $.findings: 'findings' is a required property" in prompt
    assert json.dumps(PROGRESS_NOTE) in prompt


def test_combined_continuation_that_does_not_inspect_is_not_repaired(
    tmp_path: Path,
) -> None:
    calls, result, log = _run(
        tmp_path,
        _blocks(_context()),
        [review(), review(), PROGRESS_NOTE],
        inspect_from=99,
    )
    # One combined continuation, then nothing: a repair cannot stand in for
    # reading the change, and the controller fails the run as not inspected.
    assert calls == 4
    assert _reasons(log) == [UNINSPECTED, UNINSPECTED, COMBINED]
    assert json.loads(result.read_text()) == PROGRESS_NOTE


def test_combined_continuation_is_followed_by_the_bounded_repairs(
    tmp_path: Path,
) -> None:
    calls, result, log = _run(
        tmp_path, _blocks(_context()), [PROGRESS_NOTE], inspect_from=4
    )
    # 1 first session + 2 inspection + 1 combined + 2 repairs, never more.
    assert calls == 1 + MAX_EXTRA_SESSIONS == 6
    assert _reasons(log) == [
        UNINSPECTED,
        UNINSPECTED,
        COMBINED,
        "result_schema_invalid",
        "result_schema_invalid",
    ]
    assert [m["attempt"] for m in _markers(log)] == [1, 2, 1, 1, 2]
    # Nothing is synthesised: the controller sees the model's last answer.
    assert json.loads(result.read_text()) == PROGRESS_NOTE


def test_uninspected_valid_answer_gets_no_combined_continuation(
    tmp_path: Path,
) -> None:
    calls, _, log = _run(tmp_path, _blocks(_context()), [review()], inspect_from=99)
    assert calls == 3
    assert _reasons(log) == [UNINSPECTED, UNINSPECTED]


def test_no_combined_continuation_without_room_in_the_run_budget(
    tmp_path: Path,
) -> None:
    context = _context(**{RUN_DEADLINE_EPOCH_CONTEXT_KEY: int(time.time()) + 25})
    calls, _, log = _run(tmp_path, _blocks(context), [PROGRESS_NOTE], inspect_from=99)
    assert calls == 3
    assert _reasons(log) == [UNINSPECTED, UNINSPECTED]


def test_combined_continuation_is_cut_at_the_run_budget() -> None:
    context = _context(**{RUN_DEADLINE_EPOCH_CONTEXT_KEY: 2_000_000_000})
    block = CodexAgent({})._native_result_repair_block(
        context, event_filter="cat", model="m", sandbox_flags="--yolo"
    )
    combined = block[block.index(COMBINED) : block.index("_pl_result_repair=0")]
    assert "timeout -k 5 $(_pl_repair_left)" in combined
    # One `if`, not a loop: at most one combined continuation per run.
    assert block.count(COMBINED) == 1
    assert "while" not in combined


def test_no_combined_continuation_without_the_inspection_floor() -> None:
    context = _context(
        git_clone_config={
            "publication_mode": "read_only",
            "review_integrity": {"require_inspection": False},
        }
    )
    assert COMBINED not in _blocks(context)
    assert "_pl_result_repair=0" in _blocks(context)


def test_combined_prompt_names_the_inspection_and_the_errors() -> None:
    answer = json.dumps(PROGRESS_NOTE)
    prompt = repair_prompt(
        answer, SCHEMA, "Review the change", inspection="Run `git diff B...H`."
    )
    assert prompt.startswith("Review the change")
    assert "not complete" in prompt
    assert "has not been inspected" in prompt
    assert "Run `git diff B...H`." in prompt
    assert "- $.verdict: 'verdict' is a required property" in prompt
    assert answer in prompt
    assert "progress note" in prompt
    # The plain repair prompt is unchanged and never claims an inspection gap.
    assert "has not been inspected" not in repair_prompt(answer, SCHEMA, "Review")
    assert repair_prompt(json.dumps(review()), SCHEMA, "R", inspection="x") == ""
    missing = repair_prompt(None, SCHEMA, "R", inspection="x")
    assert "- $: No final answer was written" in missing


def test_repair_follows_an_inspection_continuation(tmp_path: Path) -> None:
    calls, result, log = _run(
        tmp_path,
        _blocks(_context()),
        [review(), review(findings=...), review()],
        inspect_from=2,
    )
    assert calls == 3
    assert [m["reason"] for m in _markers(log)] == [
        "no_repository_inspection",
        "result_schema_invalid",
    ]
    assert "findings" in json.loads(result.read_text())


def test_checker_never_follows_an_agent_planted_link(tmp_path: Path) -> None:
    secret = tmp_path / "root-only-secret"
    secret.write_text("CREDENTIAL-VALUE")
    # After the first session the agent replaces its answer with a link to a
    # file only root can read; the root-run checker must not quote it.
    answer = "/workspace/.preloop-native-result.json"
    planted = f"rm -f {answer}; ln -s {secret} {answer}\n"
    calls, _, log = _run(tmp_path, planted + _blocks(_context()), [review()])
    assert calls == 1
    assert _markers(log) == []
    assert not list((tmp_path / "state").glob("prompt-2"))


def test_continuation_prompts_live_in_a_private_directory() -> None:
    block = _blocks(_context())
    assert "mktemp -d /tmp/preloop-continuation." in block
    assert "/tmp/preloop-result-repair.txt" not in block
    assert "/tmp/preloop-review-continuation.txt" not in block
    assert '"$_pl_cont_dir/result-repair.txt"' in block
    assert "O_NOFOLLOW" in block


def test_valid_answer_runs_exactly_one_session(tmp_path: Path) -> None:
    calls, _, log = _run(tmp_path, _blocks(_context()), [review()])
    assert calls == 1
    assert _markers(log) == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"agent_config": {}, "result_schema": None},
        {"agent_config": {"native_result": False}},
    ],
)
def test_flows_without_native_results_get_no_repair(overrides: dict) -> None:
    block = CodexAgent({})._native_result_repair_block(
        _context(**overrides), event_filter="cat", model="m", sandbox_flags=""
    )
    assert block == ""


def test_default_pull_request_reviewer_script_is_unchanged() -> None:
    preset = yaml.safe_load(
        (
            Path(__file__).parents[2] / "presets" / "002-pull-request-reviewer.yaml"
        ).read_text()
    )
    context = {
        "prompt": "Review the change",
        "execution_id": "fixture",
        "flow_name": "fixture",
        "agent_config": preset["agent_config"],
        "git_clone_config": preset["git_clone_config"],
        "result_schema": preset.get("result_schema"),
    }
    agent = CodexAgent({})
    with patch.object(agent, "_prepare_init_commands", return_value=""):
        script = agent._build_codex_script(context)
    assert "result_schema_invalid" not in script
    assert "_pl_result_repair" not in script
    assert COMBINED not in script


def test_review_script_contains_repair_after_inspection() -> None:
    agent = CodexAgent({})
    with patch.object(agent, "_prepare_init_commands", return_value=""):
        script = agent._build_codex_script(_context())
    inspection = script.index("_pl_review_continuation=0")
    repair = script.index("_pl_result_repair=0")
    capture = script.index("_preloop_native_result_candidates_v1")
    assert inspection < repair < capture
