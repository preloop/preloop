"""
Scheduler-side reconciliation of schedule (cron) flow triggers.

Maintains one APScheduler CronTrigger job per enabled, non-preset flow
with ``trigger_event_source == 'schedule'``. Jobs only publish the
``run_scheduled_flow`` NATS task; the worker side enforces the overlap
(skip-if-previous-running) and pause-suppression policies.
"""

from typing import Dict, Tuple

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from preloop.models.crud import crud_flow
from preloop.models.db.session import get_db_session

from ..config import logger
from .event_bus import event_bus_service

FLOW_SCHEDULE_JOB_PREFIX = "flow_schedule_"


async def publish_flow_schedule_tick(
    flow_id: str, cron: str = "", timezone: str = "UTC"
) -> None:
    """Publish a 'run_scheduled_flow' task to the NATS queue.

    ``cron`` and ``timezone`` are carried in the job args purely so the
    reconcile pass can detect config changes deterministically (and for
    log context); the worker re-reads the flow's stored config itself.
    """
    logger.info(
        f"Publishing scheduled tick for flow {flow_id} (cron='{cron}' tz='{timezone}')"
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
    Synchronize APScheduler cron jobs with schedule-triggered flows.

    Called periodically by the scheduler service (same reconcile loop
    pattern as tracker polling jobs). Disabled, deleted or re-configured
    flows have their jobs removed/replaced on the next pass.

    Args:
        scheduler: The APScheduler instance.
    """
    db = next(get_db_session())
    try:
        desired: Dict[str, Tuple[str, str]] = {}
        for flow in crud_flow.get_scheduled(db):
            config: Dict[str, str] = flow.schedule_config or {}
            cron = config.get("cron")
            tz = config.get("timezone", "UTC")
            if not cron:
                continue
            try:
                CronTrigger.from_crontab(cron, timezone=tz)
            except Exception as e:
                logger.error(
                    f"Flow {flow.id} has invalid schedule_config "
                    f"(cron='{cron}', timezone='{tz}'): {e}"
                )
                continue
            desired[str(flow.id)] = (cron, tz)

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

        # Add or update jobs for scheduled flows. The stored (cron, tz)
        # config is carried in the job args, so idempotence is a plain
        # comparison against the source config rather than a brittle
        # trigger-repr comparison.
        for flow_id, (cron, tz) in desired.items():
            existing = current.get(flow_id)
            if existing is not None and list(existing.args) == [flow_id, cron, tz]:
                continue  # unchanged
            scheduler.add_job(
                publish_flow_schedule_tick,
                id=f"{FLOW_SCHEDULE_JOB_PREFIX}{flow_id}",
                name=f"Scheduled flow {flow_id}",
                replace_existing=True,
                misfire_grace_time=60,
                args=[flow_id, cron, tz],
                trigger=CronTrigger.from_crontab(cron, timezone=tz),
            )
            logger.info(
                f"{'Updated' if existing else 'Added'} schedule job for flow "
                f"{flow_id}: cron='{cron}' tz='{tz}'"
            )
    except Exception as e:
        logger.error(f"Error during flow schedule synchronization: {e}", exc_info=True)
    finally:
        db.close()
