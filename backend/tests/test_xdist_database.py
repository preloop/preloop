"""xdist workers must never share the CI job database."""

import json
from pathlib import Path

import pytest

from tests.xdist_database import check_job_database, use_worker_database

URL = "postgresql://test_user@127.0.0.1:5432/preloop_ci_1_1_2"


def test_worker_gets_its_own_clone() -> None:
    env = {
        "PYTEST_XDIST_WORKER": "gw3",
        "PRELOOP_CI_WORKER_DATABASES": "1",
        "DATABASE_URL": URL,
        "CHAT_TEST_DATABASE_URL": URL,
    }
    use_worker_database(env)
    assert env["DATABASE_URL"] == f"{URL}_gw3"
    assert env["CHAT_TEST_DATABASE_URL"] == f"{URL}_gw3"
    assert "FLOW_FEEDBACK_TEST_DATABASE_URL" not in env


def test_query_string_is_kept_after_the_worker_suffix() -> None:
    env = {
        "PYTEST_XDIST_WORKER": "gw0",
        "PRELOOP_CI_WORKER_DATABASES": "1",
        "DATABASE_URL": f"{URL}?sslmode=disable",
    }
    use_worker_database(env)
    assert env["DATABASE_URL"] == f"{URL}_gw0?sslmode=disable"


def test_without_clones_or_xdist_the_url_is_unchanged() -> None:
    for env in (
        {"PYTEST_XDIST_WORKER": "gw0", "DATABASE_URL": URL},
        {"PRELOOP_CI_WORKER_DATABASES": "1", "DATABASE_URL": URL},
    ):
        use_worker_database(env)
        assert env["DATABASE_URL"] == URL


def _marker(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "preloop-ci-postgres.json").write_text(
        json.dumps({"database": "preloop_ci_7_1_1", "host": "127.0.0.1", "port": 5432}),
        encoding="utf-8",
    )
    return {"GITHUB_ACTIONS": "true", "RUNNER_TEMP": str(tmp_path)}


def test_job_database_check_accepts_the_worker_clone(tmp_path: Path) -> None:
    environ = _marker(tmp_path) | {
        "PYTEST_XDIST_WORKER": "gw3",
        "DATABASE_URL": "postgresql://u@127.0.0.1:5432/preloop_ci_7_1_1_gw3",
    }
    check_job_database(environ)


def test_job_database_check_names_a_leaked_url(tmp_path: Path) -> None:
    environ = _marker(tmp_path) | {
        "DATABASE_URL": "postgresql://u@127.0.0.1:15450/preloop_ci_15_2_18",
    }
    with pytest.raises(RuntimeError, match="preloop_ci_15_2_18.*GITHUB_ENV"):
        check_job_database(environ)


def test_job_database_check_is_skipped_outside_actions(tmp_path: Path) -> None:
    environ = _marker(tmp_path) | {
        "GITHUB_ACTIONS": "",
        "DATABASE_URL": "postgresql://u@localhost/preloop",
    }
    check_job_database(environ)
