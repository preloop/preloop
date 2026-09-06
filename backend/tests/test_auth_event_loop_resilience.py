"""Authentication must let the API loop progress during real row contention."""

import asyncio
from collections.abc import Iterator
import threading
from uuid import UUID, uuid4
from typing import Any

import pytest
from sqlalchemy import Engine, event, select
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_password_hash, get_user_from_token_if_valid
from preloop.api.auth.router import authenticate_user
from preloop.models import models
from preloop.services.model_gateway_auth import authenticate_bearer_token


@pytest.fixture
def committed_auth_identity(db_engine: Engine) -> Iterator[tuple[UUID, UUID, str, str]]:
    """Create an isolated identity visible to independent lock owners."""
    account_id, user_id, key_id = uuid4(), uuid4(), uuid4()
    username = f"loop-audit-{user_id}"
    token = f"local-test-{key_id}"
    with Session(db_engine) as db:
        db.add(models.Account(id=account_id, organization_name="Loop regression"))
        db.flush()
        db.add(
            models.User(
                id=user_id,
                account_id=account_id,
                username=username,
                email="loop-test@example.com",
                is_active=True,
                hashed_password=get_password_hash("local-test-password"),
            )
        )
        db.flush()
        db.add(
            models.ApiKey(
                id=key_id,
                account_id=account_id,
                user_id=user_id,
                name="Local regression",
                key=token,
                is_active=True,
            )
        )
        db.commit()
    try:
        yield user_id, key_id, username, token
    finally:
        with Session(db_engine) as db:
            db.query(models.Account).filter(models.Account.id == account_id).delete()
            db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["login", "gateway", "manual_token"])
async def test_auth_row_lock_does_not_block_loop(
    db_engine: Engine, committed_auth_identity: tuple[UUID, UUID, str, str], kind: str
) -> None:
    """A timer on the request loop can release a competing auth-row writer."""
    user_id, key_id, username, token = committed_auth_identity
    locked = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def hold_lock() -> None:
        try:
            with Session(db_engine) as db:
                row = models.User if kind == "login" else models.ApiKey
                row_id = user_id if kind == "login" else key_id
                db.execute(select(row).where(row.id == row_id).with_for_update())
                locked.set()
                if not release.wait(3):
                    raise AssertionError("Lock owner did not receive release")
                db.rollback()
        except BaseException as exc:
            errors.append(exc)
            locked.set()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert await asyncio.to_thread(locked.wait, 3)
    assert not errors
    loop_released = False
    write_started = threading.Event()
    loop_thread = threading.get_ident()
    loop_statements: list[str] = []

    def observe_write(
        conn: Any,
        cursor: Any,
        statement: str,
        parameters: Any,
        context: Any,
        executemany: bool,
    ) -> None:
        if threading.get_ident() == loop_thread:
            loop_statements.append(statement.split()[0])
        if statement.startswith(
            'UPDATE "user"' if kind == "login" else "UPDATE api_key"
        ):
            write_started.set()

    event.listen(db_engine, "before_cursor_execute", observe_write)

    def release_from_loop() -> None:
        nonlocal loop_released
        loop_released = True
        release.set()

    async def release_when_writer_waits() -> None:
        assert await asyncio.to_thread(write_started.wait, 3)
        await asyncio.sleep(0.05)
        release_from_loop()

    timer = asyncio.create_task(release_when_writer_waits())
    # A blocked-loop regression must fail promptly rather than hang pytest.
    watchdog = threading.Timer(0.7, release.set)
    watchdog.start()
    try:
        with Session(db_engine) as db:
            if kind == "login":
                result = await authenticate_user(
                    username, "local-test-password", db, source_ip="testclient"
                )
            elif kind == "gateway":
                result = await authenticate_bearer_token(token, db)
            else:
                result = await get_user_from_token_if_valid(token, db)
            assert result is not None
            resolved_user = result.user if kind == "gateway" else result
            assert resolved_user.id == user_id
            assert not loop_statements, (
                f"Auth or returned user loaded on loop: {loop_statements}"
            )
            assert loop_released, (
                "Authentication blocked the loop until watchdog release"
            )
    finally:
        release.set()
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        event.remove(db_engine, "before_cursor_execute", observe_write)
        watchdog.cancel()
        await asyncio.to_thread(holder.join, 3)
    assert not errors


@pytest.mark.asyncio
async def test_off_loop_cancellation_waits_for_session_owner() -> None:
    """Request cleanup must not race a canceled auth operation still using DB."""
    from preloop.api.loop_safety import run_db_off_loop

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def operation() -> None:
        started.set()
        try:
            assert release.wait(3)
        finally:
            finished.set()

    task = asyncio.create_task(run_db_off_loop(operation))
    assert await asyncio.to_thread(started.wait, 3)
    try:
        task.cancel()
        await asyncio.sleep(0.02)
        assert not task.done(), "Canceled request can clean up an active worker session"
        task.cancel()  # Repeated cancellation must not break session ownership.
        await asyncio.sleep(0.02)
        assert not task.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 3)


@pytest.mark.asyncio
async def test_manual_auth_does_not_disguise_pool_exhaustion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Capacity errors must reach the shared overload handler, not become 401."""
    from sqlalchemy.exc import TimeoutError as PoolTimeout
    from preloop.api.auth import jwt

    def exhausted(*args: Any, **kwargs: Any) -> None:
        raise PoolTimeout("local pool exhausted")

    monkeypatch.setattr(jwt.crud_api_key, "get_by_key", exhausted)
    with pytest.raises(PoolTimeout, match="local pool exhausted"):
        await get_user_from_token_if_valid("local-test-key", object())


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["initial", "fallback"])
async def test_summary_model_queries_run_off_loop(
    monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Both optional-summary lookup paths must avoid blocking the API loop."""
    from types import SimpleNamespace
    from preloop.services import approval_summary, model_credentials

    loop_thread = threading.get_ident()
    query_threads: list[int] = []

    def lookup(*args: Any, **kwargs: Any) -> None:
        query_threads.append(threading.get_ident())

    monkeypatch.setattr(
        approval_summary.crud_ai_model, "get_default_active_model", lookup
    )
    if phase == "initial":
        await approval_summary.generate_approval_summary(
            object(), account_id="local-test", tool_name="Read"
        )
    else:
        monkeypatch.setattr(
            model_credentials, "resolve_model_call_credentials", lambda *a, **k: {}
        )

        def fail(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError("local primary unavailable")

        await model_credentials.call_with_default_model_fallback(
            db=object(),
            account_id="local-test-fallback",
            primary_model=SimpleNamespace(
                id="primary", model_identifier="local", provider_name="local"
            ),
            caller=fail,
            operation_name="local-test",
        )
    assert query_threads and all(thread != loop_thread for thread in query_threads)


@pytest.mark.asyncio
async def test_summary_timeout_does_not_reuse_active_worker_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fallback cannot use the same Session before a timed-out worker finishes."""
    from types import SimpleNamespace
    from preloop.services import model_credentials

    started = threading.Event()
    release = threading.Event()
    fallback_started = threading.Event()

    def credentials(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        assert release.wait(3)
        return {}

    def fallback(*args: Any, **kwargs: Any) -> None:
        fallback_started.set()

    monkeypatch.setattr(
        model_credentials, "resolve_model_call_credentials", credentials
    )
    monkeypatch.setattr(
        model_credentials.crud_ai_model, "get_default_active_model", fallback
    )
    task = asyncio.create_task(
        model_credentials.call_with_default_model_fallback(
            db=object(),
            account_id="local-worker-timeout",
            primary_model=SimpleNamespace(
                id="primary", model_identifier="local", provider_name="local"
            ),
            caller=lambda *a: "done",
            operation_name="local-timeout",
            attempt_timeout=0.02,
        )
    )
    assert await asyncio.to_thread(started.wait, 3)
    try:
        await asyncio.sleep(0.06)
        assert not fallback_started.is_set(), (
            "Fallback raced the original session owner"
        )
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_anyio_cancellation_drains_worker_without_blocking_loop() -> None:
    """ASGI-style cancellation must wait for the worker while timers still run."""
    import anyio
    from preloop.api.loop_safety import run_db_off_loop

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    scope_ready = asyncio.Event()
    scopes: list[anyio.CancelScope] = []
    returned = False

    def operation() -> None:
        started.set()
        try:
            assert release.wait(3)
        finally:
            finished.set()

    async def request() -> None:
        nonlocal returned
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            scope_ready.set()
            try:
                await run_db_off_loop(operation)
            finally:
                returned = True
                assert finished.is_set(), "Dependency cleanup raced worker"

    task = asyncio.create_task(request())
    await scope_ready.wait()
    assert await asyncio.to_thread(started.wait, 3)
    watchdog = threading.Timer(1, release.set)
    watchdog.start()
    try:
        scopes[0].cancel()
        await asyncio.sleep(0.03)
        assert not release.is_set(), "Loop failed to progress before watchdog"
        assert not returned
    finally:
        release.set()
        await task
        watchdog.cancel()
    assert returned and finished.is_set()
