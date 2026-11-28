"""The collector end to end over recorded GitHub Actions and GitLab CI responses."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from preloop.services.ci_health import ledger as ledger_mod
from preloop.services.ci_health.aggregate import base_job_name
from preloop.services.ci_health.collector import (
    Options,
    Target,
    collect,
    precheck_focus,
    pull_request_repository,
)
from preloop.services.ci_health.transport import Budget
from tests.services.ci_health.conftest import FakeCi, make_providers

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
CRITICAL = ["test*", "lint"]


async def _collect(
    fake: FakeCi,
    *,
    ledger: Any = None,
    budget: Budget | None = None,
    options: Options | None = None,
    **kwargs: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    github, gitlab = make_providers(fake, budget or Budget(max_requests=200))
    targets = [
        Target(github, "example-org/app"),
        Target(gitlab, "example-group/sub/svc"),
    ]
    return await collect(
        targets,
        ledger_value=ledger,
        now=NOW,
        options=options or Options(critical_path_jobs=CRITICAL),
        **kwargs,
    )


def _repo(report: dict[str, Any], provider: str) -> dict[str, Any]:
    return next(r for r in report["repositories"] if r["provider"] == provider)


def _entry(ledger: dict[str, Any], test: str) -> dict[str, Any]:
    return next(e for e in ledger["signatures"].values() if e["test"] == test)


@pytest.mark.parametrize(
    ("name", "base"),
    [("test (1/2)", "test"), ("e2e [chromium, 3]", "e2e"), ("lint", "lint")],
)
def test_base_job_name(name: str, base: str) -> None:
    assert base_job_name(name) == base


@pytest.mark.asyncio
async def test_github_and_gitlab_aggregates(fake_ci: FakeCi) -> None:
    report, ledger = await _collect(fake_ci)
    github = _repo(report, "github")
    totals = github["totals"]
    assert (totals["runs"], totals["completed"]) == (5, 4)
    assert totals["success_rate"] == 0.75
    assert totals["rerun_rate"] == 0.2
    assert github["default_branch"] == "main"
    assert github["default_branch_state"] == "success"  # Docs run at 10:05
    jobs = {j["job"]: j for j in github["jobs"]}
    assert jobs["test"]["critical"] is True
    assert jobs["test"]["flaky_commits"] == 1
    assert jobs["test"]["reruns"] == 2
    assert jobs["frontend"]["critical"] is False
    assert github["critical_path"]["slowest_gate"]["job"] == "test"
    assert github["shard_imbalance"] == [
        {
            "workflow": "CI",
            "job": "test",
            "shards": 2,
            "imbalance_p50": 1.5,
            "slowest_shard": "test (1/2)",
            "runs": 3,
        }
    ]
    assert github["flaky_jobs"] == [{"workflow": "CI", "job": "test", "commits": 1}]

    gitlab = _repo(report, "gitlab")
    assert gitlab["totals"]["success_rate"] == 0.5
    assert gitlab["totals"]["rerun_rate"] == 0.5
    # allow_failure jobs do not count as failures.
    assert {f["job"] for f in gitlab["failures"]} == {"unit"}
    assert report["changed_since_last_run"] is True
    assert report["partial"] is False


@pytest.mark.asyncio
async def test_signatures_flakes_and_failure_lines(fake_ci: FakeCi) -> None:
    report, ledger = await _collect(fake_ci)
    flaky = _entry(
        ledger, "tests/api/test_orders.py::TestCheckout::test_retry_after_timeout"
    )
    assert flaky["flaky"] is True
    assert flaky["count"] == 1
    assert flaky["repositories"] == ["github:example-org/app"]
    assert flaky["commits"] == ["c0ffee0000000000000000000000000000000001"]
    vitest = _entry(
        ledger,
        "src/components/flow-editor.test.ts > flow editor > saves a schedule change",
    )
    assert vitest["flaky"] is False
    # A failed job whose log names no test gets a job-level signature.
    fallback = _entry(ledger, "CI / deploy-preview / Upload")
    assert fallback["framework"] == "job"
    # GitLab: JUnit test report and go test trace, both attempts counted.
    drain = _entry(ledger, "example.com/svc/internal/queue::TestQueueDrain")
    assert (drain["framework"], drain["count"]) == ("junit", 2)
    assert _entry(ledger, "example.com/svc/internal/queue.TestQueueDrain")["count"] == 2
    github = _repo(report, "github")
    first = github["failures"][0]
    assert first["url"].endswith("/job/1033")  # newest default-branch failure
    assert first["critical"] is False
    lines = [line for f in github["failures"] for line in f["lines"]]
    assert "Error: upload failed: 502 Bad Gateway" in lines
    assert report["ledger"]["counts"]["open"] == len(ledger["signatures"])
    assert report["mode"]["report_only"] is True


@pytest.mark.asyncio
async def test_second_call_is_idempotent_and_cached(fake_ci: FakeCi) -> None:
    report, ledger = await _collect(fake_ci)
    first_requests = len(fake_ci.requests)
    counts = {k: v["count"] for k, v in ledger["signatures"].items()}
    fake_ci.requests.clear()
    again, ledger = await _collect(fake_ci, ledger=ledger)
    assert again["changed_since_last_run"] is False
    assert {k: v["count"] for k, v in ledger["signatures"].items()} == counts
    assert _repo(again, "github")["failures"] == []
    # Completed runs come from cache: only the lists and the running run.
    assert len(fake_ci.requests) < first_requests / 2
    assert "/repos/example-org/app/actions/runs/105/jobs" in fake_ci.paths()
    assert "/repos/example-org/app/actions/runs/101/jobs" not in fake_ci.paths()
    assert len(ledger["history"]) == 2
    assert again["previous"]["changed"] is True


@pytest.mark.asyncio
async def test_read_only_and_credentials_stay_on_the_provider(fake_ci: FakeCi) -> None:
    await _collect(fake_ci)
    assert {r.method for r in fake_ci.requests} == {"GET"}
    for request in fake_ci.requests:
        if request.url.host == "logs.example.net":
            assert "authorization" not in request.headers
        elif request.url.host == "api.github.com":
            assert request.headers["authorization"] == "token test-token"


@pytest.mark.asyncio
async def test_request_budget_and_rate_limits(fake_ci: FakeCi) -> None:
    report, ledger = await _collect(fake_ci, budget=Budget(max_requests=3))
    assert report["partial"] is True
    assert _repo(report, "github")["partial_reason"] == "request_budget"
    # Runs without their jobs are not counted, so the next call counts them.
    assert ledger["counted_runs"].get("github:example-org/app", []) == []

    fake_ci.requests.clear()
    fake_ci.headers = {"x-ratelimit-remaining": "120"}
    report, _ = await _collect(fake_ci, budget=Budget(max_requests=200, floor=300))
    assert _repo(report, "github")["partial_reason"] == "rate_limit_floor"
    assert len(fake_ci.requests) == 1

    fake_ci.requests.clear()
    fake_ci.headers = {"x-ratelimit-remaining": "4000"}
    fake_ci.overrides[r"/actions/runs$"] = lambda request: httpx.Response(
        429, headers={"retry-after": "60"}
    )
    report, _ = await _collect(fake_ci)
    assert _repo(report, "github")["partial_reason"] == "rate_limited"
    # The shared budget stops the other repository too instead of looping.
    assert _repo(report, "gitlab")["partial_reason"] == "rate_limited"
    assert len(fake_ci.requests) == 2


@pytest.mark.asyncio
async def test_provider_errors_are_reported_per_repository(fake_ci: FakeCi) -> None:
    fake_ci.overrides[r"^/repos/example-org/app$"] = lambda request: httpx.Response(
        404, json={"message": "Not Found"}
    )
    report, _ = await _collect(fake_ci)
    github = _repo(report, "github")
    assert github["error"] == "provider answered 404"
    assert report["partial"] is True
    assert _repo(report, "gitlab")["totals"]["runs"] == 2


@pytest.mark.asyncio
async def test_precheck_focus_reads_matched_runs_first(fake_ci: FakeCi) -> None:
    precheck = {
        "decision": "run",
        "checks": {
            "new_failures": {
                "matched": True,
                "items": [
                    {"url": "https://github.com/example-org/app/actions/runs/101"}
                ],
            },
            "main_red": {"matched": False, "items": [{"url": "https://x.example"}]},
        },
    }
    focus = precheck_focus(precheck)
    assert focus == {"https://github.com/example-org/app/actions/runs/101"}
    report, _ = await _collect(
        fake_ci,
        options=Options(
            critical_path_jobs=CRITICAL, max_failed_logs=1, focus_urls=focus
        ),
    )
    github = _repo(report, "github")
    assert github["failed_logs_read"] == 1
    assert github["failures"][0]["url"].endswith("/job/1011")
    assert github["failures"][0]["signatures"]
    assert precheck_focus(None) == set()


@pytest.mark.asyncio
async def test_pull_requests_and_report_only_window(fake_ci: FakeCi) -> None:
    fake_ci.pulls["7"] = {"state": "closed", "merged_at": "2026-01-01T11:00:00Z"}
    old = ledger_mod.empty(NOW - timedelta(days=20))
    report, ledger = await _collect(
        fake_ci,
        ledger=old,
        published_pull_requests=[
            {
                "url": "https://github.com/example-org/app/pull/7",
                "execution_id": "e1",
                "opened_at": "2026-01-01T09:30:00Z",
                "estimated_minutes_saved_per_week": 45,
            },
            {"url": "https://github.com/other-org/lib/pull/3", "execution_id": "e2"},
        ],
    )
    prs = {pr["url"]: pr for pr in ledger["pull_requests"]}
    assert prs["https://github.com/example-org/app/pull/7"]["state"] == "merged"
    # Not a scanned repository: never read with another repository's token.
    assert prs["https://github.com/other-org/lib/pull/3"]["state"] == "open"
    assert report["mode"] == {
        "report_only": False,
        "reason": "report-only period is over",
    }
    forced, _ = await _collect(fake_ci, ledger=old, options=Options(report_only="on"))
    assert forced["mode"]["report_only"] is True


def test_ledger_status_lifecycle_and_caps() -> None:
    ledger = ledger_mod.empty(NOW)
    signature = {"id": "abc123abc123", "framework": "pytest", "test": "t", "error": "E"}

    def seen(at: datetime) -> dict[str, Any]:
        return ledger_mod.record_failure(
            ledger,
            signature=signature,
            repository="github:example-org/app",
            job="test",
            head_sha="c0ffee",
            run_url="https://example.com/run/1",
            seen_at=at,
        )

    seen(NOW)
    refused = ledger_mod.apply_updates(
        ledger,
        [
            {
                "signature_id": "abc123abc123",
                "status": "fixed",
                "linked": "https://github.com/example-org/app/pull/9",
                "note": "n" * 400,
            },
            {"signature_id": "missing"},
            {"signature_id": "abc123abc123", "status": "regressed"},
        ],
    )
    assert refused == [
        {"signature_id": "missing", "reason": "unknown signature"},
        {"signature_id": "abc123abc123", "reason": "invalid status"},
    ]
    entry = ledger["signatures"]["abc123abc123"]
    assert (entry["status"], len(entry["note"])) == ("fixed", ledger_mod.NOTE_CHARS)
    assert entry["linked"] == ["https://github.com/example-org/app/pull/9"]
    assert seen(NOW + timedelta(hours=1))["status"] == "regressed"
    ledger_mod.age(ledger, NOW + timedelta(days=9))
    assert entry["status"] == "quiet"
    assert seen(NOW + timedelta(days=10))["status"] == "open"
    for i in range(ledger_mod.MAX_SIGNATURES + 5):
        ledger["signatures"][f"s{i}"] = {"status": "quiet", "last_seen": f"{i:04d}"}
    ledger_mod.age(ledger, NOW + timedelta(days=10))
    assert len(ledger["signatures"]) == ledger_mod.MAX_SIGNATURES
    assert "abc123abc123" in ledger["signatures"]
    # Damaged or future ledgers start over instead of failing the run.
    assert ledger_mod.load({"version": 99}, NOW)["signatures"] == {}
    assert ledger_mod.load({"version": 1, "signatures": []}, NOW)["signatures"] == {}


def test_ledger_fit_keeps_open_signatures() -> None:
    ledger = ledger_mod.empty(NOW)
    ledger["counted_runs"] = {"github:a/b": [str(i) for i in range(1500)]}
    ledger["history"] = [{"at": str(i), "pad": "x" * 500} for i in range(100)]
    for i in range(150):
        ledger["signatures"][f"s{i}"] = {
            "status": "open" if i % 2 else "quiet",
            "last_seen": f"{i:04d}",
            "pad": "y" * 900,
        }

    def size(value: Any) -> int:
        import json

        return len(json.dumps(value))

    ledger_mod.fit(ledger, 60_000, size)
    assert size(ledger) <= 60_000
    assert all(entry["status"] == "open" for entry in ledger["signatures"].values())


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://github.com/example-org/app/pull/7",
            ("github.com", "example-org/app", "7"),
        ),
        (
            "https://gitlab.example.com/example-group/sub/svc/-/merge_requests/4",
            ("gitlab.example.com", "example-group/sub/svc", "4"),
        ),
        ("https://example.com/not/a/pr", None),
    ],
)
def test_pull_request_repository(url: str, expected: Any) -> None:
    assert pull_request_repository(url) == expected
