"""Guard GitHub Actions backend test sharding against config drift.

The suite is split with pytest-split across a matrix of jobs, then coverage
is combined before the 60% floor. ``--splits`` and the matrix group list
must stay in lockstep, and the floor must not run on a single shard.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from tests.ci_workflow import REPO_ROOT, load_ci_jobs, step_script

PYPROJECT = REPO_ROOT / "pyproject.toml"

BACKEND_TEST_SPLITS = 18


def test_pytest_split_is_a_dev_dependency() -> None:
    """CI installs ``.[dev]`` from the hash-pinned lock, so the plugin must be there."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("pytest-split") for item in dev)


def test_backend_shards_partition_with_pytest_split() -> None:
    """Each matrix group must match ``--splits N --group`` in the pytest invocation."""
    backend = load_ci_jobs()["test-backend"]
    groups = backend["strategy"]["matrix"]["group"]
    assert groups == list(range(1, BACKEND_TEST_SPLITS + 1))
    assert backend["name"] == (
        f"Backend Tests (${{{{ matrix.group }}}}/{BACKEND_TEST_SPLITS})"
    )
    assert backend["strategy"]["fail-fast"] is False

    script = step_script(backend, "Run tests")
    assert f"--splits {BACKEND_TEST_SPLITS}" in script
    assert "--group ${{ matrix.group }}" in script
    assert "--splitting-algorithm=duration_based_chunks" in script
    assert "--durations-path .github/pytest-split-durations.json" in script
    assert "--splitting-algorithm=least_duration" not in script
    assert "--cov-fail-under" not in script
    prepare = step_script(backend, "Prepare Postgres")
    assert "scripts/ci_postgres.py prepare" in prepare
    drop = next(
        step for step in backend["steps"] if step.get("name") == "Drop CI database"
    )
    assert drop.get("if") == "always()"
    assert "scripts/ci_postgres.py drop" in drop["run"]
    assert "services" not in backend
    assert backend["timeout-minutes"] <= 15
    coverage_upload = next(
        step for step in backend["steps"] if step.get("name") == "Upload coverage data"
    )
    assert "always()" not in str(coverage_upload.get("if", ""))
    assert backend["env"]["COVERAGE_FILE"] == "coverage-data.${{ matrix.group }}"
    assert backend["env"]["PRELOOP_DISABLE_TELEMETRY"] == "true"


def test_backend_coverage_job_combines_shards_before_floor() -> None:
    """The 60% floor applies only to the combined coverage data."""
    jobs = load_ci_jobs()
    coverage = jobs["test-backend-coverage"]
    # Coverage stays on ubuntu-latest and must not wait on pick-runner;
    # overflow routing is guarded in test_github_ci_self_hosted_runner.py.
    # What matters here is that the floor job waits for every shard.
    assert "changes" in coverage["needs"]
    assert "test-backend" in coverage["needs"]
    assert "pick-runner" not in coverage["needs"]

    install = step_script(coverage, "Install coverage")
    assert ".github/requirements/coverage.txt" in install
    assert "--require-hashes" in install

    script = step_script(coverage, "Combine coverage and enforce floor")
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
    needs = load_ci_jobs()["build-and-push"]["needs"]
    assert "test-backend-coverage" in needs
    assert "test-backend" not in needs


def test_ci_aggregator_fails_when_changes_fails() -> None:
    """A crashed path-filter job must not leave the required check green."""
    jobs = load_ci_jobs()
    aggregator = jobs["ci"]
    assert aggregator["needs"][0] == "changes"
    assert "changes" in aggregator["needs"]
    script = step_script(
        aggregator, "Require every suite to have passed or been skipped"
    )
    assert "check changes" in script
    assert "needs.changes.result" in str(aggregator)
