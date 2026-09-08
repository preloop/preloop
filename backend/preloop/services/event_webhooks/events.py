"""Event catalogue and envelope for outbound webhooks (v1).

The envelope is stable: ``id``, ``type``, ``version``, ``occurred_at``,
``account_id``, ``data``. Adding a key inside ``data`` is not a version bump;
removing or renaming one is.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

ENVELOPE_VERSION = "1"

EVENT_APPROVAL_CREATED = "approval.created"
EVENT_APPROVAL_DECIDED = "approval.decided"
EVENT_POLICY_DENIED = "policy.denied"
EVENT_SESSION_ENDED = "session.ended"
EVENT_BUDGET_THRESHOLD = "budget.threshold"
EVENT_BUDGET_EXCEEDED = "budget.exceeded"
EVENT_FLOW_EXECUTION_FINISHED = "flow.execution.finished"

# Everything an endpoint may subscribe to.
EVENT_TYPES_V1: tuple[str, ...] = (
    EVENT_APPROVAL_CREATED,
    EVENT_APPROVAL_DECIDED,
    EVENT_POLICY_DENIED,
    EVENT_SESSION_ENDED,
    EVENT_BUDGET_THRESHOLD,
    EVENT_BUDGET_EXCEEDED,
    EVENT_FLOW_EXECUTION_FINISHED,
)

# One line per event, rendered in the console's create form and in the
# catalogue endpoint so the list an operator picks from cannot drift from
# the list the outbox actually routes.
EVENT_TYPE_DESCRIPTIONS: dict[str, str] = {
    EVENT_APPROVAL_CREATED: "An approval request was raised for a tool call.",
    EVENT_APPROVAL_DECIDED: (
        "An approval request reached a terminal state: approved, declined, "
        "expired or cancelled."
    ),
    EVENT_POLICY_DENIED: "A policy rule denied a tool call.",
    EVENT_SESSION_ENDED: "A runtime session closed.",
    EVENT_BUDGET_THRESHOLD: "Spend crossed a configured soft budget limit.",
    EVENT_BUDGET_EXCEEDED: "Spend passed a configured hard budget limit.",
    EVENT_FLOW_EXECUTION_FINISHED: (
        "A flow execution reached a terminal status, with the evidence "
        "receipt when one was captured."
    ),
}

# Produced only by the console/API test button. It is not subscribable: an
# endpoint filtered to one event type still receives its own test send.
EVENT_TEST = "webhook.test"

# Fixed namespace for deterministic event ids. Never change it: it would
# renumber every future event and break receiver-side deduplication against
# ids they already stored.
EVENT_ID_NAMESPACE = uuid.UUID("6f2a4a5e-0b39-5f6d-9a1c-2e6f3b9d7c41")

# Hard cap on one serialized envelope. Payloads are built from a chosen field
# list, so this is a backstop, not the primary bound.
MAX_PAYLOAD_BYTES = 64 * 1024


def deterministic_event_id(key: str) -> uuid.UUID:
    """Return the stable event id for a natural key.

    Two emitters that describe the same fact produce the same id, so the
    outbox unique constraint collapses them into one delivery and receivers
    can deduplicate on ``X-Preloop-Event-Id``.

    Args:
        key: Natural key for the fact, for example
            ``"approval.decided:<approval id>:approved"``.

    Returns:
        A UUIDv5 in the Preloop event namespace.
    """
    return uuid.uuid5(EVENT_ID_NAMESPACE, key)


def _isoformat(value: datetime) -> str:
    """Render a timestamp as ISO-8601 UTC, tolerating naive datetimes."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def build_envelope(
    *,
    event_id: uuid.UUID,
    event_type: str,
    account_id: Any,
    data: Mapping[str, Any],
    occurred_at: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the v1 envelope that becomes the request body.

    Args:
        event_id: Event id, deterministic where the fact has a natural key.
        event_type: One of the v1 types (or ``webhook.test``).
        account_id: Owning account.
        data: Event specific body.
        occurred_at: When the fact happened; defaults to now.

    Returns:
        The envelope dict. Oversized ``data`` is replaced with a truncation
        marker rather than stored, so one pathological event cannot bloat the
        outbox.
    """
    when = occurred_at or datetime.now(timezone.utc)
    envelope: dict[str, Any] = {
        "id": str(event_id),
        "type": event_type,
        "version": ENVELOPE_VERSION,
        "occurred_at": _isoformat(when),
        "account_id": str(account_id),
        "data": dict(data),
    }
    if len(json.dumps(envelope, default=str).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        envelope["data"] = {
            "truncated": True,
            "reason": f"payload exceeded {MAX_PAYLOAD_BYTES} bytes",
        }
    return envelope


def endpoint_wants(event_types: Any, event_type: str) -> bool:
    """Whether an endpoint filter selects this event type.

    An empty (or unset) filter means every v1 event. ``webhook.test`` bypasses
    the filter entirely and is never routed through this helper.

    Args:
        event_types: The endpoint's stored filter list.
        event_type: The event type being routed.

    Returns:
        True when the endpoint should receive the event.
    """
    if not event_types:
        return True
    if not isinstance(event_types, (list, tuple, set)):
        return False
    return event_type in event_types
