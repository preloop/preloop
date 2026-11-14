"""Execute CI cleanup and final-gate failure paths without runner services."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def workflow(name: str) -> dict:
    """Load a workflow's jobs without evaluating GitHub expressions."""
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


@pytest.mark.parametrize(
    "first,retryable,retry,windows,paths_result,expected",
    [
        ("success", "false", "skipped", "true", "success", 0),
        ("success", "true", "success", "true", "success", 0),
        ("success", "true", "failure", "true", "success", 1),
        ("success", "true", "skipped", "true", "success", 1),
        ("failure", "false", "skipped", "true", "success", 1),
        ("success", "", "skipped", "true", "success", 1),
        ("skipped", "", "skipped", "false", "success", 0),
        ("skipped", "", "skipped", "", "success", 1),
        ("skipped", "", "skipped", "unknown", "success", 1),
        ("skipped", "", "skipped", "false", "failure", 1),
        ("success", "false", "skipped", "true", "failure", 1),
    ],
)
def test_windows_gate_never_accepts_missing_validation(
    first: str,
    retryable: str,
    retry: str,
    windows: str,
    paths_result: str,
    expected: int,
) -> None:
    """A fresh-runner retry must complete the same tests and actual scan."""
    job = workflow("windows-security.yml")["jobs"]["windows-security-tests"]
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", job["steps"][0]["run"]],
        env={
            **os.environ,
            "PATHS_RESULT": paths_result,
            "WINDOWS": windows,
            "FIRST": first,
            "RETRYABLE": retryable,
            "RETRY": retry,
        },
        check=False,
    )
    assert result.returncode == expected


def test_cleanup_removes_only_its_own_temporary_environments(tmp_path: Path) -> None:
    """Exercise the exact shell cleanup against a fake shared runner layout."""
    own = tmp_path / "runner1" / "_temp"
    sibling = tmp_path / "runner2" / "_temp" / "preloop-deps"
    sibling.mkdir(parents=True)
    shared = tmp_path / "toolcache" / "preloop-uv-cache"
    shared.mkdir(parents=True)
    own.mkdir(parents=True)
    keep = own / "unrelated"
    keep.write_text("retain")
    jobs = workflow("ci.yml")["jobs"]
    steps = [
        step
        for step in jobs["test-backend"]["steps"]
        if step.get("name", "").startswith("Remove ")
    ]
    assert len(steps) == 2
    all_steps = jobs["test-backend"]["steps"]
    positions = {step.get("name"): i for i, step in enumerate(all_steps)}
    checkout = next(
        i
        for i, step in enumerate(all_steps)
        if step.get("uses", "").startswith("actions/checkout@")
    )
    assert positions["Remove abandoned job environments"] < checkout
    completed = positions["Remove completed job environments"]
    assert positions["Upload coverage data"] < completed
    assert positions["Upload test results"] < completed
    for step in steps:
        for name in ("preloop-deps", "preloop-python"):
            (own / name).mkdir()
        subprocess.run(
            ["bash", "-c", step["run"]],
            env={**os.environ, "RUNNER_TEMP": str(own)},
            check=True,
            capture_output=True,
        )
        assert not (own / "preloop-deps").exists()
        assert not (own / "preloop-python").exists()
        assert sibling.is_dir() and shared.is_dir() and keep.is_file()
    assert "always()" in steps[-1]["if"]


def test_non_pr_runs_have_unique_concurrency_groups() -> None:
    """GitHub also cancels pending runs in a shared group when cancel is false."""
    for name in (
        "ci.yml",
        "windows-security.yml",
        "discovery-parity.yml",
        "secret-scan.yml",
        "scorecard.yml",
        "release.yml",
    ):
        concurrency = workflow(name)["concurrency"]
        assert (
            "github.event.pull_request.number || github.run_id" in concurrency["group"]
        )
        assert (
            concurrency["cancel-in-progress"]
            == "${{ github.event_name == 'pull_request' }}"
        )
