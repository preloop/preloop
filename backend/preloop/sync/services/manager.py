"""
Tracker update service manager.
(Refactored for APScheduler integration)
"""

from typing import Set
import pytz
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.jobstores.base import JobLookupError

from preloop.models.crud import crud_tracker
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import event_bus_service
from ..config import logger, SERVICE_POLL_INTERVAL

POLLING_THRESHOLD = timedelta(hours=1)
TRACKER_JOB_PREFIX = "tracker_update_"


async def poll_tracker(tracker_id: str):
    """
    Scheduled job to publish a 'poll_tracker' task to the NATS queue.
    """
    logger.info(f"Publishing poll task for tracker {tracker_id}")
    try:
        ack = await event_bus_service.publish_task("poll_tracker", tracker_id)
        if ack:
            logger.info(
                f"Successfully published poll task for tracker {tracker_id}. ACK: stream={ack.stream}, seq={ack.seq}"
            )
        else:
            logger.error(f"Failed to publish poll task for tracker {tracker_id}.")
    except Exception as e:
        logger.error(
            f"An exception occurred while trying to publish poll task for tracker {tracker_id}: {type(e).__name__}"
        )


# --- APScheduler Job Synchronization Function ---
def sync_scheduled_jobs(scheduler: AsyncIOScheduler, db: Session):
    """
    Synchronizes APScheduler jobs with active trackers in the database.

    This function should be called periodically by a dedicated APScheduler job.

    Args:
        scheduler: The APScheduler instance.
        manager: The TrackerUpdateServiceManager instance (provides DB session and service management).
    """
    logger.info("Starting tracker job synchronization...")
    # Acquire a new DB session specifically for this job run
    db = next(get_db_session())
    try:
        # 1. Get current tracker job IDs from scheduler
        current_job_ids: Set[str] = set()
        for job in scheduler.get_jobs():
            if job.id.startswith(TRACKER_JOB_PREFIX):
                # Extract tracker ID from job ID
                current_job_ids.add(job.id.replace(TRACKER_JOB_PREFIX, "", 1))

        # 2. Fetch all *active* trackers from the database using the local session
        active_trackers = crud_tracker.get_active(db)
        active_tracker_ids: Set[str] = {str(t.id) for t in active_trackers}
        logger.info(
            f"Found {len(active_trackers)} active trackers in DB: {active_tracker_ids}"
        )  # Changed to INFO
        logger.info(
            f"Found {len(current_job_ids)} existing tracker jobs in scheduler: {current_job_ids}"
        )  # Changed to INFO

        # 3. Identify trackers needing new jobs
        trackers_to_add = {
            tid for tid in active_tracker_ids if tid not in current_job_ids
        }
        logger.info(f"Trackers to add jobs for: {trackers_to_add}")

        # 4. Identify jobs to remove (for deactivated trackers)
        jobs_to_remove = {
            jid for jid in current_job_ids if jid not in active_tracker_ids
        }
        logger.info(
            f"Trackers needing job removal: {jobs_to_remove}"
        )  # Changed to INFO

        # 5. Remove jobs for deactivated trackers
        for tracker_id in jobs_to_remove:
            job_id = f"{TRACKER_JOB_PREFIX}{tracker_id}"
            try:
                scheduler.remove_job(job_id)
                logger.info(
                    f"Removed job {job_id} for deactivated tracker {tracker_id}."
                )
            except JobLookupError:
                logger.warning(
                    f"Job {job_id} not found in scheduler, likely already removed."
                )
            except Exception as e:
                logger.error(f"Error removing job {job_id}: {type(e).__name__}")

        # 6. Add jobs for new active trackers
        for tracker_id in trackers_to_add:
            logger.info(f"Processing tracker to add job for: {tracker_id}")
            # Find the tracker object
            tracker = next(
                (t for t in active_trackers if str(t.id) == tracker_id), None
            )
            if not tracker:
                logger.error(
                    f"Could not find tracker object for ID {tracker_id} during job add."
                )
                continue

            scheduler.add_job(
                poll_tracker,
                id=f"{TRACKER_JOB_PREFIX}{tracker_id}",
                name=f"Update Tracker {tracker_id}",
                replace_existing=True,
                misfire_grace_time=60,
                args=[tracker_id],  # Only pass tracker_id, job will get its own session
                trigger=IntervalTrigger(seconds=SERVICE_POLL_INTERVAL),
                next_run_time=datetime.now(pytz.utc),
            )
            logger.info(
                f"Added job for tracker {tracker_id} with interval {SERVICE_POLL_INTERVAL} seconds."
            )

        logger.info("Tracker job synchronization complete.")

    except Exception as e:
        logger.error(f"Error during tracker job synchronization: {type(e).__name__}")
    finally:
        # Ensure the locally acquired DB session is closed
        if db:
            try:
                db.close()
                logger.debug("Closed DB session for job synchronization.")
            except Exception as e:
                logger.error(
                    f"Error closing DB session in job synchronization: {type(e).__name__}"
                )
