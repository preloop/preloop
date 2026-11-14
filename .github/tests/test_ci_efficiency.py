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
    "first,retryable,retry,expected",
    [
        ("success", "false", "skipped", 0),
        ("success", "true", "success", 0),
        ("success", "true", "failure", 1),
        ("success", "true", "skipped", 1),
        ("failure", "false", "skipped", 1),
        ("success", "", "skipped", 1),
    ],
)
def test_windows_gate_never_accepts_missing_validation(
    first: str, retryable: str, retry: str, expected: int
) -> None:
    """A fresh-runner retry must complete the same tests and actual scan."""
    job = workflow("windows-security.yml")["jobs"]["windows-security-tests"]
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", job["steps"][0]["run"]],
        env={
            **os.environ,
            "PATHS_RESULT": "success",
            "WINDOWS": "true",
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
