"""
Scheduler-side reconciliation of schedule flow triggers.

Maintains one APScheduler job per enabled, non-preset flow with
``trigger_event_source == 'schedule'``. Friendly schedule forms
(interval/daily/weekly) and cron expressions are mapped onto native
APScheduler triggers by the ``ScheduleConfig`` schema. Jobs only publish
the ``run_scheduled_flow`` NATS task; the worker side enforces the
overlap (skip-if-previous-running) and pause-suppression policies.
"""

import json
from typing import Dict, Tuple

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from preloop.models.crud import crud_flow
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow import parse_schedule_config

from ..config import logger
from .event_bus import event_bus_service

FLOW_SCHEDULE_JOB_PREFIX = "flow_schedule_"


def schedule_config_key(config: dict) -> str:
    """Canonical string form of a schedule config, for change detection."""
    return json.dumps(config, sort_keys=True)


async def publish_flow_schedule_tick(flow_id: str, schedule_key: str = "") -> None:
    """Publish a 'run_scheduled_flow' task to the NATS queue.

    ``schedule_key`` (the canonical schedule config JSON) is carried in
    the job args purely so the reconcile pass can detect config changes
    deterministically (and for log context); the worker re-reads the
    flow's stored config itself.
    """
    logger.info(
        f"Publishing scheduled tick for flow {flow_id} (schedule={schedule_key})"
    )
    try:
        ack = await event_bus_service.publish_task("run_scheduled_flow", flow_id)
        if not ack:
            logger.error(f"Failed to publish scheduled tick for flow {flow_id}.")
    except Exception as e:
        logger.error(
            f"Exception while publishing scheduled tick for flow {flow_id}: {e}",
            exc_info=True,
        )


def sync_flow_schedule_jobs(scheduler: AsyncIOScheduler) -> None:
    """
    Synchronize APScheduler jobs with schedule-triggered flows.

    Called periodically by the scheduler service (same reconcile loop
    pattern as tracker polling jobs). Disabled, deleted or re-configured
    flows have their jobs removed/replaced on the next pass.

    Args:
        scheduler: The APScheduler instance.
    """
    db = next(get_db_session())
    try:
        desired: Dict[str, Tuple[str, object]] = {}
        for flow in crud_flow.get_scheduled(db):
            raw_config = flow.schedule_config or {}
            try:
                config = parse_schedule_config(raw_config)
                trigger = config.build_trigger()
            except Exception as e:
                logger.error(
                    f"Flow {flow.id} has invalid schedule_config ({raw_config!r}): {e}"
                )
                continue
            desired[str(flow.id)] = (
                schedule_config_key(config.model_dump()),
                trigger,
            )

        current = {
            job.id[len(FLOW_SCHEDULE_JOB_PREFIX) :]: job
            for job in scheduler.get_jobs()
            if job.id.startswith(FLOW_SCHEDULE_JOB_PREFIX)
        }

        # Remove jobs for flows that are gone, disabled or misconfigured
        for flow_id in set(current) - set(desired):
            try:
                scheduler.remove_job(f"{FLOW_SCHEDULE_JOB_PREFIX}{flow_id}")
                logger.info(f"Removed schedule job for flow {flow_id}.")
            except JobLookupError:
                logger.warning(f"Schedule job for flow {flow_id} already removed.")

        # Add or update jobs for scheduled flows. The canonical schedule
        # config is carried in the job args, so idempotence is a plain
        # comparison against the source config rather than a brittle
        # trigger-repr comparison.
        for flow_id, (schedule_key, trigger) in desired.items():
            existing = current.get(flow_id)
            if existing is not None and list(existing.args) == [flow_id, schedule_key]:
                continue  # unchanged
            scheduler.add_job(
                publish_flow_schedule_tick,
                id=f"{FLOW_SCHEDULE_JOB_PREFIX}{flow_id}",
                name=f"Scheduled flow {flow_id}",
                replace_existing=True,
                misfire_grace_time=60,
                args=[flow_id, schedule_key],
                trigger=trigger,
            )
            logger.info(
                f"{'Updated' if existing else 'Added'} schedule job for flow "
                f"{flow_id}: {schedule_key}"
            )
    except Exception as e:
        logger.error(f"Error during flow schedule synchronization: {e}", exc_info=True)
    finally:
        db.close()
