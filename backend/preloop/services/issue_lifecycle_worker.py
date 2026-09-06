"""Keep complete lifecycle ORM transactions away from the application loop."""

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

import anyio
from anyio import from_thread
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

P = ParamSpec("P")
T = TypeVar("T")

_lifecycle_bind: ContextVar[Connection | None] = ContextVar(
    "lifecycle_bind", default=None
)


def _caller_session(*args: Any, **kwargs: Any) -> Session | None:
    """Find the request/test Session without treating test doubles as ORM."""
    for value in (*args, *kwargs.values()):
        if isinstance(value, Session):
            return value
        db = getattr(value, "db", None)
        if isinstance(db, Session):
            return db
    return None


def worker_owned_session(owner: Any = None, fallback: Any = None) -> tuple[Any, bool]:
    """Open a Session on this worker thread instead of borrowing the caller's.

    Engine-bound callers get a fresh factory Session whose commit is durable.
    Connection-bound callers (pytest savepoints) join that connection so
    uncommitted fixture rows stay visible. Test doubles are unchanged.
    """
    join = _lifecycle_bind.get()
    session = fallback if isinstance(fallback, Session) else None
    if session is None:
        db = getattr(owner, "db", None)
        if isinstance(db, Session):
            session = db
    if join is None and session is not None:
        raw = session.get_bind()
        if isinstance(raw, Connection):
            join = raw
    if join is not None:
        return (
            Session(bind=join, join_transaction_mode="create_savepoint"),
            True,
        )
    if session is not None:
        from preloop.models.db.session import get_session_factory

        return get_session_factory()(), True
    return fallback, False


@contextmanager
def lifecycle_worker_db(owner: Any = None, fallback: Any = None) -> Iterator[Any]:
    """Open a worker-thread Session that joins the caller's transaction."""
    db, owned = worker_owned_session(owner, fallback)
    try:
        yield db
        if owned:
            db.commit()
    except Exception:
        if owned:
            db.rollback()
        raise
    finally:
        if owned:
            db.close()


def run_lifecycle_endpoint(operation: Callable[[], Awaitable[T]]) -> T:
    """Run one HTTP operation inside its synchronous FastAPI worker handler.

    The private loop handles provider I/O while all synchronous CRUD and lazy
    ORM reads stay in the same worker. Detached flow tasks must be dispatched
    through ``on_application_loop``, never started on this temporary loop.
    """
    return anyio.run(operation)


def lifecycle_worker_hook(
    operation: Callable[P, Awaitable[T]],
) -> Callable[P, Awaitable[T]]:
    """Offload a trigger or completion hook with its entire ORM transaction."""

    @wraps(operation)
    async def offload(*args: P.args, **kwargs: P.kwargs) -> T:
        caller = _caller_session(*args, **kwargs)
        join = None
        if caller is not None:
            raw = caller.get_bind()
            if isinstance(raw, Connection):
                join = raw
            else:
                caller.commit()
        token = _lifecycle_bind.set(join)
        try:
            return await anyio.to_thread.run_sync(
                partial(run_lifecycle_endpoint, partial(operation, *args, **kwargs))
            )
        finally:
            _lifecycle_bind.reset(token)
            if caller is not None:
                caller.expire_all()

    return offload


async def on_application_loop(operation: Callable[[], Awaitable[T]]) -> T:
    """Dispatch on the originating application loop, preserving detached tasks.

    Called only inside a lifecycle worker. The worker waits without using its
    session concurrently; the originating AnyIO token selects the persistent
    application loop, not the worker's temporary provider-I/O loop.
    """
    return from_thread.run(operation)


async def dispatch_lifecycle_execution(
    execution_id: UUID, local_dispatch: Callable[[], Awaitable[Any]]
) -> None:
    """Require a worker publish acknowledgment or schedule on the persistent loop."""
    from preloop.services.flow_execution_dispatcher import (
        dispatch_execute,
        flow_execution_worker_enabled,
    )
    from preloop.services.flow_trigger_service import FlowDispatchError

    async def dispatch() -> None:
        if flow_execution_worker_enabled():
            if not await dispatch_execute(execution_id):
                raise FlowDispatchError(
                    str(execution_id),
                    "PENDING",
                    RuntimeError("worker_dispatch_not_acknowledged"),
                )
        else:
            await local_dispatch()

    await on_application_loop(dispatch)
