"""Keep complete lifecycle ORM transactions away from the application loop."""

from collections.abc import Awaitable, Callable
from functools import partial, wraps
from typing import ParamSpec, TypeVar

import anyio
from anyio import from_thread

P = ParamSpec("P")
T = TypeVar("T")


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
