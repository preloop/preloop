"""Exercise watchdog decisions and mutations with synthetic GitHub responses."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
import yaml

from tests.ci_workflow import REPO_ROOT, load_ci_jobs

_SPEC = importlib.util.spec_from_file_location(
    "ci_hosted_recovery", REPO_ROOT / "scripts" / "ci_hosted_recovery.py"
)
assert _SPEC is not None and _SPEC.loader is not None
recovery = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(recovery)


def run(**extra: Any) -> dict:
    return {
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "run_attempt": 1,
        "head_repository": {"full_name": "example/repo"},
        "actor": {"login": "contributor"},
        "head_sha": "current-head",
        "event": "pull_request",
        "pull_requests": [{"number": 7}],
        "status": "in_progress",
        "conclusion": None,
        **extra,
    }


def job(**extra: Any) -> dict:
    return {
        "name": "Backend Tests (1/18)",
        "labels": sorted(recovery.GENERAL_LABELS),
        "status": "queued",
        "conclusion": None,
        "runner_id": 0,
        "created_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        **extra,
    }


class FakeGitHub:
    repository = "example/repo"

    def __init__(self, runs: list[dict] | None = None) -> None:
        self.runs = runs or [run()]
        self.jobs = [job()]
        self.runners: list[dict] | None = []
        self.mutations: list[str] = []
        self.failure: str | None = None
        self.current_sha = "current-head"

    def request(self, path: str, *, method: str = "GET", **kwargs: Any) -> Any:
        if method == "POST":
            self.mutations.append(path)
        if self.failure and self.failure in path:
            raise recovery.ApiError("synthetic API failure")
        if method == "POST":
            return None
        if path.startswith("pulls/"):
            return {
                "state": "open",
                "head": {
                    "sha": self.current_sha,
                    "repo": {"full_name": self.repository},
                },
            }
        if path == "git/ref/heads/main":
            return {"object": {"sha": self.current_sha}}
        if len(self.runs) > 1:
            return self.runs.pop(0)
        return self.runs[0]

    def pages(self, path: str, key: str, **kwargs: Any) -> list[dict]:
        if key == "jobs":
            return self.jobs
        if self.runners is None:
            raise recovery.ApiError("inventory unavailable")
        return self.runners


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    elapsed = [0]
    monkeypatch.setattr(recovery.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(
        recovery.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )


@pytest.mark.parametrize(
    ("changes", "runners", "expected"),
    [
        ({}, [], True),
        ({"created_at": datetime.now(timezone.utc).isoformat()}, [], False),
        ({"status": "in_progress", "runner_id": 10}, [], True),
        (
            {"status": "in_progress", "runner_id": 10},
            [{"id": 10, "status": "online", "busy": True}],
            False,
        ),
        (
            {"status": "completed", "conclusion": "failure", "runner_id": 10},
            [{"id": 10, "status": "offline"}],
            True,
        ),
        (
            {"status": "completed", "conclusion": "failure", "runner_id": 10},
            [{"id": 10, "status": "online"}],
            False,
        ),
        (
            {"status": "completed", "conclusion": "failure", "runner_id": 10},
            None,
            False,
        ),
        ({"status": "completed", "conclusion": "success", "runner_id": 10}, [], False),
        ({"name": "Lint & Format"}, [], False),
        ({"labels": ["ubuntu-latest"]}, [], False),
        ({"labels": sorted(recovery.GENERAL_LABELS | {"gce-tests-only"})}, [], False),
    ],
)
def test_recovery_requires_queue_pressure_or_lost_runner(
    changes: dict, runners: list[dict] | None, expected: bool
) -> None:
    assert (
        recovery.needs_recovery([job(**changes)], runners, datetime.now(timezone.utc))
        is expected
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"run_attempt": 2},
        {"actor": {"login": "dependabot[bot]"}},
        {"head_repository": {"full_name": "fork/repo"}},
        {"name": "Other workflow"},
        {"path": ".github/workflows/other.yml"},
        {"event": "workflow_dispatch"},
        {"event": "push", "head_branch": "v1.0.0"},
        {"head_sha": "stale-head"},
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "cancelled"},
    ],
)
def test_untrusted_stale_retried_and_finished_runs_never_mutate(changes: dict) -> None:
    api = FakeGitHub([run(**changes)])
    recovery.recover(api, 123)
    assert api.mutations == []


def test_queued_shard_cancels_then_retries_only_after_confirmation() -> None:
    canceled = run(status="completed", conclusion="cancelled")
    api = FakeGitHub([run(), run(), run(), canceled, canceled])
    assert recovery.recover(api, 123) == "requested one hosted retry of unfinished jobs"
    assert api.mutations == [
        "actions/runs/123/cancel",
        "actions/runs/123/rerun-failed-jobs",
    ]


def test_main_push_can_recover_without_a_pull_request() -> None:
    active = run(event="push", head_branch="main", pull_requests=[])
    canceled = {**active, "status": "completed", "conclusion": "cancelled"}
    api = FakeGitHub([active, active, canceled, canceled])
    recovery.recover(api, 123)
    assert api.mutations[-1].endswith("rerun-failed-jobs")


def test_failed_shard_with_offline_runner_retries_without_cancel() -> None:
    api = FakeGitHub([run(status="completed", conclusion="failure")])
    api.jobs = [job(status="completed", conclusion="failure", runner_id=10)]
    recovery.recover(api, 123)
    assert api.mutations == ["actions/runs/123/rerun-failed-jobs"]


def test_completed_test_failure_with_online_runner_is_not_retried() -> None:
    api = FakeGitHub([run(status="completed", conclusion="failure")])
    api.jobs = [job(status="completed", conclusion="failure", runner_id=10)]
    api.runners = [{"id": 10, "status": "online"}]
    recovery.recover(api, 123)
    assert api.mutations == []


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_superseded_run_stops_before_next_mutation(stage: int) -> None:
    canceled = run(status="completed", conclusion="cancelled")
    runs = [run(), run(), canceled, canceled]
    runs[stage] = run(head_sha="stale-head")
    api = FakeGitHub(runs)
    recovery.recover(api, 123)
    assert api.mutations == ([] if stage == 1 else ["actions/runs/123/cancel"])


def test_user_cancellation_before_watchdog_cancel_is_not_undone() -> None:
    api = FakeGitHub([run(), run(status="completed", conclusion="cancelled")])
    recovery.recover(api, 123)
    assert api.mutations == []


def test_run_finishing_successfully_during_cancel_is_not_retried() -> None:
    api = FakeGitHub([run(), run(), run(status="completed", conclusion="success")])
    recovery.recover(api, 123)
    assert api.mutations == ["actions/runs/123/cancel"]


@pytest.mark.parametrize("failure", ["/cancel", "/rerun-failed-jobs", "pulls/"])
def test_api_failure_never_continues_mutating(failure: str) -> None:
    canceled = run(status="completed", conclusion="cancelled")
    api = FakeGitHub([run(), run(), canceled, canceled])
    api.failure = failure
    with pytest.raises(recovery.ApiError):
        recovery.recover(api, 123)
    assert api.mutations.count("actions/runs/123/cancel") <= 1
    assert api.mutations.count("actions/runs/123/rerun-failed-jobs") <= 1
    if failure != "/rerun-failed-jobs":
        assert "actions/runs/123/rerun-failed-jobs" not in api.mutations


def test_cancel_deadline_prevents_unconfirmed_retry() -> None:
    api = FakeGitHub()
    assert recovery.recover(api, 123) == "cancellation did not finish; no retry"
    assert api.mutations == ["actions/runs/123/cancel"]


def test_watch_is_bounded_without_mutations() -> None:
    api = FakeGitHub()
    api.jobs = [job(status="in_progress", runner_id=10)]
    api.runners = [{"id": 10, "status": "online"}]
    assert recovery.recover(api, 123) == "watch deadline reached; no mutation"
    assert api.mutations == []


def test_missing_inventory_still_allows_long_queue_recovery() -> None:
    canceled = run(status="completed", conclusion="cancelled")
    api = FakeGitHub([run(), run(), canceled, canceled])
    api.runners = None
    recovery.recover(api, 123)
    assert len(api.mutations) == 2


def test_workflow_uses_trusted_code_and_hosted_retry_override() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github/workflows/ci-hosted-recovery.yml").read_text()
    )
    assert workflow["permissions"] == {
        "contents": "read",
        "actions": "write",
        "pull-requests": "read",
    }
    watcher = workflow["jobs"]["recover"]
    assert watcher["runs-on"] == "ubuntu-latest"
    assert watcher["steps"][0]["with"]["ref"] == "${{ github.workflow_sha }}"
    assert watcher["steps"][0]["with"]["persist-credentials"] is False
    assert "workflow_run.head_sha" not in str(watcher["steps"])
    assert "run_attempt == 1" in watcher["if"]
    runner = load_ci_jobs()["test-backend"]["runs-on"]
    assert "github.run_attempt > 1 && 'ubuntu-latest'" in runner


def test_hosted_only_run_does_not_read_runner_inventory() -> None:
    api = FakeGitHub([run(status="completed", conclusion="failure")])
    api.jobs = [job(labels=["ubuntu-latest"], status="completed", conclusion="failure")]
    original_pages = api.pages

    def pages(path: str, key: str, **kwargs: Any) -> list[dict]:
        assert key != "runners", "Hosted-only runs need no administrative read"
        return original_pages(path, key, **kwargs)

    api.pages = pages
    recovery.recover(api, 123)
    assert api.mutations == []


def test_api_uses_inventory_token_only_for_read_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    seen = []

    def request(command: list[str], **kwargs: Any) -> Any:
        seen.append((command, kwargs["env"]["GH_TOKEN"]))
        assert kwargs["timeout"] == 20
        return SimpleNamespace(returncode=0, stdout="{}")

    monkeypatch.setenv("GH_TOKEN", "synthetic-actions-token")
    monkeypatch.setenv("RUNNER_READ_TOKEN", "synthetic-read-only-token")
    monkeypatch.setattr(recovery.subprocess, "run", request)
    api = recovery.GitHub("example/repo")
    api.request("actions/runners", inventory=True)
    api.request("actions/runs/123/cancel", method="POST")
    assert [token for _, token in seen] == [
        "synthetic-read-only-token",
        "synthetic-actions-token",
    ]
    assert seen[1][0][3] == "POST"


def test_inventory_pagination_includes_later_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = recovery.GitHub("example/repo")
    paths = []

    def request(path: str, **kwargs: Any) -> dict:
        paths.append(path)
        assert kwargs["inventory"] is True
        return {
            "runners": [{"id": n} for n in range(100)]
            if len(paths) == 1
            else [{"id": 100}]
        }

    monkeypatch.setattr(api, "request", request)
    assert len(api.pages("actions/runners", "runners", inventory=True)) == 101
    assert paths[-1].endswith("page=2")
