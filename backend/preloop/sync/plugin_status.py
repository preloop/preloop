"""Minimal fleet plugin health, persisted in the existing JetStream service.

Only plugin names, task names, counters and timestamps are published. No
exception text, plugin configuration or task arguments leave the process.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import socket
import time
import threading
import uuid
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, TypeVar

import nats
from nats.js.api import KeyValueConfig
from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NoKeysError

logger = logging.getLogger(__name__)
BUCKET = "preloop_plugin_status"
PROCESS_BUCKET = "preloop_plugin_processes"
READ_TIMEOUT_SECONDS = 2.0
WRITE_TIMEOUT_SECONDS = 2.0
HEARTBEAT_SECONDS = 30
STALE_SECONDS = 120
JOB_IDS = {
    "provider_billing_ingest_job": "ingest_provider_billing",
    "subscription_reconcile_job": "reconcile_stripe_subscriptions",
    "optimization_digest_job": "send_optimization_digest",
    "governance_session_digest_sweep_job": "governance_session_digest_sweep",
    "governance_weekly_review_job": "governance_weekly_review",
}
_skips: Counter[str] = Counter()
_runs: dict[str, float] = {}
_lock = threading.Lock()
F = TypeVar("F", bound=Callable[..., Any])


def missing_service(task: str, service: str) -> None:
    """Count every skip, warning at most once per task in this process."""
    with _lock:
        _skips[task] += 1
        if _skips[task] == 1:
            logger.warning("Task %s: missing service %s; skipping", task, service)


def skipped_tasks() -> dict[str, int]:
    """Return counters for the operator health surface."""
    with _lock:
        return dict(_skips)


def last_runs() -> dict[str, float]:
    """Return successfully completed plugin task timestamps."""
    with _lock:
        return dict(_runs)


def tracked_plugin_task(function: F) -> F:
    """Record completion only when the handler returns a result."""

    @functools.wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = function(*args, **kwargs)
        account_id = kwargs.get("account_id", args[0] if args else None)
        if result is not None and account_id is None:
            with _lock:
                _runs[function.__name__] = time.time()
        return result

    return wrapped  # type: ignore[return-value]


def expected_run(*, previous_due: float, next_due: float, last_success: float) -> float:
    """Preserve an unsatisfied deadline across scheduler restarts."""
    return previous_due if previous_due and last_success < previous_due else next_due


async def _read(kv: Any, key: str) -> dict[str, Any]:
    """Read a status record; missing records have no prior state."""
    try:
        return json.loads((await kv.get(key)).value)
    except KeyNotFoundError:
        return {}


async def report(
    kv: Any,
    worker_id: str,
    role: str,
    subjects: list[str],
    *,
    processes: Any | None = None,
) -> None:
    """Publish one process heartbeat and merge durable job timestamps."""
    from preloop.plugins.base import get_plugin_manager

    manager = get_plugin_manager()
    for task, completed_at in last_runs().items():
        previous = await _read(kv, f"run.{task}")
        if completed_at > previous.get("last_success", 0):
            await kv.put(
                f"run.{task}", json.dumps({"last_success": completed_at}).encode()
            )

    jobs = {}
    if role == "scheduler":
        from preloop.sync.cli.scheduler_commands import scheduler

        for job in scheduler.get_jobs() if scheduler else []:
            task = JOB_IDS.get(job.id)
            if task is None or job.next_run_time is None:
                continue
            prior = await _read(kv, f"due.{task}")
            success = await _read(kv, f"run.{task}")
            due = expected_run(
                previous_due=prior.get("expected_at", 0),
                next_due=job.next_run_time.timestamp(),
                last_success=success.get("last_success", 0),
            )
            jobs[task] = {"expected_at": due, **success}
            await kv.put(f"due.{task}", json.dumps({"expected_at": due}).encode())
    payload = {
        "worker_id": worker_id,
        "role": role,
        "updated_at": time.time(),
        "proprietary_enabled": manager.proprietary_enabled,
        "plugins": {name: True for name in manager._plugins},
        "skipped_tasks": skipped_tasks(),
        "last_runs": last_runs(),
        "subjects": subjects,
        "jobs": jobs,
    }
    process_store = processes if processes is not None else kv
    await process_store.put(f"process.{worker_id}", json.dumps(payload).encode())


@asynccontextmanager
async def _connection() -> AsyncIterator[Any]:
    """Own the client before connecting and bound handshake/JetStream requests."""
    from preloop.config import settings

    nc = nats.NATS()
    try:
        async with asyncio.timeout(0.5):
            await nc.connect(
                settings.nats_url,
                name="preloop-plugin-status",
                connect_timeout=0.25,
                max_reconnect_attempts=1,
                reconnect_time_wait=0,
                allow_reconnect=False,
            )
        yield nc.jetstream(timeout=0.5)
    finally:
        with suppress(Exception):
            async with asyncio.timeout(0.5):
                await nc.close()


async def _heartbeat(worker_id: str, role: str, subjects: list[str]) -> None:
    """Retry reporting independently; observability never gates task processing."""
    while True:
        try:
            async with _connection() as js:
                stores = []
                for bucket, ttl, max_bytes in (
                    (BUCKET, 0, 1024 * 1024),
                    (PROCESS_BUCKET, STALE_SECONDS, 8 * 1024 * 1024),
                ):
                    try:
                        store = await js.key_value(bucket)
                    except BucketNotFoundError:
                        store = await js.create_key_value(
                            config=KeyValueConfig(
                                bucket=bucket,
                                ttl=ttl,
                                history=1,
                                max_bytes=max_bytes,
                                max_value_size=65536,
                            )
                        )
                    stores.append(store)
                while True:
                    async with asyncio.timeout(WRITE_TIMEOUT_SECONDS):
                        await report(
                            stores[0], worker_id, role, subjects, processes=stores[1]
                        )
                    await asyncio.sleep(HEARTBEAT_SECONDS)
        except Exception:
            logger.exception(
                "Plugin status reporting unavailable; processing continues"
            )
            await asyncio.sleep(HEARTBEAT_SECONDS)


async def run_with_status(
    work: Awaitable[Any], role: str, subjects: list[str] | None = None
) -> None:
    """Run work alongside best-effort status reporting for installed extensions."""
    from preloop.plugins.base import get_plugin_manager

    if not get_plugin_manager().proprietary_enabled:
        await work
        return
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    heartbeat = asyncio.create_task(_heartbeat(worker_id, role, subjects or []))
    try:
        await work
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def read_status() -> dict[str, Any]:
    """Read current processes without mutations, bounded even when NATS stalls."""
    async with asyncio.timeout(READ_TIMEOUT_SECONDS):
        async with _connection() as js:
            try:
                kv = await js.key_value(PROCESS_BUCKET)
            except BucketNotFoundError:
                return {"timestamp": time.time(), "workers": []}
            try:
                keys = await kv.keys()
            except NoKeysError:
                keys = []
            workers = []
            now = time.time()
            for key in keys:
                if key.startswith("process."):
                    payload = await _read(kv, key)
                    if now - payload.get("updated_at", 0) <= STALE_SECONDS:
                        workers.append(payload)
            return {"timestamp": now, "workers": workers}
