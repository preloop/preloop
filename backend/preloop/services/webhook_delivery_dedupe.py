"""Delivery-level idempotency for webhook-sourced flow triggers.

One provider delivery must create at most one flow execution, whatever the
message layer does with the message that carries it.

Why this exists (production, 2026-09-08): a single GitHub `labeled` delivery
created two executions of the same flow. The `tasks` JetStream stream is a
workqueue and every worker of a pool shares one durable queue consumer, so the
message was stored once and delivered once, but the pod holding it was drained
mid-handler by a rolling deploy. The drain cancels in-flight handlers and the
cancel path naks, which is an immediate redelivery to the surviving pod. The
execution row had already been committed, so the redelivery created a second
one, and the issue got two pull requests.

Message-layer fixes alone cannot close this: at-least-once delivery is the
contract (nak on cancel, `ack_wait` expiry, a pod that dies without acking).
The durable guard has to be in the database, keyed on the delivery itself.

Scope and non-goals:

* Keyed on the provider delivery id (`X-GitHub-Delivery`,
  `X-Gitlab-Event-UUID`), which is stable across redeliveries of the same
  physical delivery and different for every new user action, including the
  same label applied twice.
* Tracker sources that send no delivery id (Jira, poller-sourced events) fall
  back to a content fingerprint over the identity of the event. A fingerprint
  can legitimately repeat (the same label removed and re-applied next week),
  so fingerprints are only deduplicated inside a short redelivery window,
  never by a unique constraint.
* Generic `webhook`-source flows (POST /webhooks/flows/{id}/{secret}) are left
  alone: they have no delivery id, and their documented behaviour is
  coalescing onto a RUNNING execution by resource key. Adding time-window
  dedupe there would silently drop legitimate repeat alerts.
* Existing PR/commit coalescing is untouched. This guard runs before it and
  only ever answers "this exact delivery was already processed".
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, UTC
from typing import Any, Dict, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from preloop.models.models.flow_execution import FlowExecution

logger = logging.getLogger(__name__)

DELIVERY_PREFIX = "delivery:"
CONTENT_PREFIX = "content:"

# A provider delivery id is globally unique, so the only bound needed on the
# lookup is one that keeps the index scan small.
DELIVERY_WINDOW_DAYS = 7
# A content fingerprint is NOT unique over time: it is the identity of the
# event, not of the delivery. Only treat a repeat as a duplicate while it can
# still plausibly be a redelivery of the same message (JetStream `ack_wait` is
# 180s and a drained pod redelivers immediately).
CONTENT_WINDOW_SECONDS = 900

# Sources whose events are addressed to one specific flow and already have
# their own coalescing rules; see the module docstring.
_EXCLUDED_SOURCES = frozenset({"webhook", "schedule", "manual"})


def _clean(value: Any) -> Optional[str]:
    """Return a non-empty stripped string, or None."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, uuid.UUID)) and not isinstance(value, bool):
        return str(value)
    return None


def delivery_id_from_event(event_data: Dict[str, Any]) -> Optional[str]:
    """Provider delivery id carried by ``event_data``, if any.

    ``sync.tasks.process_webhook_event`` forwards the ingress kwargs into the
    event, so the id published from ``X-GitHub-Delivery`` /
    ``X-Gitlab-Event-UUID`` arrives as ``event_data["delivery_id"]`` (and is
    persisted at that key inside ``trigger_event_details``).
    """
    if not isinstance(event_data, dict):
        return None
    return _clean(event_data.get("delivery_id"))


def _resource_identity(event_data: Dict[str, Any]) -> Optional[str]:
    """Best available identity of the resource the event is about.

    Reuses the resource key shapes the trigger service already understands
    (``github:owner/repo:issue:506``) and falls back to the raw issue/MR id so
    a payload the resource-key extractor does not recognise still fingerprints
    to something specific.
    """
    from preloop.services.flow_trigger_service import FlowTriggerService

    key = FlowTriggerService._extract_resource_key(event_data)
    if key:
        return key

    payload = event_data.get("payload")
    if not isinstance(payload, dict):
        return None
    for container_key in (
        "issue",
        "pull_request",
        "merge_request",
        "object_attributes",
    ):
        container = payload.get(container_key)
        if isinstance(container, dict):
            identity = _clean(container.get("id")) or _clean(container.get("iid"))
            if identity:
                return f"{container_key}:{identity}"
    return None


def content_fingerprint(event_data: Dict[str, Any]) -> Optional[str]:
    """Stable hash of the event identity, for sources with no delivery id.

    Hashes (source, tracker_id, normalized type, resource key, action, label
    delta). Returns None when the event carries no resource identity at all,
    because hashing "github + issues" alone would coalesce unrelated events.
    """
    if not isinstance(event_data, dict):
        return None
    resource = _resource_identity(event_data)
    if not resource:
        return None

    payload = (
        event_data.get("payload") if isinstance(event_data.get("payload"), dict) else {}
    )
    material = {
        "source": (event_data.get("source") or "").lower(),
        "tracker_id": _clean(event_data.get("tracker_id")),
        "type": event_data.get("type"),
        "resource": resource,
        "action": payload.get("action"),
        "added_labels": sorted(payload.get("added_labels") or []),
        "removed_labels": sorted(payload.get("removed_labels") or []),
    }
    encoded = json.dumps(material, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:32]


def delivery_key_for_event(event_data: Dict[str, Any]) -> Optional[str]:
    """Idempotency key for ``event_data``, or None when it has no identity.

    ``delivery:<id>`` when the provider sent a delivery id, otherwise
    ``content:<fingerprint>``. None means "do not apply delivery-level
    dedupe", which leaves the pre-existing behaviour in place.
    """
    if not isinstance(event_data, dict):
        return None
    source = (event_data.get("source") or "").lower()
    if not source or source in _EXCLUDED_SOURCES:
        return None

    delivery_id = delivery_id_from_event(event_data)
    if delivery_id:
        return f"{DELIVERY_PREFIX}{delivery_id}"

    fingerprint = content_fingerprint(event_data)
    if fingerprint:
        return f"{CONTENT_PREFIX}{fingerprint}"
    return None


def is_delivery_key(delivery_key: Optional[str]) -> bool:
    """True for keys backed by a provider delivery id (uniquely indexed)."""
    return bool(delivery_key and delivery_key.startswith(DELIVERY_PREFIX))


def _window_start(delivery_key: str, now: Optional[datetime] = None) -> datetime:
    """Oldest execution start time this key may match, as naive UTC.

    ``FlowExecution.start_time`` is a naive column holding UTC wall time, so
    the bound has to be naive too or Postgres compares against the session
    time zone.
    """
    current = now or datetime.now(UTC)
    if current.tzinfo is not None:
        current = current.astimezone(UTC).replace(tzinfo=None)
    if is_delivery_key(delivery_key):
        return current - timedelta(days=DELIVERY_WINDOW_DAYS)
    return current - timedelta(seconds=CONTENT_WINDOW_SECONDS)


def find_execution_for_delivery(
    db: Session,
    *,
    flow_id: Any,
    delivery_key: str,
    now: Optional[datetime] = None,
) -> Optional[FlowExecution]:
    """Return this flow's earlier execution for ``delivery_key``, if any.

    Any status counts: the point is "this delivery was already turned into an
    execution", not "an execution is still running". Also matches rows that
    only carry the delivery id inside ``trigger_event_details`` (written
    before the column existed, or by paths that precreate the row), which is
    what makes the guard work across the very deploy that introduces it.
    """
    if not delivery_key:
        return None

    flow_uuid = (
        uuid.UUID(str(flow_id)) if not isinstance(flow_id, uuid.UUID) else flow_id
    )
    conditions = [FlowExecution.webhook_delivery_key == delivery_key]
    if is_delivery_key(delivery_key):
        delivery_id = delivery_key[len(DELIVERY_PREFIX) :]
        conditions.append(
            FlowExecution.trigger_event_details["delivery_id"].astext == delivery_id
        )

    return (
        db.query(FlowExecution)
        .filter(
            FlowExecution.flow_id == flow_uuid,
            FlowExecution.start_time >= _window_start(delivery_key, now),
            or_(*conditions),
        )
        .order_by(FlowExecution.start_time.asc())
        .first()
    )
