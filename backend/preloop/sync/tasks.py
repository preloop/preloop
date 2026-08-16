from preloop.sync.config import logger
from preloop.models.db.session import get_db_session
from preloop.models.crud import crud_tracker
from preloop.sync.scanner.core import scan_tracker
from datetime import datetime
from typing import Any, Optional, Union

# Every task name a worker may be dispatched (one NATS subject each:
# ``preloop.sync.tasks.<name>``).
#
# The ``tasks`` JetStream stream uses WORKQUEUE retention, where consumer
# subject filters must NOT overlap. A worker pool that excludes some tasks
# therefore cannot subscribe to the `preloop.sync.tasks.*` wildcard — it has
# to enumerate the subjects it wants, which is what this registry is for.
# Add new dispatchable tasks here or a dedicated pool will silently never
# receive them.
DISPATCHABLE_TASKS: tuple[str, ...] = (
    "scan_tracker_task",
    "poll_tracker",
    "notify_admins",
    "process_webhook_event",
    "run_scheduled_flow",
    "cleanup_tracker_webhooks",
    "reprice_gateway_usage_task",
    "ingest_provider_billing",
    "send_optimization_digest",
    "execute_flow",
    "resume_flow_execution",
)


async def scan_tracker_task(
    tracker_id: Union[int, str],
    since: Optional[datetime] = None,
    force_update: bool = False,
) -> Optional[dict[str, Any]]:
    return await poll_tracker(tracker_id, since, force_update)


async def poll_tracker(
    tracker_id: Union[int, str],
    since: Optional[datetime] = None,
    force_update: bool = False,
) -> Optional[dict[str, Any]]:
    logger.info("Starting scan for tracker %s", tracker_id)
    db = next(get_db_session())
    try:
        tracker = crud_tracker.get(db, id=tracker_id)
        if not tracker:
            logger.error("Tracker %s not found", tracker_id)
            return None

        # Await the async scan_tracker directly
        stats = await scan_tracker(db, tracker, since=since, force_update=force_update)
        crud_tracker.validate(db, id=tracker_id, is_valid=True)
        logger.info("Scan for tracker %s completed. Stats: %s", tracker_id, stats)
        return stats
    except Exception as e:
        logger.error("Error scanning tracker %s: %s", tracker_id, e, exc_info=True)
        crud_tracker.validate(db, id=tracker_id, is_valid=False, message=str(e))
        return None
    finally:
        db.close()


def notify_admins(
    subject: str, message: str, message_html: Optional[str] = None
) -> None:
    """Send admin notifications via email, Slack, and Mattermost.

    Skips all notifications during testing (when TESTING=true environment variable is set).
    Includes instance URL in Slack/Mattermost notifications for context.

    Args:
        subject: Notification subject/title.
        message: Plain text message body.
        message_html: Optional HTML version of the message for email.
    """
    import os

    from preloop.utils.email import send_email  # noqa: E402
    from preloop.config import settings  # noqa: E402
    import requests

    # Skip notifications during testing
    if os.getenv("TESTING") == "true":
        logger.info(f"Skipping admin notification (TESTING mode): {subject}")
        return

    logger.info(f"Notifying admins: {subject} - {message}")

    # Get instance URL for context in notifications
    instance_url = settings.preloop_url or "unknown instance"

    # Prefix subject with instance URL for chat notifications
    instance_prefix = f"[{instance_url}] "

    # Send email notification (only if product_team_email is configured)
    admin_email = settings.product_team_email
    if admin_email:
        # Include instance URL in email subject
        email_subject = f"{instance_prefix}{subject}"
        send_email(admin_email, email_subject, message, message_html)

    # Send Slack notification if webhook is configured
    slack_webhook = settings.slack_webhook_url
    if slack_webhook:
        try:
            # Include instance URL in Slack notification
            slack_text = f"*{instance_prefix}{subject}*\n{message}"
            slack_payload = {"text": slack_text}
            response = requests.post(slack_webhook, json=slack_payload, timeout=5)
            response.raise_for_status()
            logger.info("Slack notification sent successfully")
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")

    # Send Mattermost notification if webhook is configured
    mattermost_webhook = settings.mattermost_webhook_url
    if mattermost_webhook:
        try:
            # Include instance URL in Mattermost notification
            mattermost_text = f"**{instance_prefix}{subject}**\n{message}"
            mattermost_payload = {"text": mattermost_text}
            response = requests.post(
                mattermost_webhook, json=mattermost_payload, timeout=5
            )
            response.raise_for_status()
            logger.info("Mattermost notification sent successfully")
        except Exception as e:
            logger.error(f"Failed to send Mattermost notification: {e}")


def serialize_uuids(obj: Any) -> Any:
    """
    Recursively convert UUID objects to strings in a dictionary or list.
    This ensures UUIDs can be serialized to JSON for JSONB fields.
    """
    from uuid import UUID

    if isinstance(obj, UUID):
        return str(obj)
    elif isinstance(obj, dict):
        return {key: serialize_uuids(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [serialize_uuids(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(serialize_uuids(item) for item in obj)
    else:
        return obj


async def process_webhook_event(
    tracker_id: int,
    event_type: str,
    payload: dict[str, Any],
    **kwargs: Any,
) -> None:
    """
    This task is triggered when a webhook event is received from a tracker.
    It uses the FlowTriggerService to check if any flows should be initiated.
    """
    logger.info(f"Processing tracker event: {tracker_id} - {event_type}")
    logger.debug(f"Payload: {payload}")
    logger.debug(f"kwargs: {kwargs}")

    db = next(get_db_session())
    try:
        tracker = crud_tracker.get(db, id=tracker_id)
        if not tracker:
            logger.error(f"Tracker {tracker_id} not found.")
            return

        from preloop.services.flow_trigger_service import FlowTriggerService
        from preloop.sync.event_normalizer import (
            normalize_event_type,
            extract_filter_fields,
        )

        # Normalize the event type from tracker-specific to standard format
        normalized_event_type = normalize_event_type(
            tracker.tracker_type, event_type, payload
        )

        # Extract filter fields for conditional triggering
        filter_fields = extract_filter_fields(tracker.tracker_type, event_type, payload)

        logger.info(
            f"Normalized event type: '{event_type}' -> '{normalized_event_type}'"
        )
        logger.debug(f"Extracted filter fields: {filter_fields}")

        # Serialize UUIDs in payload and kwargs to strings for JSON storage
        serialized_payload = serialize_uuids(payload)
        serialized_kwargs = serialize_uuids(kwargs)

        # Merge filter fields into payload for trigger_config matching
        # FlowTriggerService checks payload against trigger_config
        enriched_payload = {**serialized_payload, **filter_fields}

        event_data = {
            "source": tracker.tracker_type,  # Tracker type (github, gitlab, jira)
            "tracker_id": str(tracker.id),  # Tracker UUID for project lookup
            "type": normalized_event_type,
            "payload": enriched_payload,
            "account_id": str(tracker.account_id),
            **serialized_kwargs,
        }

        trigger_service = FlowTriggerService(db)
        await trigger_service.process_event(event_data)
    finally:
        db.close()


async def run_scheduled_flow(flow_id: str) -> None:
    """
    Handle one tick of a schedule (cron) flow trigger.

    Published by the scheduler service for flows with
    ``trigger_event_source == 'schedule'``. Delegates to
    ``FlowTriggerService.run_scheduled_tick`` which enforces the overlap
    (skip-if-previous-running) and pause-suppression policies.
    """
    logger.info("Processing scheduled tick for flow %s", flow_id)
    db = next(get_db_session())
    try:
        from preloop.services.flow_trigger_service import FlowTriggerService

        trigger_service = FlowTriggerService(db)
        outcome = await trigger_service.run_scheduled_tick(flow_id)
        logger.info("Scheduled tick for flow %s -> %s", flow_id, outcome)
    finally:
        db.close()


def reprice_gateway_usage_task(
    account_id: str,
    start: str,
    end: str,
    only_unpriced: bool = True,
    dry_run: bool = False,
) -> dict[str, object] | None:
    """Re-price gateway usage rows for one account in a time window.

    Dispatched over NATS (function name in the task payload). ``start`` and
    ``end`` are ISO-8601 timestamps.
    """
    from datetime import datetime

    from preloop.services.model_price_catalog import load_catalog
    from preloop.services.usage_repricing import reprice_gateway_usage

    load_catalog()
    db = next(get_db_session())
    try:
        result = reprice_gateway_usage(
            db,
            account_id=account_id,
            start=datetime.fromisoformat(start),
            end=datetime.fromisoformat(end),
            only_unpriced=only_unpriced,
            dry_run=dry_run,
        )
        return {
            "rows_examined": result.rows_examined,
            "rows_updated": result.rows_updated,
            "rows_skipped": result.rows_skipped,
            "cost_before": result.cost_before,
            "cost_after": result.cost_after,
            "dry_run": result.dry_run,
        }
    except Exception as e:
        logger.error(
            "Error repricing usage for account %s: %s",
            account_id,
            e,
            exc_info=True,
        )
        return None
    finally:
        db.close()


def ingest_provider_billing(account_id: str | None = None) -> object | None:
    """Fetch provider billing/usage actuals for reconciliation.

    The implementation lives in the Enterprise billing plugin; this OSS shim
    resolves it through the plugin service registry and no-ops (with a debug
    log) when the plugin is not installed.
    """
    from preloop.plugins.base import get_plugin_manager

    service = get_plugin_manager().get_service("provider_billing_ingestion")
    if service is None:
        logger.debug(
            "provider_billing_ingestion service not available; skipping ingest"
        )
        return None
    db = next(get_db_session())
    try:
        return service.ingest(db, account_id=account_id)
    except Exception as e:
        logger.error("Provider billing ingestion failed: %s", e, exc_info=True)
        return None
    finally:
        db.close()


def send_optimization_digest(account_id: str | None = None) -> object | None:
    """Build and email the weekly cost optimization & savings digest.

    The implementation lives in the Enterprise billing plugin; this OSS shim
    resolves it through the plugin service registry and no-ops (with a debug
    log) when the plugin is not installed.
    """
    from preloop.plugins.base import get_plugin_manager

    service = get_plugin_manager().get_service("optimization_digest")
    if service is None:
        logger.debug("optimization_digest service not available; skipping digest")
        return None
    db = next(get_db_session())
    try:
        return service(db, account_id=account_id)
    except Exception as e:
        logger.error("Optimization digest failed: %s", e, exc_info=True)
        return None
    finally:
        db.close()


async def cleanup_tracker_webhooks(tracker_id: str) -> None:
    """
    Clean up webhooks when a tracker is deleted.

    This task:
    1. Finds all webhooks associated with projects/organizations under this tracker
    2. Deletes those webhook records from our database
    3. Checks if there are any other non-deleted trackers of the same type with the same URL
    4. If not, deletes the webhooks from the external tracker service

    Args:
        tracker_id: The ID of the deleted tracker
    """
    logger.info(f"Starting webhook cleanup for tracker {tracker_id}")

    db = next(get_db_session())
    try:
        from preloop.models.models.tracker import Tracker
        from preloop.models.models.webhook import Webhook
        from preloop.models.models.organization import Organization
        from preloop.models.models.project import Project
        from preloop.sync.trackers import create_tracker_client

        # Get the deleted tracker (include deleted ones)
        tracker = db.query(Tracker).filter(Tracker.id == tracker_id).first()
        if not tracker:
            logger.error(f"Tracker {tracker_id} not found for webhook cleanup")
            return

        logger.info(
            f"Cleaning up webhooks for tracker {tracker.name} (type: {tracker.tracker_type}, url: {tracker.url})"
        )

        # Find all organizations under this tracker
        organizations = (
            db.query(Organization).filter(Organization.tracker_id == tracker_id).all()
        )
        org_ids = [org.id for org in organizations]

        # Find all projects under this tracker (either directly or through organizations)
        projects = (
            db.query(Project)
            .filter(
                (Project.tracker_id == tracker_id)
                | (Project.organization_id.in_(org_ids) if org_ids else False)
            )
            .all()
        )
        project_ids = [proj.id for proj in projects]

        # Find all webhooks for these projects and organizations
        webhooks = (
            db.query(Webhook)
            .filter(
                (Webhook.project_id.in_(project_ids) if project_ids else False)
                | (Webhook.organization_id.in_(org_ids) if org_ids else False)
            )
            .all()
        )

        logger.info(
            f"Found {len(webhooks)} webhooks to clean up for tracker {tracker_id}"
        )

        # Check if there are other non-deleted trackers with same type and URL
        other_trackers = (
            db.query(Tracker)
            .filter(
                Tracker.tracker_type == tracker.tracker_type,
                Tracker.url == tracker.url,
                Tracker.id != tracker_id,
                Tracker.is_deleted.is_(False),
            )
            .all()
        )

        should_delete_external = len(other_trackers) == 0
        if should_delete_external:
            logger.info(
                "No other active trackers with same type/URL found. Will delete webhooks from external tracker."
            )
        else:
            logger.info(
                f"Found {len(other_trackers)} other active trackers with same type/URL. "
                f"Will not delete webhooks from external tracker."
            )

        # Delete webhooks from external tracker if needed
        if should_delete_external and webhooks:
            try:
                # Create a tracker client to delete webhooks
                client = await create_tracker_client(
                    tracker_type=tracker.tracker_type,
                    tracker_id=tracker_id,
                    api_key=tracker.resolved_api_key,
                    connection_details={
                        "url": tracker.url,
                        **(tracker.connection_details or {}),
                    },
                )

                for webhook in webhooks:
                    try:
                        if webhook.external_id:
                            await client.delete_webhook(webhook.external_id)
                            logger.info(
                                f"Deleted webhook {webhook.external_id} from external tracker"
                            )
                    except Exception as e:
                        logger.error(
                            f"Failed to delete webhook {webhook.external_id} from external tracker: {e}",
                            exc_info=True,
                        )
            except Exception as e:
                logger.error(
                    f"Failed to create tracker client for webhook cleanup: {e}",
                    exc_info=True,
                )

        # Delete webhook records from our database
        for webhook in webhooks:
            try:
                db.delete(webhook)
                logger.info(f"Deleted webhook record {webhook.id} from database")
            except Exception as e:
                logger.error(
                    f"Failed to delete webhook record {webhook.id}: {e}", exc_info=True
                )

        db.commit()
        logger.info(
            f"Webhook cleanup completed for tracker {tracker_id}. Deleted {len(webhooks)} webhooks."
        )

    except Exception as e:
        db.rollback()
        logger.error(
            f"Error during webhook cleanup for tracker {tracker_id}: {e}",
            exc_info=True,
        )
    finally:
        db.close()


# Tasks that ack JetStream after a successful DB claim, then run for a long time.
ACK_AFTER_CLAIM_TASKS = frozenset({"execute_flow", "resume_flow_execution"})


async def execute_flow(
    execution_id: str,
    *,
    _ack: Any = None,
) -> dict[str, Any] | None:
    """Claim and run a flow execution on a sync worker."""
    from preloop.services.flow_execution_runner import claim_and_run_execution

    logger.info("execute_flow task started for execution %s", execution_id)
    return await claim_and_run_execution(execution_id, resume=False, ack=_ack)


async def resume_flow_execution(
    execution_id: str,
    *,
    _ack: Any = None,
) -> dict[str, Any] | None:
    """Claim and resume monitoring for an orphaned/stale flow execution."""
    from preloop.services.flow_execution_runner import claim_and_run_execution

    logger.info("resume_flow_execution task started for execution %s", execution_id)
    return await claim_and_run_execution(execution_id, resume=True, ack=_ack)
