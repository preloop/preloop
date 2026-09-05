"""Helpers for account-scoped realtime control-plane events."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID

from preloop.sync.services.event_bus import get_nats_client

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
    """Publish one account-scoped realtime event when an event loop exists."""
    if not event.get("account_id") or not event.get("topic"):
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        from preloop.tools.utils import run_async

        try:
            run_async(_publish_account_event(event))
        except Exception:
            # Realtime delivery should not fail the caller.
            logger.exception("Failed to publish account realtime event synchronously")
        return
    loop.create_task(_publish_account_event(event))


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
            preview["messages"] = []
            metadata = preview.get("metadata")
            if isinstance(metadata, dict):
                preview["metadata"] = {
                    **metadata,
                    "has_truncated_content": True,
                }
            payload["conversation_preview"] = preview
        event["payload"] = payload
    return event


async def _publish_account_event(event: dict[str, Any]) -> None:
    account_id = event.get("account_id")
    if not account_id:
        return
    try:
        nats_client = await get_nats_client()
        if not nats_client or not nats_client.is_connected:
            return

        payload_bytes = encode_realtime_event_for_nats(
            event,
            context=f"account {account_id}",
        )
        if payload_bytes is None:
            return

        await nats_client.publish(
            f"account-updates.{account_id}",
            payload_bytes,
        )
    except Exception:
        logger.exception("Failed to publish account realtime event")
