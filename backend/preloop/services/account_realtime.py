"""Helpers for account-scoped realtime control-plane events."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from preloop.sync.services.realtime_publisher import publish_realtime

logger = logging.getLogger(__name__)

ACCOUNT_TOPIC_FLOW_EXECUTIONS = "flow_executions"
ACCOUNT_TOPIC_APPROVALS = "approvals"
ACCOUNT_TOPIC_ACTIVITY = "activity"
ACCOUNT_TOPIC_RUNTIME_SESSIONS = "runtime_sessions"
ACCOUNT_TOPIC_MANAGED_AGENTS = "managed_agents"
ACCOUNT_TOPIC_AGENT_CONTROL = "agent_control"
ACCOUNT_TOPIC_GATEWAY_ACTIVITY = "gateway_activity"
ACCOUNT_TOPIC_BUDGET_HEALTH = "budget_health"
ACCOUNT_TOPIC_AUDIT = "audit"
ACCOUNT_TOPIC_RUNNERS = "runners"
# Live activity board (#1889): aggregate metrics for every account viewer,
# and presence changes gated per subject in ``WebSocketManager``.
ACCOUNT_TOPIC_ACTIVITY_METRICS = "activity_metrics"
ACCOUNT_TOPIC_PRESENCE = "presence"
PRESENCE_PERSON_SUBJECTS = frozenset({"person", "device", "person_device"})
#: Pull and merge requests opened, commented on, reviewed or merged.
ACCOUNT_TOPIC_CODE_ACTIVITY = "code_activity"
#: Live turn progress (reply deltas, tool rows, turn bounds) of a terminal
#: session a sidecar can drive. Its own topic so views that reload on any
#: agent_control event are not woken several times a second.
ACCOUNT_TOPIC_SESSION_PROGRESS = "session_progress"
MAX_NATS_PAYLOAD_BYTES = 1_000_000

ACCOUNT_REALTIME_TOPICS = {
    ACCOUNT_TOPIC_FLOW_EXECUTIONS,
    ACCOUNT_TOPIC_APPROVALS,
    ACCOUNT_TOPIC_ACTIVITY,
    ACCOUNT_TOPIC_RUNTIME_SESSIONS,
    ACCOUNT_TOPIC_MANAGED_AGENTS,
    ACCOUNT_TOPIC_AGENT_CONTROL,
    ACCOUNT_TOPIC_GATEWAY_ACTIVITY,
    ACCOUNT_TOPIC_BUDGET_HEALTH,
    ACCOUNT_TOPIC_AUDIT,
    ACCOUNT_TOPIC_RUNNERS,
    ACCOUNT_TOPIC_ACTIVITY_METRICS,
    ACCOUNT_TOPIC_PRESENCE,
    ACCOUNT_TOPIC_CODE_ACTIVITY,
    ACCOUNT_TOPIC_SESSION_PROGRESS,
}


def build_account_event(
    *,
    account_id: str,
    topic: str,
    event_type: str,
    payload: Optional[dict[str, Any]] = None,
    timestamp: Optional[datetime] = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build one normalized account-scoped realtime event."""
    event: dict[str, Any] = {
        "account_id": str(account_id),
        "topic": topic,
        "type": event_type,
        "timestamp": (timestamp or datetime.now(timezone.utc)).isoformat(),
        "payload": payload or {},
    }
    for key, value in fields.items():
        if value is not None:
            event[key] = value
    return event


def emit_account_event(event: dict[str, Any]) -> None:
    """Queue one account-scoped realtime event. Never blocks or raises.

    The event is encoded here, so later changes to ``event`` by the caller are
    not published, and the bytes are handed to the loop that owns the NATS
    client. Callers on worker threads or on other loops never touch the client.
    """
    account_id = event.get("account_id")
    if not account_id or not event.get("topic"):
        return
    try:
        payload_bytes = encode_realtime_event_for_nats(
            event,
            context=f"account {account_id}",
        )
    except Exception:
        # Realtime delivery should not fail the caller.
        logger.exception("Failed to encode account realtime event")
        return
    if payload_bytes is None:
        return
    publish_realtime(f"account-updates.{account_id}", payload_bytes)


def _json_safe(value: Any) -> Any:
    """Recursively convert realtime event values to JSON-serializable forms."""
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return value


def encode_realtime_event_for_nats(
    event: dict[str, Any],
    *,
    context: str,
) -> bytes | None:
    """Encode a realtime event, truncating heavy payload fields if needed."""
    payload_bytes = json.dumps(_json_safe(event)).encode()
    if len(payload_bytes) <= MAX_NATS_PAYLOAD_BYTES:
        return payload_bytes

    logger.warning(
        "NATS payload size %s exceeds 1MB limit for %s, truncating event details",
        len(payload_bytes),
        context,
    )
    event = _truncate_realtime_event(event)
    payload_bytes = json.dumps(_json_safe(event)).encode()
    if len(payload_bytes) <= MAX_NATS_PAYLOAD_BYTES:
        return payload_bytes

    logger.error(
        "NATS payload size %s still exceeds 1MB limit after truncation, "
        "dropping event for %s",
        len(payload_bytes),
        context,
    )
    return None


def _truncate_realtime_event(event: dict[str, Any]) -> dict[str, Any]:
    event = event.copy()
    if "payload" in event and isinstance(event["payload"], dict):
        payload = event["payload"].copy()
        payload.pop("request", None)
        payload.pop("response", None)
        if "conversation_preview" in payload and isinstance(
            payload["conversation_preview"], dict
        ):
            preview = payload["conversation_preview"].copy()
            # The history endpoint retains the full sanitized preview. Realtime
            # only needs recent text for canvas bubbles; dropping every message
            # made large Claude conversations look silent while still active.
            messages = preview.get("messages")
            preview["messages"] = [
                {
                    "source": str(message.get("source", ""))[:16],
                    "role": str(message.get("role", ""))[:32],
                    "text": message["text"][:300],
                    "redacted": bool(message.get("redacted")),
                    "truncated": True,
                }
                for message in (messages[-2:] if isinstance(messages, list) else [])
                if isinstance(message, dict) and isinstance(message.get("text"), str)
            ]
            metadata = preview.get("metadata")
            if isinstance(metadata, dict):
                preview["metadata"] = {
                    **metadata,
                    "has_truncated_content": True,
                }
            payload["conversation_preview"] = preview
        event["payload"] = payload
    return event
