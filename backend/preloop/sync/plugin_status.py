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
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Any, TypeVar

import nats
from nats.js.api import KeyValueConfig
from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NoKeysError

logger = logging.getLogger(__name__)
BUCKET = "preloop_plugin_status"
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


async def report(kv: Any, worker_id: str, role: str, subjects: list[str]) -> None:
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
    await kv.put(f"process.{worker_id}", json.dumps(payload).encode())


async def run_with_status(
    work: Awaitable[Any], role: str, subjects: list[str] | None = None
) -> None:
    """Run a service with a status heartbeat; fail startup if status cannot initialize."""
    from preloop.plugins.base import get_plugin_manager

    if not get_plugin_manager().proprietary_enabled:
        await work
        return
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    nc = None
    heartbeat = None
    try:
        from preloop.config import settings

        nc = await nats.connect(settings.nats_url)
        js = nc.jetstream()
        try:
            kv = await js.key_value(BUCKET)
        except BucketNotFoundError:
            kv = await js.create_key_value(config=KeyValueConfig(bucket=BUCKET))
        await report(kv, worker_id, role, subjects or [])

        async def beat() -> None:
            while True:
                await asyncio.sleep(HEARTBEAT_SECONDS)
                try:
                    await report(kv, worker_id, role, subjects or [])
                except Exception:
                    logger.exception("Plugin health heartbeat failed")

        heartbeat = asyncio.create_task(beat())
        await work
    finally:
        if heartbeat:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
        elif hasattr(work, "close"):
            work.close()
        if nc:
            await nc.close()


async def read_status() -> dict[str, Any]:
    """Read fleet state without creating buckets or running any plugin job."""
    from preloop.config import settings

    nc = await nats.connect(settings.nats_url)
    try:
        kv = await nc.jetstream().key_value(BUCKET)
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
    finally:
        await nc.close()
