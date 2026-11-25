"""Guard GitHub Actions backend test sharding against config drift.

The suite is partitioned into complete files before collection, then coverage
is combined before the 60% floor. The shard count and matrix group list
must stay in lockstep, and the floor must not run on a single shard.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tomllib
from types import ModuleType

import pytest
import yaml
from pathlib import Path

from tests.ci_workflow import REPO_ROOT, load_ci_jobs, step_script

PYPROJECT = REPO_ROOT / "pyproject.toml"

BACKEND_TEST_SPLITS = 6


def test_pytest_plugins_are_dev_dependencies() -> None:
    """CI installs ``.[dev]`` from the hash-pinned lock, so the plugins must be there."""
    with PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(item.startswith("pytest-split") for item in dev)
    assert any(item.startswith("pytest-xdist") for item in dev)
    lock = (REPO_ROOT / ".github/requirements/app-dev.txt").read_text()
    assert "\npytest-xdist==" in lock


def test_backend_shards_partition_before_collection() -> None:
    """Each matrix group must match the pre-collection file partition."""
    backend = load_ci_jobs()["test-backend"]
    groups = backend["strategy"]["matrix"]["group"]
    # The selection sizes the matrix: BACKEND_TEST_SPLITS for the whole suite.
    assert groups == "${{ fromJSON(needs.changes.outputs.backend_groups) }}"
    assert backend["name"] == (
        "Backend Tests (${{ matrix.group }}/${{ needs.changes.outputs.backend_jobs }})"
    )
    assert backend["strategy"]["fail-fast"] is False

    script = step_script(backend, "Run tests")
    assert "scripts/run_backend_shard.py run --plan backend-shard-plan.json" in script
    assert '--group "$SHARD_GROUP"' in script
    assert '--workers "$SHARD_WORKERS"' in script
    run_env = next(s for s in backend["steps"] if s.get("name") == "Run tests")["env"]
    assert run_env["SHARD_WORKERS"] == (
        "${{ runner.environment == 'self-hosted' && '2' || 'auto' }}"
    )
    assert "backend-plan" in backend["needs"]
    assert "needs.backend-plan.result == 'success'" in backend["if"]
    assert "--splits" not in script
    assert "--splitting-algorithm" not in script
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
    # A hang bound sized for the slower self-hosted pool, not a speed target.
    assert backend["timeout-minutes"] <= 25
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
    assert '-ne "$SHARDS"' in script
    assert 'if [ "$BACKEND_FULL" = "true" ]' in script
    step = next(
        s
        for s in coverage["steps"]
        if s.get("name") == "Combine coverage and enforce floor"
    )
    assert step["env"] == {
        "SHARDS": "${{ needs.changes.outputs.backend_jobs }}",
        "BACKEND_FULL": "${{ needs.changes.outputs.backend_full }}",
    }
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
    policy = json.loads((REPO_ROOT / ".github/ci-policy.json").read_text())
    return yaml.safe_dump(policy["legacy"])


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


def test_docs_lock_exclusion_kept_for_comparison_and_full_override() -> None:
    """The legacy filter still excludes the mkdocs lock; the override wins.

    The legacy backend filter keeps its docs-lock exclusion so the comparison
    decision is unchanged, but ``requirements/**`` is a full-suite path, so a
    docs-lock change now runs the full tier (approved dependency override).

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
    # These exclusions are preserved for comparison. The approved dependency
    # override now expands docs-lock changes to the full validation tier.
    policy = json.loads((REPO_ROOT / ".github/ci-policy.json").read_text())
    assert "requirements/**" in policy["sensitive"]


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
        assert permissions.get("packages") != "write", name
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


def test_browser_profile_does_not_require_sudo_on_self_hosted() -> None:
    """Self-hosted jobs install browsers, while host setup owns OS packages."""
    job = load_ci_jobs()["test-browser-profile"]
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Install pinned Playwright MCP and Chromium"
    )
    assert step["env"]["WITH_DEPS"] == (
        "${{ runner.environment == 'github-hosted' && '--with-deps' || '' }}"
    )
    assert "npm ci" in step["run"]
    assert "npx playwright install $WITH_DEPS chromium" in step["run"]
    assert "PRELOOP_BROWSER_CI" in str(job)


def test_subscription_compatibility_installs_a_verified_lock() -> None:
    """The extra SQLAlchemy version must retain CI's pinned/hash-verified installs."""
    backend = load_ci_jobs()["test-backend"]
    step = next(
        step
        for step in backend["steps"]
        if step.get("name") == "Check subscription aggregates with current SQLAlchemy"
    )
    assert step["if"] == "matrix.group == 1"
    install = step["run"]
    assert "--require-hashes -r .github/requirements/sqlalchemy-21.txt" in install
    assert "test_subscription_columns_drivers.py" in install
    version = _pinned_version(
        REPO_ROOT / ".github" / "requirements" / "sqlalchemy-21.txt", "sqlalchemy"
    )
    assert version is not None and version.startswith("2.1.")
    paths = _filter_patterns(_path_filters(), "backend")
    assert ".github/requirements/sqlalchemy-21.in" in paths
    assert ".github/requirements/sqlalchemy-21.txt" in paths


def test_backend_plan_job_checks_predicted_balance() -> None:
    """One plan per run, from the newest main durations, gated at 1.15x."""
    plan = load_ci_jobs()["backend-plan"]
    restore = next(s for s in plan["steps"] if s.get("id") == "durations")
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"]["restore-keys"] == "pytest-durations-"
    script = step_script(plan, "Plan shards and check predicted balance")
    assert '--shards "$SHARDS"' in script
    assert "--selection ci-selection.json" in script
    assert "ratio=1.15" in script
    assert '--max-ratio "$ratio"' in script
    step = next(
        s
        for s in plan["steps"]
        if s.get("name") == "Plan shards and check predicted balance"
    )
    assert step["env"]["SHARDS"] == "${{ needs.changes.outputs.backend_shards }}"
    assert step["env"]["BACKEND_FULL"] == ("${{ needs.changes.outputs.backend_full }}")
    names = [s.get("name") for s in plan["steps"]]
    assert names.index("Download test selection") < names.index(
        "Plan shards and check predicted balance"
    )
    assert ".github/backend-shard-durations.json" in script


def test_main_runs_refresh_durations_for_pull_requests() -> None:
    """Only green main runs write the durations cache that PRs restore."""
    job = load_ci_jobs()["backend-durations"]
    assert "needs.test-backend.result == 'success'" in job["if"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert "pull_request" not in job["if"]
    # A selective run times only its own files; never let it be the cache.
    assert "needs.changes.outputs.backend_full == 'true'" in job["if"]
    script = step_script(job, "Convert JUnit times to node durations")
    assert '--shards "$SHARDS"' in script
    convert = next(
        s for s in job["steps"] if s.get("name", "").startswith("Convert JUnit")
    )
    assert convert["env"]["SHARDS"] == "${{ needs.changes.outputs.backend_jobs }}"
    save = next(
        s for s in job["steps"] if s.get("name") == "Save durations for pull requests"
    )
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"]["key"].startswith("pytest-durations-")


def test_coverage_job_summarises_actual_shard_durations() -> None:
    coverage = load_ci_jobs()["test-backend-coverage"]
    script = step_script(coverage, "Summarise actual shard durations")
    assert '--shards "$SHARDS" --summary' in script
    step = next(
        s
        for s in coverage["steps"]
        if s.get("name") == "Summarise actual shard durations"
    )
    assert step["env"]["SHARDS"] == "${{ needs.changes.outputs.backend_jobs }}"


def _load_refresh_durations() -> ModuleType:
    """Import ``scripts/ci_refresh_durations.py`` without a package."""
    path = REPO_ROOT / "scripts" / "ci_refresh_durations.py"
    spec = importlib.util.spec_from_file_location("ci_refresh_durations", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_shard_report(directory: Path, shard: int, seconds: float) -> None:
    """Write a passing JUnit report the way a backend shard uploads it."""
    (directory / f"test-results.{shard}.xml").write_text(
        f'<testsuites><testsuite name="pytest" time="{seconds}">'
        '<testcase classname="backend.tests.test_github_ci_backend_shards" '
        'name="test_pytest_plugins_are_dev_dependencies" time="0.5"/>'
        "</testsuite></testsuites>"
    )


@pytest.mark.parametrize("shards", [1, BACKEND_TEST_SPLITS])
def test_refresh_durations_reads_one_shard_and_many(
    shards: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A selective one-shard run must summarise like a whole-suite run.

    download-artifact extracts a lone pattern match without its artifact
    directory, so CI merges every shard into one flat directory.
    """
    module = _load_refresh_durations()
    for shard in range(1, shards + 1):
        _write_shard_report(tmp_path, shard, 60.0 if shard == shards else 10.0)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ci_refresh_durations.py", str(tmp_path), "--shards", str(shards)]
        + ["--summary"],
    )
    module.main()
    out = capsys.readouterr().out
    assert f"- shard {shards}: 60.0s" in out
    assert out.count("- shard ") == shards
    median = 60.0 if shards == 1 else 10.0
    assert f"max / median {median:.1f}s = {60 / median:.3f}" in out


def test_zero_median_has_no_duration_ratio(tmp_path: Path) -> None:
    """Two zero-duration reports make the median zero even with a slow shard."""
    for shard, seconds in enumerate((0.0, 0.0, 3.0), start=1):
        _write_shard_report(tmp_path, shard, seconds)
    summary = _load_refresh_durations().summarise(sorted(tmp_path.glob("*.xml")))
    assert "max 3.0s / mean 1.0s = 3.000" in summary
    assert "max / median" not in summary


def test_refresh_durations_rejects_a_missing_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shard that uploaded nothing must still fail the summary."""
    module = _load_refresh_durations()
    _write_shard_report(tmp_path, 1, 10.0)
    _write_shard_report(tmp_path, 3, 30.0)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ci_refresh_durations.py", str(tmp_path), "--shards", "3", "--summary"],
    )
    with pytest.raises(ValueError, match="exactly one report from each of 3"):
        module.main()


def test_shard_artifact_downloads_merge_into_one_layout() -> None:
    """Every pattern download must not depend on how many artifacts match."""
    jobs = load_ci_jobs()
    backend = jobs["test-backend"]
    run = step_script(backend, "Run tests")
    assert '--junitxml="test-results.$SHARD_GROUP.xml"' in run
    upload = next(s for s in backend["steps"] if s.get("name") == "Upload test results")
    assert upload["with"]["path"] == "test-results.${{ matrix.group }}.xml"
    downloads = [
        step
        for job in jobs.values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/download-artifact@")
        and str(step.get("with", {}).get("pattern", "")).startswith("backend-")
    ]
    assert len(downloads) == 3
    for step in downloads:
        assert step["with"].get("merge-multiple") is True, step["name"]
