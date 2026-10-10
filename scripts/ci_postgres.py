#!/usr/bin/env python
"""Create one database in a Postgres that is already running, or start one.

GitHub-hosted runners have no Postgres until this step. Self-hosted runners
may already be listening on 127.0.0.1:5432. A reachable server is reused:
this process creates and later drops only the job database. A refused
connection starts ``pgvector/pgvector:pg16`` bound to 127.0.0.1:5432, and
the drop command removes that container. A server that answers and then
rejects ``test_user`` is left alone.

The runner setup and the manual check are documented in
``.github/self-hosted-runners.md``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

_CI_DATABASE_NAME = re.compile(r"^preloop_ci_[0-9]+_[0-9]+_[0-9]+$")

POSTGRES_IMAGE = "pgvector/pgvector:pg16"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432
# Ephemeral fallbacks avoid 5432, which is the persistent server. One port
# per shard so two jobs on one VM do not bind the same address.
EPHEMERAL_PORT_BASE = 15432
DEFAULT_USER = "test_user"
DEFAULT_PASSWORD = "test_password"
DEFAULT_MAINTENANCE_DB = "postgres"
CONNECT_TIMEOUT_SECONDS = 3
START_TIMEOUT_SECONDS = 60
STALE_DATABASE_TTL_SECONDS = 6 * 60 * 60

Connect = Callable[[str, int, str, str, str], Any]


class PostgresUnavailableError(Exception):
    """Nothing accepted a connection at the configured host and port."""


class PostgresRejectedError(Exception):
    """A server is listening, and it refused the configured role."""


def job_database_name(run_id: str, attempt: str, shard: str) -> str:
    """Return the database name for one CI job.

    Args:
        run_id: ``GITHUB_RUN_ID``. Digits only.
        attempt: ``GITHUB_RUN_ATTEMPT``. Digits only.
        shard: Pytest-split group. Digits only.

    Returns:
        A Postgres identifier, at most 63 characters.

    Raises:
        ValueError: A component is empty or not decimal digits.
    """
    for label, value in (
        ("GITHUB_RUN_ID", run_id),
        ("GITHUB_RUN_ATTEMPT", attempt),
        ("PRELOOP_CI_SHARD", shard),
    ):
        if not value.isdigit():
            raise ValueError(f"{label} must be decimal digits, got {value!r}")
    name = f"preloop_ci_{run_id}_{attempt}_{shard}"
    if len(name) > 63:
        raise ValueError(f"database name {name!r} exceeds 63 characters")
    return name


def ephemeral_container_name(run_id: str, attempt: str, shard: str) -> str:
    """Return a Docker name that belongs to one job and no other."""
    job_database_name(run_id, attempt, shard)
    return f"preloop-ci-{run_id}-{attempt}-{shard}"


def ephemeral_host_port(shard: str, listen_port: int) -> int:
    """Return the host port for a server this job starts.

    The default listen port stays reserved for a persistent server. Each
    shard gets its own fallback port so two jobs on one machine do not
    bind the same address or delete each other's container. An explicit
    ``PRELOOP_CI_POSTGRES_PORT`` is that one server, so the fallback uses
    it unchanged.
    """
    if listen_port != DEFAULT_PORT:
        return listen_port
    if not shard.isdigit():
        raise ValueError(f"PRELOOP_CI_SHARD must be decimal digits, got {shard!r}")
    return EPHEMERAL_PORT_BASE + int(shard)


def database_url(user: str, host: str, port: int, name: str) -> str:
    """Build a SQLAlchemy URL for one database on the CI server.

    The password stays in ``PGPASSWORD``. A URL that contains it is a second
    copy of the credential in the log and in ``GITHUB_ENV``.
    """
    return f"postgresql://{quote_plus(user)}@{host}:{port}/{name}"


def _is_rejection(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "authentication failed" in text
        or "password authentication failed" in text
        or ("role" in text and "does not exist" in text)
    )


def connect(host: str, port: int, user: str, password: str, dbname: str) -> Any:
    """Open an autocommit connection, or raise a classified error.

    Raises:
        PostgresRejectedError: The server rejected the role or password.
        PostgresUnavailableError: No server accepted the TCP connection.
    """
    try:
        import psycopg2
    except ImportError:
        import psycopg as psycopg2  # type: ignore[no-redef]

    try:
        connection = psycopg2.connect(
            host=host,
            port=port,
            user=user,
            password=password,
            dbname=dbname,
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if _is_rejection(exc):
            raise PostgresRejectedError(str(exc)) from exc
        raise PostgresUnavailableError(str(exc)) from exc
    connection.autocommit = True
    return connection


def _marker_path() -> Path:
    root = os.environ.get("RUNNER_TEMP") or os.environ.get("TMPDIR") or "/tmp"
    return Path(root) / "preloop-ci-postgres.json"


def _write_marker(payload: dict[str, Any]) -> None:
    path = _marker_path()
    path.write_text(json.dumps(payload), encoding="utf-8")


def _read_marker() -> dict[str, Any] | None:
    path = _marker_path()
    if not path.is_file():
        return None
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"Postgres marker {path} is not an object")
    return loaded


def _publish(url: str) -> None:
    """Expose the job URL to later steps and to a local shell."""
    print(f"DATABASE_URL={url}")
    github_env = os.environ.get("GITHUB_ENV")
    if not github_env:
        return
    with open(github_env, "a", encoding="utf-8") as handle:
        for name in (
            "DATABASE_URL",
            "FLOW_FEEDBACK_TEST_DATABASE_URL",
            "CHAT_TEST_DATABASE_URL",
        ):
            handle.write(f"{name}={url}\n")


def _ensure_database(connection: Any, name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        if cursor.fetchone() is not None:
            print(f"database {name} already exists")
            return
        # Identifiers cannot be bound parameters. job_database_name() only
        # returns [a-z0-9_], so this interpolation stays inside that set.
        cursor.execute(f"CREATE DATABASE {name}")
    print(f"created database {name}")


def _start_ephemeral_server(
    user: str, password: str, port: int, container: str
) -> None:
    """Start pgvector on localhost. Remove only this job's container name."""
    subprocess.run(
        ["docker", "rm", "-f", container],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            container,
            "-p",
            f"127.0.0.1:{port}:5432",
            "-e",
            "POSTGRES_USER",
            "-e",
            "POSTGRES_PASSWORD",
            "-e",
            "POSTGRES_DB",
            POSTGRES_IMAGE,
        ],
        check=True,
        text=True,
        env={
            **os.environ,
            "POSTGRES_USER": user,
            "POSTGRES_PASSWORD": password,
            "POSTGRES_DB": "postgres",
        },
    )
    print(f"started {POSTGRES_IMAGE} as {container} on 127.0.0.1:{port}")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _sweep_stale_databases(connection: Any, keep: str) -> None:
    """Drop ``preloop_ci_*`` databases older than the TTL.

    A cancelled job may never reach its drop step. ``pg_database`` has no
    creation time; the data directory's ``PG_VERSION`` mtime is the age.
    Failures are logged. A sweep must not stop the job that is starting.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT d.datname,
                       (pg_stat_file('base/' || d.oid || '/PG_VERSION')).modification
                FROM pg_database d
                WHERE d.datname LIKE 'preloop_ci_%'
                  AND d.datname <> %s
                """,
                (keep,),
            )
            rows = cursor.fetchall()
    except Exception as exc:
        print(f"skipping stale database sweep: {exc}", file=sys.stderr)
        return
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=STALE_DATABASE_TTL_SECONDS)
    for datname, modified in rows:
        if datname == keep:
            continue
        if not isinstance(datname, str) or _CI_DATABASE_NAME.fullmatch(datname) is None:
            continue
        if not isinstance(modified, datetime) or _as_utc(modified) >= cutoff:
            continue
        try:
            _drop_database(connection, datname)
        except Exception as exc:
            print(f"could not drop stale database {datname}: {exc}", file=sys.stderr)


def prepare(
    *,
    opener: Connect = connect,
    start_server: Callable[[str, str, int, str], None] = _start_ephemeral_server,
) -> str:
    """Reuse a listening Postgres, or start one, then create the job database.

    Returns:
        The job database URL.

    Raises:
        PostgresRejectedError: A server is up and will not accept the CI role.
        PostgresUnavailableError: No server could be started or reached.
        ValueError: The run id, attempt, or shard is not numeric.
    """
    host = os.environ.get("PRELOOP_CI_POSTGRES_HOST", DEFAULT_HOST)
    listen_port = int(os.environ.get("PRELOOP_CI_POSTGRES_PORT", str(DEFAULT_PORT)))
    user = os.environ.get("PRELOOP_CI_POSTGRES_USER", DEFAULT_USER)
    password = os.environ.get("PRELOOP_CI_POSTGRES_PASSWORD", DEFAULT_PASSWORD)
    maintenance = os.environ.get("PRELOOP_CI_MAINTENANCE_DB", DEFAULT_MAINTENANCE_DB)
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    shard = os.environ.get("PRELOOP_CI_SHARD", "")
    name = job_database_name(run_id, attempt, shard)
    container_name = ephemeral_container_name(run_id, attempt, shard)
    port = listen_port
    started = False
    try:
        connection = opener(host, listen_port, user, password, maintenance)
    except PostgresRejectedError:
        raise
    except PostgresUnavailableError:
        port = ephemeral_host_port(shard, listen_port)
        print(
            f"no Postgres at {host}:{listen_port}; starting {POSTGRES_IMAGE} "
            f"on 127.0.0.1:{port}",
            file=sys.stderr,
        )
        start_server(user, password, port, container_name)
        started = True
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        connection = None
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                connection = opener(host, port, user, password, maintenance)
                break
            except PostgresUnavailableError as exc:
                last_error = exc
                time.sleep(1)
        if connection is None:
            raise PostgresUnavailableError(
                f"{POSTGRES_IMAGE} did not accept connections: {last_error}"
            )
    else:
        print(f"reusing Postgres at {host}:{listen_port}")
        _sweep_stale_databases(connection, name)

    try:
        _ensure_database(connection, name)
    finally:
        connection.close()

    url = database_url(user, host, port, name)
    _write_marker(
        {
            "database": name,
            "host": host,
            "port": port,
            "user": user,
            "maintenance_db": maintenance,
            "container": container_name if started else None,
        }
    )
    _publish(url)
    return url


def _drop_database(connection: Any, name: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s",
            (name,),
        )
        cursor.execute(f"DROP DATABASE IF EXISTS {name}")
    print(f"dropped database {name}")


def drop(*, opener: Connect = connect) -> None:
    """Drop the job database. Stop the container only if this job started it."""
    marker = _read_marker()
    if marker is None:
        print("no CI Postgres marker; nothing to drop")
        return
    name = str(marker["database"])
    host = str(marker["host"])
    port = int(marker["port"])
    user = str(marker["user"])
    password = os.environ.get("PRELOOP_CI_POSTGRES_PASSWORD", DEFAULT_PASSWORD)
    maintenance = str(marker["maintenance_db"])
    connection = opener(host, port, user, password, maintenance)
    try:
        _drop_database(connection, name)
    finally:
        connection.close()
    container = marker.get("container")
    if container:
        subprocess.run(
            ["docker", "rm", "-f", str(container)],
            check=False,
            capture_output=True,
            text=True,
        )
        print(f"removed container {container}")
    _marker_path().unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    """Run ``prepare`` or ``drop``."""
    if len(argv) != 2 or argv[1] not in {"prepare", "drop"}:
        print("usage: ci_postgres.py prepare|drop", file=sys.stderr)
        return 2
    try:
        if argv[1] == "prepare":
            prepare()
        else:
            drop()
    except (PostgresRejectedError, PostgresUnavailableError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        print("See .github/self-hosted-runners.md", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
