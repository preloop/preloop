"""Test trusted control decisions with synthetic forge responses only."""

import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from ci_control import approve_full, approved_head, failing_tests, nightly_issue  # noqa: E402

HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
PR = {
    "number": 1,
    "state": "open",
    "user": {"login": "author"},
    "head": {"sha": HEAD},
    "base": {"sha": BASE},
    "merge_commit_sha": MERGE,
}
REVIEW = {
    "id": 1,
    "user": {"login": "reviewer"},
    "state": "APPROVED",
    "commit_id": HEAD,
}


@pytest.fixture(autouse=True)
def selection_receipt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Use synthetic metadata while testing approval state transitions."""
    monkeypatch.setattr(
        "ci_control.read_selection",
        lambda api, run_id: {
            "head_sha": HEAD,
            "base_sha": BASE,
            "tested_sha": MERGE,
            "full": False,
        },
    )


def test_approval_is_tied_to_current_head_and_latest_review() -> None:
    """Old approvals and later change requests cannot admit a full run."""
    assert approved_head(PR, [REVIEW])
    assert not approved_head(PR, [dict(REVIEW, commit_id="d" * 40)])
    assert not approved_head(
        PR, [REVIEW, dict(REVIEW, id=2, state="CHANGES_REQUESTED")]
    )
    assert not approved_head(PR, [REVIEW, dict(REVIEW, id=2, state="DISMISSED")])
    assert not approved_head(PR, [dict(REVIEW, user={"login": "author"})])


class Forge:
    """Small deterministic forge with mutation capture and race injection."""

    repository = "example/project"

    def __init__(self) -> None:
        self.pr = copy.deepcopy(PR)
        self.fast = "success"
        self.dispatches: list[dict] = []
        self.pr_reads = 0
        self.race = False
        self.existing = False
        self.parents = [BASE, HEAD]

    def request(
        self, path: str, data: dict | None = None, *, method: str = "GET"
    ) -> dict | None:
        """Return fixtures or capture a structured dispatch."""
        if method == "POST":
            self.dispatches.append({"path": path, "data": data})
            return None
        if path == "actions/runs/2":
            return {"run_attempt": 2 if self.existing else 1, "status": "completed"}
        if path == "pulls/1":
            self.pr_reads += 1
            pr = copy.deepcopy(self.pr)
            if self.race and self.pr_reads > 1:
                pr["head"]["sha"] = "d" * 40
            return pr
        if path == f"git/commits/{MERGE}":
            return {"parents": [{"sha": sha} for sha in self.parents]}
        raise AssertionError(path)

    def pages(self, path: str, key: str = "") -> list[dict]:
        """Supply reviews, current run and idempotency evidence."""
        if path.endswith("/reviews"):
            return [REVIEW]
        if "event=pull_request" in path:
            return [
                {
                    "id": 2,
                    "run_attempt": 2 if self.existing else 1,
                    "status": "completed",
                    "head_sha": HEAD,
                    "pull_requests": [{"number": 1, "base": {"sha": BASE}}],
                }
            ]
        if path.endswith("/jobs"):
            return [{"name": "CI Fast", "conclusion": self.fast}]
        if "event=workflow_dispatch" in path:
            return (
                [{"display_title": f"Full PR #1 {HEAD} {BASE}"}]
                if self.existing
                else []
            )
        raise AssertionError(path)


def test_approved_green_reruns_actual_pr_candidate_once() -> None:
    """The rerun preserves the PR context and requires its exact head/base parents."""
    forge = Forge()
    approve_full(forge, 1)
    assert forge.dispatches == [{"path": "actions/runs/2/rerun", "data": {}}]
    forge.existing = True
    approve_full(forge, 1)
    assert len(forge.dispatches) == 1


@pytest.mark.parametrize("fast", ["failure", "skipped", "cancelled", None])
def test_non_green_fast_never_dispatches(fast: str | None) -> None:
    """Missing, skipped, cancelled and failed gates remain closed."""
    forge = Forge()
    forge.fast = fast
    approve_full(forge, 1)
    assert not forge.dispatches


def test_new_push_racing_dispatch_is_rejected() -> None:
    """Approval and fast evidence cannot carry over to another head."""
    forge = Forge()
    forge.race = True
    approve_full(forge, 1)
    assert not forge.dispatches


@pytest.mark.parametrize("field", ["head_sha", "base_sha"])
def test_stale_selection_receipt_never_dispatches(
    field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An otherwise green run cannot reuse evidence from another head or base."""
    forge = Forge()
    receipt = {"head_sha": HEAD, "base_sha": BASE, "tested_sha": MERGE, field: "d" * 40}
    monkeypatch.setattr("ci_control.read_selection", lambda api, run_id: receipt)

    assert "no receipt for the current head/base" in approve_full(forge, 1)
    assert not forge.dispatches


@pytest.mark.parametrize("parents", [[HEAD], [HEAD, BASE], [BASE, "d" * 40]])
def test_wrong_merge_parents_never_dispatch(parents: list[str]) -> None:
    """Even a current receipt must identify the exact base-then-head merge."""
    forge = Forge()
    forge.parents = parents

    assert "tested candidate does not contain" in approve_full(forge, 1)
    assert not forge.dispatches


def test_nightly_issue_preserves_first_failure_range() -> None:
    """Later failures update the same job without moving its first range."""
    job = {"name": "Backend Tests (1/18)", "html_url": "https://example.com/job"}
    run = {"head_sha": HEAD, "html_url": "https://example.com/run"}
    first = nightly_issue(job, run, BASE, ["tests/test_example.py::test_example"], None)
    later = nightly_issue(job, dict(run, head_sha=MERGE), HEAD, ["TestExample"], first)
    assert later["title"] == first["title"]
    assert f"{BASE}...{HEAD}" in later["body"]
    assert "TestExample" in later["body"]
    assert f"{HEAD}...{MERGE}" not in later["body"]


def test_only_test_identifiers_are_copied_from_logs() -> None:
    """Do not paste arbitrary captured output into public tracking issues."""
    assert failing_tests(
        "secret=value\nFAILED tests/test_a.py::test_a - detail\n--- FAIL: TestExample (1s)"
    ) == ["TestExample", "tests/test_a.py::test_a"]


def test_nightly_upserts_one_issue_per_job_without_default_flow_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated failures reuse the issue and its original regression boundary."""
    from ci_control import follow_up_nightly
    from types import SimpleNamespace

    class NightlyForge:
        repository = "example/project"

        def __init__(self) -> None:
            self.issues: list[dict] = []
            self.labels: list[dict] = []

        def request(
            self, path: str, data: dict | None = None, *, method: str = "GET"
        ) -> dict:
            if path == "actions/runs/2":
                return {
                    "id": 2,
                    "path": ".github/workflows/ci.yml",
                    "event": "schedule",
                    "head_branch": "main",
                    "status": "completed",
                    "head_sha": HEAD,
                    "head_repository": {"full_name": self.repository},
                    "created_at": "2026-01-02T00:00:00Z",
                    "html_url": "https://example.com/run",
                }
            if method == "POST" and path == "issues":
                issue = dict(data, number=len(self.issues) + 1)
                self.issues.append(issue)
                return issue
            if path.endswith("/labels"):
                self.labels.append(data)
                return {}
            if method == "PATCH":
                self.issues[int(path.split("/")[-1]) - 1].update(data)
                return {}
            raise AssertionError(path)

        def pages(self, path: str, key: str = "") -> list[dict]:
            if path == "issues?state=all":
                return self.issues.copy()
            if path == "actions/runs/2/jobs":
                return [
                    {
                        "id": 3,
                        "name": "Backend Tests (1/18)",
                        "conclusion": "failure",
                        "html_url": "https://example.com/job",
                    }
                ]
            if path == "actions/runs/1/jobs":
                return [{"name": "Backend Tests (1/18)", "conclusion": "success"}]
            if "status=success" in path:
                return [
                    {
                        "id": 1,
                        "created_at": "2026-01-01T00:00:00Z",
                        "event": "schedule",
                        "head_sha": BASE,
                    }
                ]
            raise AssertionError(path)

    monkeypatch.delenv("CI_NIGHTLY_ISSUE_LABEL", raising=False)
    monkeypatch.setattr(
        "ci_control.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stdout="FAILED tests/test_example.py::test_example"
        ),
    )
    forge = NightlyForge()
    follow_up_nightly(forge, 2)
    follow_up_nightly(forge, 2)
    assert len(forge.issues) == 1
    assert f"{BASE}...{HEAD}" in forge.issues[0]["body"]
    assert not forge.labels
    monkeypatch.setenv("CI_NIGHTLY_ISSUE_LABEL", "synthetic-flow-entry")
    follow_up_nightly(forge, 2)
    assert forge.labels == [{"labels": ["synthetic-flow-entry"]}]
