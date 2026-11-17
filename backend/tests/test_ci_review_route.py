"""Synthetic checks for the trusted CI route; no provider or production calls."""

import copy
import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "ci_review_route", ROOT / "scripts/ci_review_route.py"
)
assert _spec and _spec.loader
route = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(route)
HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
EXECUTION = "00000000-0000-4000-8000-000000000003"
FLOW = "00000000-0000-4000-8000-000000000001"
PROJECT = "00000000-0000-4000-8000-000000000002"


@pytest.mark.parametrize(
    ("files", "patterns", "accepted"),
    [
        ([{"filename": "docs/index.md"}, {"filename": "cli/main.go"}], "", True),
        (
            [
                {"filename": "runtime-plugins/a/src/x.py"},
                {"filename": "helm/chart.yaml"},
            ],
            "",
            True,
        ),
        ([{"filename": "docs/index.md"}, {"filename": "backend/main.py"}], "", False),
        ([], "", False),
        ([{"filename": "docs/x", "previous_filename": "backend/x"}], "", False),
        ([{"filename": "docs/new", "previous_filename": "docs/old"}], "", True),
        ([{"filename": "docs-old/x"}], "", False),
        ([{"filename": "src/x.py"}], '["src/**"]', True),
        ([{"filename": "docs/x"}], '["src/**"]', False),
        ([{"filename": "docs/X.md"}], '["docs/*.MD"]', False),
    ],
)
def test_path_subset(
    files: list[dict[str, str]], patterns: str, accepted: bool
) -> None:
    assert route.path_subset(files, patterns) is accepted


@pytest.mark.parametrize(
    "patterns", ["[]", "{}", '["!docs/**"]', '["/docs/**"]', "[null]", "docs/**"]
)
def test_invalid_path_configuration_fails_closed(patterns: str) -> None:
    with pytest.raises((route.VerificationError, ValueError)):
        route.path_subset([{"filename": "docs/x"}], patterns)


def pr() -> dict[str, Any]:
    repository = {"id": 17, "full_name": "example/repository"}
    return dict(
        id=23,
        state="open",
        draft=False,
        changed_files=1,
        head={"sha": HEAD, "repo": repository},
        base={"sha": BASE, "repo": repository},
    )


def result() -> dict[str, Any]:
    return dict(
        id=EXECUTION,
        execution_id=EXECUTION,
        flow_id=FLOW,
        project_id=PROJECT,
        repository_identifier="17",
        provider_pr_id="23",
        pr_number=7,
        head_sha=HEAD,
        base_sha=BASE,
        status="SUCCEEDED",
        startup_receipt={
            "commit": HEAD,
            "executor": "hosted",
            "checkout_verified": True,
            "setup_completed": True,
            "diff": {
                "head_sha": HEAD,
                "base_sha": BASE,
                "merge_base_sha": MERGE,
                "sha256": "d" * 64,
            },
        },
        result={
            "head_sha": HEAD,
            "base_sha": BASE,
            "verdict": "approve",
            "risk_level": "low",
            "context_complete": True,
            "findings": [],
            "review": f"<!-- preloop-review:reviewed-sha:{HEAD} -->\n## 🔍 Preloop Code Review\n### Checks\nThe changed documentation consistently describes the new configuration and its defaults. No actionable correctness defects were found in the inspected diff.",
        },
        result_withheld_paths=[],
        schema_validation={"valid": True},
        completion={
            "result_valid": True,
            "review_integrity": {
                "status": "verified",
                "inspection": {"inspected": True},
            },
        },
    )


def command() -> str:
    return f"git --no-pager diff --no-ext-diff --no-textconv {MERGE} {HEAD} --"


def evidence(document: dict[str, Any]) -> dict[str, Any]:
    content = {
        "execution_id": EXECUTION,
        "capture_complete": True,
        "storage_redacted": False,
        "observations": [
            {
                "source": "native",
                "kind": "command",
                "event": "item.completed",
                "exit_code": 0,
                "redacted": False,
                "truncated": False,
                "value": command(),
            }
        ],
    }
    records = [
        {
            "id": "record-1",
            "integrity_valid": True,
            "sha256": route.json_digest(content),
            "document": content,
        }
    ]
    return dict(
        execution_id=EXECUTION,
        status="SUCCEEDED",
        binding=document,
        records=records,
        page_sha256=route.json_digest(records),
        skip=0,
        total=1,
        next_skip=1,
        has_more=False,
        captured_result_sha256=route.json_digest(document["result"]),
    )


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in {
        "CI_HEAD": HEAD,
        "CI_BASE": BASE,
        "CI_FLOW": FLOW,
        "CI_PROJECT": PROJECT,
        "CI_PR": "7",
        "CI_EXECUTION": EXECUTION,
        "CI_URL": "https://example.com",
        "GITHUB_REPOSITORY": "example/repository",
        "CI_POST_REVIEW": "",
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
    }.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("field", ["head_sha", "base_sha"])
@pytest.mark.parametrize("where", ["result", "startup_receipt.diff", "envelope"])
def test_rejects_changed_commit(field: str, where: str) -> None:
    doc = result()
    target = doc
    if where != "envelope":
        for key in where.split("."):
            target = target[key]
    target[field] = "e" * 40
    with pytest.raises(route.VerificationError):
        route.validate_result(doc, HEAD, BASE)


@pytest.mark.parametrize(
    "change",
    [
        "failed",
        "empty",
        "short",
        "repeated",
        "schema",
        "withheld",
        "inspection",
        "verdict",
        "contradiction",
    ],
)
def test_rejects_invalid_completion(change: str) -> None:
    doc = result()
    if change == "failed":
        doc["status"] = "FAILED"
    if change == "empty":
        doc["result"]["review"] = ""
    if change == "short":
        doc["result"]["review"] = "Looks good"
    if change == "repeated":
        doc["result"]["review"] = "x" * 100
    if change == "schema":
        doc["schema_validation"]["valid"] = False
    if change == "withheld":
        doc["result_withheld_paths"] = ["/review"]
    if change == "inspection":
        doc["completion"]["review_integrity"]["inspection"]["inspected"] = False
    if change == "verdict":
        doc["result"]["verdict"] = "unknown"
    if change == "contradiction":
        doc["result"]["findings"] = [{"severity": "high"}]
    with pytest.raises(route.VerificationError):
        route.validate_result(doc, HEAD, BASE)


@pytest.mark.parametrize("bad", ["echo ", "printf ", "false && "])
def test_command_text_is_not_execution(bad: str) -> None:
    assert not route.read_diff_command(bad + command(), MERGE, HEAD)


def test_diff_reader_accepts_shell_wrapper_but_rejects_filters() -> None:
    assert route.read_diff_command(f"/bin/bash -lc '{command()}'", MERGE, HEAD)
    for candidate in [
        command() + " | head",
        command() + " docs/",
        command().replace("diff ", "diff --stat "),
        command().replace(MERGE, BASE),
        command() + " > /dev/null",
    ]:
        assert not route.read_diff_command(candidate, MERGE, HEAD)


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize(
    "change",
    [
        None,
        "exit",
        "started",
        "redacted",
        "truncated",
        "incomplete",
        "hash",
        "echo",
        "binding",
        "result",
        "stalled",
        "duplicate",
    ],
)
def test_evidence_validation(
    monkeypatch: pytest.MonkeyPatch, change: str | None
) -> None:
    doc = result()
    page = evidence(doc)
    content = page["records"][0]["document"]
    observation = content["observations"][0]
    if change == "exit":
        observation["exit_code"] = 1
    if change == "started":
        observation["event"] = "item.started"
    if change in {"redacted", "truncated"}:
        observation[change] = True
    if change == "incomplete":
        content["capture_complete"] = False
    if change == "echo":
        observation["value"] = "echo '" + command() + "'"
    if change == "binding":
        page["binding"] = {**doc, "head_sha": "e" * 40}
    if change == "result":
        page["captured_result_sha256"] = "e" * 64
    if change == "stalled":
        page.update(records=[], has_more=True, next_skip=0)
    if change == "duplicate":
        page.update(records=page["records"] * 2, total=2, next_skip=2)
    if page["records"]:
        page["records"][0]["sha256"] = route.json_digest(content)
    page["page_sha256"] = route.json_digest(page["records"])
    if change == "hash":
        page["page_sha256"] = "e" * 64
    monkeypatch.setattr(route, "remote", lambda *a: page)
    if change is None:
        route.validate_evidence(EXECUTION, doc, route.expected(pr()))
    else:
        with pytest.raises(route.VerificationError):
            route.validate_evidence(EXECUTION, doc, route.expected(pr()))


@pytest.mark.usefixtures("environment")
def test_evidence_follows_next_skip(monkeypatch: pytest.MonkeyPatch) -> None:
    doc = result()
    first = evidence(doc)
    first.update(total=2, has_more=True)
    second = copy.deepcopy(first)
    second.update(skip=1, next_skip=2, has_more=False)
    second["records"][0]["id"] = "record-2"
    second["page_sha256"] = route.json_digest(second["records"])
    paths: list[str] = []

    def remote(path: str) -> dict[str, Any]:
        paths.append(path)
        return first if len(paths) == 1 else second

    monkeypatch.setattr(route, "remote", remote)
    route.validate_evidence(EXECUTION, doc, route.expected(pr()))
    assert paths[-1].endswith("skip=1&limit=10")


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize(
    "change",
    [None, "head", "base", "empty", "evidence", "missing", "failed", "changes"],
)
def test_publisher_shadow_and_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str | None
) -> None:
    doc = result()
    current = pr()
    if change in {"head", "base"}:
        current[change]["sha"] = "e" * 40
    if change == "empty":
        doc["result"]["review"] = ""
    if change == "failed":
        doc["status"] = "FAILED"
    if change == "changes":
        doc["result"]["verdict"] = "request_changes"
        doc["result"]["findings"] = [{"severity": "medium"}]
    page = evidence(doc)
    if change == "evidence":
        page.update(records=[], total=0, next_skip=0, page_sha256=route.json_digest([]))
    if change == "missing":
        monkeypatch.setenv("CI_EXECUTION", "")
    writes: list[dict[str, Any]] = []

    def github(path: str, method: str = "GET", body: Any = None) -> Any:
        if method == "POST":
            assert "/statuses/" in path, "Shadow must not post reviews/comments/checks"
            writes.append(body)
            return {}
        assert path.endswith("/pulls/7")
        return current

    monkeypatch.setattr(route, "github", github)
    monkeypatch.setattr(
        route, "remote", lambda path: page if "tool-evidence" in path else doc
    )
    if change not in {None, "changes"}:
        with pytest.raises(route.VerificationError):
            route.publish()
        assert writes[0]["state"] == "error"
    else:
        route.publish()
        assert writes[0]["state"] == ("failure" if change else "success")
    assert writes[0]["context"] == "preloop-review/ci-route"
    summary = (tmp_path / "summary").read_text()
    assert "Verdict:" in summary and "Findings:" in summary and "Execution:" in summary


@pytest.mark.usefixtures("environment")
def test_gate_rejects_truncated_file_list(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    current = pr()
    current["changed_files"] = 3001
    monkeypatch.setattr(
        route,
        "github",
        lambda path: [{"filename": "docs/x"}] * 100 if "/files?" in path else current,
    )
    route.gate()
    assert "eligible=false" in (tmp_path / "output").read_text()


@pytest.mark.usefixtures("environment")
def test_gate_accepts_complete_subset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Selection accepts every page and rechecks the admitted revisions."""
    current = pr()
    current["changed_files"] = 101
    calls: list[str] = []

    def github(path: str) -> Any:
        calls.append(path)
        if "/files?" in path:
            count = 100 if path.endswith("page=1") else 1
            return [{"filename": f"docs/page-{i}.md"} for i in range(count)]
        return current

    monkeypatch.setattr(route, "github", github)
    route.gate()
    assert (tmp_path / "output").read_text() == "eligible=true\n"
    assert calls == [
        route.pr_path(),
        route.pr_path() + "/files?per_page=100&page=1",
        route.pr_path() + "/files?per_page=100&page=2",
        route.pr_path(),
    ]


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("change", ["head", "base", "deadline"])
def test_trigger_stops_owned_work(monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    """A revision change or polling deadline stops only the owned execution."""
    doc = result()
    doc["status"] = "RUNNING"
    current = pr()
    moved = copy.deepcopy(current)
    if change != "deadline":
        moved[change]["sha"] = "e" * 40
    reads = iter([current, moved])
    clock = iter([0, 601])
    calls: list[tuple[str, str, Any]] = []

    def remote(path: str, method: str = "GET", body: Any = None) -> Any:
        calls.append((path, method, body))
        return [doc] if "?flow_id=" in path else doc

    monkeypatch.setattr(route, "github", lambda path: next(reads))
    monkeypatch.setattr(route, "remote", remote)
    monkeypatch.setattr(route.time, "monotonic", lambda: next(clock))
    with pytest.raises(route.VerificationError):
        route.trigger()
    assert [call for call in calls if call[1] == "POST"] == [
        (f"/flows/executions/{EXECUTION}/command", "POST", {"command": "stop"})
    ]


@pytest.mark.parametrize("verdict", ["approve", "comment"])
@pytest.mark.parametrize("risk", ["high", "critical", "unknown", None])
def test_rejects_approval_with_blocking_or_invalid_risk(
    verdict: str, risk: str | None
) -> None:
    """A success verdict cannot contradict the declared overall risk."""
    doc = result()
    doc["result"].update(verdict=verdict, risk_level=risk)
    with pytest.raises(route.VerificationError):
        route.validate_result(doc, HEAD, BASE)


def test_workflow_trust_and_disabled_defaults() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/preloop-ci-route.yml").read_text()
    )
    events = workflow.get("on", workflow.get(True))
    assert events == {
        "pull_request_target": {
            "types": ["opened", "synchronize", "reopened", "ready_for_review"],
        }
    }
    jobs = workflow["jobs"]
    assert "vars.PRELOOP_CI_ROUTE_ENABLED == 'enabled'" in jobs["select"]["if"]
    assert "head.repo.full_name == github.repository" in jobs["select"]["if"]
    assert '-n "$CI_KEY"' in jobs["select"]["steps"][0]["run"]
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert "always()" in jobs["publish"]["if"]
    assert "statuses" in jobs["publish"]["permissions"]
    for job in jobs.values():
        for step in job["steps"]:
            if step.get("uses", "").startswith("actions/checkout@"):
                assert step["with"] == {
                    "ref": (
                        "${{ github.event.repository.default_branch }}"
                        if job is jobs["select"]
                        else "${{ needs.select.outputs.trusted_sha }}"
                    ),
                    "persist-credentials": False,
                }
    assert workflow["env"]["PRELOOP_DISABLE_TELEMETRY"] == "true"


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("change", ["fork", "draft", "closed", "head", "base"])
def test_gate_skips_ineligible_prs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: str
) -> None:
    current = pr()
    if change == "fork":
        current["head"]["repo"] = {"full_name": "example/fork"}
    elif change == "draft":
        current["draft"] = True
    elif change == "closed":
        current["state"] = "closed"
    else:
        current[change]["sha"] = "e" * 40

    def github(path: str) -> dict[str, Any]:
        assert "/files?" not in path
        return current

    monkeypatch.setattr(route, "github", github)
    route.gate()
    assert (tmp_path / "output").read_text() == "eligible=false\n"


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("reuse", [False, True])
def test_trigger_uses_documented_body_and_reuses_owned_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reuse: bool
) -> None:
    doc = result()
    calls: list[tuple[str, str, Any]] = []

    def remote(path: str, method: str = "GET", body: Any = None) -> Any:
        calls.append((path, method, body))
        if "?flow_id=" in path:
            return [doc] if reuse else []
        return doc

    monkeypatch.setattr(route, "github", lambda path: pr())
    monkeypatch.setattr(route, "remote", remote)
    route.trigger()
    posts = [c for c in calls if c[1] == "POST"]
    assert posts == (
        []
        if reuse
        else [(f"/flows/{FLOW}/trigger", "POST", {"pr_number": 7, "head_sha": HEAD})]
    )
    assert (tmp_path / "output").read_text() == f"execution={EXECUTION}\n"


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("status", [401, 403, 404, 409])
def test_result_pending_retry_is_bounded_to_409(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    attempts = []
    waits = []

    def remote(path: str) -> dict[str, Any]:
        attempts.append(path)
        if len(attempts) == 1:
            raise route.TransportError("Synthetic transport error", status=status)
        return result()

    monkeypatch.setattr(route, "remote", remote)
    monkeypatch.setattr(route.time, "sleep", waits.append)
    if status == 409:
        assert route.fetch_result(EXECUTION) == result()
        assert waits == [5]
    else:
        with pytest.raises(route.TransportError):
            route.fetch_result(EXECUTION)
        assert len(attempts) == 1
        assert waits == []


@pytest.mark.usefixtures("environment")
@pytest.mark.parametrize("existing", [False, True])
def test_review_opt_in_requires_exact_bot_receipt_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch, existing: bool
) -> None:
    monkeypatch.setenv("CI_POST_REVIEW", "enabled")
    doc = result()
    marker = f"<!-- preloop-ci-route:{EXECUTION}:{HEAD} -->"
    review = dict(
        id=1,
        body=doc["result"]["review"] + "\n\n" + marker,
        commit_id=HEAD,
        user={"id": 9},
        state="COMMENTED",
        submitted_at="2030-01-01T00:00:00Z",
    )
    posts: list[str] = []

    def github(path: str, method: str = "GET", body: Any = None) -> Any:
        if path.startswith("/users/"):
            return {"id": 9}
        if method == "POST":
            posts.append(path)
            if path.endswith("/reviews"):
                assert body == {
                    "commit_id": HEAD,
                    "event": "COMMENT",
                    "body": review["body"],
                }
            return review
        if "/reviews?" in path:
            # A matching marker from another user must not prevent publication.
            return [review] if existing else [{**review, "user": {"id": 123}}]
        if path.endswith("/reviews/1"):
            return review
        return pr()

    monkeypatch.setattr(route, "github", github)
    monkeypatch.setattr(
        route, "remote", lambda path: evidence(doc) if "tool-evidence" in path else doc
    )
    route.publish()
    assert sum(path.endswith("/reviews") for path in posts) == (0 if existing else 1)
    assert posts[-1].endswith("/statuses/" + HEAD)


def test_native_schema_and_adapted_prompt_preserve_review_conventions() -> None:
    import json

    import jsonschema

    from preloop.services.flow_result_schema import check_native_result_schema

    schema = json.loads(
        (ROOT / "docs/examples/ci-route-result.schema.json").read_text()
    )
    check_native_result_schema(schema)
    doc = result()["result"]
    doc.update(risk_level="low", issue_coverage=[])
    jsonschema.validate(doc, schema)
    prompt = (ROOT / "docs/examples/ci-route-prompt.md").read_text()
    preset = yaml.safe_load(
        (ROOT / "backend/presets/002-pull-request-reviewer.yaml").read_text()
    )["prompt_template"]
    # Preserve the preset's analysis, verification and severity calibration verbatim.
    start, end = "**Step 2.1:", "**Step 2.8:"
    assert preset[preset.index(start) : preset.index(end)].strip() in prompt
    assert "### 🎯 Issue Coverage" in prompt
    assert "### Checks" in prompt
    assert (
        "criteria_total" in schema["properties"]["issue_coverage"]["items"]["required"]
    )
    assert "{{trigger_event.payload.pull_request.head.sha}}" in prompt
    assert "get_pull_request" not in prompt and "get_issue" not in prompt


def test_accepts_withheld_controller_termination_but_not_review_fields() -> None:
    from preloop.services.ci_execution import ci_result_withheld_paths, public_ci_result

    doc = result()
    stored = {
        **doc["result"],
        "container_termination": {"runtime": "docker", "exit_code": 0},
    }
    doc["result"] = public_ci_result(stored)
    doc["result_withheld_paths"] = ci_result_withheld_paths(stored)
    assert doc["result_withheld_paths"] == ["/container_termination"]
    assert route.validate_result(doc, HEAD, BASE) == doc["result"]
