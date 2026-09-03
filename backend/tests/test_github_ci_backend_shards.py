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

BACKEND_TEST_SPLITS = 8


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
    assert backend["name"] == (
        f"Backend Tests (${{{{ matrix.group }}}}/{BACKEND_TEST_SPLITS})"
    )
    assert backend["strategy"]["fail-fast"] is False

    script = _step_script(backend, "Run tests")
    assert f"--splits {BACKEND_TEST_SPLITS}" in script
    assert "--group ${{ matrix.group }}" in script
    assert "--splitting-algorithm=duration_based_chunks" in script
    assert "--splitting-algorithm=least_duration" not in script
    assert "--cov-fail-under" not in script
    coverage_upload = next(
        step for step in backend["steps"] if step.get("name") == "Upload coverage data"
    )
    assert "always()" not in str(coverage_upload.get("if", ""))
    assert backend["env"]["COVERAGE_FILE"] == "coverage-data.${{ matrix.group }}"
    assert backend["env"]["PRELOOP_DISABLE_TELEMETRY"] == "true"


def test_backend_coverage_job_combines_shards_before_floor() -> None:
    """The 60% floor applies only to the combined coverage data."""
    jobs = _load_ci_jobs()
    coverage = jobs["test-backend-coverage"]
    assert coverage["needs"] == "test-backend"

    install = _step_script(coverage, "Install coverage")
    assert ".github/requirements/coverage.txt" in install
    assert "--require-hashes" in install

    script = _step_script(coverage, "Combine coverage and enforce floor")
    assert "coverage combine" in script
    assert "--fail-under=60" in script
    assert f"-ne {BACKEND_TEST_SPLITS}" in script
    assert "coverage-data.*" in script


def test_coverage_lock_matches_app_dev_lock() -> None:
    """coverage.txt must pin the same coverage.py version as app-dev.txt.

    The combine/report job reads data files written by the shards, which use
    app-dev.txt; a version mismatch can make ``coverage combine`` reject or
    misread them.
    """
    coverage_version = _pinned_version(
        REPO_ROOT / ".github" / "requirements" / "coverage.txt", "coverage"
    )
    app_dev_version = _pinned_version(
        REPO_ROOT / ".github" / "requirements" / "app-dev.txt", "coverage"
    )
    assert coverage_version is not None, "coverage.txt must pin coverage==<version>"
    assert app_dev_version is not None, "app-dev.txt must pin coverage==<version>"
    assert coverage_version == app_dev_version


def _pinned_version(lock_path: Path, package: str) -> str | None:
    """Return the pinned ``package==x.y.z`` version from a requirements lock."""
    prefix = f"{package}=="
    with lock_path.open(encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Requirement lines may be backslash-continued; the first physical
            # line holds the name==version specifier.
            requirement = stripped.split("\\")[0].strip()
            if requirement.startswith(prefix):
                return requirement[len(prefix) :].strip()
    return None


def test_build_and_push_waits_for_combined_backend_coverage() -> None:
    """Image publish must not proceed on a shard pass with incomplete coverage."""
    needs = _load_ci_jobs()["build-and-push"]["needs"]
    assert "test-backend-coverage" in needs
    assert "test-backend" not in needs
