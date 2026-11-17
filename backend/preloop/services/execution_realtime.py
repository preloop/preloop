"""Sequenced execution updates and bounded lifecycle-only projections."""

import logging

from sqlalchemy.orm import Session

from preloop.models.crud import execution_realtime as crud_realtime
from preloop.services.db_executor import run_db_async, run_db_sync
from preloop.utils.execution_credentials import CredentialMasker

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


def _stamp(
    db: Session, event: dict, credential_masker: CredentialMasker | None = None
) -> dict:
    """Mask an event before allocating its delivery cursor."""
    from preloop.models.crud.execution_credentials import for_execution

    masker = (
        credential_masker
        if credential_masker is not None
        else for_execution(db, event["execution_id"])
    )
    event = masker.mask(event)
    seq, lifecycle_seq = crud_realtime.allocate_sequence(
        db,
        event["execution_id"],
        lifecycle=event.get("type") in LIFECYCLE_TYPES,
    )
    return {
        **event,
        "seq": seq,
        "lifecycle_seq": lifecycle_seq,
        "sequence_domain": "execution",
    }


def _resync_without_output(event: dict) -> dict:
    """Request resync without forwarding output when masking cannot be checked."""
    return _unsequenced(
        {
            key: event[key]
            for key in ("execution_id", "flow_id", "account_id", "type", "timestamp")
            if key in event
        }
    )


def stamp_execution_event(event: dict) -> dict:
    """Mask and allocate an authoritative cursor for a synchronous publisher."""
    try:
        return run_db_sync(lambda db: _stamp(db, event))
    except Exception:
        return _resync_without_output(event)


async def stamp_execution_event_async(
    event: dict, *, credential_masker: CredentialMasker | None = None
) -> dict:
    """Mask and allocate, optionally reusing the current line's trusted snapshot."""
    try:
        return await run_db_async(lambda db: _stamp(db, event, credential_masker))
    except Exception:
        return _resync_without_output(event)


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
