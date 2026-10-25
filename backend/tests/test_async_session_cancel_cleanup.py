"""A cancelled caller must still return its async connection to the pool."""

import asyncio

import pytest

from preloop.models.db import session as session_module


class _SlowSession:
    """Async session double whose rollback yields, so cancel lands mid-cleanup."""

    def __init__(self) -> None:
        self.rolled_back = False
        self.closed = False
        self.rollback_started = asyncio.Event()

    def in_transaction(self) -> bool:
        return True

    async def rollback(self) -> None:
        self.rollback_started.set()
        await asyncio.sleep(0.05)
        self.rolled_back = True

    async def close(self) -> None:
        await asyncio.sleep(0)
        self.closed = True

    async def invalidate(self) -> None:  # pragma: no cover - not reached
        self.closed = True


@pytest.mark.asyncio
async def test_cancel_during_cleanup_still_closes_session() -> None:
    db = _SlowSession()
    task = asyncio.ensure_future(session_module._safe_close_async_db_session(db))  # type: ignore[arg-type]
    await db.rollback_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(20):
        if db.closed:
            break
        await asyncio.sleep(0.01)
    assert db.rolled_back
    assert db.closed


@pytest.mark.asyncio
async def test_cleanup_without_cancel_closes_session() -> None:
    db = _SlowSession()
    await session_module._safe_close_async_db_session(db)  # type: ignore[arg-type]
    assert db.rolled_back and db.closed
    assert not session_module._pending_async_closes
    assert isinstance(session_module._pending_async_closes, set)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_body", [False, True])
async def test_context_cleanup_has_one_owner(
    monkeypatch: pytest.MonkeyPatch, cancel_body: bool
) -> None:
    class Session(_SlowSession):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.close_calls = 0
            self.overlap = False

        async def __aenter__(self) -> "Session":
            return self

        async def __aexit__(self, *args: object) -> None:
            await self.close()

        async def rollback(self) -> None:
            self.rollback_started.set()
            await self.release.wait()
            self.rolled_back = True

        async def close(self) -> None:
            self.overlap |= not self.rolled_back
            self.close_calls += 1
            await super().close()

    db = Session()
    monkeypatch.setattr(session_module, "get_async_session_factory", lambda: lambda: db)
    entered = asyncio.Event()

    async def request() -> None:
        async with session_module.get_async_db_session():
            entered.set()
            if cancel_body:
                await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await entered.wait()
    if cancel_body:
        task.cancel()
    await asyncio.wait_for(db.rollback_started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    db.release.set()
    await asyncio.gather(*session_module._pending_async_closes)
    assert db.rolled_back and db.closed
    assert db.close_calls == 1
    assert not db.overlap


@pytest.mark.asyncio
async def test_detached_cleanup_failure_is_retrieved_and_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    logger = MagicMock()
    monkeypatch.setattr(session_module, "logger", logger)
    failure = RuntimeError("cleanup failed")
    cleanup = asyncio.get_running_loop().create_future()
    cleanup.set_exception(failure)
    session_module._pending_async_closes.add(cleanup)
    session_module._async_close_done(cleanup)
    assert cleanup not in session_module._pending_async_closes
    logger.opt.assert_called_once_with(exception=failure)
    logger.opt.return_value.error.assert_called_once()
