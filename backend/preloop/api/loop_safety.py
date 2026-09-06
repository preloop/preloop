"""Helpers that keep synchronous database work off the API event loop.

Why this module exists
----------------------
FastAPI dispatches ``def`` handlers (and ``def`` dependencies, including the
``get_db_session`` generator) on the anyio worker threadpool, but it runs
``async def`` handlers directly on the event loop. A SQLAlchemy ``Session`` is
lazy: no connection is checked out when the dependency yields, only when the
first query runs inside the handler body. So an ``async def`` handler that
takes ``db: Session = Depends(get_db_session)`` and then queries is doing a
blocking pool checkout on the loop. When the pool is saturated that checkout
blocks for up to ``pool_timeout`` seconds, and while it blocks *nothing else*
on that worker can run, including the dependency-free ``/api/v1/ping``
liveness probe. The kubelet then kills a process whose only problem was a
queue of database waiters.

Handlers on hot console paths therefore either stay plain ``def`` (threadpool)
or wrap their synchronous body in :func:`run_db_off_loop`.
"""

from __future__ import annotations

import asyncio
from typing import Callable, TypeVar

from anyio import CancelScope
from fastapi.concurrency import run_in_threadpool

T = TypeVar("T")

__all__ = ["run_db_off_loop"]


async def run_db_off_loop(operation: Callable[[], T]) -> T:
    """Run a blocking database call on the threadpool instead of the loop.

    The request-scoped ``Session`` stays the same object; only the blocking
    part (pool checkout, query, result fetch) moves to a worker thread, so a
    saturated pool costs one worker thread rather than the whole event loop.
    The session is still used by one thread at a time, which is all
    SQLAlchemy's session-per-request contract requires.

    Args:
        operation: Zero-argument callable performing the synchronous database
            work, usually a closure over the request-scoped session.

    Returns:
        Whatever ``operation`` returns.
    """
    # Shield the worker future from raw asyncio cancellation as well as AnyIO
    # cancellation scopes. Dependency cleanup may rollback/close the same
    # request Session as soon as this coroutine exits.
    worker = asyncio.create_task(run_in_threadpool(operation))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        # AnyIO cancellation is level-triggered. Shield its enclosing scope
        # while draining; still handle repeated raw Task.cancel separately.
        with CancelScope(shield=True):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        # Retrieve a worker exception even when cancellation takes precedence.
        if not worker.cancelled():
            worker.exception()
        raise
