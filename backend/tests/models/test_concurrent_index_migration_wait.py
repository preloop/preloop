"""Concurrent index DDL can wait for an old writer without blocking new writes."""

from __future__ import annotations

import importlib.util
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from typing import Iterator

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from preloop.models.concurrent_index_ddl import concurrent_index_ddl
from preloop.models.migration_runtime import sqlstate_of


@pytest.fixture
def index_database() -> Iterator[Engine]:
    """Use a private database so installed indexes cannot affect the revision.

    The catalog probes in historical revisions predate schema qualification.
    A seeded main test database can contain identically named indexes, so a
    separate schema would not isolate these actual migration entry points.
    """
    url = os.getenv("DATABASE_URL")
    if not url or not url.startswith("postgresql"):
        pytest.skip("requires a disposable PostgreSQL DATABASE_URL")
    database = f"migration_wait_{secrets.token_hex(6)}"
    base_url = make_url(url)
    admin = create_engine(
        base_url.set(database="postgres"), isolation_level="AUTOCOMMIT"
    )
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
    except DBAPIError as error:
        admin.dispose()
        if sqlstate_of(error) == "42501":
            pytest.skip("test database role lacks CREATEDB privilege")
        raise
    engine = create_engine(
        base_url.set(database=database),
        connect_args={"options": "-c lock_timeout=5s -c statement_timeout=23s"},
    )
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE account (id uuid PRIMARY KEY)"))
            connection.execute(text('CREATE TABLE "user" (id uuid PRIMARY KEY)'))
            connection.execute(
                text("CREATE TABLE approval_request (resolved_at timestamptz)")
            )
            connection.execute(
                text(
                    "CREATE TABLE api_usage (id bigint, account_id bigint, "
                    "action_type text, meta_data jsonb, timestamp timestamptz, "
                    "estimated_cost numeric, ai_model_id bigint, status_code integer, "
                    "prompt_tokens bigint, completion_tokens bigint, total_tokens bigint, "
                    "cache_read_tokens bigint, cache_creation_tokens bigint, "
                    "model_alias text, provider_name text, flow_id bigint, "
                    "runtime_principal_type text, runtime_principal_id text)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE audit_log (id bigint, account_id bigint, chain_seq bigint)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE runtime_session_activity (id bigint, account_id bigint, "
                    "timestamp timestamptz, activity_type text, status text, tool_name text, "
                    "flow_execution_id bigint, runtime_session_id bigint)"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE issue_cost_execution (id bigint, account_id bigint, pr_key text)"
                )
            )
        yield engine
    finally:
        engine.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE "{database}" WITH (FORCE)'))
        admin.dispose()


@pytest.mark.parametrize(
    ("revision_file", "table", "index_name", "invalid_prior"),
    [
        (
            "20261018_governance_spend_idx.py",
            "api_usage",
            "ix_api_usage_governance_spend",
            False,
        ),
        (
            "20261029_audit_unsealed_idx.py",
            "audit_log",
            "ix_audit_log_account_unsealed",
            False,
        ),
        (
            "20261030_gateway_model_idx.py",
            "api_usage",
            "ix_api_usage_gateway_model_totals",
            False,
        ),
        (
            "20261031_code_tool_activity_idx.py",
            "runtime_session_activity",
            "ix_runtime_session_activity_code_tools",
            False,
        ),
        (
            "20261031_live_activity.py",
            "runtime_session_activity",
            "ix_rsa_account_turn_events",
            False,
        ),
        (
            "20261031_live_activity.py",
            "runtime_session_activity",
            "ix_rsa_account_turn_events",
            True,
        ),
        (
            "20261018_governance_spend_idx.py",
            "api_usage",
            "ix_api_usage_governance_spend",
            True,
        ),
        (
            "20261030_gateway_model_idx.py",
            "api_usage",
            "ix_api_usage_gateway_model_totals",
            True,
        ),
        (
            "20261101_customer_usage_idx.py",
            "api_usage",
            "ix_api_usage_customer_totals",
            False,
        ),
        (
            "20261101_customer_usage_idx.py",
            "api_usage",
            "ix_api_usage_customer_totals",
            True,
        ),
        (
            "20261102_agent_usage_idx.py",
            "api_usage",
            "ix_api_usage_principal_totals",
            False,
        ),
        (
            "20261102_agent_usage_idx.py",
            "api_usage",
            "ix_api_usage_principal_totals",
            True,
        ),
    ],
)
def test_concurrent_index_survives_writer_older_than_the_ddl_lock_budget(
    index_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    revision_file: str,
    table: str,
    index_name: str,
    invalid_prior: bool,
) -> None:
    """Exercise the production revision, including restoring the 5s DDL budget."""
    path = (
        Path(__file__).resolve().parents[2]
        / "preloop/models/alembic/versions"
        / revision_file
    )
    spec = importlib.util.spec_from_file_location("governance_spend_revision", path)
    assert spec is not None and spec.loader is not None
    revision = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(revision)
    if invalid_prior:
        # Reproduce the canceled-build residue seen in the incident, using the
        # original plain-autocommit revision policy with a short test budget.
        with index_database.connect() as writer:
            transaction = writer.begin()
            writer.execute(text(f"INSERT INTO {table} (id) VALUES (0)"))
            try:
                with index_database.connect() as failed_connection:
                    failed_connection.execute(text("SET lock_timeout = '100ms'"))
                    failed_connection.commit()
                    failed_context = MigrationContext.configure(failed_connection)
                    with monkeypatch.context() as original_policy:
                        original_policy.setattr(
                            revision,
                            "concurrent_index_ddl",
                            lambda context: context.autocommit_block(),
                        )
                        with Operations.context(failed_context):
                            with pytest.raises(DBAPIError) as error:
                                revision.upgrade()
                    assert sqlstate_of(error.value) == "55P03"
            finally:
                transaction.rollback()
        with index_database.connect() as check:
            assert (
                check.execute(
                    text(
                        "SELECT indisvalid FROM pg_index i JOIN pg_class c "
                        "ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE c.relname = :name AND n.nspname = current_schema()"
                    ),
                    {"name": index_name},
                ).scalar_one()
                is False
            )
        # The setup intentionally changed a session GUC. Close those pooled
        # sessions so the actual retry starts with the normal 5s policy.
        index_database.dispose()
    started = Event()

    def migrate() -> tuple[str, str]:
        with index_database.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"transaction_per_migration": True}
            )
            with Operations.context(context):
                with context.begin_transaction():
                    started.set()
                    revision.upgrade()
            assert connection.execute(
                text(
                    "SELECT indisvalid FROM pg_index i JOIN pg_class c "
                    "ON c.oid = i.indexrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = :name AND n.nspname = current_schema()"
                ),
                {"name": index_name},
            ).scalar_one()
            return (
                connection.execute(text("SHOW lock_timeout")).scalar_one(),
                connection.execute(text("SHOW statement_timeout")).scalar_one(),
            )

    with index_database.connect() as writer, ThreadPoolExecutor(1) as executor:
        transaction = writer.begin()
        writer.execute(text(f"INSERT INTO {table} (id) VALUES (1)"))
        future = executor.submit(migrate)
        try:
            assert started.wait(5)
            # A CREATE INDEX CONCURRENTLY must wait for this existing writer.
            # The incident's global five-second lock timeout aborts it here.
            time.sleep(6)
            assert not future.done(), (
                "Concurrent DDL inherited the blocking-DDL timeout: "
                f"{future.exception()}"
            )
            # Waiting on an older writer must leave ordinary new writes free.
            with index_database.begin() as another_writer:
                another_writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
                another_writer.execute(text(f"INSERT INTO {table} (id) VALUES (2)"))
        finally:
            transaction.rollback()
        assert future.result(timeout=15) == ("5s", "23s")


def test_concurrent_statement_budget_is_finite_and_restored_on_failure(
    index_database: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A slow concurrent operation cannot consume the whole deployment forever."""
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", "2")
    with index_database.connect() as connection:
        context = MigrationContext.configure(connection)
        with pytest.raises(DBAPIError) as error:
            with concurrent_index_ddl(context):
                connection.execute(text("SELECT pg_sleep(3)"))
        assert sqlstate_of(error.value) == "57014"
        assert "SQLSTATE 57014" in caplog.text
        assert "pg_stat_progress_create_index" in caplog.text
        assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "23s"


@pytest.mark.parametrize("value", ["0", "-1", "901", "not-seconds"])
def test_invalid_or_unbounded_concurrent_timeouts_keep_finite_defaults(
    index_database: Engine, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """Bad overrides must not disable a deadline or reach PostgreSQL as SQL."""
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", value)
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", value)
    with index_database.connect() as connection:
        context = MigrationContext.configure(connection)
        with concurrent_index_ddl(context):
            assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "1min"
            assert (
                connection.execute(text("SHOW statement_timeout")).scalar_one()
                == "2min"
            )
        assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "23s"


def test_all_incident_revisions_finish_inside_original_deployment_deadline(
    index_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measure the complete sequence with populated tables and live contention.

    This is a synthetic regression, not a production performance guarantee.
    The original inherited five-second policy fails before the old writer ends;
    the scoped policy waits once and builds every index, repairing invalid residue.
    """
    revision_names = [
        "20261018_governance_spend_idx.py",
        "20261029_audit_unsealed_idx.py",
        "20261030_gateway_model_idx.py",
        "20261031_code_tool_activity_idx.py",
    ]
    revisions = []
    for name in revision_names:
        path = (
            Path(__file__).resolve().parents[2]
            / "preloop/models/alembic/versions"
            / name
        )
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        revision = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(revision)
        revisions.append(revision)
    usage_rows = int(os.getenv("PRELOOP_MIGRATION_BENCHMARK_ROWS", "175000"))
    with index_database.begin() as connection:
        connection.execute(text("SET LOCAL statement_timeout = '120s'"))
        connection.execute(
            text(
                "INSERT INTO api_usage (id, account_id, action_type, meta_data, timestamp, "
                "estimated_cost, ai_model_id) SELECT n, n % 10, 'model_gateway', "
                "jsonb_build_object('purpose', CASE WHEN n % 2 = 0 THEN "
                "'governance_review' ELSE NULL END), now(), 0.01, n % 5 "
                "FROM generate_series(1, :rows) AS n"
            ),
            {"rows": usage_rows},
        )
        connection.execute(
            text(
                "INSERT INTO audit_log (id, account_id) "
                "SELECT n, n % 10 FROM generate_series(1, 50000) AS n"
            )
        )
        connection.execute(
            text(
                "INSERT INTO runtime_session_activity "
                "(id, account_id, timestamp, activity_type, status, tool_name) "
                "SELECT n, n % 10, now(), 'tool_call', 'succeeded', 'create_pull_request' "
                "FROM generate_series(1, 50000) AS n"
            )
        )
    # A previous cancellation left the same invalid gateway index as production.
    with index_database.connect() as writer:
        transaction = writer.begin()
        writer.execute(text("INSERT INTO api_usage (id) VALUES (-1)"))
        try:
            with index_database.connect() as failed:
                failed.execute(text("SET lock_timeout = '100ms'"))
                failed.commit()
                with monkeypatch.context() as original_policy:
                    original_policy.setattr(
                        revisions[2],
                        "concurrent_index_ddl",
                        lambda context: context.autocommit_block(),
                    )
                    with Operations.context(MigrationContext.configure(failed)):
                        with pytest.raises(DBAPIError):
                            revisions[2].upgrade()
        finally:
            transaction.rollback()
    index_database.dispose()
    started = Event()

    def migrate_all() -> float:
        before = time.monotonic()
        with index_database.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"transaction_per_migration": True}
            )
            with Operations.context(context):
                started.set()
                for revision in revisions:
                    with context.begin_transaction():
                        revision.upgrade()
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname IN ('ix_api_usage_governance_spend', "
                        "'ix_audit_log_account_unsealed', 'ix_api_usage_gateway_model_totals', "
                        "'ix_runtime_session_activity_code_tools', "
                        "'ix_issue_cost_execution_account_pr_key') AND i.indisvalid"
                    )
                ).scalar_one()
                == 5
            )
            assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        return time.monotonic() - before

    with index_database.connect() as writer, ThreadPoolExecutor(1) as executor:
        transaction = writer.begin()
        for table in ("api_usage", "audit_log", "runtime_session_activity"):
            writer.execute(text(f"INSERT INTO {table} (id) VALUES (-2)"))
        future = executor.submit(migrate_all)
        try:
            assert started.wait(5)
            time.sleep(8)
            assert not future.done(), "The old five-second budget canceled the build"
            with index_database.begin() as live_writer:
                live_writer.execute(text("SET LOCAL lock_timeout = '500ms'"))
                live_writer.execute(text("INSERT INTO api_usage (id) VALUES (-3)"))
        finally:
            transaction.rollback()
        elapsed = future.result(timeout=240)
    assert elapsed < 240, (
        f"Full sequence took {elapsed:.2f}s; original Helm deadline is 300s"
    )
    print(
        f"Full four-revision sequence, {usage_rows + 100000} rows, old writer and invalid index: {elapsed:.2f}s (original Helm deadline 300s)"
    )


def test_in_range_custom_concurrent_budgets_restore_original_settings(
    index_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator overrides apply to the SQL session only inside the explicit scope."""
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", "180")
    with index_database.connect() as connection:
        with concurrent_index_ddl(MigrationContext.configure(connection)):
            assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "45s"
            assert (
                connection.execute(text("SHOW statement_timeout")).scalar_one()
                == "3min"
            )
        assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "23s"


@pytest.mark.parametrize(
    ("lock", "statement"), [("300", "120"), ("60", "60"), ("60", "1")]
)
def test_conflicting_budgets_use_a_retryable_lock_wait_before_statement_deadline(
    index_database: Engine, monkeypatch: pytest.MonkeyPatch, lock: str, statement: str
) -> None:
    """A separately valid override must not turn a lock retry into cancellation."""
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", lock)
    monkeypatch.setenv(
        "PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", statement
    )
    with index_database.connect() as connection:
        with concurrent_index_ddl(MigrationContext.configure(connection)):
            assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "1min"
            assert (
                connection.execute(text("SHOW statement_timeout")).scalar_one()
                == "2min"
            )
        assert connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
        assert connection.execute(text("SHOW statement_timeout")).scalar_one() == "23s"


def test_valid_short_lock_budget_remains_retryable_before_statement_cancellation(
    index_database: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL reports lock contention first when a writer outlives the wait."""
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS", "2")
    with index_database.connect() as writer:
        transaction = writer.begin()
        writer.execute(text("INSERT INTO api_usage (id) VALUES (0)"))
        try:
            with index_database.connect() as connection:
                with pytest.raises(DBAPIError) as error:
                    with concurrent_index_ddl(MigrationContext.configure(connection)):
                        connection.execute(
                            text(
                                "CREATE INDEX CONCURRENTLY retryable_wait_test ON api_usage (id)"
                            )
                        )
                assert sqlstate_of(error.value) == "55P03"
                assert (
                    connection.execute(text("SHOW lock_timeout")).scalar_one() == "5s"
                )
                assert (
                    connection.execute(text("SHOW statement_timeout")).scalar_one()
                    == "23s"
                )
        finally:
            transaction.rollback()
