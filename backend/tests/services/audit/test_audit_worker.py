"""Audit writes must not occupy the event loop or an unbounded work queue."""

from __future__ import annotations

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any, Callable
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from preloop.services.audit import service as audit

_SECRET = "audit-secret-token-9f3c"

WRAPPERS = [
    ("log_approval_lifecycle", {"approval_id": "approval", "event": "approved"}),
    (
        "log_approval_notification",
        {"approval_id": "approval", "channel": "email", "status": "sent"},
    ),
    (
        "log_approval_tool_executed",
        {"approval_id": "approval", "tool_name": "tool", "status": "executed"},
    ),
    ("log_tool_call", {"tool_name": "tool", "tool_args": {"value": "before"}}),
    ("log_policy_decision", {"tool_name": "tool", "action": "allow"}),
    (
        "log_runtime_session_event",
        {
            "runtime_session_id": "session",
            "event": "created",
            "session_source_type": "cli",
            "session_source_id": "source",
        },
    ),
    (
        "log_ai_approval",
        {
            "approval_request_id": "approval",
            "tool_name": "tool",
            "decision": "approve",
            "confidence": 1.0,
            "reasoning": "safe",
            "model_used": "model",
        },
    ),
]


async def wait_until(done: threading.Event) -> None:
    """Wait on a worker observation without occupying another executor."""
    async with asyncio.timeout(2):
        while not done.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,fields", WRAPPERS)
async def test_all_async_wrappers_own_session_in_worker(
    monkeypatch: pytest.MonkeyPatch, method: str, fields: dict[str, Any]
) -> None:
    loop_thread = threading.get_ident()
    observed: list[tuple[str, int]] = []
    done = threading.Event()
    account_id = uuid4()
    service = audit.AuditService()

    def close() -> None:
        observed.append(("close", threading.get_ident()))
        done.set()

    def factory() -> Any:
        observed.append(("factory", threading.get_ident()))
        return SimpleNamespace(close=close)

    def write(**kwargs: Any) -> None:
        assert kwargs["account_id"] == account_id
        observed.append(("write", threading.get_ident()))

    monkeypatch.setattr(service, method, write)
    getattr(service, method + "_async")(
        db_factory=factory, account_id=account_id, **fields
    )
    await wait_until(done)
    assert [name for name, _ in observed] == ["factory", "write", "close"]
    assert len({ident for _, ident in observed}) == 1
    assert observed[0][1] != loop_thread


@pytest.fixture(autouse=True)
def isolated_writer(monkeypatch: pytest.MonkeyPatch) -> Any:
    from preloop.services.audit import async_writer

    writer = async_writer.BoundedAuditWriter(
        max_workers=2, max_pending=4, reserved_priority=0
    )
    monkeypatch.setattr(async_writer, "writer", writer)
    yield writer
    writer.shutdown()


@pytest.mark.asyncio
async def test_priority_writes_use_reserved_slots() -> None:
    from preloop.services.audit.async_writer import BoundedAuditWriter

    started = threading.Event()
    release = threading.Event()
    writer = BoundedAuditWriter(max_workers=1, max_pending=4, reserved_priority=2)
    timer = threading.Timer(1, release.set)
    timer.start()

    def factory() -> Any:
        return SimpleNamespace(close=lambda: None)

    def blocked(**kwargs: Any) -> None:
        started.set()
        assert release.wait(2)

    try:
        for _ in range(2):
            writer.submit(
                asyncio.get_running_loop(), factory, blocked, {}, priority=False
            )
        await wait_until(started)
        writer.submit(asyncio.get_running_loop(), factory, blocked, {}, priority=False)
        assert writer.pending_count == 2
        assert writer.dropped_count == 1
        writer.submit(asyncio.get_running_loop(), factory, blocked, {}, priority=True)
        writer.submit(asyncio.get_running_loop(), factory, blocked, {}, priority=True)
        assert writer.pending_count == 4
        assert writer.dropped_count == 1
        writer.submit(asyncio.get_running_loop(), factory, blocked, {}, priority=True)
        assert writer.dropped_count == 2
    finally:
        release.set()
        timer.cancel()
        writer.shutdown()
        await wait_idle(writer)


@pytest.mark.asyncio
async def test_nested_containers_are_copied_scalars_are_shared(
    monkeypatch: pytest.MonkeyPatch, isolated_writer: Any
) -> None:
    from preloop.services.audit import async_writer

    copied: list[object] = []
    real_deepcopy = async_writer.deepcopy

    def spy(value: object) -> object:
        copied.append(value)
        return real_deepcopy(value)

    monkeypatch.setattr(async_writer, "deepcopy", spy)
    writes: list[dict[str, Any]] = []
    done = threading.Event()
    account_id = "account"
    tool_args = {"nested": "original"}

    def write(**kwargs: Any) -> None:
        writes.append(kwargs)
        done.set()

    async_writer.schedule_audit_write(
        lambda: SimpleNamespace(close=lambda: None),
        write,
        account_id=account_id,
        tool_name="tool",
        tool_args=tool_args,
    )
    tool_args["nested"] = "mutated"
    await wait_until(done)
    await wait_idle(isolated_writer)
    assert writes[0]["account_id"] is account_id
    assert writes[0]["tool_name"] == "tool"
    assert writes[0]["tool_args"] is not tool_args
    assert writes[0]["tool_args"]["nested"] == "original"
    assert copied == [tool_args]


async def wait_idle(writer: Any) -> None:
    async with asyncio.timeout(2):
        while writer.pending_count:
            await asyncio.sleep(0.001)


@pytest.mark.asyncio
async def test_blocked_database_keeps_loop_responsive_and_admission_bounded(
    isolated_writer: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from preloop.services.audit.async_writer import schedule_audit_write

    started = threading.Event()
    release = threading.Event()
    factories: list[int] = []
    closed: list[int] = []
    timer = threading.Timer(1, release.set)
    timer.start()

    def factory() -> Any:
        factories.append(threading.get_ident())
        return SimpleNamespace(close=lambda: closed.append(threading.get_ident()))

    def blocked(**kwargs: Any) -> None:
        started.set()
        assert release.wait(2)

    before = len(asyncio.all_tasks())
    try:
        for _ in range(1000):
            schedule_audit_write(factory, blocked, account_id="account")
        await wait_until(started)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not release.is_set(), "Blocked audit prevented the loop from advancing"
        assert isolated_writer.pending_count == 4
        assert isolated_writer.dropped_count == 996
        assert len(factories) <= 2
        assert len(asyncio.all_tasks()) == before
        assert "dropped_total=" in caplog.text
        assert len(caplog.records) < 20
    finally:
        release.set()
        timer.cancel()
        await wait_idle(isolated_writer)
    assert len(factories) == len(closed) == 4


@pytest.mark.asyncio
async def test_context_snapshot_and_broadcast_stay_on_origin_loop(
    monkeypatch: pytest.MonkeyPatch, isolated_writer: Any
) -> None:
    from contextvars import ContextVar
    from preloop.services.audit import async_writer
    from preloop.services import account_realtime

    trace: ContextVar[str] = ContextVar("audit_test_trace", default="missing")
    trace.set("request-context")
    loop_thread = threading.get_ident()
    broadcast = asyncio.Event()
    writes: list[dict[str, Any]] = []
    details = {"nested": {"label": "original"}}
    release = threading.Event()
    single = async_writer.BoundedAuditWriter(max_workers=1, max_pending=2)
    monkeypatch.setattr(async_writer, "writer", single)

    def write(**kwargs: Any) -> None:
        assert release.wait(2)
        assert trace.get() == "request-context"
        assert threading.get_ident() != loop_thread
        writes.append(kwargs)
        audit._broadcast_audit_event(
            account_id="account", action="test", status="ok", details=kwargs["details"]
        )

    def emitted(event: dict[str, Any]) -> None:
        assert threading.get_ident() == loop_thread
        assert trace.get() == "request-context"
        assert event["account_id"] == "account"
        broadcast.set()

    monkeypatch.setattr(account_realtime, "emit_account_event", emitted)
    try:
        async_writer.schedule_audit_write(
            lambda: SimpleNamespace(close=lambda: None), write, details=details
        )
        details["nested"]["label"] = "mutated"
        trace.set("later-context")
        release.set()
        await asyncio.wait_for(broadcast.wait(), 2)
        await wait_idle(single)
    finally:
        release.set()
        single.shutdown()
    assert writes[0]["details"]["nested"]["label"] == "original"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["factory", "write", "close", "submit"])
async def test_failures_release_admission_and_worker_owned_session(
    monkeypatch: pytest.MonkeyPatch, isolated_writer: Any, failure: str
) -> None:
    from preloop.services.audit.async_writer import schedule_audit_write

    closed: list[int] = []

    def close() -> None:
        closed.append(threading.get_ident())
        if failure == "close":
            raise RuntimeError("close failed")

    def factory() -> Any:
        if failure == "factory":
            raise RuntimeError("factory failed")
        return SimpleNamespace(close=close)

    def write(**kwargs: Any) -> None:
        if failure == "write":
            raise RuntimeError("write failed")

    with monkeypatch.context() as patch:
        if failure == "submit":

            def reject(*args: Any, **kwargs: Any) -> Any:
                raise RuntimeError("submit failed")

            patch.setattr(isolated_writer._executor, "submit", reject)
        schedule_audit_write(factory, write)
        await wait_idle(isolated_writer)
    assert len(closed) == (0 if failure in {"factory", "submit"} else 1)
    assert isolated_writer.dropped_count == (1 if failure == "submit" else 0)
    done = threading.Event()
    schedule_audit_write(lambda: SimpleNamespace(close=done.set), lambda **kwargs: None)
    await wait_until(done)
    await wait_idle(isolated_writer)


@pytest.mark.asyncio
async def test_shutdown_cancels_pending_without_waiting_or_releasing_running_slot(
    isolated_writer: Any,
) -> None:
    from preloop.services.audit.async_writer import schedule_audit_write

    started = threading.Event()
    release = threading.Event()
    closed: list[int] = []

    def factory() -> Any:
        return SimpleNamespace(close=lambda: closed.append(threading.get_ident()))

    def blocked(**kwargs: Any) -> None:
        started.set()
        assert release.wait(2)

    timer = threading.Timer(1, release.set)
    timer.start()
    try:
        for _ in range(4):
            schedule_audit_write(factory, blocked)
        await wait_until(started)
        isolated_writer.shutdown()
        assert not release.is_set(), "Shutdown waited for blocked audit workers"
        assert 1 <= isolated_writer.pending_count <= 2
        assert isolated_writer.dropped_count == 4 - isolated_writer.pending_count
        assert not closed
    finally:
        release.set()
        timer.cancel()
        await wait_idle(isolated_writer)
    assert 1 <= len(closed) <= 2


def test_without_running_loop_skips_without_coroutine_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import gc
    import warnings
    from unittest.mock import Mock

    service = audit.AuditService()
    factory = Mock()
    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        service.log_approval_lifecycle_async(factory, "account", "approval", "approved")
        gc.collect()
    factory.assert_not_called()
    assert not any("never awaited" in str(w.message) for w in seen)


@pytest.mark.asyncio
async def test_disabled_service_does_not_submit(isolated_writer: Any) -> None:
    from unittest.mock import Mock

    service = audit.AuditService()
    service.disable()
    factory = Mock()
    service.log_approval_lifecycle_async(factory, "account", "approval", "approved")
    assert isolated_writer.pending_count == isolated_writer.dropped_count == 0
    factory.assert_not_called()


def _pool_timeout() -> PoolTimeoutError:
    return PoolTimeoutError(f"QueuePool checkout timed out token={_SECRET}")


def _disconnected(*, invalidated: bool) -> OperationalError:
    message = "boom" if invalidated else "SSL connection has been closed unexpectedly"
    return OperationalError(
        f"INSERT INTO audit_log (secret) VALUES ('{_SECRET}')",
        {"token": _SECRET},
        Exception(message),
        connection_invalidated=invalidated,
    )


def _deadlock() -> OperationalError:
    return OperationalError(
        "SELECT 'ssl connection has been closed' AS note",
        {"token": _SECRET},
        Exception("deadlock detected"),
        connection_invalidated=False,
    )


def _warning_records(
    caplog: pytest.LogCaptureFixture, logger_name: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno == logging.WARNING and record.name == logger_name
    ]


def _error_records(
    caplog: pytest.LogCaptureFixture, logger_name: str
) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and record.name == logger_name
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["factory", "close"])
@pytest.mark.parametrize(
    ("make_exc", "fragment"),
    [
        (_pool_timeout, "pool timeout"),
        (lambda: _disconnected(invalidated=True), "disconnected"),
        (lambda: _disconnected(invalidated=False), "disconnected"),
    ],
)
async def test_session_boundary_infrastructure_failures_warn_and_recover(
    isolated_writer: Any,
    caplog: pytest.LogCaptureFixture,
    boundary: str,
    make_exc: Callable[[], BaseException],
    fragment: str,
) -> None:
    """Pool and disconnect failures at session open or close stay warnings."""
    from preloop.services.audit.async_writer import schedule_audit_write

    closed: list[int] = []
    exc = make_exc()

    def close() -> None:
        closed.append(1)
        if boundary == "close":
            raise exc

    def factory() -> Any:
        if boundary == "factory":
            raise exc
        return SimpleNamespace(close=close)

    caplog.set_level(logging.DEBUG, logger="preloop.services.audit.async_writer")
    schedule_audit_write(factory, lambda **kwargs: None)
    await wait_idle(isolated_writer)

    assert len(closed) == (0 if boundary == "factory" else 1)
    assert isolated_writer.pending_count == 0
    assert isolated_writer.dropped_count == 0
    warnings = _warning_records(caplog, "preloop.services.audit.async_writer")
    assert len(warnings) == 1
    assert fragment in warnings[0].getMessage()
    assert warnings[0].exc_info is None
    assert _error_records(caplog, "preloop.services.audit.async_writer") == []
    assert _SECRET not in caplog.text
    assert "Traceback" not in caplog.text

    seen: dict[str, Any] = {}
    done = threading.Event()

    def healthy(**kwargs: Any) -> None:
        seen["account_id"] = kwargs["account_id"]
        seen["event"] = kwargs["event"]
        done.set()

    schedule_audit_write(
        lambda: SimpleNamespace(close=lambda: None),
        healthy,
        account_id="account",
        event="approved",
    )
    await wait_until(done)
    await wait_idle(isolated_writer)
    assert seen == {"account_id": "account", "event": "approved"}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["factory", "close"])
@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: RuntimeError(f"programming fault {_SECRET}"),
        lambda: TimeoutError(f"builtin timeout {_SECRET}"),
        lambda: IntegrityError(
            "INSERT INTO audit_log (id) VALUES (%(id)s)",
            {"id": _SECRET},
            Exception("duplicate key value violates unique constraint"),
        ),
        _deadlock,
    ],
)
async def test_session_boundary_other_failures_keep_error_diagnostics(
    isolated_writer: Any,
    caplog: pytest.LogCaptureFixture,
    boundary: str,
    make_exc: Callable[[], BaseException],
) -> None:
    """Unrelated and non-disconnect operational errors keep a traceback."""
    from preloop.services.audit.async_writer import schedule_audit_write

    exc = make_exc()

    def close() -> None:
        if boundary == "close":
            raise exc

    def factory() -> Any:
        if boundary == "factory":
            raise exc
        return SimpleNamespace(close=close)

    caplog.set_level(logging.DEBUG, logger="preloop.services.audit.async_writer")
    schedule_audit_write(factory, lambda **kwargs: None)
    await wait_idle(isolated_writer)

    assert isolated_writer.pending_count == 0
    assert isolated_writer.dropped_count == 0
    errors = _error_records(caplog, "preloop.services.audit.async_writer")
    assert len(errors) == 1
    assert errors[0].exc_info is not None
    assert "Asynchronous audit database write failed" in errors[0].getMessage()
    assert not any(
        record.levelno == logging.WARNING and "pool timeout" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("make_exc", "fragment", "expect_warning"),
    [
        (_pool_timeout, "pool timeout", True),
        (lambda: _disconnected(invalidated=True), "disconnected", True),
        (lambda: _disconnected(invalidated=False), "disconnected", True),
        (_deadlock, "", False),
        (lambda: RuntimeError(f"programming fault {_SECRET}"), "", False),
        (
            lambda: IntegrityError(
                "INSERT INTO audit_log (id) VALUES (%(id)s)",
                {"id": _SECRET},
                Exception("duplicate key value violates unique constraint"),
            ),
            "",
            False,
        ),
    ],
)
async def test_lifecycle_crud_failure_classification_closes_session(
    monkeypatch: pytest.MonkeyPatch,
    isolated_writer: Any,
    caplog: pytest.LogCaptureFixture,
    make_exc: Callable[[], BaseException],
    fragment: str,
    expect_warning: bool,
) -> None:
    """Lifecycle CRUD failures stay inside the worker and do not drop admission."""
    closed: list[int] = []
    broadcasts: list[dict[str, Any]] = []
    stored: list[dict[str, Any]] = []

    def close() -> None:
        closed.append(1)

    def fail(**kwargs: Any) -> None:
        raise make_exc()

    def record_broadcast(**kwargs: Any) -> None:
        broadcasts.append(kwargs)

    monkeypatch.setattr(
        "preloop.services.audit.service.crud_audit_log.log_action", fail
    )
    monkeypatch.setattr(audit, "_broadcast_audit_event", record_broadcast)
    caplog.set_level(logging.DEBUG)
    service = audit.AuditService()
    service.log_approval_lifecycle_async(
        lambda: SimpleNamespace(close=close),
        "account",
        "approval",
        "approved",
    )
    await wait_idle(isolated_writer)

    assert closed == [1]
    assert isolated_writer.pending_count == 0
    assert isolated_writer.dropped_count == 0
    assert broadcasts == []
    service_warnings = _warning_records(caplog, "preloop.services.audit.service")
    service_errors = _error_records(caplog, "preloop.services.audit.service")
    worker_errors = _error_records(caplog, "preloop.services.audit.async_writer")
    if expect_warning:
        assert len(service_warnings) == 1
        assert fragment in service_warnings[0].getMessage()
        assert service_warnings[0].exc_info is None
        assert service_errors == []
        assert worker_errors == []
        assert _SECRET not in caplog.text
        assert "Traceback" not in caplog.text
    else:
        assert len(service_errors) == 1
        assert service_errors[0].exc_info is not None
        assert service_warnings == []

    def succeed(**kwargs: Any) -> Any:
        stored.append(kwargs)
        return SimpleNamespace(id="log-1")

    done = threading.Event()

    def close_ok() -> None:
        done.set()

    monkeypatch.setattr(
        "preloop.services.audit.service.crud_audit_log.log_action", succeed
    )
    service.log_approval_lifecycle_async(
        lambda: SimpleNamespace(close=close_ok),
        "account",
        "approval-2",
        "approved",
        reason="ok",
    )
    await wait_until(done)
    await wait_idle(isolated_writer)
    assert stored[0]["resource_type"] == "approval"
    assert stored[0]["status"] == "approved"
    assert stored[0]["details"]["approval_id"] == "approval-2"
    assert stored[0]["details"]["reason"] == "ok"
    assert broadcasts[0]["action"] == "approval_approved"
    assert broadcasts[0]["status"] == "approved"
    assert broadcasts[0]["details"]["approval_id"] == "approval-2"
    assert broadcasts[0]["audit_log_id"] == "log-1"


def test_disconnect_phrases_match_sentry_transient_database_list() -> None:
    """Audit disconnect phrases stay aligned with the Sentry filter list."""
    from preloop.utils.sentry_filters import _TRANSIENT_DB_PHRASES

    from preloop.services.audit.async_writer import _DISCONNECTED_CONNECTION_PHRASES

    assert _DISCONNECTED_CONNECTION_PHRASES == _TRANSIENT_DB_PHRASES


def test_infrastructure_skip_is_counted_without_changing_drop_accounting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pool and disconnect warnings increment their own counter, not drops."""
    from preloop.services.audit import async_writer

    reasons: list[str] = []
    drops: list[str] = []
    monkeypatch.setattr(
        async_writer, "_increment_infrastructure_skip_metric", reasons.append
    )
    monkeypatch.setattr(async_writer, "_increment_drop_metric", drops.append)
    log = logging.getLogger("preloop.services.audit.async_writer")
    async_writer.log_best_effort_failure(
        log, _pool_timeout(), summary="Asynchronous audit database write failed"
    )
    async_writer.log_best_effort_failure(
        log,
        _disconnected(invalidated=True),
        summary="Asynchronous audit database write failed",
    )
    async_writer.log_best_effort_failure(
        log,
        RuntimeError("programming fault"),
        summary="Asynchronous audit database write failed",
    )
    assert reasons == ["pool_timeout", "disconnected_connection"]
    assert drops == []


def test_infrastructure_skip_metric_does_not_raise() -> None:
    """The skip counter is best effort when metrics are unavailable."""
    from preloop.services.audit import async_writer

    async_writer._infrastructure_skip_counter = None
    async_writer._infrastructure_skip_counter_failed = False
    async_writer._increment_infrastructure_skip_metric("pool_timeout")
    async_writer._infrastructure_skip_counter_failed = False
