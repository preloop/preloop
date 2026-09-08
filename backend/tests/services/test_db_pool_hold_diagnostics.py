"""Local pool events reveal holds without reading application/SQL payloads."""

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool
from sqlalchemy.util.concurrency import greenlet_spawn

from preloop.models.db import pool_diagnostics as diagnostics
from preloop.services.db_pool_monitor import DbPoolMonitor


@pytest.fixture(autouse=True)
def capture_monitor_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("preloop.services.db_pool_monitor")
    monkeypatch.setattr(logger, "handlers", [*logger.handlers, caplog.handler])
    # The handler is attached directly; propagation would capture it again at root.
    monkeypatch.setattr(logger, "propagate", False)


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.delenv("DB_POOL_HOLD_DIAGNOSTICS", raising=False)
    monkeypatch.delenv("DB_POOL_HOLD_STACKS", raising=False)
    pool_engine = create_engine(
        "sqlite://", poolclass=QueuePool, pool_size=2, max_overflow=0
    )
    diagnostics.install_pool_hold_diagnostics(pool_engine)
    yield pool_engine
    pool_engine.dispose()


def test_disabled_does_not_capture_or_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_POOL_HOLD_DIAGNOSTICS", "false")
    engine = create_engine("sqlite://", poolclass=QueuePool)
    with patch.object(diagnostics, "_capture_callsite") as capture:
        diagnostics.install_pool_hold_diagnostics(engine)
        with engine.connect():
            assert diagnostics.collect_pool_holds(engine) is None
        capture.assert_not_called()
    engine.dispose()


def test_checkout_age_and_release(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [100.0]
    monkeypatch.setattr(diagnostics, "monotonic", lambda: now[0])
    connection = engine.connect()
    now[0] += 27.0
    snapshot = diagnostics.collect_pool_holds(engine)
    assert snapshot["oldest"][0]["held_seconds"] == 27.0
    connection.close()
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0


@pytest.mark.asyncio
async def test_delayed_provider_keeps_acquisition_evidence(engine: Engine) -> None:
    provider_can_finish = asyncio.Event()
    connection = engine.connect()

    async def delayed_provider() -> None:
        await provider_can_finish.wait()
        connection.close()

    provider = asyncio.create_task(delayed_provider())
    await asyncio.sleep(0)
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 1
    provider_can_finish.set()
    assert await provider is None
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0


def test_saturation_evidence_survives_release_and_expires(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    now = [100.0]
    monkeypatch.setattr(diagnostics, "monotonic", lambda: now[0])
    first, second = engine.connect(), engine.connect()
    now[0] += 27.0
    first.close()
    second.close()
    # Simulate a stalled monitor recovering only after the requests finished.
    with (
        patch("preloop.models.db.session.get_engine_if_initialized", return_value=None),
        patch(
            "preloop.models.db.session.get_async_engine_if_initialized",
            return_value=SimpleNamespace(sync_engine=engine),
        ),
        caplog.at_level(logging.WARNING),
    ):
        stats = DbPoolMonitor().check_once()
    assert stats[0]["checked_out"] == 0
    assert stats[0]["holds"]["recent_released"][0]["held_seconds"] == 27.0
    assert "engine=async" in caplog.text
    assert "recently saturated" in caplog.text
    now[0] += diagnostics.RECENT_HOLD_TTL_SECONDS + 1
    snapshot = diagnostics.collect_pool_holds(engine)
    assert snapshot["recent_released"] == []
    assert snapshot["recent_saturation_seconds_ago"] is None


@pytest.mark.parametrize("action", ["invalidate", "detach"])
def test_release_events_remove_metadata(engine: Engine, action: str) -> None:
    connection = engine.connect()
    getattr(connection, action)()
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0
    connection.close()


def test_dispose_rebinds_and_does_not_keep_old_holds(engine: Engine) -> None:
    old = engine.connect()
    tracker = getattr(engine, diagnostics._ATTRIBUTE)
    old_pool = engine.pool
    engine.dispose()
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0
    for name, callback in tracker._listeners:
        assert not event.contains(old_pool, name, callback)
        assert event.contains(engine.pool, name, callback)
    with engine.connect():
        assert diagnostics.collect_pool_holds(engine)["tracked"] == 1
        old.close()
        assert diagnostics.collect_pool_holds(engine)["tracked"] == 1
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0


def test_active_and_recent_state_are_capped(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(diagnostics, "MAX_TRACKED_HOLDS", 2)
    tracker = getattr(engine, diagnostics._ATTRIBUTE)
    now = [100.0]
    monkeypatch.setattr(diagnostics, "monotonic", lambda: now[0])
    records = [object() for _ in range(8)]
    for record in records:
        tracker._checkout(None, record, None)
    assert len(tracker._holds) == 2
    tracker._last_saturation = now[0]
    now[0] += 10
    for record in records:
        tracker._release(None, record)
    assert len(tracker._holds) == 0
    for _ in range(10):
        record = object()
        tracker._checkout(None, record, None)
        tracker._last_saturation = now[0]
        now[0] += 10
        tracker._release(None, record)
    assert len(tracker._recent) == diagnostics.MAX_REPORTED_HOLDS


def test_callsite_does_not_read_locals_sql_or_source(engine: Engine) -> None:
    secret = "private-customer-token-and-account-id"
    namespace: dict[str, Any] = {"engine": engine}
    filename = str(Path(diagnostics._PACKAGE_ROOT) / "services" / "sample_provider.py")
    exec(
        compile(
            "def delayed_provider():\n"
            f"    authorization = {secret!r}\n"
            "    sql = 'SELECT private_credentials FROM private_accounts'\n"
            "    return engine.connect()\n",
            filename,
            "exec",
        ),
        namespace,
    )
    connection = namespace["delayed_provider"]()
    snapshot = json.dumps(diagnostics.collect_pool_holds(engine))
    assert "services/sample_provider.py:delayed_provider:4" in snapshot
    for forbidden in (
        secret,
        "authorization",
        "SELECT",
        "private_accounts",
        diagnostics._PACKAGE_ROOT,
    ):
        assert forbidden not in snapshot
    connection.close()


def test_listener_failures_do_not_prevent_checkout(engine: Engine) -> None:
    with patch.object(
        diagnostics, "_capture_callsite", side_effect=RuntimeError("private")
    ):
        with engine.connect():
            assert diagnostics.collect_pool_holds(engine)["tracked"] == 0


def test_install_once_and_partial_failure_cleanup(engine: Engine) -> None:
    tracker = getattr(engine, diagnostics._ATTRIBUTE)
    diagnostics.install_pool_hold_diagnostics(engine)
    assert getattr(engine, diagnostics._ATTRIBUTE) is tracker
    other = create_engine("sqlite://", poolclass=QueuePool)
    listen = event.listen
    calls = []

    def fail_on_engine(*args: Any, **kwargs: Any) -> None:
        if args[1] == "engine_disposed":
            raise RuntimeError("private")
        calls.append(args)
        listen(*args, **kwargs)

    with patch.object(event, "listen", side_effect=fail_on_engine):
        diagnostics.install_pool_hold_diagnostics(other)
    assert diagnostics.collect_pool_holds(other) is None
    for target, name, callback in calls:
        assert not event.contains(target, name, callback)
    with other.connect():
        pass
    other.dispose()


@pytest.mark.asyncio
async def test_callsite_crosses_sqlalchemy_async_greenlet(engine: Engine) -> None:
    namespace: dict[str, Any] = {"engine": engine, "greenlet_spawn": greenlet_spawn}
    filename = str(Path(diagnostics._PACKAGE_ROOT) / "services" / "async_provider.py")
    exec(
        compile(
            "async def prepare_provider():\n"
            "    return await greenlet_spawn(engine.connect)\n",
            filename,
            "exec",
        ),
        namespace,
    )
    connection = await namespace["prepare_provider"]()
    try:
        snapshot = json.dumps(diagnostics.collect_pool_holds(engine))
        assert "services/async_provider.py:prepare_provider:2" in snapshot
    finally:
        connection.close()


def test_request_factories_instrument_both_engines_but_not_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from preloop.models.db import session

    for name in ("_engine", "_async_engine", "_health_engine"):
        monkeypatch.setattr(session, name, None)
    sync, async_engine, health = MagicMock(), MagicMock(), MagicMock()
    with (
        patch.object(session, "create_engine", side_effect=[sync, health]),
        patch.object(session, "create_async_engine", return_value=async_engine),
        patch.object(session, "check_pgvector_extension", return_value=True),
        patch.object(session, "install_pool_hold_diagnostics") as install,
        patch.dict("os.environ", {"DATABASE_URL": "postgresql://local/test"}),
    ):
        assert session.get_engine() is sync
        assert session.get_async_engine() is async_engine
        assert session.get_health_engine() is health
        assert [call.args[0] for call in install.call_args_list] == [
            sync,
            async_engine.sync_engine,
        ]


def test_public_stats_omit_acquisition_metadata(engine: Engine) -> None:
    from preloop.services.db_pool_monitor import collect_pool_stats

    with (
        engine.connect(),
        patch(
            "preloop.models.db.session.get_engine_if_initialized", return_value=engine
        ),
        patch(
            "preloop.models.db.session.get_async_engine_if_initialized",
            return_value=None,
        ),
    ):
        public = collect_pool_stats()
        logged = collect_pool_stats(include_holds=True)
    assert "holds" not in public[0]
    assert "acquired_at" not in json.dumps(public)
    assert logged[0]["holds"]["tracked"] == 1


def test_dispose_during_capture_cannot_reinsert_old_hold(engine: Engine) -> None:
    tracker = getattr(engine, diagnostics._ATTRIBUTE)
    old_record = object()
    with patch.object(
        diagnostics,
        "_capture_callsite",
        side_effect=lambda _limit: engine.dispose() or (),
    ):
        tracker._checkout(None, old_record, None)
    assert diagnostics.collect_pool_holds(engine)["tracked"] == 0
