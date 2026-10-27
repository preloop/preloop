"""Guard the self-hosted runner routing in the GitHub CI workflow.

ubuntu-latest is the default pool. Idle private VMs take overflow backend
shards only; GitHub will wait forever on a self-hosted ``runs-on`` rather
than fall back to hosted, so sending every test job to three VMs serializes
the suite. Two properties matter enough to pin down here:

1. Routing reaches only backend shards, per group. Frontend, plugins, and
   coverage stay on ubuntu-latest and must not wait for pick-runner.
2. The fallback is unconditional. ``pick-runner`` must never fail the
   workflow and must never leave ``backend_plan`` unset: a missing secret,
   a token without ``administration: read``, or an API error all have to
   land on eighteen hosted shards. Otherwise an unrelated PR goes red over
   CI plumbing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.ci_workflow import load_ci_jobs, step_script
from tests.test_github_ci_backend_shards import BACKEND_TEST_SPLITS

BACKEND_SHARD = "[matrix.group]"
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


def _hosted_slot() -> dict[str, Any]:
    """Public ubuntu-latest shard. Postgres is the machine's, not a service."""
    return {"runner": "ubuntu-latest"}


def _overflow_slot(pyver: str = "3.11") -> dict[str, Any]:
    """Idle self-hosted shard. Same machine Postgres, no nested container.

    ``pyver`` is unused. The VM supplies Python 3.11; a nested
    ``python:3.11-bookworm`` would hide ``127.0.0.1:5432``.
    """
    del pyver
    return {"runner": ["self-hosted", "Linux", "X64", "preloop-ci-general"]}


def _backend_plan(idle: int, pyver: str = "3.11") -> list[dict[str, Any] | None]:
    """Hosted-first overflow: the last ``idle`` shards go to private VMs.

    GitHub expressions cannot subtract, so the array is 1-based: a dummy
    ``null`` at index 0 lets ``[matrix.group]`` address groups 1 through
    the shard count. Mirrors pick-runner's jq in Python so GitLab's unit
    image (no ``jq``) can still pin the routing.
    """
    idle = max(0, min(int(idle), BACKEND_TEST_SPLITS // 2))
    threshold = BACKEND_TEST_SPLITS - idle
    shards: list[dict[str, Any] | None] = [None]
    shards.extend(
        _overflow_slot(pyver) if index >= threshold else _hosted_slot()
        for index in range(BACKEND_TEST_SPLITS)
    )
    return shards


def test_backend_shards_route_through_pick_runner_plan() -> None:
    """Each backend shard reads its own slot from backend_plan."""
    backend = load_ci_jobs()["test-backend"]
    assert BACKEND_SHARD in backend["runs-on"]
    assert "backend_plan" in backend["runs-on"]
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
        assert job["runs-on"] == "ubuntu-latest", name
        assert "pick-runner" not in job["needs"], name


def test_non_test_jobs_stay_on_their_own_runners() -> None:
    """Code-quality jobs and the Windows CLI job are not rerouted."""
    jobs = load_ci_jobs()
    for name in PINNED_JOBS:
        assert jobs[name]["runs-on"] == "ubuntu-latest", name
    assert jobs["test-cli-windows"]["runs-on"] == "windows-latest"


def test_helm_checks_install_pinned_helm_before_rendering() -> None:
    """An ubuntu-latest label does not guarantee Helm on a self-hosted runner."""
    steps = load_ci_jobs()["helm-lint"]["steps"]
    setup_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Set up Helm"
    )
    setup = steps[setup_index]
    assert setup["uses"] == (
        "azure/setup-helm@9bc31f4ebc9c6b171d7bfbaa5d006ae7abdb4310"
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
        assert jobs[name]["runs-on"] == "ubuntu-latest", name
    pick = jobs["pick-runner"]
    assert pick["outputs"]["backend_plan"] == "${{ steps.pick.outputs.backend_plan }}"
    assert "timeout-minutes" in pick


def test_pick_runner_falls_back_to_the_public_runner() -> None:
    """Missing secret, disabled variable, or API error all mean ubuntu-latest."""
    script = _pick_script()
    pick = load_ci_jobs()["pick-runner"]
    assert "CI_SELF_HOSTED_TESTS" in str(pick)
    assert '"${SELF_HOSTED_TESTS:-true}" = "false"' in script
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
    """Only an online, not-busy runner carrying all three labels counts."""
    script = _pick_script()
    assert '["self-hosted","Linux","X64","preloop-ci-general"]' in script
    assert '.status == "online"' in script
    assert ".busy == false" in script
    for label in ("self-hosted", "Linux", "X64"):
        assert f'index("{label}")' in script
    # Hosted first; idle VMs take the tail of the matrix.
    assert "SPLITS=18" in script
    assert "$idle / $SPLITS" in script
    assert "/ 8" not in script
    assert "range(0; $splits)" in script
    assert ". >= ($splits - $idle)" in script
    # Dummy at [0] so YAML can index with matrix.group (no minus).
    assert "[null] +" in script
    assert "]*18" in script


def test_three_idle_runners_only_overflow_the_last_three_shards() -> None:
    """Three VMs take shards 16-18; groups 1-15 stay hosted."""
    plan = _backend_plan(3)
    assert plan[0] is None
    assert len(plan) == BACKEND_TEST_SPLITS + 1
    hosted = plan[1:16]
    overflow = plan[16:]
    assert all(slot["runner"] == "ubuntu-latest" for slot in hosted)
    assert all("container" not in slot for slot in hosted)
    assert all(
        slot["runner"] == ["self-hosted", "Linux", "X64", "preloop-ci-general"]
        for slot in overflow
    )
    assert all("container" not in slot for slot in overflow)


def test_zero_idle_runners_keeps_every_shard_on_hosted() -> None:
    """No private capacity means the matrix matches pre-self-hosted CI."""
    plan = _backend_plan(0)
    assert plan[0] is None
    assert len(plan) == BACKEND_TEST_SPLITS + 1
    shards = plan[1:]
    assert all(
        slot is not None and slot["runner"] == "ubuntu-latest" for slot in shards
    )
    assert all(slot is not None and "container" not in slot for slot in shards)


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
    assert "actions/checkout@" in steps[1].get("uses", "")
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
    assert pick["outputs"]["backend_plan"] == ("${{ steps.pick.outputs.backend_plan }}")
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
    ("case", "expected"),
    [
        ("pressure", 2),
        ("current-not-recent", 3),
        ("older-queued-run", 2),
        ("current-and-recent", 2),
        ("no-pressure", 0),
        ("young", 0),
        ("assigned", 0),
        ("waiting", 0),
        ("private-queue", 0),
        ("offline", 0),
        ("absent", 0),
        ("busy", 0),
        ("dedicated", 0),
        ("unlabeled", 0),
        ("fork", 0),
        ("api-error", 0),
        ("malformed", 0),
        ("disabled", 0),
        ("missing-token", 0),
        ("inventory-error", 0),
        ("jobs-error", 0),
        ("inventory-malformed", 0),
        ("many", 9),
    ],
)
def test_real_selector_uses_optional_overflow(
    tmp_path: Path, case: str, expected: int
) -> None:
    """Execute the workflow's actual selector against synthetic API responses."""
    if not shutil.which("jq"):
        pytest.skip("jq required to execute workflow shell")
    gh = tmp_path / "gh"
    gh.write_text("""#!/usr/bin/env python3
import datetime, json, os, sys
case = os.environ['CASE']
endpoint = sys.argv[2]
if case == 'api-error':
    sys.exit(1)
if case == 'malformed':
    print('invalid json')
    sys.exit(0)
if case == 'inventory-error' and '/actions/runners' in endpoint:
    sys.exit(1)
if case == 'jobs-error' and '/jobs?' in endpoint:
    sys.exit(1)
if case == 'inventory-malformed' and '/actions/runners' in endpoint:
    print('invalid json')
    sys.exit(0)
if '/jobs?' in endpoint:
    age = 10 if case == 'young' else 300
    created = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=age)
    job = dict(status='waiting' if case == 'waiting' else 'queued',
               runner_id=7 if case == 'assigned' else 0,
               labels=['self-hosted', 'ubuntu-latest'] if case == 'private-queue' else ['ubuntu-latest'], created_at=created.strftime('%Y-%m-%dT%H:%M:%SZ'))
    jobs = [] if case == 'no-pressure' else [job] * (30 if case == 'many' else 2)
    if case == 'current-not-recent':
        jobs = [job] * 3 if '/runs/999/' in endpoint else []
    if case == 'older-queued-run':
        jobs = [job] * 2 if '/runs/456/' in endpoint else []
    print(json.dumps({'jobs': jobs}))
elif '/actions/runs?' in endpoint:
    if case == 'older-queued-run':
        print('[456]' if 'status=queued' in endpoint else '[]')
    else:
        print('[123]')
else:
    labels = ['self-hosted', 'Linux', 'X64', 'preloop-ci-general']
    if case == 'dedicated':
        labels.append('gce-tests-only')
    if case == 'unlabeled':
        labels.remove('preloop-ci-general')
    runner = dict(status='offline' if case == 'offline' else 'online',
                  busy=case == 'busy', labels=[{'name': name} for name in labels])
    runners = [] if case == 'absent' else [runner] * (30 if case == 'many' else 3)
    print(json.dumps({'runners': runners}))
""")
    gh.chmod(0o755)
    output = tmp_path / "output"
    env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        CASE=case,
        TRUSTED_SOURCE="false" if case == "fork" else "true",
        RUNNER_READ_TOKEN="" if case == "missing-token" else "synthetic",
        SELF_HOSTED_TESTS="false" if case == "disabled" else "true",
        REPO="example/repository",
        CURRENT_RUN_ID="999" if case == "current-not-recent" else "123",
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
    plan = json.loads(output.read_text().splitlines()[1])
    assert len(plan) == BACKEND_TEST_SPLITS + 1
    assert plan[0] is None
    private = [slot for slot in plan[1:] if isinstance(slot["runner"], list)]
    assert len(private) == expected
    assert all("preloop-ci-general" in slot["runner"] for slot in private)
    assert sum(slot["runner"] == "ubuntu-latest" for slot in plan[1:]) >= 9
