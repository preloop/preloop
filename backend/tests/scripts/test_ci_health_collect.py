"""CI health collector: aggregates, signatures, ledger and value summary.

Replays recorded-shape GitHub Actions and GitLab CI responses (synthetic
ids) through a fake transport, so no network is touched.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
import ci_health_collect as chc  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures" / "ci_health"
NOW = datetime(2026, 11, 28, 6, 0, tzinfo=timezone.utc)
REPO = "example-org/example-repo"


class FakeApi:
    """Serve fixture files by URL and record each request.

    Args:
        remaining: The X-RateLimit-Remaining value to report.
        overrides: URL substring to (status, headers, body) responses.
    """

    def __init__(
        self,
        remaining: int = 4000,
        overrides: dict[str, tuple[int, dict[str, str], bytes]] | None = None,
    ) -> None:
        self.remaining = remaining
        self.overrides = overrides or {}
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(
        self, url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        """Answer one GET.

        Args:
            url: The requested URL.
            headers: The request headers.

        Returns:
            Status, headers and body.
        """
        self.urls.append(url)
        self.headers.append(headers)
        for needle, response in self.overrides.items():
            if needle in url:
                return response
        rate = {"x-ratelimit-remaining": str(self.remaining)}
        self.remaining -= 1
        name = self._fixture(url)
        if name is None or not (FIXTURES / name).exists():
            return 404, rate, b""
        return 200, rate, (FIXTURES / name).read_bytes()

    @staticmethod
    def _fixture(url: str) -> str | None:
        path = url.split("?")[0]
        parts = path.rstrip("/").split("/")
        if "/actions/runs/" in path and path.endswith("/jobs"):
            return f"gh_jobs_{parts[-2]}.json"
        if path.endswith("/actions/runs"):
            return "gh_runs_page1.json"
        if parts[-2] == "runs" and parts[-1].isdigit():
            return f"gh_run_{parts[-1]}.json"
        if "/actions/jobs/" in path:
            return f"gh_log_{parts[-2]}.txt"
        if path.endswith("/pulls"):
            return "gh_pulls.json"
        if path.endswith("/pipelines"):
            return "gl_pipelines.json"
        if "/pipelines/" in path and path.endswith("/jobs"):
            return f"gl_jobs_{parts[-2]}.json"
        if path.endswith("/trace"):
            return f"gl_trace_{parts[-2]}.txt"
        return None


def github(api: FakeApi, **kwargs: Any) -> chc.GitHubSource:
    """Build a GitHub source over the fake API.

    Args:
        api: The fake transport.
        **kwargs: Client keyword arguments.

    Returns:
        The source.
    """
    client = chc.Client(api, {"Authorization": "Bearer t"}, **kwargs)
    return chc.GitHubSource(client, REPO)


@pytest.fixture
def output() -> dict[str, Any]:
    """Collect once over the GitHub fixtures with no previous state.

    Returns:
        The collector output.
    """
    return chc.collect([github(FakeApi())], {}, NOW, 24)


# ---------------------------------------------------------------- signatures


def test_pytest_summary_lines_become_signatures() -> None:
    """FAILED and ERROR lines yield test ids with the error class."""
    sigs = chc.extract_signatures((FIXTURES / "gh_log_5001.txt").read_text())
    assert [(s["kind"], s["test"], s["error"]) for s in sigs] == [
        (
            "pytest",
            "tests/test_flows.py::TestSchedule::test_next_run",
            "AssertionError",
        ),
        ("pytest", "tests/test_db.py::test_pool", "sqlalchemy.exc.OperationalError"),
    ]


def test_go_failures_are_qualified_by_package() -> None:
    """A go test failure is named package.Test."""
    sigs = chc.extract_signatures((FIXTURES / "gh_log_5004.txt").read_text())
    assert [s["test"] for s in sigs] == [
        "example.com/cli/internal/runner.TestRunnerHeartbeat"
    ]


def test_vitest_and_jest_failures() -> None:
    """Vitest FAIL lines and jest bullet titles are both recognized."""
    vitest = chc.extract_signatures((FIXTURES / "gh_log_5201.txt").read_text())
    jest = chc.extract_signatures((FIXTURES / "gh_log_5202.txt").read_text())
    assert vitest[0]["test"] == (
        "src/components/flow-list.test.ts > flow list > renders empty state"
    )
    assert jest[0]["kind"] == "jest"
    assert jest[0]["test"] == "FlowEditor › saves the schedule"


def test_non_test_failure_falls_back_to_the_error_annotation() -> None:
    """A lint failure still gets a signature, with numbers masked."""
    sigs = chc.extract_signatures((FIXTURES / "gh_log_5203.txt").read_text())
    assert len(sigs) == 1
    assert sigs[0]["kind"] == "step"
    assert sigs[0]["error"] == "src/app.ts:N:N 'unused' is defined but never used"


def test_junit_xml_failures() -> None:
    """JUnit failures yield classname::name with the failure type."""
    sigs = chc.junit_signatures((FIXTURES / "junit.xml").read_bytes())
    assert [(s["test"], s["error"]) for s in sigs] == [
        ("tests.test_api::test_timeout", "TimeoutError")
    ]


def test_signature_ids_ignore_volatile_details() -> None:
    """Different addresses and counts give the same signature id."""
    one = chc.extract_signatures("FAILED t.py::a - boom at 0x1 after 3 tries")
    two = chc.extract_signatures("FAILED t.py::a - boom at 0xff after 12 tries")
    assert one[0]["id"] == two[0]["id"]


# --------------------------------------------------------------- aggregation


def test_totals_and_reruns(output: dict[str, Any]) -> None:
    """Every attempt counts; reruns and runner minutes by OS are reported."""
    totals = output["totals"]
    assert totals["runs"] == 3
    assert totals["jobs"] == 11
    assert totals["reruns"] == 1
    assert totals["failures"] == 6
    assert totals["runner_minutes_by_os"]["windows"] == 10.0
    assert output["window"]["start"] == "2026-11-27T06:00:00Z"


def test_shard_imbalance(output: dict[str, Any]) -> None:
    """Slowest over mean shard, per run, for successful shard sets."""
    (row,) = output["shards"]
    assert row["group"] == f"{REPO}:CI/backend"
    # Run 1002: 8 min and 4 min shards, mean 6, ratio 1.33.
    assert row["imbalance_mean"] == 1.33
    assert row["slowest_shard"] == "backend (1/2)"


def test_failure_that_passed_on_rerun_is_flagged_flaky(
    output: dict[str, Any],
) -> None:
    """The backend shard failed, then passed on attempt 2 of the same commit."""
    by_test = {f["test"]: f for f in output["failures"]}
    assert by_test["tests/test_flows.py::TestSchedule::test_next_run"][
        "passed_on_same_sha"
    ]
    go = by_test["example.com/cli/internal/runner.TestRunnerHeartbeat"]
    assert go["count"] == 2
    assert not go["passed_on_same_sha"]
    assert len(go["shas"]) == 2


def test_step_fallbacks_are_keyed_by_job(output: dict[str, Any]) -> None:
    """A lint failure is attributed to its job, not merged with others."""
    steps = [f for f in output["failures"] if f["kind"] == "step"]
    assert [f["test"] for f in steps] == ["Frontend/lint"]


# -------------------------------------------------------------- rate limits


def test_low_rate_limit_stops_and_marks_partial() -> None:
    """Below the floor the collector stops instead of draining the quota."""
    api = FakeApi(remaining=202)
    out = chc.collect([github(api, min_remaining=200)], {}, NOW, 24)
    assert out["window"]["partial"]
    assert "rate limit remaining" in out["window"]["partial_reasons"][0]
    # 202, 201, 200 pass the floor; 199 is the last answer before stopping.
    assert len(api.urls) == 4


def test_retry_after_stops_without_looping() -> None:
    """A secondary rate-limit response ends collection for that source."""
    api = FakeApi(overrides={"/actions/runs?": (403, {"retry-after": "60"}, b"")})
    out = chc.collect([github(api)], {}, NOW, 24)
    assert out["window"]["partial"]
    assert len(api.urls) == 1


def test_log_fetches_are_capped() -> None:
    """Failed jobs beyond the log cap are counted, not fetched."""
    api = FakeApi()
    out = chc.collect([github(api)], {}, NOW, 24, max_logs=2)
    assert sum("/logs" in u for u in api.urls) == 2
    assert "4 failed jobs have no signature" in out["window"]["partial_reasons"][0]


def test_cache_serves_immutable_responses(tmp_path: Path) -> None:
    """Jobs and logs are cached; the run listing is always fresh."""
    api = FakeApi()
    chc.collect([github(api, cache_dir=tmp_path)], {}, NOW, 24)
    first = len(api.urls)
    source = github(api, cache_dir=tmp_path)
    chc.collect([source], {}, NOW, 24)
    assert len(api.urls) - first == 1
    assert source.client.cache_hits == first - 1


def test_requests_are_get_only_and_logs_drop_auth_on_redirect() -> None:
    """The real transport builds GET requests; redirects carry no token."""
    handler = chc._NoAuthRedirect()
    original = chc.urllib.request.Request("https://api.example.com/logs")
    original.add_unredirected_header("Authorization", "Bearer secret")
    redirected = handler.redirect_request(
        original, None, 302, "Found", {}, "https://blob.example.com/log"
    )
    assert redirected is not None
    assert redirected.get_method() == "GET"
    assert not redirected.has_header("Authorization")


def test_network_error_marks_the_source_partial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TLS or DNS failure ends collection as partial instead of crashing."""

    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise chc.urllib.error.URLError("certificate verify failed")

    monkeypatch.setattr(chc.urllib.request.OpenerDirector, "open", refuse)
    client = chc.Client(chc.urllib_transport, {})
    out = chc.collect([chc.GitHubSource(client, REPO)], {}, NOW, 24)
    assert out["window"]["partial"]
    assert "network error" in out["window"]["partial_reasons"][0]


def _listing(*runs: dict[str, Any]) -> tuple[int, dict[str, str], bytes]:
    return 200, {}, json.dumps({"workflow_runs": list(runs)}).encode()


def test_long_running_run_is_read_once_it_completes() -> None:
    """A run older than the overlap and still running is re-read by id."""
    fixture = json.loads((FIXTURES / "gh_run_1002.json").read_text())
    running = {
        **fixture,
        "status": "in_progress",
        "created_at": chc.iso(NOW - timedelta(hours=3)),
    }
    first = chc.collect(
        [github(FakeApi(overrides={"/actions/runs?": _listing(running)}))],
        {},
        NOW,
        24,
    )
    state = first[chc.STATE_KEY]
    assert first["window"]["runs_read"] == 0
    assert list(state["pending_runs"]) == [f"github:{REPO}:1002"]
    # Two hours later the run is outside the listing window but completed.
    api = FakeApi(overrides={"/actions/runs?": _listing()})
    second = chc.collect([github(api)], state, NOW + timedelta(hours=2), 24)
    assert second["window"]["runs_read"] == 1
    assert second["totals"]["failures"] == 1
    assert second[chc.STATE_KEY]["pending_runs"] == {}
    assert any(u.endswith("/actions/runs/1002") for u in api.urls)


def test_stale_pending_runs_are_dropped() -> None:
    """A run unfinished for over a day is given up, not polled forever."""
    previous = {
        "window_end": chc.iso(NOW - timedelta(hours=2)),
        "pending_runs": {
            f"github:{REPO}:1002": {"created_at": chc.iso(NOW - timedelta(days=2))}
        },
    }
    stuck = {"status": "queued", "id": 1002}
    api = FakeApi(
        overrides={
            "/actions/runs/1002": (200, {}, json.dumps(stuck).encode()),
            "/actions/runs?": _listing(),
        }
    )
    out = chc.collect([github(api)], previous, NOW, 24)
    assert out[chc.STATE_KEY]["pending_runs"] == {}
    assert out["window"]["pending_dropped"] == 1


def test_missing_repository_is_not_reported_as_quiet(tmp_path: Path) -> None:
    """A 404 on the run listing is partial and exits 2, not an empty success."""
    api = FakeApi(overrides={"/actions/runs?": (404, {}, b"")})
    out = chc.collect([github(api)], {}, NOW, 24)
    assert out["window"]["partial"]
    assert "404 on a listing" in out["window"]["partial_reasons"][0]
    code = chc.main(
        [
            "collect",
            "--github-repo",
            REPO,
            "--previous",
            "",
            "--cache-dir",
            "",
            "--out",
            str(tmp_path / "o.json"),
            "--now",
            chc.iso(NOW),
        ],
        transport=api,
    )
    assert code == 2


def test_job_listings_are_paginated() -> None:
    """A run with more than 100 jobs is read in full."""
    template = json.loads((FIXTURES / "gh_jobs_1002.json").read_text())["jobs"][0]
    pages = {
        "page=1": [{**template, "id": i, "name": f"job {i}"} for i in range(100)],
        "page=2": [{**template, "id": 100, "name": "job 100", "conclusion": "failure"}],
    }
    run = json.loads((FIXTURES / "gh_run_1002.json").read_text())

    def transport(
        url: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes]:
        if "/actions/runs?" in url:
            return _listing(run)
        if "/jobs?" in url:
            page = "page=2" if "page=2" in url else "page=1"
            return 200, {}, json.dumps({"jobs": pages[page]}).encode()
        return 404, {}, b""

    out = chc.collect([github(transport)], {}, NOW, 24)  # type: ignore[arg-type]
    assert out["totals"]["jobs"] == 101
    assert out["totals"]["failures"] == 1


def test_run_ids_are_keyed_by_provider_and_repository() -> None:
    """Two repositories with the same run ids are both read, then both skipped."""
    api = FakeApi()
    other = chc.GitHubSource(chc.Client(api, {}), "example-org/other-repo")
    first = chc.collect([github(api), other], {}, NOW, 24)
    assert first["window"]["runs_read"] == 6
    state = first[chc.STATE_KEY]
    second = chc.collect(
        [github(FakeApi()), chc.GitHubSource(chc.Client(FakeApi(), {}), other.repo)],
        state,
        NOW + timedelta(hours=1),
        24,
    )
    assert second["window"]["runs_read"] == 0
    assert second["window"]["runs_skipped_already_seen"] == 6


# --------------------------------------------------------------------- gitlab


def test_gitlab_pipelines_jobs_and_retries() -> None:
    """GitLab jobs get the same aggregates, retries count as reruns."""
    api = FakeApi()
    client = chc.Client(api, {"PRIVATE-TOKEN": "t"})
    source = chc.GitLabSource(
        client, "group/project", "https://gitlab.example.com/api/v4"
    )
    out = chc.collect([source], {}, NOW, 24)
    assert out["totals"]["runs"] == 1  # the running pipeline is skipped
    assert out["totals"]["reruns"] == 1
    (failure,) = out["failures"]
    assert failure["test"] == "tests/test_ee_billing.py::test_invoice_rounding"
    assert failure["passed_on_same_sha"]
    assert "group%2Fproject" in api.urls[0]


# --------------------------------------------------------------------- ledger


def test_ledger_persists_across_runs_and_preserves_agent_fields(
    output: dict[str, Any],
) -> None:
    """A second window updates counts and keeps the linked issue."""
    state = output[chc.STATE_KEY]
    state["value_summary_at"] = chc.iso(NOW)
    sig = chc.extract_signatures((FIXTURES / "gh_log_5004.txt").read_text())[0]
    state["ledger"][sig["id"]]["issue"] = "https://github.com/example/i/1"
    later = NOW + timedelta(hours=2)
    second = chc.collect([github(FakeApi())], state, later, 24)
    entry = second[chc.STATE_KEY]["ledger"][sig["id"]]
    # Runs already seen in the overlap are skipped, so nothing double counts.
    assert entry["failures"] == 2
    assert entry["issue"] == "https://github.com/example/i/1"
    assert entry["status"] == "open"
    assert second["window"]["start"] == "2026-11-28T05:00:00Z"
    assert second["window"]["runs_skipped_already_seen"] == 3
    assert second["early_stop"]


def test_resolved_signature_that_fails_again_regresses(
    output: dict[str, Any],
) -> None:
    """Marking an entry resolved does not hide a recurrence."""
    state = output[chc.STATE_KEY]
    for entry in state["ledger"].values():
        entry["status"] = "resolved"
    state["seen_runs"] = []
    state["value_summary_at"] = chc.iso(NOW)
    second = chc.collect([github(FakeApi())], state, NOW + timedelta(hours=2), 24)
    statuses = {e["status"] for e in second[chc.STATE_KEY]["ledger"].values()}
    assert statuses == {"regressed"}
    assert second["new_signatures"]
    assert not second["early_stop"]


def test_ledger_is_bounded(output: dict[str, Any]) -> None:
    """Old resolved entries go first; the total stays under the cap."""
    ledger = {
        f"id{i}": {
            "status": "resolved" if i % 2 else "open",
            "last_seen": chc.iso(NOW - timedelta(days=40 if i % 2 else 1)),
        }
        for i in range(400)
    }
    bounded = chc.bound_ledger(ledger, NOW)
    assert len(bounded) == chc.MAX_LEDGER_ENTRIES
    assert all(v["status"] == "open" for v in bounded.values())


def test_state_fits_the_previous_result_budget(output: dict[str, Any]) -> None:
    """A full ledger, trend and history stay under the 256 KiB baseline cap."""
    state = output[chc.STATE_KEY]
    template = next(iter(state["ledger"].values()))
    state["ledger"] = {
        f"{i:012x}": {**template, "line": "x" * 200, "note": "n" * 200}
        for i in range(chc.MAX_LEDGER_ENTRIES)
    }
    state["trend"] = state["trend"] * chc.MAX_TREND_ENTRIES
    state["seen_runs"] = [f"gi{10**10 + i}" for i in range(chc.MAX_SEEN_RUNS)]
    state["history"] = [{"at": chc.iso(NOW), "change": "u" * 80}] * 60
    assert len(json.dumps(state)) < chc.STATE_BUDGET_BYTES


def test_large_history_is_trimmed_as_finalize_writes_it(
    tmp_path: Path, output: dict[str, Any]
) -> None:
    """History and seen runs shrink too; the written file meets the budget."""
    output[chc.STATE_KEY]["ledger"] = {}
    output[chc.STATE_KEY]["history"] = [{"change": "h" * 4000}] * 60
    output[chc.STATE_KEY]["seen_runs"] = ["x" * 200] * 600
    collector = tmp_path / "c.json"
    collector.write_text(json.dumps(output))
    result = tmp_path / "result.json"
    chc.finalize(collector, result, {"change": "e" * 2000}, NOW)
    state = json.loads(result.read_text())[chc.STATE_KEY]
    assert len(chc.dump_result(state)) <= chc.STATE_BUDGET_BYTES
    assert state["history"][-1]["change"] == "e" * 2000


def test_oversized_state_is_trimmed_to_the_budget() -> None:
    """Long agent notes and a huge ledger cannot overflow the next run."""
    state = {
        "trend": [{"window_end": "x" * 100}] * 84,
        "ledger": {
            f"{i:012x}": {
                "last_seen": chc.iso(NOW),
                "note": "n" * 5000,
                "line": "l" * 2000,
            }
            for i in range(200)
        },
    }
    chc.fit_state(state)
    assert len(chc.dump_result(state)) <= chc.STATE_BUDGET_BYTES
    assert all(len(e["note"]) <= chc.MAX_NOTE_CHARS for e in state["ledger"].values())
    assert state["ledger"]


# ---------------------------------------------------------- value and finalize


def test_finalize_merges_state_and_agent_ledger_edits(
    tmp_path: Path, output: dict[str, Any]
) -> None:
    """The agent's status edits win; the history entry is appended."""
    collector = tmp_path / "ci-health.json"
    collector.write_text(json.dumps(output))
    sig_id = next(iter(output[chc.STATE_KEY]["ledger"]))
    result = tmp_path / "result.json"
    result.write_text(
        json.dumps(
            {
                "status": "success",
                chc.STATE_KEY: {"ledger": {sig_id: {"status": "resolved"}}},
            }
        )
    )
    code = chc.main(
        [
            "finalize",
            "--collector",
            str(collector),
            "--result",
            str(result),
            "--entry",
            json.dumps({"change": "flake", "cost_usd": 1.5}),
            "--now",
            chc.iso(NOW),
        ]
    )
    assert code == 0
    merged = json.loads(result.read_text())
    assert merged["status"] == "success"
    entry = merged[chc.STATE_KEY]["ledger"][sig_id]
    assert entry["status"] == "resolved"
    assert entry["resolved_at"] == chc.iso(NOW)
    assert merged[chc.STATE_KEY]["history"][-1]["cost_usd"] == 1.5


def test_weekly_value_summary_reads_pr_states(tmp_path: Path) -> None:
    """Merged PRs, usage and minutes saved roll up into the weekly summary."""
    previous = {
        "window_end": chc.iso(NOW - timedelta(hours=2)),
        "history": [
            {
                "at": chc.iso(NOW - timedelta(days=2)),
                "change": "flake",
                "tokens": 1_000_000,
                "cost_usd": 4.0,
            },
            {
                "at": chc.iso(NOW - timedelta(days=1)),
                "early_stop": True,
                "cost_usd": 0.5,
                "tokens": 50_000,
            },
            {"at": chc.iso(NOW - timedelta(days=9)), "cost_usd": 9.0},
        ],
    }
    prev_path = tmp_path / "previous.json"
    prev_path.write_text(json.dumps({chc.STATE_KEY: previous}))
    out_path = tmp_path / "out.json"
    api = FakeApi()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GH_TOKEN", "t")
        code = chc.main(
            [
                "collect",
                "--github-repo",
                REPO,
                "--previous",
                str(prev_path),
                "--cache-dir",
                "",
                "--out",
                str(out_path),
                "--now",
                chc.iso(NOW),
                "--flow-branch-prefix",
                "preloop/daily-ci-and-qa-review-",
            ],
            transport=api,
        )
    assert code == 0
    assert all(h.get("Authorization") == "Bearer t" for h in api.headers)
    summary = json.loads(out_path.read_text())["value_summary"]
    assert summary["runs"] == 2
    # The unrelated branch and the PR older than a week do not count.
    assert summary["prs_opened"] == 2
    assert summary["prs_merged"] == 1
    assert summary["merged_pull_requests"] == [
        "https://github.com/example-org/example-repo/pull/21"
    ]
    assert summary["early_stops"] == 1
    assert summary["cost_usd"] == 4.5
    assert summary["cost_per_merged_pr_usd"] == 4.5
    assert summary["tokens_per_merged_pr"] == 1_050_000
    assert summary["runner_minutes_saved_per_week_est"] == 90.0


def test_value_summary_without_prefix_marks_prs_unknown(
    output: dict[str, Any],
) -> None:
    """Without a branch prefix the PR counts are flagged as incomplete."""
    chc.add_value_summary(output, [github(FakeApi())], NOW, None)
    assert output["value_summary"]["pull_requests_complete"] is False
    assert output[chc.STATE_KEY]["value_summary_at"] == chc.iso(NOW)


def test_quiet_window_stops_early(output: dict[str, Any]) -> None:
    """No new runs, no failures and no summary due: nothing to do."""
    state = output[chc.STATE_KEY]
    state["value_summary_at"] = chc.iso(NOW)
    api = FakeApi(overrides={"/actions/runs?": (200, {}, b'{"workflow_runs": []}')})
    out = chc.collect([github(api)], state, NOW + timedelta(hours=2), 24)
    assert out["early_stop"]
    assert out["changed_reasons"] == []


def test_unreadable_source_exits_nonzero(tmp_path: Path) -> None:
    """A refused token with nothing read is a failure the flow reports."""
    api = FakeApi(overrides={"/actions/runs?": (401, {}, b"")})
    code = chc.main(
        [
            "collect",
            "--github-repo",
            REPO,
            "--previous",
            "",
            "--cache-dir",
            "",
            "--out",
            str(tmp_path / "o.json"),
            "--now",
            chc.iso(NOW),
        ],
        transport=api,
    )
    assert code == 2
