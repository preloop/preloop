"""xdist workers must never share the CI job database."""

from tests.xdist_database import use_worker_database

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


def test_without_clones_or_xdist_the_url_is_unchanged() -> None:
    for env in (
        {"PYTEST_XDIST_WORKER": "gw0", "DATABASE_URL": URL},
        {"PRELOOP_CI_WORKER_DATABASES": "1", "DATABASE_URL": URL},
    ):
        use_worker_database(env)
        assert env["DATABASE_URL"] == URL
