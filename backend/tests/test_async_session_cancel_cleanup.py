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
