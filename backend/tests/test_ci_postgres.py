"""CI Postgres setup reuses a listening server and only creates a database."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from tests.ci_workflow import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "ci_postgres", REPO_ROOT / "scripts" / "ci_postgres.py"
)
assert _SPEC is not None and _SPEC.loader is not None
ci_postgres = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ci_postgres)


class _Cursor:
    def __init__(self, databases: set[str]) -> None:
        self.databases = databases
        self.statements: list[tuple[str, tuple[Any, ...] | None]] = []
        self._row: tuple[int, ...] | None = None

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.statements.append((sql, params))
        if "pg_database" in sql:
            assert params is not None
            self._row = (1,) if params[0] in self.databases else None
            return
        if sql.startswith("CREATE DATABASE "):
            self.databases.add(sql.removeprefix("CREATE DATABASE ").strip())
        if sql.startswith("DROP DATABASE"):
            self.databases.discard(sql.split()[-1])
        self._row = None

    def fetchone(self) -> tuple[int, ...] | None:
        return self._row


class _Connection:
    def __init__(self, databases: set[str]) -> None:
        self.databases = databases
        self.cursor_impl = _Cursor(databases)
        self.closed = False

    def cursor(self) -> _Cursor:
        return self.cursor_impl

    def close(self) -> None:
        self.closed = True


def test_job_database_name_rejects_non_digits() -> None:
    with pytest.raises(ValueError, match="GITHUB_RUN_ID"):
        ci_postgres.job_database_name("run", "1", "2")


def test_prepare_reuses_a_listening_server_and_only_creates_a_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    databases: set[str] = set()
    started: list[tuple[str, str, int]] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "37987118309")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("PRELOOP_CI_SHARD", "4")
    monkeypatch.delenv("GITHUB_ENV", raising=False)

    def opener(*_args: object) -> _Connection:
        return _Connection(databases)

    def start_server(user: str, password: str, port: int) -> None:
        started.append((user, password, port))

    url = ci_postgres.prepare(opener=opener, start_server=start_server)

    assert started == []
    assert databases == {"preloop_ci_37987118309_1_4"}
    assert url.endswith("/preloop_ci_37987118309_1_4")
    marker = json.loads((tmp_path / "preloop-ci-postgres.json").read_text())
    assert marker["container"] is None
    assert marker["database"] == "preloop_ci_37987118309_1_4"


def test_prepare_starts_pgvector_only_when_nothing_is_listening(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    databases: set[str] = set()
    calls = {"n": 0}
    started: list[int] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "15")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    monkeypatch.setenv("PRELOOP_CI_SHARD", "18")

    def opener(*_args: object) -> _Connection:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ci_postgres.PostgresUnavailableError("connection refused")
        return _Connection(databases)

    url = ci_postgres.prepare(
        opener=opener,
        start_server=lambda _user, _password, port: started.append(port),
    )

    assert started == [5432]
    assert url.endswith("/preloop_ci_15_2_18")
    marker = json.loads((tmp_path / "preloop-ci-postgres.json").read_text())
    assert marker["container"] == ci_postgres.EPHEMERAL_CONTAINER


def test_prepare_does_not_start_a_server_that_rejects_the_role(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "15")
    monkeypatch.setenv("PRELOOP_CI_SHARD", "1")
    started: list[int] = []

    def opener(*_args: object) -> _Connection:
        raise ci_postgres.PostgresRejectedError("password authentication failed")

    with pytest.raises(ci_postgres.PostgresRejectedError):
        ci_postgres.prepare(
            opener=opener,
            start_server=lambda _user, _password, port: started.append(port),
        )
    assert started == []


def test_drop_removes_the_job_database_and_only_an_ephemeral_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    databases = {"preloop_ci_15_1_3"}
    removed: list[list[str]] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    (tmp_path / "preloop-ci-postgres.json").write_text(
        json.dumps(
            {
                "database": "preloop_ci_15_1_3",
                "host": "127.0.0.1",
                "port": 5432,
                "user": "test_user",
                "maintenance_db": "postgres",
                "container": ci_postgres.EPHEMERAL_CONTAINER,
            }
        ),
        encoding="utf-8",
    )

    def opener(*_args: object) -> _Connection:
        return _Connection(databases)

    monkeypatch.setattr(
        ci_postgres.subprocess,
        "run",
        lambda args, **_kwargs: removed.append(list(args)),
    )
    ci_postgres.drop(opener=opener)

    assert "preloop_ci_15_1_3" not in databases
    assert removed == [["docker", "rm", "-f", ci_postgres.EPHEMERAL_CONTAINER]]
    assert not (tmp_path / "preloop-ci-postgres.json").exists()


def test_drop_leaves_a_reused_server_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    databases = {"preloop_ci_15_1_3"}
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    (tmp_path / "preloop-ci-postgres.json").write_text(
        json.dumps(
            {
                "database": "preloop_ci_15_1_3",
                "host": "127.0.0.1",
                "port": 5432,
                "user": "test_user",
                "maintenance_db": "postgres",
                "container": None,
            }
        ),
        encoding="utf-8",
    )
    ci_postgres.drop(opener=lambda *_args: _Connection(databases))
    assert databases == set()


def test_prepare_publishes_the_three_ci_urls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env_file = tmp_path / "github.env"
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_ENV", str(env_file))
    monkeypatch.setenv("GITHUB_RUN_ID", "9")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    monkeypatch.setenv("PRELOOP_CI_SHARD", "2")
    env_file.write_text("", encoding="utf-8")

    ci_postgres.prepare(
        opener=lambda *_args: _Connection(set()), start_server=lambda *_a: None
    )
    published = env_file.read_text(encoding="utf-8")
    assert (
        "DATABASE_URL=postgresql://test_user:test_password@127.0.0.1:5432/preloop_ci_9_1_2"
        in published
    )
    assert "FLOW_FEEDBACK_TEST_DATABASE_URL=" in published
    assert "CHAT_TEST_DATABASE_URL=" in published
