"""The migration runner must not hold locks that live traffic needs.

A release runs ``alembic upgrade head`` from a pre-upgrade hook while the
previous API pods and sync workers are still serving. A batch of revisions
applied in a single transaction keeps the ACCESS EXCLUSIVE lock taken by its
first `ALTER TABLE` until its last one commits, which is long enough to
deadlock against an ordinary request and long enough to queue every later
query behind the migration. These tests pin the three settings that prevent
that: one transaction per revision, a short session ``lock_timeout``, and a
retry loop that only retries lock contention.
"""

import importlib.util
import types
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError

from preloop.models import migration_runtime

ENV_PATH = (
    Path(__file__).resolve().parents[2] / "preloop" / "models" / "alembic" / "env.py"
)

MIGRATION_ENV_VARS = (
    "PRELOOP_MIGRATION_LOCK_TIMEOUT",
    "PRELOOP_MIGRATION_MAX_ATTEMPTS",
    "PRELOOP_MIGRATION_RETRY_MIN_SECONDS",
    "PRELOOP_MIGRATION_RETRY_MAX_SECONDS",
)


@pytest.fixture(autouse=True)
def clean_migration_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Defaults must be observable even on a developer machine with overrides."""
    for name in MIGRATION_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


class RecordingContext:
    """Stands in for ``alembic.context`` and keeps the configure kwargs."""

    def __init__(self) -> None:
        self.configure_kwargs: dict[str, Any] = {}

    def configure(self, **kwargs: Any) -> None:
        self.configure_kwargs = kwargs


def _error(sqlstate: str) -> DBAPIError:
    """A driver error carrying a SQLSTATE, shaped like psycopg's."""
    original = types.SimpleNamespace(sqlstate=sqlstate)
    return OperationalError("ALTER TABLE ...", {}, original)


class TestPerRevisionTransactions:
    def test_online_context_commits_each_revision(self) -> None:
        context = RecordingContext()
        migration_runtime.configure_online_context(
            context, connection="conn", target_metadata="metadata"
        )
        assert context.configure_kwargs["transaction_per_migration"] is True
        assert context.configure_kwargs["connection"] == "conn"
        assert context.configure_kwargs["target_metadata"] == "metadata"

    @pytest.mark.parametrize("existing_table", [None, "alembic_version", "account"])
    def test_env_py_uses_the_shared_policy(
        self, monkeypatch: pytest.MonkeyPatch, existing_table: str | None
    ) -> None:
        """Load the real env.py online path with the database stubbed out.

        Asserting on ``migration_runtime`` alone would pass even if env.py
        stopped calling it, which is precisely the regression that produced
        the incident.
        """
        import alembic.context as alembic_context
        from alembic.config import Config

        context = RecordingContext()
        monkeypatch.setattr(alembic_context, "config", Config(), raising=False)
        monkeypatch.setattr(
            alembic_context, "is_offline_mode", lambda: False, raising=False
        )
        monkeypatch.setattr(
            alembic_context, "configure", context.configure, raising=False
        )
        monkeypatch.setattr(
            alembic_context, "run_migrations", lambda **kwargs: None, raising=False
        )

        class _NullTransaction:
            def __enter__(self) -> "_NullTransaction":
                return self

            def __exit__(self, *exc_info: object) -> bool:
                return False

        monkeypatch.setattr(
            alembic_context,
            "begin_transaction",
            lambda: _NullTransaction(),
            raising=False,
        )

        engine_kwargs: dict[str, Any] = {}

        engine = sqlalchemy.create_engine("sqlite://")
        if existing_table is not None:
            metadata = sqlalchemy.MetaData()
            sqlalchemy.Table(
                existing_table, metadata, sqlalchemy.Column("id", sqlalchemy.Integer)
            )
            metadata.create_all(engine)

        def fake_engine_from_config(
            configuration: Any, **kwargs: Any
        ) -> sqlalchemy.Engine:
            engine_kwargs.update(kwargs)
            return engine

        monkeypatch.setattr(
            sqlalchemy, "engine_from_config", fake_engine_from_config, raising=True
        )
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+psycopg://postgres:postgres@localhost/preloop"
        )

        spec = importlib.util.spec_from_file_location(
            "alembic_env_under_test", ENV_PATH
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)

        assert context.configure_kwargs["transaction_per_migration"] is True
        assert engine_kwargs["connect_args"] == {"options": "-c lock_timeout=5s"}
        assert alembic_context.config.attributes["fresh_database"] is (
            existing_table is None
        )
        engine.dispose()


class TestLockTimeout:
    def test_default_is_seconds_not_minutes(self) -> None:
        assert migration_runtime.lock_timeout() == "5s"

    def test_operator_override_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRELOOP_MIGRATION_LOCK_TIMEOUT", "250ms")
        assert migration_runtime.lock_timeout() == "250ms"
        assert migration_runtime.connect_args("postgresql+psycopg://x/y") == {
            "options": "-c lock_timeout=250ms"
        }

    @pytest.mark.parametrize(
        "value",
        [
            "5 seconds; DROP",
            "",
            "abc",
            "-1s",
            # An internal space becomes a second bare argument inside the one
            # libpq `options` token, so the connection is refused.
            "5 s",
            # lock_timeout is an integer GUC: no fractions.
            "5.5",
            "0.25min",
            # Overflows the integer GUC.
            "99999999999999",
            # Not a unit Postgres accepts for this setting.
            "1d",
        ],
    )
    def test_a_malformed_override_falls_back_to_the_default(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """A typo here kills the Job at connect time, before the first revision."""
        monkeypatch.setenv("PRELOOP_MIGRATION_LOCK_TIMEOUT", value)
        assert migration_runtime.lock_timeout() == "5s"

    @pytest.mark.parametrize("value", ["250ms", "2min", "5000", "30s", "1h", "500us"])
    def test_the_shapes_postgres_accepts_are_kept(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("PRELOOP_MIGRATION_LOCK_TIMEOUT", value)
        assert migration_runtime.lock_timeout() == value

    def test_non_postgres_urls_get_no_options(self) -> None:
        assert migration_runtime.connect_args("sqlite:///./test.db") == {}


class TestRetryPolicy:
    def test_defaults(self) -> None:
        assert migration_runtime.max_attempts() == 20
        assert migration_runtime.retry_delay_bounds() == (3.0, 15.0)

    def test_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PRELOOP_MIGRATION_MAX_ATTEMPTS", "4")
        monkeypatch.setenv("PRELOOP_MIGRATION_RETRY_MIN_SECONDS", "1")
        monkeypatch.setenv("PRELOOP_MIGRATION_RETRY_MAX_SECONDS", "2.5")
        assert migration_runtime.max_attempts() == 4
        assert migration_runtime.retry_delay_bounds() == (1.0, 2.5)

    def test_inverted_bounds_do_not_produce_a_negative_sleep(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRELOOP_MIGRATION_RETRY_MIN_SECONDS", "10")
        monkeypatch.setenv("PRELOOP_MIGRATION_RETRY_MAX_SECONDS", "2")
        low, high = migration_runtime.retry_delay_bounds()
        assert low == 10.0
        assert high == 10.0

    @pytest.mark.parametrize("sqlstate", sorted(migration_runtime.RETRYABLE_SQLSTATES))
    def test_lock_contention_is_retried_until_it_succeeds(self, sqlstate: str) -> None:
        attempts: list[int] = []
        slept: list[float] = []

        def operation() -> str:
            attempts.append(len(attempts) + 1)
            if len(attempts) < 3:
                raise _error(sqlstate)
            return "upgraded"

        result = migration_runtime.run_with_lock_retries(
            operation,
            attempts=5,
            delay_bounds=(3.0, 15.0),
            sleep=slept.append,
            jitter=lambda low, high: (low + high) / 2,
        )
        assert result == "upgraded"
        assert len(attempts) == 3
        assert slept == [9.0, 9.0]

    def test_a_broken_revision_fails_on_the_first_attempt(self) -> None:
        """Only lock contention is transient. A bug must fail the release."""
        calls: list[int] = []

        def operation() -> None:
            calls.append(1)
            raise IntegrityError("INSERT ...", {}, Exception("duplicate key"))

        with pytest.raises(IntegrityError):
            migration_runtime.run_with_lock_retries(
                operation, attempts=5, delay_bounds=(0.0, 0.0), sleep=lambda _: None
            )
        assert calls == [1]

    def test_a_statement_timeout_is_not_retried(self) -> None:
        """57014 means the revision is too slow, which no retry can fix.

        Twenty attempts at a revision the cluster `statement_timeout` kills
        would take the target table's exclusive lock twenty more times, each
        for the full timeout, with live traffic queued behind every one. It
        belongs in the "drain the API first" path instead.
        """
        assert "57014" not in migration_runtime.RETRYABLE_SQLSTATES
        calls: list[int] = []

        def operation() -> None:
            calls.append(1)
            raise _error("57014")

        with pytest.raises(OperationalError):
            migration_runtime.run_with_lock_retries(
                operation, attempts=5, delay_bounds=(0.0, 0.0), sleep=lambda _: None
            )
        assert calls == [1]

    def test_an_interrupt_stops_the_release_immediately(self) -> None:
        """A cancelled Job must not sleep and try again."""
        calls: list[int] = []

        def operation() -> None:
            calls.append(1)
            raise KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            migration_runtime.run_with_lock_retries(
                operation, attempts=5, delay_bounds=(0.0, 0.0), sleep=lambda _: None
            )
        assert calls == [1]

    def test_a_non_database_error_is_not_retried(self) -> None:
        with pytest.raises(RuntimeError):
            migration_runtime.run_with_lock_retries(
                _raise(RuntimeError("boom")),
                attempts=5,
                delay_bounds=(0.0, 0.0),
                sleep=lambda _: None,
            )

    def test_the_attempt_budget_is_bounded(self) -> None:
        calls: list[int] = []

        def operation() -> None:
            calls.append(1)
            raise _error("55P03")

        with pytest.raises(OperationalError):
            migration_runtime.run_with_lock_retries(
                operation, attempts=4, delay_bounds=(0.0, 0.0), sleep=lambda _: None
            )
        assert len(calls) == 4

    def test_jitter_stays_inside_the_configured_bounds(self) -> None:
        slept: list[float] = []

        def operation() -> None:
            raise _error("40P01")

        with pytest.raises(OperationalError):
            migration_runtime.run_with_lock_retries(
                operation, attempts=8, delay_bounds=(3.0, 15.0), sleep=slept.append
            )
        assert len(slept) == 7
        assert all(3.0 <= delay <= 15.0 for delay in slept)

    def test_psycopg2_style_pgcode_is_recognised(self) -> None:
        original = types.SimpleNamespace(pgcode="40P01")
        error = OperationalError("ALTER TABLE ...", {}, original)
        assert migration_runtime.sqlstate_of(error) == "40P01"
        assert migration_runtime.is_lock_contention(error) is True


def _raise(error: BaseException):
    def operation() -> None:
        raise error

    return operation
