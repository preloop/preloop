"""Sequenced execution updates and bounded lifecycle-only projections."""

import logging

from preloop.models.crud import execution_realtime as crud_realtime
from preloop.services.db_executor import run_db_async, run_db_sync

logger = logging.getLogger(__name__)


def _unsequenced(event: dict) -> dict:
    """Keep best-effort delivery available without inventing a cursor."""
    logger.warning("Execution realtime cursor unavailable; requesting client resync")
    return {
        key: value
        for key, value in {
            **event,
            "resync_required": True,
            "sequence_domain": "execution",
        }.items()
        if key not in {"seq", "lifecycle_seq"}
    }


LIFECYCLE_TYPES = frozenset(
    {
        "execution_started",
        "status_update",
        "execution_completed",
        "execution_failed",
        "execution_parked",
        "execution_retry",
        "agent_status",
        "agent_started",
        "agent_stopped",
        "user_stopped",
    }
)
# No logs, prompts, results, fleet context or arbitrary tool arguments are allowed.
LIFECYCLE_FIELDS = (
    "status",
    "agent_session_reference",
    "start_time",
    "end_time",
    "stop_requested_at",
    "stop_confirmed_at",
)
MAX_LIFECYCLE_BYTES = 8192


def stamp_execution_event(event: dict) -> dict:
    """Allocate an authoritative cursor for a synchronous publisher."""
    try:
        seq, lifecycle_seq = run_db_sync(
            lambda db: crud_realtime.allocate_sequence(
                db,
                event["execution_id"],
                lifecycle=event.get("type") in LIFECYCLE_TYPES,
            )
        )
    except Exception:
        return _unsequenced(event)
    return {
        **event,
        "seq": seq,
        "lifecycle_seq": lifecycle_seq,
        "sequence_domain": "execution",
    }


async def stamp_execution_event_async(event: dict) -> dict:
    """Allocate without blocking an async publisher's event loop."""
    try:
        seq, lifecycle_seq = await run_db_async(
            lambda db: crud_realtime.allocate_sequence(
                db,
                event["execution_id"],
                lifecycle=event.get("type") in LIFECYCLE_TYPES,
            )
        )
    except Exception:
        return _unsequenced(event)
    return {
        **event,
        "seq": seq,
        "lifecycle_seq": lifecycle_seq,
        "sequence_domain": "execution",
    }


def lifecycle_event(event: dict) -> dict | None:
    """Project only bounded lifecycle scalars before serializing to a client."""
    if event.get("type") not in LIFECYCLE_TYPES:
        return None
    payload = event.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    projected = {
        key: value[:256]
        for key in LIFECYCLE_FIELDS
        if isinstance(value := payload.get(key), str)
    }
    # IDs, type and timestamp are server-generated but bound defensively too.
    result = {
        key: value[:128]
        for key in ("type", "execution_id", "flow_id", "timestamp")
        if isinstance(value := event.get(key), str)
    }
    seq = event.get("lifecycle_seq")
    if type(seq) is int and 0 <= seq < 2**63:
        result["seq"] = seq
    if event.get("resync_required") is True:
        result["resync_required"] = True
    result["sequence_domain"] = "lifecycle"
    result["payload"] = projected
    return result
