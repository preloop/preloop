"""Keep complete lifecycle ORM transactions away from the application loop."""

from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import Any, ParamSpec, TypeVar
from uuid import UUID

import anyio
from anyio import from_thread
from sqlalchemy.orm import Session

P = ParamSpec("P")
T = TypeVar("T")


def worker_owned_session(owner: Any = None, fallback: Any = None) -> tuple[Any, bool]:
    """Open a Session on this worker thread instead of borrowing the caller's.

    Test doubles that are not SQLAlchemy sessions are returned unchanged.
    """
    create = getattr(owner, "_create_orchestrator_session", None)
    if callable(create):
        return create(), True
    if isinstance(fallback, Session):
        from preloop.models.db.session import get_session_factory

        return get_session_factory()(), True
    return fallback, False


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
        return await anyio.to_thread.run_sync(
            partial(run_lifecycle_endpoint, partial(operation, *args, **kwargs))
        )

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
