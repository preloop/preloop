"""Dispatch flow executions to sync workers (or local create_task fallback)."""

from __future__ import annotations

import inspect
import logging
import os
import socket
import uuid
from typing import Any, Awaitable, Callable, Optional, cast

from preloop.config import settings
from preloop.sync.services.event_bus import event_bus_service

logger = logging.getLogger(__name__)

EXECUTE_FLOW_TASK = "execute_flow"
RESUME_FLOW_EXECUTION_TASK = "resume_flow_execution"


def flow_execution_worker_enabled() -> bool:
    """Return whether worker-based flow orchestration is enabled."""
    return bool(getattr(settings, "flow_execution_worker_enabled", False))


def get_orchestrator_worker_id() -> str:
    """Stable worker identity for claim leases (pod name preferred)."""
    return (
        os.getenv("POD_NAME")
        or os.getenv("HOSTNAME")
        or socket.gethostname()
        or f"worker-{os.getpid()}"
    )


def claim_stale_after_seconds() -> int:
    """Seconds after last heartbeat before a claim is considered abandoned."""
    return int(getattr(settings, "flow_execution_claim_stale_seconds", 120) or 120)


async def dispatch_execute(
    execution_id: uuid.UUID | str,
    *,
    local_fallback: Optional[Callable[[], Any]] = None,
) -> bool:
    """Publish ``execute_flow`` for a pending/created execution.

    Args:
        execution_id: Flow execution to run.
        local_fallback: Optional coroutine factory used when the worker flag is
            off (or publish fails and fallback is provided). When the flag is
            on and publish fails, returns False without fallback so recovery
            can re-dispatch.

    Returns:
        True if the execution was handed off (published or local fallback ran).
    """
    return await _dispatch(
        EXECUTE_FLOW_TASK,
        execution_id,
        local_fallback=local_fallback,
    )


async def dispatch_resume(
    execution_id: uuid.UUID | str,
    *,
    local_fallback: Optional[Callable[[], Any]] = None,
) -> bool:
    """Publish ``resume_flow_execution`` for an orphaned/stale execution."""
    return await _dispatch(
        RESUME_FLOW_EXECUTION_TASK,
        execution_id,
        local_fallback=local_fallback,
    )


async def _run_local_fallback(local_fallback: Callable[[], Any]) -> None:
    result = local_fallback()
    if inspect.isawaitable(result):
        await cast(Awaitable[Any], result)


async def _dispatch(
    task_name: str,
    execution_id: uuid.UUID | str,
    *,
    local_fallback: Optional[Callable[[], Any]] = None,
) -> bool:
    execution_id_str = str(execution_id)

    if not flow_execution_worker_enabled():
        if local_fallback is None:
            logger.error(
                "FLOW_EXECUTION_WORKER_ENABLED is false and no local fallback "
                "was provided for %s(%s)",
                task_name,
                execution_id_str,
            )
            return False
        logger.debug(
            "Worker orchestration disabled; running local fallback for %s(%s)",
            task_name,
            execution_id_str,
        )
        await _run_local_fallback(local_fallback)
        return True

    try:
        ack = await event_bus_service.publish_task(
            task_name, execution_id=execution_id_str
        )
    except Exception as exc:  # noqa: BLE001 - publish is best-effort; recovery retries
        logger.error(
            "Failed to publish %s for execution %s: %s",
            task_name,
            execution_id_str,
            exc,
            exc_info=True,
        )
        ack = None

    if ack is not None:
        logger.info(
            "Dispatched %s for execution %s (stream=%s seq=%s)",
            task_name,
            execution_id_str,
            getattr(ack, "stream", None),
            getattr(ack, "seq", None),
        )
        return True

    # Workers enabled but publish failed: leave PENDING for recovery re-dispatch.
    # Local fallback only runs when the feature flag is off (handled above).
    logger.error(
        "Could not dispatch %s for execution %s; leaving PENDING for recovery",
        task_name,
        execution_id_str,
    )
    return False
