"""Guard GitHub Actions backend test sharding against config drift.

The suite is split with pytest-split across a matrix of jobs, then coverage
is combined before the 60% floor. ``--splits`` and the matrix group list
must stay in lockstep, and the floor must not run on a single shard.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"

BACKEND_TEST_SPLITS = 4


def _load_ci_jobs() -> dict[str, Any]:
    """Return the jobs mapping from the GitHub CI workflow."""
    with CI_WORKFLOW.open(encoding="utf-8") as handle:
        doc = yaml.safe_load(handle)
    jobs = doc["jobs"]
    assert isinstance(jobs, dict)
    return jobs


def _step_script(job: dict[str, Any], name: str) -> str:
    """Return the run script for a named workflow step."""
    for step in job["steps"]:
        if step.get("name") == name:
            script = step["run"]
            assert isinstance(script, str)
            return script
    raise AssertionError(f"No step named {name!r}")


def test_pytest_split_is_a_dev_dependency() -> None:
    """CI installs ``.[dev]`` from the hash-pinned lock, so the plugin must be there."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("pytest-split") for item in dev)


def test_backend_shards_partition_with_pytest_split() -> None:
    """Each matrix group must match ``--splits N --group`` in the pytest invocation."""
    backend = _load_ci_jobs()["test-backend"]
    groups = backend["strategy"]["matrix"]["group"]
    assert groups == list(range(1, BACKEND_TEST_SPLITS + 1))
    assert backend["strategy"]["fail-fast"] is False

    script = _step_script(backend, "Run tests")
    assert f"--splits {BACKEND_TEST_SPLITS}" in script
    assert "--group ${{ matrix.group }}" in script
    assert "--splitting-algorithm=least_duration" in script
    assert "--cov-fail-under" not in script
    assert backend["env"]["COVERAGE_FILE"] == "coverage-data.${{ matrix.group }}"
    assert backend["env"]["PRELOOP_DISABLE_TELEMETRY"] == "true"


def test_backend_coverage_job_combines_shards_before_floor() -> None:
    """The 60% floor applies only to the combined coverage data."""
    jobs = _load_ci_jobs()
    coverage = jobs["test-backend-coverage"]
    assert coverage["needs"] == "test-backend"

    install = _step_script(coverage, "Install coverage")
    assert ".github/requirements/app-dev.txt" in install

    script = _step_script(coverage, "Combine coverage and enforce floor")
    assert "coverage combine" in script
    assert "--fail-under=60" in script
    assert f"-ne {BACKEND_TEST_SPLITS}" in script
    assert "coverage-data.*" in script


def test_build_and_push_waits_for_combined_backend_coverage() -> None:
    """Image publish must not proceed on a shard pass with incomplete coverage."""
    needs = _load_ci_jobs()["build-and-push"]["needs"]
    assert "test-backend-coverage" in needs
    assert "test-backend" not in needs
