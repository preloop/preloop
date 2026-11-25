"""Exercise the backend runner picker and its hosted fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ci_workflow import load_ci_jobs, step_script

HOSTED_JOBS = (
    "test-backend-coverage",
    "test-frontend",
    "test-runtime-plugins",
)
PINNED_JOBS = (
    "requirements-resolve",
    "lint",
    "helm-lint",
    "cli-vuln-scan",
    *HOSTED_JOBS,
)
TEST_JOBS = ("test-backend", *HOSTED_JOBS)


def _pick_script() -> str:
    """Return the run script of the pick-runner decision step."""
    for step in load_ci_jobs()["pick-runner"]["steps"]:
        if step.get("id") == "pick":
            script = step["run"]
            assert isinstance(script, str)
            return script
    raise AssertionError("pick-runner has no step with id 'pick'")


def test_backend_shards_route_through_pick_runner_plan() -> None:
    """Every group reads the same runner class, including the serial group."""
    backend = load_ci_jobs()["test-backend"]
    assert "matrix." not in backend["runs-on"]
    assert "fromJSON(needs.pick-runner.outputs.backend_runner)" in backend["runs-on"]
    assert "pick-runner" in backend["needs"]
    assert "container" not in backend
    # GitHub expressions have no arithmetic. `matrix.group - 1` makes the
    # workflow file invalid and no job starts.
    assert "matrix.group - 1" not in str(backend)


def test_extra_suites_stay_on_hosted_and_do_not_wait() -> None:
    """Frontend, plugins, and coverage must not serialize behind private VMs."""
    jobs = load_ci_jobs()
    for name in HOSTED_JOBS:
        job = jobs[name]
        assert job["runs-on"] == "ubuntu-24.04", name
        assert "pick-runner" not in job["needs"], name


def test_non_test_jobs_stay_on_their_own_runners() -> None:
    """Code-quality jobs and the Windows CLI job are not rerouted."""
    jobs = load_ci_jobs()
    for name in PINNED_JOBS:
        assert jobs[name]["runs-on"] == "ubuntu-24.04", name
    assert jobs["test-cli-windows"]["runs-on"] == "windows-latest"


def test_helm_checks_install_pinned_helm_before_rendering() -> None:
    """Use the pinned Helm version rather than the runner image default."""
    steps = load_ci_jobs()["helm-lint"]["steps"]
    setup_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Set up Helm"
    )
    setup = steps[setup_index]
    assert setup["uses"] == (
        "azure/setup-helm@2f5e7bc67f1f9972ab16657bb795f36d8f04de0e"
    )
    assert setup["with"]["version"] == "v3.19.0"
    assert setup_index < next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Helm render checks (chart lint + CNPG backup wiring)"
    )


def test_bootstrap_and_aggregation_do_not_depend_on_optional_capacity() -> None:
    """An offline spot pool must not prevent the fallback picker from running."""
    jobs = load_ci_jobs()
    for name in ("changes", "pick-runner", "ci"):
        assert jobs[name]["runs-on"] == "ubuntu-24.04", name
    pick = jobs["pick-runner"]
    assert (
        pick["outputs"]["backend_runner"] == "${{ steps.pick.outputs.backend_runner }}"
    )
    assert "timeout-minutes" in pick


def test_pick_runner_falls_back_to_the_public_runner() -> None:
    """Missing secret, disabled variable, or API error all mean ubuntu-24.04."""
    script = _pick_script()
    pick = load_ci_jobs()["pick-runner"]
    assert "CI_SELF_HOSTED_TESTS" in str(pick)
    assert '"${SELF_HOSTED_TESTS:-false}" != "true"' in script
    assert "TRUSTED_SOURCE" in str(pick)
    assert "github.event.pull_request.head.repo.full_name == github.repository" in str(
        pick
    )
    assert "dependabot[bot]" in str(pick)
    assert '-z "${RUNNER_READ_TOKEN:-}"' in script
    # A failed API call must not abort the step.
    assert "could not list repository runners" in script
    assert "could not parse the runner list" in script
    # `set -e` would turn any of the above into a red required check.
    assert "set -e" not in script
    # Fallback is hosted runners with no nested container. Postgres is
    # decided later by scripts/ci_postgres.py, not by this plan.
    assert "container" not in script
    assert "bookworm" not in script
    assert "postgres_ports" not in script
    # All-or-nothing self-hosted routing is what made CI slower.
    assert 'pick "$SELF_HOSTED"' not in script
    assert "emit 0 " in script


def test_pick_runner_requires_an_idle_matching_runner() -> None:
    """Only an online, not-busy runner carrying all four labels counts."""
    script = _pick_script()
    assert '["self-hosted","Linux","X64","preloop-ci-general"]' in script
    assert '.status == "online"' in script
    assert ".busy == false" in script
    for label in ("self-hosted", "Linux", "X64"):
        assert f'index("{label}")' in script
    # One runner class per run: all shards private, or all hosted.
    assert 'SPLITS="${BACKEND_SHARDS:-}"' in script
    assert "$idle / $SPLITS" in script
    assert "/ 8" not in script


def test_ci_aggregator_fails_when_pick_runner_fails() -> None:
    """A crashed pick must not read as a path-filter skip on the required check."""
    aggregator = load_ci_jobs()["ci"]
    assert "pick-runner" in aggregator["needs"]
    for step in aggregator["steps"]:
        if step.get("name") == "Require every suite to have passed or been skipped":
            assert "check pick-runner" in step["run"]
            assert step["env"]["PICKRUNNER"] == "${{ needs.pick-runner.result }}"
            return
    raise AssertionError("ci aggregator has no result-check step")


RECLAIM_STEP = "Reclaim self-hosted workspace ownership"


def test_workspace_reclaim_runs_before_checkout_on_self_hosted_only() -> None:
    """Root-owned residue in _work must be fixed before checkout, and sudo
    is only ever non-interactive (``sudo -n``), behind a ``sudo -n true``
    probe, so a VM without passwordless sudo falls back to docker."""
    steps = load_ci_jobs()["test-backend"]["steps"]
    assert steps[0].get("name") == RECLAIM_STEP
    checkout_index = next(
        i for i, step in enumerate(steps) if "actions/checkout@" in step.get("uses", "")
    )
    assert checkout_index > 0
    reclaim = steps[0]
    assert reclaim.get("if") == "runner.environment == 'self-hosted'"
    script = reclaim["run"]
    assert 'dirname "$RUNNER_TEMP"' in script
    assert "if sudo -n true" in script
    assert "sudo " not in script.replace("sudo -n", "")
    assert "pgvector/pgvector:pg16" in script
    # Both ownership probes tolerate find errors, so an unreadable path
    # cannot hide a foreign-owned one from the re-check.
    probes = [line for line in script.splitlines() if "find " in line]
    assert len(probes) == 2
    assert all("|| true" in line for line in probes)


def test_root_only_steps_are_gated_to_the_github_image() -> None:
    """sudo/apt steps must not run on a VM whose user may have no sudo."""
    jobs = load_ci_jobs()
    for name in TEST_JOBS:
        for step in jobs[name]["steps"]:
            script = step.get("run", "")
            if not isinstance(script, str):
                continue
            if step.get("name") == RECLAIM_STEP:
                continue
            if "sudo" in script or "apt-get" in script:
                assert step.get("if") == "runner.environment == 'github-hosted'", (
                    f"{name}: step {step.get('name')!r} needs root but is not gated"
                )


def test_test_jobs_are_bounded_and_start_clean() -> None:
    """A reused self-hosted workspace gets wiped, and no job can hang forever."""
    jobs = load_ci_jobs()
    for name in TEST_JOBS:
        job = jobs[name]
        assert isinstance(job["timeout-minutes"], int), name
        checkout = next(
            step
            for step in job["steps"]
            if str(step.get("uses", "")).startswith("actions/checkout@")
        )
        assert checkout["with"]["clean"] is True, name


def test_backend_postgres_reuses_a_listening_instance() -> None:
    """Shards do not start a service container before checking the machine."""
    pick = load_ci_jobs()["pick-runner"]
    assert pick["outputs"]["backend_runner"] == (
        "${{ steps.pick.outputs.backend_runner }}"
    )
    script = _pick_script()
    assert "bookworm" not in script

    backend = load_ci_jobs()["test-backend"]
    assert "services" not in backend
    assert "container" not in backend
    prepare = step_script(backend, "Prepare Postgres")
    assert "scripts/ci_postgres.py prepare" in prepare
    assert "DATABASE_URL" not in backend["env"]
    for name in HOSTED_JOBS:
        assert "container" not in load_ci_jobs()[name]
        assert "services" not in load_ci_jobs()[name]


@pytest.mark.parametrize(
    ("case", "expected", "shards"),
    [
        ("available", 0, "6"),
        ("partial", 0, "6"),
        ("offline", 0, "6"),
        ("absent", 0, "6"),
        ("busy", 0, "6"),
        ("dedicated", 0, "6"),
        ("unlabeled", 0, "6"),
        ("hosted-alias", 0, "6"),
        ("fork", 0, "6"),
        ("api-error", 0, "6"),
        ("malformed", 0, "6"),
        ("disabled", 0, "6"),
        ("missing-token", 0, "6"),
        ("standby", 0, "6"),
        ("irrelevant", 0, "6"),
        ("many", 7, "7"),
        ("unset", 0, "2"),
        ("partial", 0, "2"),
        # A narrow selection runs fewer shards, so three idle VMs cover all.
        ("available", 2, "2"),
        ("available", 3, "3"),
        # Malformed counts cannot opt an unknown matrix into private capacity.
        ("many", 0, "not-a-number"),
    ],
)
def test_real_selector_uses_available_capacity(
    tmp_path: Path, case: str, expected: int, shards: str
) -> None:
    """Execute the workflow's actual selector against synthetic API responses."""
    if not shutil.which("jq"):
        pytest.skip("jq required to execute workflow shell")
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import json, os, sys
case = os.environ['CASE']
# Capacity routing must not wait for unrelated hosted queue pressure.
assert sys.argv[2] == 'repos/example/repository/actions/runners'
if case == 'api-error':
    sys.exit(1)
if case == 'malformed':
    print('invalid json')
    sys.exit(0)
labels = ['self-hosted', 'Linux', 'X64', 'preloop-ci-general']
if case == 'dedicated':
    labels.append('gce-tests-only')
if case in {'unlabeled', 'hosted-alias'}:
    labels.remove('preloop-ci-general')
if case == 'hosted-alias':
    labels.append('ubuntu-latest')
runner = dict(name='deb1' if case == 'standby' else 'general', status='offline' if case == 'offline' else 'online',
              busy=case == 'busy', labels=[{'name': name} for name in labels])
runners = [] if case == 'absent' else [runner] * (30 if case == 'many' else 3)
if case == 'partial':
    runners = [runner, dict(runner, status='offline'), dict(runner, busy=True)]
print(json.dumps({'runners': runners}))
""")
    gh.chmod(0o755)
    output = tmp_path / "output"
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        CASE=case,
        BACKEND_REQUIRED="false" if case == "irrelevant" else "true",
        BACKEND_SHARDS=shards,
        TRUSTED_SOURCE="false" if case == "fork" else "true",
        RUNNER_READ_TOKEN="" if case == "missing-token" else "synthetic",
        SELF_HOSTED_TESTS={"disabled": "false", "unset": ""}.get(case, "true"),
        REPO="example/repository",
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
    )
    result = subprocess.run(
        ["bash", "-c", _pick_script()],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    runner = json.loads(outputs["backend_runner"])
    assert runner == (
        ["self-hosted", "Linux", "X64", "preloop-ci-general"]
        if expected
        else "ubuntu-24.04"
    )
    assert int(outputs["observed_idle"]) == expected
