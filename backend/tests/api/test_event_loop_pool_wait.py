"""Regression tests for pool waits blocking the API event loop.

Background: on 2026-09-03 the staging API pods crash-looped after a burst from
the console Models page saturated the per-pod SQLAlchemy pool (helm sizing
``8 + 12``). The founder-visible symptom was
``QueuePool limit of size 8 overflow 12 reached, connection timed out``, but
the reason the pods died was the liveness probe: ``GET /api/v1/ping`` takes no
database dependency at all, yet it timed out, because ``async def`` handlers
that check out a synchronous ``Session`` were sitting on the event loop
waiting for a connection.

These tests pin both halves of the fix:

* the console's hot paths never do a blocking checkout on the loop, and
* a pool timeout on one of those paths leaves ``/api/v1/ping`` responsive.
"""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any, Callable, Iterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.exc import TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_for_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.db.session import get_db_session
from preloop.models.models.account import Account
from preloop.models.models.user import User

# The error SQLAlchemy raises once ``pool_timeout`` expires on a full pool.
POOL_TIMEOUT_MESSAGE = (
    "QueuePool limit of size 8 overflow 12 reached, connection timed out, timeout 5.00"
)

# How long the fake saturated pool blocks its worker before giving up.
BLOCKED_CHECKOUT_SECONDS = 2.0

# ``/api/v1/ping`` is the liveness probe: no auth, no database. The kubelet
# gives it 5s (helm ``api-deployment.yaml``), so anything close to that is a
# pod kill.
PING_BUDGET_SECONDS = 1.0

# Console paths that are polled or fetched in bursts (the request mix seen in
# the incident logs plus the dashboard poll set). A blocking checkout on any of
# them takes the whole event loop down with it, so each must either be a
# synchronous handler (FastAPI runs those on the threadpool) or offload its
# database work explicitly.
CONSOLE_HOT_PATHS: tuple[tuple[str, str], ...] = (
    ("GET", "/api/v1/ai-models"),
    ("GET", "/api/v1/ai-models/overview"),
    ("GET", "/api/v1/ai-models/{model_id}"),
    ("GET", "/api/v1/ai-models/{model_id}/summary"),
    ("GET", "/api/v1/ai-models/{model_id}/runtime-sessions"),
    ("GET", "/api/v1/ai-models/{model_id}/interactions"),
    ("GET", "/api/v1/ai-models/{model_id}/pricing"),
    ("GET", "/api/v1/account/gateway-usage/summary"),
    ("GET", "/api/v1/account/telemetry/dashboard"),
    ("GET", "/api/v1/attention/dismissals"),
    ("GET", "/api/v1/roles"),
    ("GET", "/api/v1/runtime-sessions"),
    ("GET", "/api/v1/runtime-sessions/{runtime_session_id}"),
    ("GET", "/api/v1/runtime-sessions/{runtime_session_id}/activity"),
    ("GET", "/api/v1/runtime-sessions/{runtime_session_id}/gateway-events"),
    ("GET", "/api/v1/runtime-sessions/{runtime_session_id}/interactions"),
    ("GET", "/api/v1/runtime-sessions/{runtime_session_id}/requests"),
    ("GET", "/api/v1/version"),
)

# Markers that show a handler moved its blocking work off the loop.
OFFLOAD_MARKERS = ("run_db_off_loop", "run_in_threadpool", "run_db_async")

# ``async def`` handlers that still take a synchronous ``Session``. Most are
# safe in practice (single cheap query, rarely called), but every one of them
# is a latent liveness risk, so the count is ratcheted: it may shrink, never
# grow. Lower this number when you convert more handlers.
ASYNC_SYNC_SESSION_ROUTE_BUDGET = 128


def _iter_route_contexts(app: FastAPI) -> Iterator[Any]:
    """Yield FastAPI route contexts, flattening included routers.

    FastAPI >= 0.137 nests included routers in ``_IncludedRouter`` objects
    that expose neither ``path`` nor ``dependant``, so the public
    ``iter_route_contexts`` helper is the only reliable way to see the real
    endpoints.
    """
    from fastapi.routing import iter_route_contexts

    yield from iter_route_contexts(app.routes)


def _async_routes_with_sync_session(app: FastAPI) -> set[tuple[str, str]]:
    """Return ``(method, path)`` for async routes depending on ``get_db_session``."""
    found: set[tuple[str, str]] = set()
    for context in _iter_route_contexts(app):
        endpoint = getattr(context, "endpoint", None)
        dependant = getattr(context, "dependant", None)
        if endpoint is None or dependant is None:
            continue
        if not inspect.iscoroutinefunction(endpoint):
            continue
        if not any(sub.call is get_db_session for sub in dependant.dependencies):
            continue
        for method in context.methods or {"GET"}:
            found.add((method, context.path))
    return found


def _endpoint_for(app: FastAPI, method: str, path: str) -> Callable[..., Any]:
    """Return the endpoint registered for one method and path."""
    for context in _iter_route_contexts(app):
        if context.path == path and method in (context.methods or set()):
            endpoint = getattr(context, "endpoint", None)
            if endpoint is not None:
                return endpoint
    raise AssertionError(f"route not registered: {method} {path}")


@pytest.fixture(scope="module")
def app() -> FastAPI:
    """Build the real API application once for this module."""
    return create_app()


def test_console_hot_paths_keep_pool_waits_off_the_loop(app: FastAPI) -> None:
    """No console burst path may block the loop while waiting for a connection."""
    offenders: list[str] = []
    for method, path in CONSOLE_HOT_PATHS:
        endpoint = inspect.unwrap(_endpoint_for(app, method, path))
        if not inspect.iscoroutinefunction(endpoint):
            # Synchronous handlers are dispatched on the anyio threadpool.
            continue
        source = inspect.getsource(endpoint)
        if not any(marker in source for marker in OFFLOAD_MARKERS):
            offenders.append(f"{method} {path} ({endpoint.__qualname__})")

    assert not offenders, (
        "async handlers on console burst paths must offload their database "
        "work (see preloop.api.loop_safety.run_db_off_loop): "
        + ", ".join(sorted(offenders))
    )


def test_async_sync_session_route_count_does_not_grow(app: FastAPI) -> None:
    """Ratchet the number of async handlers holding a synchronous session."""
    routes = _async_routes_with_sync_session(app)
    assert len(routes) <= ASYNC_SYNC_SESSION_ROUTE_BUDGET, (
        f"{len(routes)} async routes depend on get_db_session, budget is "
        f"{ASYNC_SYNC_SESSION_ROUTE_BUDGET}. New async handlers should take an "
        "async session or offload with run_db_off_loop."
    )


class _SaturatedPoolSession:
    """Session stand-in that behaves like a checkout against a full pool.

    Any attribute use blocks the calling thread for ``BLOCKED_CHECKOUT_SECONDS``
    and then raises the SQLAlchemy timeout the incident produced.
    """

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """Return a callable that blocks and then times out, like the pool does."""

        def _blocking_call(*args: Any, **kwargs: Any) -> Any:
            time.sleep(BLOCKED_CHECKOUT_SECONDS)
            raise SQLAlchemyTimeoutError(POOL_TIMEOUT_MESSAGE)

        return _blocking_call


def _saturated_db_session() -> _SaturatedPoolSession:
    """Dependency override that simulates a pool checkout timeout."""
    return _SaturatedPoolSession()


def _stub_user() -> User:
    """Build a detached user for dependency overrides."""
    return User(
        id=uuid.uuid4(),
        account_id=uuid.uuid4(),
        username="jane.doe",
        email="jane.doe@example.com",
        full_name="Jane Doe",
        is_active=True,
    )


class _LoopHeartbeat:
    """Measure the longest stall of the event loop while requests are in flight.

    A blocking pool checkout on the loop shows up here directly: the heartbeat
    cannot tick, so the largest gap between ticks is the length of the block.
    That is exactly what starved ``/api/v1/ping`` during the incident.
    """

    def __init__(self, interval: float = 0.01) -> None:
        """Store the tick interval and reset the measurement."""
        self._interval = interval
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.longest_stall = 0.0

    async def _run(self) -> None:
        """Tick until stopped, recording the largest gap between ticks."""
        previous = time.perf_counter()
        while not self._stop.is_set():
            await asyncio.sleep(self._interval)
            now = time.perf_counter()
            self.longest_stall = max(self.longest_stall, now - previous)
            previous = now

    async def __aenter__(self) -> "_LoopHeartbeat":
        """Start ticking."""
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(self._interval)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Stop ticking and wait for the task to finish."""
        self._stop.set()
        if self._task is not None:
            await self._task


@pytest.mark.asyncio
async def test_pool_timeout_on_a_burst_path_does_not_stall_ping(app: FastAPI) -> None:
    """A saturated pool must fail one request, not the liveness probe."""
    user = _stub_user()
    account = Account(id=user.account_id, organization_name="Example Org")

    app.dependency_overrides[get_db_session] = _saturated_db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    app.dependency_overrides[get_account_for_user] = lambda: account
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            async with _LoopHeartbeat() as heartbeat:
                started = time.perf_counter()
                burst = asyncio.gather(
                    *(client.get("/api/v1/runtime-sessions") for _ in range(4)),
                    return_exceptions=True,
                )
                ping = await client.get("/api/v1/ping")
                # Measured from the moment the burst was launched, so a loop
                # blocked by the burst shows up in this number.
                ping_seconds = time.perf_counter() - started
                results = await burst
    finally:
        app.dependency_overrides.clear()

    assert ping.status_code == 200
    assert ping_seconds < PING_BUDGET_SECONDS, (
        f"/api/v1/ping took {ping_seconds:.2f}s while the pool was saturated; "
        "a blocking checkout is back on the event loop"
    )
    assert heartbeat.longest_stall < PING_BUDGET_SECONDS, (
        f"the event loop stalled for {heartbeat.longest_stall:.2f}s during the "
        "burst; database work is running on the loop again"
    )
    # The burst itself is allowed to fail: the pool really is exhausted. It
    # must fail as a 503 with Retry-After, not as an opaque 500.
    assert all(getattr(result, "status_code", 0) == 503 for result in results)
    assert all(result.headers.get("Retry-After") for result in results)


@pytest.mark.asyncio
async def test_blocking_checkout_in_an_async_handler_stalls_the_loop() -> None:
    """Document the failure mode the fix removes.

    This is the pre-fix shape: an ``async def`` handler that queries a
    synchronous ``Session`` directly. It reproduces the incident in miniature,
    and the offloaded variant next to it shows the fix.
    """
    demo = FastAPI()

    @demo.get("/blocking")
    async def blocking(db: Session = Depends(_SaturatedPoolSession)) -> dict[str, str]:
        """Pre-fix shape: blocking checkout on the event loop."""
        db.query(object)
        return {"status": "ok"}

    @demo.get("/offloaded")
    async def offloaded(db: Session = Depends(_SaturatedPoolSession)) -> dict[str, str]:
        """Post-fix shape: the same wait, moved to a worker thread."""
        await run_db_off_loop(lambda: db.query(object))
        return {"status": "ok"}

    async def _longest_stall(path: str) -> float:
        """Return the longest event-loop stall caused by two requests to path."""
        transport = httpx.ASGITransport(app=demo, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            async with _LoopHeartbeat() as heartbeat:
                await asyncio.gather(
                    *(client.get(path) for _ in range(2)), return_exceptions=True
                )
            return heartbeat.longest_stall

    blocked_stall = await _longest_stall("/blocking")
    offloaded_stall = await _longest_stall("/offloaded")

    assert blocked_stall >= BLOCKED_CHECKOUT_SECONDS
    assert offloaded_stall < PING_BUDGET_SECONDS
