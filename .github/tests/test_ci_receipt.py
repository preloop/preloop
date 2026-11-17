"""Ensure full receipts cannot accept incomplete or skipped coverage."""

import copy
import json
from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from ci_receipt import (  # noqa: E402
    ALWAYS,
    JOB_PATHS,
    SCANNERS,
    fetch_jobs,
    timings,
    validate,
)
from ci_refresh_durations import read_durations  # noqa: E402


def needs() -> dict:
    """Produce a complete synthetic full-gate result."""
    result = {job: {"result": "success"} for job in ALWAYS | SCANNERS | set(JOB_PATHS)}
    result["changes"]["outputs"] = {output: "true" for output in JOB_PATHS.values()}
    return result


@pytest.mark.parametrize("state", ["skipped", "failure", "cancelled", ""])
@pytest.mark.parametrize("job", sorted(ALWAYS | SCANNERS | set(JOB_PATHS)))
def test_full_gate_rejects_every_missing_or_red_suite(job: str, state: str) -> None:
    """Even a successful aggregate must account for each expected test suite."""
    result = needs()
    result[job]["result"] = state
    with pytest.raises(ValueError):
        validate(result, "merge_group")


def test_only_explicit_irrelevant_paths_allow_a_skip() -> None:
    """A missing or malformed selector output cannot explain skipped tests."""
    result = needs()
    validate(result, "merge_group")
    result["changes"]["outputs"]["frontend"] = "false"
    result["test-frontend"]["result"] = "skipped"
    validate(result, "merge_group")
    result["changes"]["outputs"]["frontend"] = "unknown"
    with pytest.raises(ValueError):
        validate(result, "merge_group")


def test_gate_inventory_matches_the_workflow() -> None:
    """Adding a suite without including it in the required gate must fail."""
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    assert set(jobs["ci"]["needs"]) == ALWAYS | SCANNERS | set(JOB_PATHS)
    for job in JOB_PATHS:
        if job == "test-backend-coverage":
            assert "test-backend" in jobs[job]["needs"]
        elif job != "requirements-resolve":
            assert "fast" in jobs[job]["needs"]
    assert "fast" in jobs["pick-runner"]["needs"]
    assert "fast" in jobs["review"]["needs"]


def test_timing_receipts_keep_runner_classes_and_queue_separate() -> None:
    """Averages across unlike runner classes would hide host contention."""
    job = {
        "id": 1,
        "name": "Backend Tests (1/18)",
        "conclusion": "success",
        "created_at": "2026-01-01T00:00:00Z",
        "started_at": "2026-01-01T00:01:00Z",
        "completed_at": "2026-01-01T00:03:00Z",
        "labels": ["self-hosted"],
    }
    hosted = copy.deepcopy(job)
    hosted.update(id=2, labels=["ubuntu-24.04"])
    receipt = timings([job, hosted])
    assert receipt["jobs"][0]["queue_seconds"] == 60
    assert receipt["jobs"][0]["execution_seconds"] == 120
    assert set(receipt["shard_max_over_median"]) == {"self-hosted", "github-hosted"}


def test_duration_refresh_rejects_partial_failures_and_duplicates(
    tmp_path: Path,
) -> None:
    """Failed execution and duplicate nodes must not distort shard weights."""
    (tmp_path / "test_example.py").write_text("pass\n")
    report = tmp_path / "result.xml"
    report.write_text(
        '<testsuite><testcase classname="test_example" name="test_one" time="1.5"/></testsuite>'
    )
    assert read_durations([report], tmp_path) == {"test_example.py::test_one": 1.5}
    with pytest.raises(ValueError, match="Duplicate"):
        read_durations([report, report], tmp_path)
    report.write_text(
        '<testsuite><testcase classname="test_example" name="test_one" time="1.5"><failure/></testcase></testsuite>'
    )
    with pytest.raises(ValueError, match="successful"):
        read_durations([report], tmp_path)


def test_refreshed_measured_weights_meet_the_balance_target() -> None:
    """Record a measured execution replay, without claiming wall-time savings."""
    receipt = json.loads((ROOT / ".github/ci-duration-receipt.json").read_text())
    assert len(receipt["group_seconds"]) == 18
    assert receipt["max_over_median"] <= 1.25
    assert receipt["test_cases"] == 30045


def test_fork_prs_may_skip_the_secret_bound_claude_leaf() -> None:
    """The live-SDK leaf needs secrets and skips on forks; only forks may skip."""
    result = needs()
    for scanner in SCANNERS:
        result[scanner]["result"] = "skipped"  # separate every-PR workflows
    result["leaf-claude"]["result"] = "skipped"
    validate(result, "pull_request", fork=True)
    with pytest.raises(ValueError):
        validate(result, "pull_request")


def test_timing_fetch_failure_does_not_gate_the_aggregate() -> None:
    """An unreadable jobs API leaves an incomplete receipt, not a red CI check."""

    class Failing:
        def pages(self, path: str, key: str = "") -> list:
            raise RuntimeError(f"GitHub GET {path} failed (403)")

    jobs, error = fetch_jobs(Failing(), "1")  # type: ignore[arg-type]
    assert jobs == []
    assert error and "403" in error
    assert timings(jobs)["jobs"] == []
