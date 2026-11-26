"""Exercise the backend runner picker and its hosted fallback."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.ci_workflow import load_ci_jobs, step_script

PICKER = Path(__file__).resolve().parents[2] / ".github/scripts/pick-runners.sh"
LOCAL = ["self-hosted", "Linux", "X64", "preloop-ci-local"]
GENERAL = ["self-hosted", "Linux", "X64", "preloop-ci-general"]

HOSTED_JOBS = (
    "test-backend-coverage",
    "test-frontend",
    "test-runtime-plugins",
)
PINNED_JOBS = (
    "requirements-resolve",
    "helm-lint",
    "cli-vuln-scan",
    *HOSTED_JOBS,
)
TEST_JOBS = ("test-backend", *HOSTED_JOBS)


def _pick_step(job: str) -> dict:
    """Return the picker step of a CI job."""
    for step in load_ci_jobs()[job]["steps"]:
        if step.get("id") == "pick":
            return step
    raise AssertionError(f"{job} has no step with id 'pick'")


def _pick_script() -> str:
    """Return the shared picker script both pick jobs run."""
    assert _pick_step("pick-runner")["run"].strip() == (
        "bash .github/scripts/pick-runners.sh late"
    )
    assert _pick_step("pick-light")["run"].strip() == (
        "bash .github/scripts/pick-runners.sh early"
    )
    return PICKER.read_text()


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
    for name in ("pick-light", "pick-runner", "ci"):
        assert jobs[name]["runs-on"] == "ubuntu-24.04", name
    # Light jobs run even when the light picker failed, then on hosted.
    for name in ("changes", "lint", "cli-fast"):
        assert "!cancelled()" in jobs[name]["if"], name
        assert jobs[name]["runs-on"].endswith("|| 'ubuntu-24.04' }}"), name
        assert "github.run_attempt == 1 &&" in jobs[name]["runs-on"], name
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
    assert "exit 0" in script


def test_pick_runner_requires_an_idle_matching_runner() -> None:
    """Only an online, not-busy runner carrying all four labels counts."""
    script = _pick_script()
    assert json.dumps(GENERAL, separators=(",", ":")) in script
    assert json.dumps(LOCAL, separators=(",", ":")) in script
    assert '.status == "online"' in script
    assert ".busy == false" in script
    for label in ("self-hosted", "Linux", "X64"):
        assert f'index("{label}")' in script
    # Runners are classified by label, never by name.
    assert "(.name" not in script
    assert "^deb" not in script


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


def _runner(labels: list[str], **extra: object) -> dict:
    """Return a synthetic runner registration."""
    runner: dict = {
        "name": "runner",
        "status": "online",
        "busy": False,
        "labels": [{"name": name} for name in labels],
    }
    runner.update(extra)
    return runner


def _pick(
    tmp_path: Path,
    mode: str,
    runners: list[dict] | str | None,
    **env: str,
) -> dict[str, str]:
    """Run the picker script against a fake ``gh`` and return its outputs.

    Args:
        tmp_path: Scratch directory for the fake binary and output files.
        mode: ``early`` or ``late``.
        runners: Registrations to report, raw stdout, or None for an API error.
        **env: Overrides for the picker environment.

    Returns:
        The ``GITHUB_OUTPUT`` key/value pairs.
    """
    if not shutil.which("jq"):
        pytest.skip("jq required to execute the picker")
    payload = tmp_path / "runners.json"
    if runners is None:
        body = "sys.exit(1)"
    else:
        text = runners if isinstance(runners, str) else json.dumps({"runners": runners})
        payload.write_text(text)
        body = f"print(open({str(payload)!r}).read())"
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "assert sys.argv[2] == 'repos/example/repository/actions/runners'\n"
        f"{body}\n"
    )
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    full_env = dict(
        os.environ,
        PATH=f"{tmp_path}:{os.environ['PATH']}",
        BACKEND_REQUIRED="true",
        BACKEND_SHARDS="7",
        TRUSTED_SOURCE="true",
        RUN_ATTEMPT="1",
        RUNNER_READ_TOKEN="synthetic",
        SELF_HOSTED_TESTS="true",
        LOCAL_RUNNERS="",
        REPO="example/repository",
        GITHUB_OUTPUT=str(output),
        GITHUB_STEP_SUMMARY=str(tmp_path / "summary"),
    )
    full_env.update(env)
    result = subprocess.run(
        ["bash", str(PICKER), mode],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


HOSTED = "ubuntu-24.04"


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
        ("rerun", 0, "2"),
        ("api-error", 0, "6"),
        ("malformed", 0, "6"),
        ("disabled", 0, "6"),
        ("missing-token", 0, "6"),
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
    """Execute the picker against synthetic general-pool inventories."""
    labels = list(GENERAL)
    if case == "dedicated":
        labels.append("gce-tests-only")
    if case in {"unlabeled", "hosted-alias"}:
        labels.remove("preloop-ci-general")
    if case == "hosted-alias":
        labels.append("ubuntu-latest")
    runner = _runner(labels, status="offline" if case == "offline" else "online")
    runner["busy"] = case == "busy"
    runners: list[dict] | str | None = [runner] * (30 if case == "many" else 3)
    if case == "absent":
        runners = []
    if case == "partial":
        runners = [runner, dict(runner, status="offline"), dict(runner, busy=True)]
    if case == "api-error":
        runners = None
    if case == "malformed":
        runners = "invalid json"
    outputs = _pick(
        tmp_path,
        "late",
        runners,
        BACKEND_REQUIRED="false" if case == "irrelevant" else "true",
        BACKEND_SHARDS=shards,
        TRUSTED_SOURCE="false" if case == "fork" else "true",
        RUN_ATTEMPT="2" if case == "rerun" else "1",
        RUNNER_READ_TOKEN="" if case == "missing-token" else "synthetic",
        SELF_HOSTED_TESTS={"disabled": "false", "unset": ""}.get(case, "true"),
    )
    assert json.loads(outputs["backend_runner"]) == (GENERAL if expected else HOSTED)
    assert int(outputs["observed_idle"]) == expected
    assert outputs["self_hosted"] == ("true" if expected else "false")


@pytest.mark.parametrize(
    ("general", "local", "shards", "expected"),
    [
        # Hosted by default: three local slots cannot take a seven-group set.
        (0, 3, "7", "hosted"),
        # A narrow set that fits wholly on the local slots goes there.
        (0, 3, "3", "local"),
        (0, 3, "1", "local"),
        (0, 2, "3", "hosted"),
        # The general pool wins when it can take the set.
        (3, 3, "3", "general"),
        # Never split: two general plus two local is not four of one class.
        (2, 2, "4", "hosted"),
    ],
)
def test_backend_set_goes_to_exactly_one_class(
    tmp_path: Path, general: int, local: int, shards: str, expected: str
) -> None:
    """The whole backend set lands on one runner class, hosted by default."""
    runners = [_runner(GENERAL)] * general + [_runner(LOCAL, name="deb1")] * local
    outputs = _pick(tmp_path, "late", runners, BACKEND_SHARDS=shards)
    want = {"hosted": HOSTED, "general": GENERAL, "local": LOCAL}[expected]
    assert json.loads(outputs["backend_runner"]) == want
    assert outputs["runner_class"] == expected
    assert outputs["self_hosted"] == ("false" if expected == "hosted" else "true")


def test_local_pool_does_not_need_the_general_opt_in(tmp_path: Path) -> None:
    """CI_SELF_HOSTED_TESTS gates the general pool only."""
    outputs = _pick(
        tmp_path, "late", [_runner(LOCAL)] * 3, BACKEND_SHARDS="2", SELF_HOSTED_TESTS=""
    )
    assert outputs["runner_class"] == "local"


@pytest.mark.parametrize(
    "env",
    [
        {"LOCAL_RUNNERS": "false"},
        {"TRUSTED_SOURCE": "false"},
        {"RUN_ATTEMPT": "2"},
        {"RUNNER_READ_TOKEN": ""},
    ],
)
def test_local_pool_is_skipped_when_disabled_untrusted_or_rerun(
    tmp_path: Path, env: dict[str, str]
) -> None:
    """Forks, Dependabot and reruns never reach a docker-group runner."""
    runners = [_runner(LOCAL)] * 3
    late = _pick(
        tmp_path,
        "late",
        runners,
        BACKEND_SHARDS="1",
        LEAF_FUZZ="true",
        LEAF_BROWSER_FIXTURE="true",
        **env,
    )
    early = _pick(tmp_path, "early", runners, **env)
    for key in ("backend_runner", "browser_fixture_runner", "fuzz_runner"):
        assert json.loads(late[key]) == HOSTED, key
    for key in ("changes_runner", "lint_runner", "cli_fast_runner"):
        assert json.loads(early[key]) == HOSTED, key
    assert late["local_idle"] == early["local_idle"] == "0"


@pytest.mark.parametrize(
    ("idle", "changes", "lint", "cli_fast"),
    [
        (0, HOSTED, HOSTED, HOSTED),
        (1, LOCAL, LOCAL, HOSTED),
        (2, LOCAL, LOCAL, LOCAL),
        (3, LOCAL, LOCAL, LOCAL),
    ],
)
def test_early_light_jobs_prefer_idle_local_slots(
    tmp_path: Path, idle: int, changes: object, lint: object, cli_fast: object
) -> None:
    """Changes runs alone first, so its slot is reused by Lint and CLI Fast."""
    runners = [_runner(LOCAL)] * idle + [
        _runner(LOCAL, busy=True),
        _runner(LOCAL, status="offline"),
        _runner(["self-hosted", "Linux", "X64"], name="deb9"),
    ]
    outputs = _pick(tmp_path, "early", runners)
    assert json.loads(outputs["changes_runner"]) == changes
    assert json.loads(outputs["lint_runner"]) == lint
    assert json.loads(outputs["cli_fast_runner"]) == cli_fast


@pytest.mark.parametrize(
    ("idle", "shards", "fixture", "fuzz"),
    [
        # Backend takes three, nothing left for the leaves.
        (3, "3", HOSTED, HOSTED),
        # Seven groups go hosted; leaves use the slots in priority order.
        (3, "7", LOCAL, LOCAL),
        (2, "7", LOCAL, HOSTED),
        (3, "1", LOCAL, HOSTED),
        (1, "7", LOCAL, HOSTED),
    ],
)
def test_leaves_take_local_slots_the_backend_left(
    tmp_path: Path, idle: int, shards: str, fixture: object, fuzz: object
) -> None:
    """Fuzz needs one slot per concurrent leg, so it goes whole or hosted."""
    outputs = _pick(
        tmp_path,
        "late",
        [_runner(LOCAL)] * idle,
        BACKEND_SHARDS=shards,
        LEAF_FUZZ="true",
        LEAF_BROWSER_FIXTURE="true",
    )
    assert json.loads(outputs["browser_fixture_runner"]) == fixture
    assert json.loads(outputs["fuzz_runner"]) == fuzz


def test_leaves_that_do_not_run_hold_no_slot(tmp_path: Path) -> None:
    """A skipped leaf leaves its slot to the next one."""
    outputs = _pick(
        tmp_path,
        "late",
        [_runner(LOCAL)] * 2,
        BACKEND_SHARDS="7",
        LEAF_BROWSER_FIXTURE="false",
        LEAF_FUZZ="true",
    )
    assert json.loads(outputs["fuzz_runner"]) == LOCAL
    assert "docs_runner" not in outputs


def test_leaf_workflows_accept_the_picked_runner() -> None:
    """Each leaf passes the picker output and its callee defaults to hosted."""
    jobs = load_ci_jobs()
    root = PICKER.parents[2]
    # Docs stays hosted: its lock needs Python >= 3.11.5, Debian 12 has 3.11.2.
    assert "runner" not in jobs["leaf-docs"]["with"]
    for leaf, output, workflow in (
        ("leaf-fuzz", "fuzz_runner", "fuzz"),
        ("leaf-browser-fixture", "browser_fixture_runner", "browser-fixture"),
    ):
        job = jobs[leaf]
        assert "pick-runner" in job["needs"], leaf
        assert job["with"]["runner"] == (
            "${{ github.run_attempt == 1 && "
            f"needs.pick-runner.outputs.{output} || '\"ubuntu-24.04\"' }}}}"
        )
        text = (root / f".github/workflows/{workflow}.yml").read_text()
        assert "fromJSON(inputs.runner || '\"ubuntu-24.04\"')" in text, workflow
        assert "default: '\"ubuntu-24.04\"'" in text, workflow
