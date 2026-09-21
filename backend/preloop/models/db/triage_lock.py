"""Bounded dedicated connections for triage locks held across data commits."""

from contextlib import contextmanager
from dataclasses import dataclass
from threading import BoundedSemaphore, Lock
from typing import Iterator
from weakref import WeakKeyDictionary

from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import QueuePool


@dataclass
class _LockPool:
    engine: Engine
    admission: BoundedSemaphore


_pools: WeakKeyDictionary[Engine, _LockPool] = WeakKeyDictionary()
_pools_lock = Lock()


def _lock_pool(source: Engine) -> _LockPool:
    """Keep the source connection creator, dialect and pool event handlers."""
    with _pools_lock:
        state = _pools.get(source)
        if state is None:
            # Admission bounds actual connections even if the source permits
            # overflow. Never admit more than the recreated pool's base size.
            capacity = (
                min(2, max(1, source.pool.size()))
                if isinstance(source.pool, QueuePool)
                else 1
            )
            dedicated = Engine(source.pool.recreate(), source.dialect, source.url)
            state = _LockPool(dedicated, BoundedSemaphore(capacity))
            _pools[source] = state

            def dispose_lock_pool(_source: Engine) -> None:
                dedicated.dispose()

            event.listen(source, "engine_disposed", dispose_lock_pool)
        return state


@contextmanager
def triage_lock_connection(source: Engine) -> Iterator[Connection]:
    """Fail fast when lock capacity is busy, without using the data pool.

    Recreating the pool preserves connection arguments (including SSL), the
    driver creator, and pool instrumentation. Admission is nonblocking because
    callers may already hold a data Session connection. The dedicated pool is
    disposed with its source engine.
    """
    state = _lock_pool(source)
    if not state.admission.acquire(blocking=False):
        raise ValueError("triage_operation_in_progress")
    try:
        with state.engine.connect() as connection:
            yield connection
    finally:
        state.admission.release()
