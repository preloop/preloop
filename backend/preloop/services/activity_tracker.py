"""Activity tracking service for user interactions and conversions."""

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy.orm import Session

from preloop.models.models import Event
from preloop.services.db_executor import run_db_async
from preloop.services.session_manager import WebSocketSession, session_manager

logger = logging.getLogger(__name__)


class ActivityEventType(str, Enum):
    """Activity event types."""

    PAGE_VIEW = "page_view"
    ACTION = "action"
    CONVERSION = "conversion"
    FLOW_EXECUTION_STARTED = "flow_execution_started"
    FLOW_EXECUTION_COMPLETED = "flow_execution_completed"
    FLOW_EXECUTION_FAILED = "flow_execution_failed"
    TRACKER_POLL_COMPLETED = "tracker_poll_completed"
    WEBHOOK_RECEIVED = "webhook_received"
    BACKGROUND_TASK_COMPLETED = "background_task_completed"


def _build_activity_event(data: dict, session: WebSocketSession) -> Event:
    """Build an Event model from client activity data."""
    event_type = data.get("event")
    if not event_type:
        raise ValueError("Activity message missing event type")

    timestamp_str = data.get("timestamp")
    if timestamp_str:
        try:
            if isinstance(timestamp_str, int):
                timestamp = datetime.fromtimestamp(
                    timestamp_str / 1000, tz=timezone.utc
                )
            else:
                timestamp = datetime.fromisoformat(timestamp_str.replace("Z", "+00:00"))
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid timestamp format: {timestamp_str}, error: {e}")
            timestamp = datetime.now(timezone.utc)
    else:
        timestamp = datetime.now(timezone.utc)

    activity = Event(
        session_id=uuid.UUID(session.id),
        user_id=session.user_id,
        account_id=session.account_id,
        fingerprint=session.fingerprint,
        event_type=event_type,
        timestamp=timestamp,
        ip_address=session.ip_address,
    )

    metadata = data.get("metadata", {})

    if event_type == ActivityEventType.PAGE_VIEW:
        activity.path = data.get("path")
        activity.referrer = data.get("referrer") or metadata.get("referrer")
        activity.event_data = metadata
    elif event_type == ActivityEventType.ACTION:
        activity.action = data.get("action")
        activity.element = metadata.get("element")
        activity.element_text = metadata.get("text")
        activity.event_data = metadata
    elif event_type == ActivityEventType.CONVERSION:
        activity.conversion_event = data.get("conversion_event")
        activity.conversion_value = data.get("value")
        activity.event_data = metadata
    else:
        activity.event_data = data.copy()
        for field in ["event", "timestamp", "metadata"]:
            activity.event_data.pop(field, None)

    return activity


async def handle_activity(data: dict, session: WebSocketSession) -> None:
    """Handle activity tracking messages from client."""
    event_type = data.get("event")
    if not event_type:
        logger.warning(f"Activity message missing event type: {data}")
        return

    session_manager.update_activity(session.id)

    try:
        activity = _build_activity_event(data, session)
    except ValueError as e:
        logger.warning(str(e))
        return

    try:
        activity = await run_db_async(lambda db: _persist_activity_event(db, activity))
    except Exception as e:
        logger.error(f"Failed to persist activity: {e}", exc_info=True)
        return

    logger.debug(
        f"Tracked {event_type} activity for session {session.id} "
        f"({'user ' + str(session.user_id) if session.user_id else 'anonymous'})"
    )

    import json
    from preloop.sync.services.event_bus import event_bus_service

    activity_message = {
        "type": "activity_update",
        "account_id": str(activity.account_id) if activity.account_id else None,
        "activity": {
            "id": str(activity.id),
            "session_id": str(activity.session_id) if activity.session_id else None,
            "user_id": str(activity.user_id) if activity.user_id else None,
            "account_id": str(activity.account_id) if activity.account_id else None,
            "event_type": activity.event_type,
            "timestamp": activity.timestamp.isoformat(),
            "path": activity.path,
            "action": activity.action,
            "event_data": activity.event_data,
        },
    }

    if event_bus_service.nc and event_bus_service.nc.is_connected:
        try:
            await event_bus_service.nc.publish(
                "admin.activity", json.dumps(activity_message).encode()
            )
            logger.debug(f"Published activity event to NATS: {event_type}")
        except Exception as nats_error:
            logger.error(f"Failed to publish to NATS: {nats_error}")


def _persist_activity_event(db: Session, activity: Event) -> Event:
    """Persist an activity event within the provided session."""
    db.add(activity)
    db.commit()
    db.refresh(activity)
    return activity


async def track_system_event(
    event_type: str,
    account_id: uuid.UUID,
    event_data: dict,
    db: Session,
) -> None:
    """Track a system event (not tied to a user session)."""
    activity = Event(
        session_id=None,
        user_id=None,
        account_id=account_id,
        fingerprint=None,
        event_type=event_type,
        timestamp=datetime.now(timezone.utc),
        event_data=event_data,
    )

    try:
        db.add(activity)
        db.commit()
        logger.debug(f"Tracked system event {event_type} for account {account_id}")
    except Exception as e:
        logger.error(f"Failed to persist system event: {e}", exc_info=True)
        db.rollback()


async def track_flow_execution_event(
    event_type: str,
    execution_id: uuid.UUID,
    flow_id: uuid.UUID,
    account_id: uuid.UUID,
    status: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = None,
) -> None:
    """Track a flow execution event."""
    event_data = {
        "execution_id": str(execution_id),
        "flow_id": str(flow_id),
    }

    if status:
        event_data["status"] = status
    if error:
        event_data["error"] = error

    await track_system_event(event_type, account_id, event_data, db)
