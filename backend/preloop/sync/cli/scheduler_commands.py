import click
import logging
import atexit
import signal  # Import signal
import pytz
from datetime import datetime, timedelta
import asyncio
from preloop.config import settings
from preloop.models.db.session import get_db_session
from ..services.manager import sync_scheduled_jobs
from ..services.flow_schedules import sync_flow_schedule_jobs
from ..services.event_bus import event_bus_service


from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from ..config import logger


# --- Scheduler Setup ---
# Global scheduler instance
scheduler = None


def optimization_digest_trigger() -> CronTrigger:
    """Return the weekly digest trigger: Monday 09:00 UTC.

    No startup offset. A freshly started scheduler waits until the next
    Monday, so a deploy cannot send the digest.
    """
    return CronTrigger(day_of_week="mon", hour=9, minute=0, timezone="UTC")


def governance_session_digest_sweep_trigger() -> CronTrigger:
    """Return the hourly session-digest sweep: minute 0 of every hour, UTC.

    Cron only, same as the weekly digest. An interval trigger with a startup
    offset would summarise sessions after every scheduler restart.
    """
    return CronTrigger(minute=0, timezone="UTC")


def governance_weekly_review_trigger() -> CronTrigger:
    """Return the weekly governance review trigger: Monday 07:00 UTC.

    Two hours before the Monday 09:00 UTC optimization digest, so the digest
    can include the review. No startup offset.
    """
    return CronTrigger(day_of_week="mon", hour=7, minute=0, timezone="UTC")


def spend_outlier_daily_trigger() -> CronTrigger:
    """Return the daily spend outlier trigger: 00:30 UTC.

    Half an hour after the UTC day closes, so yesterday is complete, and well
    before the Monday 09:00 UTC digest that lists the findings.
    """
    return CronTrigger(hour=0, minute=30, timezone="UTC")


#: How often the per-session cost check runs. Not per request: a session that
#: crosses its threshold is reported within this interval.
SPEND_OUTLIER_SESSION_CHECK_MINUTES = 15


#: Minutes between post-deploy backfill passes. Each pass is bounded by
#: ``preloop.models.backfills.run_pending_backfills`` (60 s by default).
# Keep long backfill passes from dominating the shared worker slot.
DATA_BACKFILL_INTERVAL_MINUTES = 15


def shutdown_scheduler():
    """Function to shut down the scheduler."""
    global scheduler
    if scheduler and scheduler.running:
        logger.info("Shutting down scheduler...")
        try:
            scheduler.shutdown(wait=False)  # Use wait=False for atexit
            logger.info("Scheduler shut down successfully.")
        except Exception as e:
            logger.error(f"Error shutting down scheduler: {e}")


# Register the shutdown hook globally for the CLI process
atexit.register(shutdown_scheduler)


async def run_scheduler_async(
    scheduler: AsyncIOScheduler, reload_interval: int, db, max_workers: int
):
    """Runs the scheduler in an asyncio event loop."""

    # Connect to NATS using the shared task publisher service
    # This ensures the stream is created with the correct, robust configuration.
    try:
        await event_bus_service.connect()
    except Exception as e:
        logger.error(f"Scheduler failed to connect to NATS: {e}", exc_info=True)
        # Depending on strictness, you might want to exit here.
        # For now, we'll allow the scheduler to run but it won't be able to queue tasks.
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def shutdown_handler(sig):
        logger.info(f"Received signal {sig}, stopping scheduler...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler, sig)

    scheduler.start()
    logger.info(f"APScheduler started with max_workers={max_workers}")

    scheduler.add_job(
        sync_scheduled_jobs,
        trigger=IntervalTrigger(seconds=reload_interval),
        args=[scheduler, db],
        id="tracker_reload_job",
        name="Sync Tracker Jobs",
        replace_existing=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(pytz.utc),
    )
    logger.info(
        f"Scheduled tracker job synchronization every {reload_interval} seconds."
    )

    scheduler.add_job(
        sync_flow_schedule_jobs,
        trigger=IntervalTrigger(seconds=reload_interval),
        args=[scheduler],
        id="flow_schedule_sync_job",
        name="Sync Scheduled Flow Jobs",
        replace_existing=True,
        misfire_grace_time=60,
        next_run_time=datetime.now(pytz.utc),
    )
    logger.info(
        f"Scheduled flow schedule synchronization every {reload_interval} seconds."
    )

    # Daily provider-billing ingestion (cost reconciliation). The worker-side
    # task no-ops unless the Enterprise billing plugin (and at least one
    # provider connection) is present.
    if getattr(settings, "provider_billing_sync_enabled", True):

        async def _publish_provider_billing_ingest() -> None:
            try:
                await event_bus_service.publish_task("ingest_provider_billing")
            except Exception:
                logger.exception("Failed to publish provider billing ingest task")

        scheduler.add_job(
            _publish_provider_billing_ingest,
            trigger=IntervalTrigger(hours=24),
            id="provider_billing_ingest_job",
            name="Ingest Provider Billing Actuals",
            replace_existing=True,
            misfire_grace_time=3600,
            next_run_time=datetime.now(pytz.utc) + timedelta(minutes=5),
        )
        logger.info("Scheduled daily provider billing ingestion.")

    # Daily GitHub Copilot usage import. The task pulls the newest report day
    # GitHub has finished (two full UTC days after it closes) and no-ops when
    # no account has a Copilot connection.
    if getattr(settings, "copilot_usage_sync_enabled", True):

        async def _publish_copilot_usage_import() -> None:
            try:
                await event_bus_service.publish_task("ingest_copilot_usage")
            except Exception:
                logger.exception("Failed to publish Copilot usage import task")

        scheduler.add_job(
            _publish_copilot_usage_import,
            trigger=IntervalTrigger(hours=24),
            id="copilot_usage_import_job",
            name="Import GitHub Copilot Usage",
            replace_existing=True,
            misfire_grace_time=3600,
            next_run_time=datetime.now(pytz.utc) + timedelta(minutes=10),
        )
        logger.info("Scheduled daily GitHub Copilot usage import.")

    # Daily Claude Code Analytics import (#1413). Imports up to yesterday UTC
    # (the report lags about an hour) and no-ops without a connection.
    if getattr(settings, "anthropic_usage_sync_enabled", True):

        async def _publish_anthropic_usage_import() -> None:
            try:
                await event_bus_service.publish_task("ingest_anthropic_usage")
            except Exception:
                logger.exception("Failed to publish Anthropic usage import task")

        scheduler.add_job(
            _publish_anthropic_usage_import,
            trigger=IntervalTrigger(hours=24),
            id="anthropic_usage_import_job",
            name="Import Anthropic Claude Code Usage",
            replace_existing=True,
            misfire_grace_time=3600,
            next_run_time=datetime.now(pytz.utc) + timedelta(minutes=12),
        )
        logger.info("Scheduled daily Anthropic usage import.")

    # Scheduled model-catalog sync (the automatic 'preloop models sync').
    # Default OFF: self-hosted catalogs must never change on upgrade without
    # an explicit opt-in (MODEL_CATALOG_SYNC_SCHEDULED_ENABLED=true). The
    # worker-side task re-checks the setting, so a queued task after a
    # disable still no-ops. Principal-bound subscription-OAuth credentials
    # are never used for discovery.
    if getattr(settings, "model_catalog_sync_scheduled_enabled", False):
        catalog_sync_interval_hours = max(
            1, int(getattr(settings, "model_catalog_sync_interval_hours", 24) or 24)
        )

        async def _publish_model_catalog_sync() -> None:
            try:
                await event_bus_service.publish_task("sync_model_catalog")
            except Exception:
                logger.exception("Failed to publish model catalog sync task")

        scheduler.add_job(
            _publish_model_catalog_sync,
            trigger=IntervalTrigger(hours=catalog_sync_interval_hours),
            id="model_catalog_sync_job",
            name="Sync Provider Model Catalogs",
            replace_existing=True,
            misfire_grace_time=3600,
            next_run_time=datetime.now(pytz.utc) + timedelta(minutes=15),
        )
        logger.info(
            "Scheduled model catalog sync every %d hour(s).",
            catalog_sync_interval_hours,
        )

    async def _publish_feedback_tick() -> None:
        try:
            await event_bus_service.publish_task("reconcile_flow_feedback")
        except Exception:
            logger.exception("Failed to publish feedback reconciliation")

    scheduler.add_job(
        _publish_feedback_tick,
        trigger=IntervalTrigger(seconds=15),
        id="flow_feedback_reconciliation",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Pull request record refresh: a bounded batch per minute, each record
    # re-armed after five minutes; webhooks make a record due at once.
    async def _publish_pull_request_refresh() -> None:
        try:
            await event_bus_service.publish_task("refresh_pull_request_records")
        except Exception:
            logger.exception("Failed to publish pull request record refresh")

    scheduler.add_job(
        _publish_pull_request_refresh,
        trigger=IntervalTrigger(seconds=60),
        id="pull_request_record_refresh",
        replace_existing=True,
        misfire_grace_time=60,
    )

    async def _publish_security_maintenance_tick() -> None:
        try:
            await event_bus_service.publish_task("reconcile_security_maintenance")
        except Exception:
            logger.exception("Failed to publish security-maintenance reconciliation")

    scheduler.add_job(
        _publish_security_maintenance_tick,
        trigger=IntervalTrigger(seconds=30),
        id="security_maintenance_reconciliation",
        replace_existing=True,
        misfire_grace_time=30,
    )

    # Post-deploy data backfills. Migrations only register them (the Helm
    # pre-upgrade hook has a five-minute budget); a worker advances pending
    # ones in committed batches. The first pass runs shortly after start, so
    # a deploy that registered a backfill picks it up without waiting a full
    # interval. A pass with nothing pending is one indexed query.
    async def _publish_data_backfills() -> None:
        try:
            await event_bus_service.publish_task("run_data_backfills")
        except Exception:
            logger.exception("Failed to publish data backfill task")

    scheduler.add_job(
        _publish_data_backfills,
        trigger=IntervalTrigger(minutes=DATA_BACKFILL_INTERVAL_MINUTES),
        id="data_backfill_job",
        name="Run Pending Data Backfills",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=120,
        next_run_time=datetime.now(pytz.utc) + timedelta(minutes=1),
    )

    # Hourly workspace retention pass: delete captured workspace snapshots
    # and Docker agent-workspace-* volumes older than the retention window.
    async def _publish_workspace_cleanup() -> None:
        try:
            await event_bus_service.publish_task("cleanup_flow_workspaces")
        except Exception:
            logger.exception("Failed to publish workspace cleanup task")

    scheduler.add_job(
        _publish_workspace_cleanup,
        trigger=IntervalTrigger(hours=1),
        id="workspace_cleanup_job",
        name="Reap Expired Flow Workspaces",
        replace_existing=True,
        misfire_grace_time=600,
        next_run_time=datetime.now(pytz.utc) + timedelta(minutes=2),
    )
    logger.info(
        "Scheduled hourly workspace retention (ttl=%s hours).",
        getattr(settings, "workspace_snapshot_ttl_hours", 24),
    )

    # Hourly gateway activity maintenance: compact model gateway call rows
    # stored before slim storage and, when enabled, strip message content
    # older than the plan history window. Bounded per pass.
    async def _publish_gateway_activity_maintenance() -> None:
        try:
            await event_bus_service.publish_task("maintain_gateway_activity")
        except Exception:
            logger.exception("Failed to publish gateway activity maintenance task")

    if (
        settings.gateway_activity_compaction_enabled
        or settings.gateway_activity_body_retention_enabled
        or settings.gateway_activity_size_backfill_enabled
        or settings.flow_log_gateway_compaction_enabled
    ):
        scheduler.add_job(
            _publish_gateway_activity_maintenance,
            trigger=IntervalTrigger(hours=1),
            id="gateway_activity_maintenance_job",
            name="Compact Stored Gateway Activity",
            replace_existing=True,
            misfire_grace_time=600,
            next_run_time=datetime.now(pytz.utc) + timedelta(minutes=5),
        )

    # Periodic Stripe subscription reconciliation. A missed or rejected
    # webhook used to leave a subscription stale until an operator reconciled
    # it by hand; this pass repairs it within one interval. The worker-side
    # task no-ops unless the Enterprise billing plugin is present and a Stripe
    # key is configured, and it only ever reads from Stripe. The first run is
    # scheduled shortly after sync-role startup so a restart also repairs
    # whatever drifted while the process was down.
    subscription_reconcile_hours = max(
        1, int(getattr(settings, "billing_subscription_reconcile_hours", 6) or 6)
    )

    async def _publish_subscription_reconcile() -> None:
        try:
            await event_bus_service.publish_task("reconcile_stripe_subscriptions")
        except Exception:
            logger.exception("Failed to publish subscription reconciliation task")

    scheduler.add_job(
        _publish_subscription_reconcile,
        trigger=IntervalTrigger(hours=subscription_reconcile_hours),
        id="subscription_reconcile_job",
        name="Reconcile Stripe Subscriptions",
        replace_existing=True,
        misfire_grace_time=3600,
        next_run_time=datetime.now(pytz.utc) + timedelta(minutes=3),
    )
    logger.info(
        "Scheduled subscription reconciliation every %d hour(s).",
        subscription_reconcile_hours,
    )

    # Spend outlier alerts (#960). A daily pass for yesterday's per-user spend
    # and model mix, and a periodic check of recently active sessions. Both
    # record each finding once, so a restart that re-runs them is harmless.
    async def _publish_spend_outlier_daily() -> None:
        try:
            await event_bus_service.publish_task("evaluate_spend_outliers")
        except Exception:
            logger.exception("Failed to publish spend outlier daily pass")

    scheduler.add_job(
        _publish_spend_outlier_daily,
        trigger=spend_outlier_daily_trigger(),
        id="spend_outlier_daily_job",
        name="Evaluate Spend Outliers",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    async def _publish_spend_outlier_sessions() -> None:
        try:
            await event_bus_service.publish_task("evaluate_spend_outlier_sessions")
        except Exception:
            logger.exception("Failed to publish spend outlier session check")

    scheduler.add_job(
        _publish_spend_outlier_sessions,
        trigger=IntervalTrigger(minutes=SPEND_OUTLIER_SESSION_CHECK_MINUTES),
        id="spend_outlier_session_job",
        name="Check Session Spend Outliers",
        replace_existing=True,
        misfire_grace_time=300,
    )
    logger.info(
        "Scheduled spend outlier checks: daily 00:30 UTC, sessions every %d min.",
        SPEND_OUTLIER_SESSION_CHECK_MINUTES,
    )

    # Weekly cost digest. Cron only: the job store is in-memory, so a
    # next_run_time of "now + 10 minutes" sent a digest after every scheduler
    # restart, including every prod deploy. CronTrigger's next fire is the
    # next Monday 09:00 UTC, including when the process starts on Monday
    # after that minute. The worker-side task no-ops unless the Enterprise
    # billing plugin is present.
    if getattr(settings, "cost_digest_enabled", True):

        async def _publish_optimization_digest() -> None:
            try:
                await event_bus_service.publish_task("send_optimization_digest")
            except Exception:
                logger.exception("Failed to publish optimization digest task")

        scheduler.add_job(
            _publish_optimization_digest,
            trigger=optimization_digest_trigger(),
            id="optimization_digest_job",
            name="Send Weekly Optimization Digest",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("Scheduled weekly optimization digest for Monday 09:00 UTC.")

    # Governance review (#1709). Cron only, for the same reason as the digest
    # above: the job store is in-memory, so a next_run_time of "now" would
    # run the review after every scheduler restart. The worker tasks no-op
    # unless an Enterprise handler is registered.
    if getattr(settings, "governance_review_enabled", True):

        async def _publish_governance_session_digest_sweep() -> None:
            try:
                await event_bus_service.publish_task("governance_session_digest_sweep")
            except Exception:
                logger.exception("Failed to publish governance session digest sweep")

        scheduler.add_job(
            _publish_governance_session_digest_sweep,
            trigger=governance_session_digest_sweep_trigger(),
            id="governance_session_digest_sweep_job",
            name="Sweep Governance Session Digests",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        async def _publish_governance_weekly_review() -> None:
            try:
                await event_bus_service.publish_task("governance_weekly_review")
            except Exception:
                logger.exception("Failed to publish governance weekly review")

        scheduler.add_job(
            _publish_governance_weekly_review,
            trigger=governance_weekly_review_trigger(),
            id="governance_weekly_review_job",
            name="Run Weekly Governance Review",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info(
            "Scheduled governance review: digest sweep hourly, "
            "weekly review Monday 07:00 UTC."
        )

    await stop_event.wait()
    logger.info("Scheduler event loop stopped.")


@click.option(
    "--reload-interval",
    type=int,
    default=60,
    help="Interval (in seconds) to reload tracker list and sync jobs.",
    show_default=True,
)
@click.option(
    "--max-workers",
    type=int,
    default=10,
    help="Maximum number of concurrent tracker update jobs.",
    show_default=True,
)
@click.option(
    "--log-level",
    type=click.Choice(
        ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False
    ),
    default="INFO",
    help="Set the logging level.",
    show_default=True,
)
@click.command(name="scheduler")
def scheduler_cmd(reload_interval: int, max_workers: int, log_level: str):
    """
    Start the Preloop Sync scheduler service in the foreground.

    This service periodically checks for active trackers and schedules
    background jobs to scan them for updates based on their configured intervals.
    Press Ctrl+C to stop the service.
    """
    global scheduler
    # Set up logging level based on command option
    logging.getLogger("preloop-sync").setLevel(getattr(logging, log_level.upper()))
    # Configure root logger for APScheduler logs etc.
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    click.echo(
        f"Starting Preloop Sync scheduler service in foreground (reload interval: {reload_interval}s, max workers: {max_workers})..."
    )
    click.echo("Press Ctrl+C to stop.")

    # Get database session (ensure it stays open for the service duration)
    # Note: The session created here is primarily for the manager initialization.
    # The sync_scheduled_jobs function now creates its own session per run.
    db = next(get_db_session())

    # Configure scheduler executor
    executors = {"default": AsyncIOExecutor()}
    job_defaults = {"coalesce": False, "max_instances": 1}

    # Initialize the scheduler
    scheduler = AsyncIOScheduler(
        executors=executors, job_defaults=job_defaults, timezone="UTC"
    )

    from preloop.services.audit import start_audit, stop_audit

    start_audit()
    try:
        # Run the scheduler in an asyncio event loop
        asyncio.run(run_scheduler_async(scheduler, reload_interval, db, max_workers))
        logger.info("Main loop exited.")

    finally:
        # Explicitly attempt scheduler shutdown here
        shutdown_scheduler()
        stop_audit()

        # Close DB session
        # Use db.is_active check instead of is_closed
        if db and db.is_active:
            try:
                db.close()
                logger.info("Initial database session closed.")
            except Exception as e:
                logger.error(f"Error closing initial database session: {e}")
        click.echo("Preloop Sync scheduler service stopped.")
