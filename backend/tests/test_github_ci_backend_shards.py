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
    # Runs after test failures, but not when Python never got set up
    # (a failed checkout would otherwise add a misleading exit 127).
    assert drop.get("if") == "always() && steps.setup-python.outcome == 'success'"
    assert any(step.get("id") == "setup-python" for step in backend["steps"])
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


def _path_filters() -> str:
    """Return the paths-filter document from the changes job."""
    changes = load_ci_jobs()["changes"]
    for step in changes["steps"]:
        if step.get("id") == "filter":
            filters = step["with"]["filters"]
            assert isinstance(filters, str)
            return filters
    raise AssertionError("changes job has no paths filter")


def _filter_patterns(filters: str, name: str) -> list[str]:
    """Return the include and exclude globs for one named filter."""
    patterns: list[str] = []
    collecting = False
    for line in filters.splitlines():
        if line.startswith(f"{name}:"):
            collecting = True
            continue
        if collecting and line and not line[:1].isspace() and line.endswith(":"):
            break
        if not collecting:
            continue
        stripped = line.strip()
        if stripped.startswith("- "):
            patterns.append(stripped[2:].strip("'\""))
    return patterns


def test_docs_lock_does_not_select_backend_shards() -> None:
    """The mkdocs lock must not schedule the backend shards.

    ``requirements/docs.txt`` is the docs build lock. Backend shards install
    ``.github/requirements/app-dev.txt``. Requirements Resolve still sees the
    docs lock, because that job is the one that checks hash-pinned locks.
    """
    filters = _path_filters()
    backend = _filter_patterns(filters, "backend")
    requirements = _filter_patterns(filters, "requirements")
    assert "!requirements/docs.in" in backend
    assert "!requirements/docs.txt" in backend
    assert "requirements/**" in requirements
    assert "!requirements/docs.in" not in requirements
    assert "!requirements/docs.txt" not in requirements
    # `some` treats `!path` as a match for every other file, so a negation
    # only excludes when the step uses some-with-excludes.
    filter_step = next(
        step
        for step in load_ci_jobs()["changes"]["steps"]
        if step.get("id") == "filter"
    )
    has_negation = any(pattern.startswith("!") for pattern in backend)
    if has_negation:
        assert filter_step["with"].get("predicate-quantifier") == "some-with-excludes"


def test_ci_does_not_publish_container_images() -> None:
    """CI must not push images. Release.yml does that, on version tags only.

    The old ``build-and-push`` job published after the coverage floor. A
    shard pass is no longer an image publish, so the job and its registry
    login are gone.
    """
    jobs = load_ci_jobs()
    assert "build-and-push" not in jobs
    for name, job in jobs.items():
        permissions = job.get("permissions") or {}
        assert "packages" not in permissions, name
        for step in job.get("steps") or []:
            uses = str(step.get("uses") or "")
            assert "docker/build-push-action" not in uses, name
            assert "docker/login-action" not in uses, name


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
