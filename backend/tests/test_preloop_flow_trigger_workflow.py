"""Drift guards for .github/workflows/preloop-flow-trigger.yml.

The workflow triggers the Pull Request Reviewer through the manual trigger
endpoint, which stores the request body as the trigger event verbatim. The
002 preset reads ``trigger_event.payload.object_attributes.*``, so the body
the workflow builds has to normalize into that shape. These tests run the
workflow's own jq program and render every field the preset reads through
the real resolver, so a change on either side fails here instead of on the
first prod review.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml  # type: ignore[import-untyped]

from preloop.api.endpoints.flows import RESERVED_TRIGGER_KEYS
from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.trigger_event import TriggerEventResolver

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "preloop-flow-trigger.yml"
PRESET = REPO_ROOT / "backend" / "presets" / "002-pull-request-reviewer.yaml"
CLI_FLOW = REPO_ROOT / "cli" / "internal" / "cmd" / "flow.go"

HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"

# Subset of the GitHub REST "get a pull request" response, which is what
# the workflow feeds jq (`gh api repos/{repo}/pulls/{n}`).
GITHUB_PR = {
    "number": 992,
    "title": "Review every non-draft PR on prod from GitHub Actions",
    "body": "Closes #948.\n\nDetails.",
    "html_url": "https://github.com/preloop/preloop/pull/992",
    "state": "open",
    "draft": False,
    "user": {"login": "octocat"},
    "author_association": "MEMBER",
    "head": {
        "ref": "ci/prod-pr-reviewer-actions",
        "sha": "f" * 40,
        "repo": {"full_name": "preloop/preloop"},
    },
    "base": {
        "ref": "main",
        "sha": "e" * 40,
        "repo": {
            "id": 1,
            "full_name": "preloop/preloop",
            "html_url": "https://github.com/preloop/preloop",
            "clone_url": "https://github.com/preloop/preloop.git",
            "default_branch": "main",
        },
    },
}


def _steps() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["trigger"]["steps"]
    return {step["id"]: step for step in steps if "id" in step} | {
        step["name"]: step for step in steps if "name" in step
    }


def _jq_program() -> str:
    script = _steps()["trigger"]["run"]
    match = re.search(
        r"--arg run_url \"\$RUN_URL\" '\n(.*?)' \"\$pr_json\"", script, re.S
    )
    assert match, "jq payload program not found in the trigger step"
    return match.group(1)


def _build_body(tmp_path: Path, *, event_head_sha: str = "") -> dict[str, Any]:
    if shutil.which("jq") is None:
        pytest.skip("jq is not installed")
    pr_json = tmp_path / "pr.json"
    pr_json.write_text(json.dumps(GITHUB_PR), encoding="utf-8")
    out = subprocess.run(
        [
            "jq",
            "-c",
            "--arg",
            "type",
            "pull_request_updated",
            "--arg",
            "action",
            "synchronize",
            "--arg",
            "sha",
            event_head_sha,
            "--arg",
            "run_url",
            "https://github.com/preloop/preloop/actions/runs/1",
            _jq_program(),
            str(pr_json),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(out.stdout)


def _preset_object_attribute_fields() -> set[str]:
    template = yaml.safe_load(PRESET.read_text(encoding="utf-8"))["prompt_template"]
    fields = set(
        re.findall(r"trigger_event\.payload\.object_attributes\.(\w+)", template)
    )
    assert fields, "preset no longer reads object_attributes; update this guard"
    return fields


def _resolve(body: dict[str, Any], path: str) -> str | None:
    # What the manual trigger endpoint stores: the body plus these two keys.
    data = dict(body, test_mode=True, triggered_by="ci")
    context = ResolverContext(
        db=MagicMock(), trigger_event_data=data, flow_id="f", execution_id="e"
    )
    return asyncio.run(TriggerEventResolver().resolve(path, context))


def test_body_renders_every_field_the_reviewer_prompt_reads(tmp_path: Path) -> None:
    body = _build_body(tmp_path)
    expected = {
        "title": GITHUB_PR["title"],
        "description": GITHUB_PR["body"],
        "author": "octocat",
        "url": GITHUB_PR["html_url"],
        "source_branch": "ci/prod-pr-reviewer-actions",
        "target_branch": "main",
    }
    for field in _preset_object_attribute_fields():
        value = _resolve(body, f"payload.object_attributes.{field}")
        assert value, f"object_attributes.{field} renders empty"
        if field in expected:
            assert value == expected[field]
    assert "#948" in (
        _resolve(body, "payload.object_attributes.referenced_issues") or ""
    )
    assert _resolve(body, "type") == "pull_request_updated"


def test_body_carries_what_the_orchestrator_clones_from(tmp_path: Path) -> None:
    body = _build_body(tmp_path)
    pr = body["payload"]["pull_request"]
    assert body["source"] == "github"
    assert pr["number"] == 992
    assert pr["head"]["ref"] == "ci/prod-pr-reviewer-actions"
    assert pr["base"]["ref"] == "main"
    # No event SHA (workflow_dispatch): the live head from the API.
    assert pr["head"]["sha"] == "f" * 40
    assert body["payload"]["repository"]["full_name"] == "preloop/preloop"


def test_event_head_sha_wins_over_the_live_head(tmp_path: Path) -> None:
    body = _build_body(tmp_path, event_head_sha=HEAD_SHA)
    assert body["payload"]["pull_request"]["head"]["sha"] == HEAD_SHA


def test_body_sets_no_reserved_trigger_key(tmp_path: Path) -> None:
    body = _build_body(tmp_path)
    assert not RESERVED_TRIGGER_KEYS.intersection(body)
    assert "matrix" not in body


def test_gate_runs_gh_without_a_checkout() -> None:
    # The gate calls `gh pr view` before any checkout; without GH_REPO gh
    # fails with "not a git repository" (run 36290238949).
    gate = _steps()["gate"]
    assert gate["env"]["GH_REPO"] == "${{ github.repository }}"
    assert "state" in gate["run"] and "OPEN" in gate["run"]
    assert "PRELOOP_PROD_REVIEW" in gate["env"]["PROD_REVIEW"]


def test_cleanup_parses_the_execution_id_the_cli_prints() -> None:
    cli_format = re.search(
        r'"(Triggered flow %s \(execution %s, status %s\)\\n)"', CLI_FLOW.read_text()
    )
    assert cli_format, "CLI trigger line changed; update the cleanup step's sed"
    exec_id = "11111111-2222-4333-8444-555555555555"
    line = f"Triggered flow abc (execution {exec_id}, status PENDING)\n"
    script = _steps()["Stop the execution if this job did not finish it"]["run"]
    sed_expr = re.search(r"sed -n '([^']+)'", script)
    assert sed_expr
    out = subprocess.run(
        ["sed", "-n", sed_expr.group(1)],
        input="noise\n" + line,
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.stdout.strip() == exec_id


def test_cleanup_never_stops_a_terminal_execution() -> None:
    script = _steps()["Stop the execution if this job did not finish it"]["run"]
    stop_branch = re.search(r"\n\s*([A-Z|]+)\)\n\s*echo \"Stopping", script)
    assert stop_branch
    assert set(stop_branch.group(1).split("|")) == {
        "RUNNING",
        "STARTING",
        "INITIALIZING",
        "PENDING",
    }


def _run_gate(
    tmp_path: Path,
    *,
    author_association: str,
    labels: list[str],
    event_name: str = "pull_request_target",
    is_fork: bool = True,
) -> tuple[str, str]:
    """Run the gate step's shell with a stubbed ``gh`` and return (notice, skip)."""
    if shutil.which("bash") is None or shutil.which("jq") is None:
        pytest.skip("bash and jq are required")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    meta = json.dumps(
        {
            "state": "OPEN",
            "isDraft": False,
            "labels": [{"name": name} for name in labels],
        }
    )
    gh = bin_dir / "gh"
    gh.write_text(f"#!/bin/sh\nprintf '%s' '{meta}'\n", encoding="utf-8")
    gh.chmod(0o755)
    output = tmp_path / "github_output"
    output.touch()
    env = {
        "PATH": f"{bin_dir}:{shutil.os.environ['PATH']}",
        "GITHUB_OUTPUT": str(output),
        "GH_TOKEN": "x",
        "GH_REPO": "preloop/preloop",
        "PR_NUMBER": "1116",
        "EVENT_NAME": event_name,
        "PROD_REVIEW": "enabled",
        "IS_FORK": "true" if is_fork else "false",
        "AUTHOR_ASSOCIATION": author_association,
    }
    out = subprocess.run(
        ["bash", "-c", _steps()["gate"]["run"]],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    notice = re.search(r"::notice title=Preloop review skipped::(.*)", out.stdout)
    skip = output.read_text(encoding="utf-8").strip()
    return (notice.group(1) if notice else "", skip)


def test_gate_skips_a_fork_pr_from_a_contributor_without_the_label(
    tmp_path: Path,
) -> None:
    # PR #1116 (2026-10-01): a CONTRIBUTOR fork PR never reached the reviewer.
    notice, skip = _run_gate(tmp_path, author_association="CONTRIBUTOR", labels=[])
    assert skip == "skip=true"
    assert "Fork PR #1116 from a CONTRIBUTOR author" in notice
    assert "preloop-review" in notice


def test_gate_reviews_a_fork_pr_a_maintainer_labelled(tmp_path: Path) -> None:
    notice, skip = _run_gate(
        tmp_path, author_association="CONTRIBUTOR", labels=["preloop-review"]
    )
    assert skip == "skip=false"
    assert notice == ""


def test_gate_skip_label_beats_the_opt_in_label(tmp_path: Path) -> None:
    notice, skip = _run_gate(
        tmp_path,
        author_association="MEMBER",
        labels=["preloop-review", "preloop-skip-review"],
    )
    assert skip == "skip=true"
    assert "preloop-skip-review" in notice


def test_gate_reviews_a_fork_pr_from_a_member_without_a_label(tmp_path: Path) -> None:
    for association in ("OWNER", "MEMBER", "COLLABORATOR"):
        _, skip = _run_gate(
            tmp_path / association, author_association=association, labels=[]
        )
        assert skip == "skip=false", association


def test_label_events_only_retrigger_for_the_two_owned_labels() -> None:
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    on = doc.get("on") or doc[True]
    for event in ("pull_request", "pull_request_target"):
        assert {"labeled", "unlabeled"} <= set(on[event]["types"]), event
    condition = doc["jobs"]["trigger"]["if"]
    assert (
        "github.event.action != 'labeled' || github.event.label.name == 'preloop-review'"
        in condition
    )
    assert (
        "github.event.action != 'unlabeled' || github.event.label.name == 'preloop-skip-review'"
        in condition
    )
