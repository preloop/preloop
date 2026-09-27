"""The environment image ships a ready backend test venv (no network)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ENV_DIR = REPO / "environments" / "preloop"
DOCKERFILE = ENV_DIR / "Dockerfile"
SMOKE = ENV_DIR / "python-venv-smoke.sh"
RUNNER = ENV_DIR / "bin" / "preloop-pytest"
FRONTEND_DEPS = ENV_DIR / "bin" / "preloop-frontend-deps"
# Set to a built image (tag or digest) to run the smoke inside it.
IMAGE = os.environ.get("PRELOOP_ENVIRONMENT_IMAGE", "")


def test_venv_installs_the_ci_backend_lock_with_hashes() -> None:
    """The venv installs the same hashed lock as the CI backend shards.

    A second, smaller install set drifted from the backend lock and left
    the reviewer without pytest, so the image reads app-dev.txt directly.
    """
    text = DOCKERFILE.read_text()
    assert "COPY .github/requirements/app-dev.txt /opt/preloop-pip/app-dev.txt" in text
    assert "--require-hashes" in text
    assert "-r /opt/preloop-pip/app-dev.txt" in text
    assert "/opt/preloop-tests/bin:${PATH}" in text
    assert not (ENV_DIR / "requirements.txt").exists()
    dockerignore = (REPO / ".dockerignore").read_text().splitlines()
    assert "!.github/requirements/app-dev.txt" in dockerignore
    assert dockerignore.index("!.github/requirements/app-dev.txt") > dockerignore.index(
        ".github"
    )


def test_image_builds_pgvector_that_has_subvector() -> None:
    """Migrations call subvector(); Ubuntu 24.04's pgvector 0.6.0 lacks it."""
    text = DOCKERFILE.read_text()
    assert "PGVECTOR_VERSION=0.8.6" in text
    assert "sha256sum -c -" in text
    assert 'OPTFLAGS=""' in text


def test_image_runs_the_smoke_and_ships_the_runners() -> None:
    """The build gate runs the smoke; both runners land on PATH."""
    text = DOCKERFILE.read_text()
    assert "RUN bash /opt/preloop-pip/python-venv-smoke.sh" in text
    assert "environments/preloop/bin/preloop-pytest" in text
    assert "environments/preloop/bin/preloop-frontend-deps" in text
    assert "COPY frontend/package.json frontend/package-lock.json" in text
    assert "useradd --uid 10000" in text


def test_runner_keeps_agent_tokens_away_from_checkout_code() -> None:
    """Tests, migrations, and init_db.py run under an env allowlist."""
    text = RUNNER.read_text()
    assert "export OPENAI_API_KEY=mock_key ANTHROPIC_API_KEY=mock_key" in text
    assert "clean_env pytest" in text
    assert "clean_env python scripts/init_db.py" in text
    for token in ("PRELOOP_API_TOKEN", "PRELOOP_MODEL_GATEWAY_TOKEN", "GIT"):
        assert token not in text.split("KEEP_ENV=(", 1)[1].split(")", 1)[0]


def test_runner_reports_missing_postgres_instead_of_a_find_error(
    tmp_path: Path,
) -> None:
    """Outside the image (no PostgreSQL, no DATABASE_URL) it exits 69."""
    env = {key: value for key, value in os.environ.items() if key != "DATABASE_URL"}
    env["PRELOOP_TEST_PGROOT"] = str(tmp_path / "no-postgresql")
    result = subprocess.run(
        ["bash", str(RUNNER), "--version"],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 69, result.stderr
    assert "no PostgreSQL here and DATABASE_URL is unset" in result.stderr
    assert "find:" not in result.stderr


def test_runner_keys_migration_state_to_its_own_cluster() -> None:
    """A wiped or second cluster never reuses another cluster's marker."""
    text = RUNNER.read_text()
    assert 'marker="${data}.schema"' in text
    assert '"${data}.init.log"' in text
    assert "/tmp/preloop-test-pg.schema" not in text
    fresh = text.split("if [[ ! -s $data/PG_VERSION ]]; then", 1)[1]
    assert fresh.split("mkdir", 1)[0].strip() == 'rm -f "$marker"'
    smoke = SMOKE.read_text()
    assert 'rm -rf "$pgdata"' in smoke


def test_reviewer_prompt_never_lets_npx_fetch_a_runner() -> None:
    """A bare `npx web-test-runner` falls back to the registry."""
    preset = (
        REPO / "backend" / "presets" / "002-pull-request-reviewer.yaml"
    ).read_text()
    assert "npx --no-install web-test-runner" in preset
    assert "npx web-test-runner" not in preset
    assert 'preloop-pytest -q -m "not integration"' in preset


def test_backend_profile_example_uses_the_baked_venv() -> None:
    """The backend profile needs no network: no venv, no pip install."""
    from preloop.services.flow_environment import EnvironmentProfile

    raw = json.loads((ENV_DIR / "profile.json.example").read_text())["preloop-backend"]
    assert raw["setup_commands"] == [
        "PYTHONPATH=backend python scripts/init_db.py --force"
    ]
    assert raw["test_commands"]["backend"] == [
        'preloop-pytest -m "not integration" backend/tests'
    ]
    assert ".github/requirements/app-dev.txt" in raw["lockfiles"]
    assert "cache_paths" not in raw
    rendered = json.dumps(raw).replace("REPLACE_WITH_PUBLISHED_DIGEST", "a" * 64)
    rendered = rendered.replace("REPLACE_WITH_APPROVED_DIGEST", "b" * 64)
    EnvironmentProfile.model_validate(json.loads(rendered))


@pytest.mark.parametrize("script", [SMOKE, RUNNER, FRONTEND_DEPS], ids=lambda p: p.name)
def test_scripts_pass_bash_n(script: Path) -> None:
    """Each script is valid bash and executable."""
    assert os.access(script, os.X_OK)
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason="set PRELOOP_ENVIRONMENT_IMAGE to a built environments/preloop image",
)
def test_image_runs_real_backend_and_frontend_tests_offline() -> None:
    """Inside the image, as the Docker harness uid, with no network."""
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "10000:10000",
            "-e",
            "HOME=/tmp",
            "-v",
            f"{REPO}:/src:ro",
            IMAGE,
            "/opt/preloop-pip/python-venv-smoke.sh",
            "/src",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-4000:]
    assert "8 passed" in output
    assert "python-venv-smoke: backend and frontend tests passed offline" in output
