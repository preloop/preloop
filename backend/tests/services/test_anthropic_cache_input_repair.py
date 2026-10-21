"""Rows written raw by pods on the previous release are converted after the
rollout (#1955 review: the migration alone cannot see them)."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.models.crud.api_usage import (
    ANTHROPIC_CACHE_INPUT_REPAIR_LOCK,
    ANTHROPIC_CACHE_INPUT_REPAIR_SQL,
)
from preloop.services import anthropic_cache_input_repair as repair

RAW = {
    "input_tokens": 100,
    "output_tokens": 50,
    "cache_read_input_tokens": 10_000,
    "cache_creation_input_tokens": 2_000,
}
NOW = datetime.now(timezone.utc).replace(tzinfo=None)


def _row(db: Session, account_id: Any, **changes: Any) -> models.ApiUsage:
    values: dict[str, Any] = {
        "account_id": account_id,
        "endpoint": "/anthropic/v1/messages",
        "method": "POST",
        "status_code": 200,
        "duration": 0.1,
        "action_type": "model_gateway",
        "usage_source": "provider",
        "timestamp": NOW - timedelta(minutes=5),
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "total_tokens": 150,
        "meta_data": {"usage_details": dict(RAW)},
    }
    row = models.ApiUsage(**(values | changes))
    db.add(row)
    db.flush()
    return row


def test_repair_converts_recent_raw_rows_once(db_session, test_user) -> None:
    raw = _row(db_session, test_user.account_id)
    old = _row(db_session, test_user.account_id, timestamp=NOW - timedelta(days=3))
    inclusive = _row(
        db_session, test_user.account_id, prompt_tokens=12_100, total_tokens=12_150
    )
    imported = _row(
        db_session,
        test_user.account_id,
        action_type="imported_usage",
        usage_source="imported",
    )
    since = NOW - timedelta(days=1)

    assert crud_api_usage.repair_anthropic_cache_input(db_session, since=since) == 1
    assert crud_api_usage.repair_anthropic_cache_input(db_session, since=since) == 0

    for row in (raw, old, inclusive, imported):
        db_session.refresh(row)
    assert (raw.prompt_tokens, raw.total_tokens) == (12_100, 12_150)
    assert raw.meta_data["input_cache_backfill"] == 12_000
    assert raw.meta_data["usage_details"]["input_tokens"] == 100
    # Outside the window, already inclusive, or imported: untouched.
    assert old.prompt_tokens == 100
    assert inclusive.prompt_tokens == 12_100
    assert "input_cache_backfill" not in inclusive.meta_data
    assert imported.prompt_tokens == 100


def test_repair_runs_once_after_the_delay() -> None:
    calls: list[datetime] = []
    with (
        patch.object(repair, "background_passes_allowed", return_value=True),
        patch.object(
            repair,
            "run_anthropic_cache_input_repair_once",
            side_effect=lambda since: calls.append(since) or 0,
        ),
    ):

        async def scenario() -> None:
            task = repair.AnthropicCacheInputRepair(delay_seconds=0)
            await task.start()
            await task.start()
            assert task._task is not None
            await task._task
            await task.stop()

        asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0] <= datetime.now(timezone.utc) - repair.REPAIR_LOOKBACK + (
        timedelta(seconds=5)
    )


def test_repair_stop_cancels_a_pending_pass() -> None:
    with (
        patch.object(repair, "background_passes_allowed", return_value=True),
        patch.object(repair, "run_anthropic_cache_input_repair_once") as run_once,
    ):

        async def scenario() -> None:
            task = repair.AnthropicCacheInputRepair(delay_seconds=3600)
            await task.start()
            await task.stop()

        asyncio.run(scenario())
    run_once.assert_not_called()


def test_repair_is_not_scheduled_on_a_gateway_only_role() -> None:
    with patch.object(repair, "background_passes_allowed", return_value=False):

        async def scenario() -> repair.AnthropicCacheInputRepair:
            task = repair.AnthropicCacheInputRepair(delay_seconds=0)
            await task.start()
            return task

        assert asyncio.run(scenario())._task is None


def _release(connection) -> None:
    """Return a pooled connection with the default search path."""
    connection.rollback()
    connection.execute(text("SET search_path TO public"))
    connection.commit()
    connection.close()


def _isolated_schema(connection, schema: str) -> None:
    connection.execute(text(f"CREATE SCHEMA {schema}"))
    connection.execute(text(f"SET search_path TO {schema}"))
    connection.execute(
        text(
            "CREATE TABLE api_usage (id uuid primary key, timestamp timestamp, "
            "action_type varchar(50), usage_source varchar(16), "
            "prompt_tokens integer, total_tokens integer, meta_data jsonb)"
        )
    )
    connection.execute(
        text(
            "INSERT INTO api_usage VALUES (:id, now(), 'model_gateway', "
            "'provider', 100, 150, CAST(:meta AS jsonb))"
        ),
        {"id": str(uuid4()), "meta": json.dumps({"usage_details": RAW})},
    )


def test_concurrent_passes_never_convert_a_row_twice(db_engine: Engine) -> None:
    """A writer that waited on the row lock re-checks the row and skips it."""
    schema = "anthropic_repair_race_" + uuid4().hex
    since = datetime(2000, 1, 1)
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as setup:
        _isolated_schema(setup, schema)
        setup.execute(text("SET search_path TO public"))
    try:
        first = db_engine.connect()
        second = db_engine.connect()
        try:
            for connection in (first, second):
                connection.execute(text(f"SET search_path TO {schema}"))
                connection.commit()
            # Both statements take their snapshot while the row is still raw.
            first_result = first.execute(
                text(ANTHROPIC_CACHE_INPUT_REPAIR_SQL), {"since": since}
            )
            assert first_result.rowcount == 1
            outcome: dict[str, int] = {}

            def run_second() -> None:
                result = second.execute(
                    text(ANTHROPIC_CACHE_INPUT_REPAIR_SQL), {"since": since}
                )
                outcome["rows"] = result.rowcount
                second.commit()

            worker = threading.Thread(target=run_second)
            worker.start()
            # Let the second statement reach the row lock, then release it.
            worker.join(timeout=1)
            first.commit()
            worker.join(timeout=10)
            assert not worker.is_alive()
            assert outcome["rows"] == 0
            prompt, total, added = first.execute(
                text(
                    "SELECT prompt_tokens, total_tokens, "
                    "(meta_data ->> 'input_cache_backfill')::int FROM api_usage"
                )
            ).one()
            assert (prompt, total, added) == (12_100, 12_150, 12_000)
        finally:
            _release(first)
            _release(second)
    finally:
        with db_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as cleanup:
            cleanup.execute(text(f"DROP SCHEMA {schema} CASCADE"))


def test_a_pass_skips_while_another_holds_the_lock(db_engine: Engine) -> None:
    schema = "anthropic_repair_lock_" + uuid4().hex
    with db_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as setup:
        _isolated_schema(setup, schema)
        setup.execute(text("SET search_path TO public"))
    holder = db_engine.connect()
    other = db_engine.connect()
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": ANTHROPIC_CACHE_INPUT_REPAIR_LOCK},
        )
        other.execute(text(f"SET search_path TO {schema}"))
        other.commit()
        session = Session(bind=other)
        assert (
            crud_api_usage.repair_anthropic_cache_input(
                session, since=datetime(2000, 1, 1)
            )
            == 0
        )
        assert (
            other.execute(text("SELECT prompt_tokens FROM api_usage")).scalar() == 100
        )
        holder.rollback()
        assert (
            crud_api_usage.repair_anthropic_cache_input(
                session, since=datetime(2000, 1, 1)
            )
            == 1
        )
        session.close()
    finally:
        _release(holder)
        _release(other)
        with db_engine.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as cleanup:
            cleanup.execute(text(f"DROP SCHEMA {schema} CASCADE"))
