"""Transactional outbox for outbound event webhooks.

Emitters insert rows; the delivery worker drains them. Sync emitters insert in
the caller's own transaction, so a delivery cannot outlive a state change that
rolled back. Nothing here performs HTTP: the request path only writes a row.
"""

from __future__ import annotations

import logging
import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.models.webhook_endpoint import (
    DELIVERY_DEAD,
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    SOURCE_ACCOUNT,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks.events import (
    EVENT_TEST,
    build_envelope,
    deterministic_event_id,
    endpoint_wants,
)

logger = logging.getLogger(__name__)

# Total attempts before a delivery is dead-lettered, and the base delay after
# attempts 1..5. Sum is 61 minutes 10 seconds of wall clock before the row
# dies, which is long enough to ride out a receiver restart and short enough
# that an operator sees the failure the same hour.
MAX_ATTEMPTS = 6
RETRY_BASE_DELAYS: tuple[int, ...] = (10, 60, 300, 900, 2400)
# Multiplicative jitter bounds. A fleet of deliveries that failed together
# must not retry together.
JITTER_MIN = 0.8
JITTER_MAX = 1.2
# A claimed row whose worker died is reclaimed after this long.
CLAIM_LEASE_SECONDS = 300
ERROR_MAX_CHARS = 500


@dataclass
class EnqueueResult:
    """What one emit did, for logs and for the test-send endpoint."""

    event_id: Optional[uuid.UUID] = None
    delivery_ids: list[uuid.UUID] = field(default_factory=list)
    endpoints_matched: int = 0
    skipped_queue_full: int = 0


def _utcnow() -> datetime:
    """Naive UTC now, matching the DateTime columns used across the schema."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    """Normalize an aware timestamp to naive UTC for storage/comparison."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def retry_delay_seconds(
    attempt: int, *, rng: Optional[random.Random] = None
) -> Optional[float]:
    """Return the delay before the next attempt, or None when exhausted.

    Args:
        attempt: How many attempts have already been made (1-based).
        rng: Random source, injected by tests to assert jitter bounds.

    Returns:
        Seconds to wait, jittered, or None when the delivery is dead.
    """
    if attempt < 1 or attempt > len(RETRY_BASE_DELAYS):
        return None
    base = RETRY_BASE_DELAYS[attempt - 1]
    source = rng or random
    return base * source.uniform(JITTER_MIN, JITTER_MAX)


def endpoint_is_deliverable(endpoint: WebhookEndpoint, now: datetime) -> bool:
    """Whether an endpoint may be attempted right now.

    Inactive endpoints never are. An open circuit blocks attempts until the
    cooldown elapses, after which exactly one probe is allowed through (the
    breaker stays "open" until that probe succeeds).

    Args:
        endpoint: The endpoint row.
        now: Naive UTC now.

    Returns:
        True when the worker may attempt this endpoint.
    """
    if not endpoint.active:
        return False
    opened = _naive(endpoint.circuit_opened_at)
    if opened is None:
        return True
    cooldown = timedelta(seconds=settings.webhook_circuit_cooldown_seconds)
    return now >= opened + cooldown


def _truncate_error(message: str) -> str:
    """Keep stored errors short; response bodies are never stored."""
    return message[:ERROR_MAX_CHARS]


def _pending_count(db: Session, account_id: Any) -> int:
    """Count undelivered rows for one account (the queue bound)."""
    return int(
        db.execute(
            select(func.count())
            .select_from(WebhookDelivery)
            .where(
                WebhookDelivery.account_id == account_id,
                WebhookDelivery.status == DELIVERY_PENDING,
            )
        ).scalar_one()
    )


def _delivery_values(
    *,
    account_id: Any,
    endpoint: WebhookEndpoint,
    event_id: uuid.UUID,
    event_type: str,
    occurred_at: datetime,
    payload: Mapping[str, Any],
    generation: int,
    now: datetime,
    subject_id: Optional[Any] = None,
) -> dict[str, Any]:
    """Build one outbox row. Due immediately; the worker owns the schedule."""
    return {
        "id": uuid.uuid4(),
        "account_id": account_id,
        "endpoint_id": endpoint.id,
        "event_id": event_id,
        "event_type": event_type,
        "subject_id": subject_id,
        "occurred_at": occurred_at,
        "payload": dict(payload),
        "status": DELIVERY_PENDING,
        "attempt_count": 0,
        "next_attempt_at": now,
        "generation": generation,
        "created_at": now,
        "updated_at": now,
    }


def _insert_deliveries(db: Session, rows: Sequence[dict[str, Any]]) -> list[uuid.UUID]:
    """Insert outbox rows, ignoring duplicates of (endpoint, event, generation).

    Args:
        db: Caller's session; the insert joins the caller's transaction.
        rows: Row dicts from :func:`_delivery_values`.

    Returns:
        Ids of the rows actually inserted (duplicates are dropped).
    """
    if not rows:
        return []
    statement = (
        pg_insert(WebhookDelivery)
        .values(list(rows))
        .on_conflict_do_nothing(constraint="uq_webhook_delivery_event")
        .returning(WebhookDelivery.id)
    )
    return [row[0] for row in db.execute(statement).fetchall()]


def select_endpoints(
    db: Session, *, account_id: Any, event_type: str
) -> list[WebhookEndpoint]:
    """Return the account endpoints subscribed to an event type.

    Shim endpoints (``source='approval_workflow'``) are excluded: they carry
    the historical approval body and are targeted explicitly by the shim.

    Args:
        db: Database session.
        account_id: Owning account.
        event_type: The v1 event type being routed.

    Returns:
        Matching active endpoints.
    """
    endpoints = (
        db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.account_id == account_id,
                WebhookEndpoint.active.is_(True),
                WebhookEndpoint.source == SOURCE_ACCOUNT,
            )
        )
        .scalars()
        .all()
    )
    return [
        endpoint
        for endpoint in endpoints
        if event_type == EVENT_TEST or endpoint_wants(endpoint.event_types, event_type)
    ]


def enqueue_event(
    db: Session,
    *,
    account_id: Any,
    event_type: str,
    data: Mapping[str, Any],
    occurred_at: Optional[datetime] = None,
    natural_key: Optional[str] = None,
    subject_id: Optional[Any] = None,
    endpoints: Optional[Iterable[WebhookEndpoint]] = None,
) -> EnqueueResult:
    """Record one event for every subscribed endpoint.

    Never raises: an emit failure must not break the governance action that
    produced the event.

    Args:
        db: Caller's session. The rows join the caller's transaction.
        account_id: Owning account.
        event_type: One of the v1 types, or ``webhook.test``.
        data: Event body.
        occurred_at: When the fact happened; defaults to now.
        natural_key: Stable key for the fact. When given, the event id is
            derived from it and a repeated emit is a no-op.
        subject_id: The object the event is about, when it has an id.
        endpoints: Explicit target list (used by the test-send endpoint).

    Returns:
        What was enqueued.
    """
    result = EnqueueResult()
    try:
        now = _utcnow()
        targets = (
            list(endpoints)
            if endpoints is not None
            else select_endpoints(db, account_id=account_id, event_type=event_type)
        )
        result.endpoints_matched = len(targets)
        if not targets:
            return result

        event_id = deterministic_event_id(natural_key) if natural_key else uuid.uuid4()
        result.event_id = event_id
        occurred = _naive(occurred_at) or now
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            account_id=account_id,
            data=data,
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )

        pending = _pending_count(db, account_id)
        rows: list[dict[str, Any]] = []
        for endpoint in targets:
            if pending + len(rows) >= settings.webhook_max_pending_per_account:
                result.skipped_queue_full += 1
                endpoint.last_error = _truncate_error(
                    "queue_full: account outbox is at "
                    f"{settings.webhook_max_pending_per_account} pending deliveries"
                )
                db.add(endpoint)
                continue
            rows.append(
                _delivery_values(
                    account_id=account_id,
                    endpoint=endpoint,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=occurred,
                    payload=envelope,
                    generation=0,
                    now=now,
                    subject_id=subject_id,
                )
            )
        if result.skipped_queue_full:
            logger.warning(
                "Webhook outbox full for account %s: dropped %d %s deliveries",
                account_id,
                result.skipped_queue_full,
                event_type,
            )
        result.delivery_ids = _insert_deliveries(db, rows)
        return result
    except Exception:  # noqa: BLE001 - emitting must never break the caller
        logger.warning("Failed to enqueue webhook event %s", event_type, exc_info=True)
        return result


def enqueue_raw_delivery(
    db: Session,
    *,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: Mapping[str, Any],
    natural_key: str,
    occurred_at: Optional[datetime] = None,
    subject_id: Optional[Any] = None,
) -> EnqueueResult:
    """Enqueue a body that is not a v1 envelope, for one explicit endpoint.

    Used by the approval-workflow compatibility shim, which must keep posting
    the historical message shape.

    Args:
        db: Caller's session.
        endpoint: The shim endpoint.
        event_type: Recorded type (still ``approval.created``).
        payload: The literal body to POST.
        natural_key: Stable key for idempotency.
        occurred_at: When the fact happened.

    Returns:
        What was enqueued.
    """
    result = EnqueueResult()
    try:
        now = _utcnow()
        event_id = deterministic_event_id(natural_key)
        result.event_id = event_id
        result.endpoints_matched = 1
        if _pending_count(db, endpoint.account_id) >= (
            settings.webhook_max_pending_per_account
        ):
            result.skipped_queue_full = 1
            logger.warning(
                "Webhook outbox full for account %s: dropped shim delivery",
                endpoint.account_id,
            )
            return result
        result.delivery_ids = _insert_deliveries(
            db,
            [
                _delivery_values(
                    account_id=endpoint.account_id,
                    endpoint=endpoint,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=_naive(occurred_at) or now,
                    payload=payload,
                    generation=0,
                    now=now,
                    subject_id=subject_id,
                )
            ],
        )
        return result
    except Exception:  # noqa: BLE001 - shim emit must never break approvals
        logger.warning("Failed to enqueue legacy approval webhook", exc_info=True)
        return result


def replay_event(
    db: Session, *, account_id: Any, event_id: uuid.UUID
) -> list[uuid.UUID]:
    """Re-enqueue one event id to every endpoint that already received it.

    History is preserved: the replay is a new generation rather than a reset
    of the original row, so the dead-lettered attempt stays visible.

    Args:
        db: Database session.
        account_id: Owning account (scoping is mandatory).
        event_id: The event id to replay.

    Returns:
        Ids of the newly created deliveries.
    """
    now = _utcnow()
    existing = (
        db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.account_id == account_id,
                WebhookDelivery.event_id == event_id,
            )
            .order_by(WebhookDelivery.generation.desc())
        )
        .scalars()
        .all()
    )
    if not existing:
        return []
    next_generation = existing[0].generation + 1
    seen: set[uuid.UUID] = set()
    rows: list[dict[str, Any]] = []
    for delivery in existing:
        if delivery.endpoint_id in seen:
            continue
        seen.add(delivery.endpoint_id)
        rows.append(
            {
                "id": uuid.uuid4(),
                "account_id": delivery.account_id,
                "endpoint_id": delivery.endpoint_id,
                "event_id": delivery.event_id,
                "event_type": delivery.event_type,
                "subject_id": delivery.subject_id,
                "occurred_at": delivery.occurred_at,
                "payload": delivery.payload,
                "status": DELIVERY_PENDING,
                "attempt_count": 0,
                "next_attempt_at": now,
                "generation": next_generation,
                "created_at": now,
                "updated_at": now,
            }
        )
    return _insert_deliveries(db, rows)


def claim_due_deliveries(
    db: Session, *, limit: Optional[int] = None, now: Optional[datetime] = None
) -> list[WebhookDelivery]:
    """Claim a bounded batch of due deliveries for this worker.

    ``FOR UPDATE SKIP LOCKED`` keeps several API replicas from fighting over
    the same rows. Rows whose claim has expired are picked up again, which is
    the at-least-once part of the contract.

    Args:
        db: Database session.
        limit: Batch size; defaults to the configured batch size.
        now: Naive UTC now, injectable by tests.

    Returns:
        The claimed rows, already stamped with ``claimed_at``.
    """
    moment = now or _utcnow()
    lease_cutoff = moment - timedelta(seconds=CLAIM_LEASE_SECONDS)
    batch = limit or settings.webhook_delivery_batch_size
    rows = (
        db.execute(
            select(WebhookDelivery)
            .where(
                WebhookDelivery.status == DELIVERY_PENDING,
                WebhookDelivery.next_attempt_at <= moment,
                (WebhookDelivery.claimed_at.is_(None))
                | (WebhookDelivery.claimed_at < lease_cutoff),
            )
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(batch)
            .with_for_update(skip_locked=True)
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.claimed_at = moment
        db.add(row)
    return list(rows)


def record_attempt(
    db: Session,
    *,
    delivery: WebhookDelivery,
    endpoint: WebhookEndpoint,
    success: bool,
    response_status: Optional[int] = None,
    error: Optional[str] = None,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
) -> str:
    """Apply one attempt's outcome to the delivery and its endpoint.

    Args:
        db: Database session.
        delivery: The row that was attempted.
        endpoint: Its endpoint.
        success: Whether the receiver answered 2xx.
        response_status: HTTP status, when there was one.
        error: Short failure description; response bodies are never stored.
        now: Naive UTC now.
        rng: Random source for jitter, injectable by tests.

    Returns:
        The delivery's new status.
    """
    moment = now or _utcnow()
    delivery.attempt_count += 1
    delivery.claimed_at = None
    delivery.response_status = response_status
    delivery.last_error = _truncate_error(error) if error else None
    endpoint.last_delivery_at = moment
    endpoint.last_response_code = response_status

    if success:
        delivery.status = DELIVERY_DELIVERED
        delivery.delivered_at = moment
        endpoint.consecutive_failures = 0
        endpoint.circuit_opened_at = None
        endpoint.last_delivery_status = DELIVERY_DELIVERED
        endpoint.last_error = None
    else:
        endpoint.consecutive_failures += 1
        endpoint.last_error = _truncate_error(error) if error else None
        threshold = settings.webhook_circuit_failure_threshold
        if endpoint.consecutive_failures >= threshold:
            # Re-stamp on every failure past the threshold so a failed probe
            # restarts the cooldown instead of retrying every pass.
            endpoint.circuit_opened_at = moment
        delay = retry_delay_seconds(delivery.attempt_count, rng=rng)
        if delay is None:
            delivery.status = DELIVERY_DEAD
            endpoint.last_delivery_status = DELIVERY_DEAD
        else:
            delivery.next_attempt_at = moment + timedelta(seconds=delay)
            endpoint.last_delivery_status = "failed"

    db.add(delivery)
    db.add(endpoint)
    return delivery.status


def purge_terminal_deliveries(db: Session, *, now: Optional[datetime] = None) -> int:
    """Delete delivered and dead rows past the retention window.

    Args:
        db: Database session.
        now: Naive UTC now.

    Returns:
        How many rows were deleted.
    """
    cutoff = (now or _utcnow()) - timedelta(
        days=settings.webhook_delivery_retention_days
    )
    deleted = (
        db.query(WebhookDelivery)
        .filter(
            WebhookDelivery.status.in_((DELIVERY_DELIVERED, DELIVERY_DEAD)),
            WebhookDelivery.updated_at < cutoff,
        )
        .delete(synchronize_session=False)
    )
    return int(deleted or 0)


async def _pending_count_async(db: Any, account_id: Any) -> int:
    """Async twin of :func:`_pending_count`."""
    result = await db.execute(
        select(func.count())
        .select_from(WebhookDelivery)
        .where(
            WebhookDelivery.account_id == account_id,
            WebhookDelivery.status == DELIVERY_PENDING,
        )
    )
    return int(result.scalar_one())


async def select_endpoints_async(
    db: Any, *, account_id: Any, event_type: str
) -> list[WebhookEndpoint]:
    """Async twin of :func:`select_endpoints`."""
    result = await db.execute(
        select(WebhookEndpoint).where(
            WebhookEndpoint.account_id == account_id,
            WebhookEndpoint.active.is_(True),
            WebhookEndpoint.source == SOURCE_ACCOUNT,
        )
    )
    return [
        endpoint
        for endpoint in result.scalars().all()
        if event_type == EVENT_TEST or endpoint_wants(endpoint.event_types, event_type)
    ]


async def enqueue_event_async(
    db: Any,
    *,
    account_id: Any,
    event_type: str,
    data: Mapping[str, Any],
    occurred_at: Optional[datetime] = None,
    natural_key: Optional[str] = None,
    subject_id: Optional[Any] = None,
) -> EnqueueResult:
    """Async twin of :func:`enqueue_event` for AsyncSession callers.

    ``ApprovalService`` holds an async session (or the sync adapter in
    :mod:`preloop.models.db.session`), so approvals emit through this.

    Args:
        db: Async session, or anything exposing an awaitable ``execute``.
        account_id: Owning account.
        event_type: One of the v1 types.
        data: Event body.
        occurred_at: When the fact happened.
        natural_key: Stable key for idempotency.

    Returns:
        What was enqueued.
    """
    result = EnqueueResult()
    try:
        now = _utcnow()
        targets = await select_endpoints_async(
            db, account_id=account_id, event_type=event_type
        )
        result.endpoints_matched = len(targets)
        if not targets:
            return result
        event_id = deterministic_event_id(natural_key) if natural_key else uuid.uuid4()
        result.event_id = event_id
        envelope = build_envelope(
            event_id=event_id,
            event_type=event_type,
            account_id=account_id,
            data=data,
            occurred_at=occurred_at or datetime.now(timezone.utc),
        )
        pending = await _pending_count_async(db, account_id)
        rows: list[dict[str, Any]] = []
        for endpoint in targets:
            if pending + len(rows) >= settings.webhook_max_pending_per_account:
                result.skipped_queue_full += 1
                continue
            rows.append(
                _delivery_values(
                    account_id=account_id,
                    endpoint=endpoint,
                    event_id=event_id,
                    event_type=event_type,
                    occurred_at=_naive(occurred_at) or now,
                    payload=envelope,
                    generation=0,
                    now=now,
                    subject_id=subject_id,
                )
            )
        if result.skipped_queue_full:
            logger.warning(
                "Webhook outbox full for account %s: dropped %d %s deliveries",
                account_id,
                result.skipped_queue_full,
                event_type,
            )
        if rows:
            inserted = await db.execute(
                pg_insert(WebhookDelivery)
                .values(rows)
                .on_conflict_do_nothing(constraint="uq_webhook_delivery_event")
                .returning(WebhookDelivery.id)
            )
            result.delivery_ids = [row[0] for row in inserted.fetchall()]
        return result
    except Exception:  # noqa: BLE001 - emitting must never break the caller
        logger.warning("Failed to enqueue webhook event %s", event_type, exc_info=True)
        return result


async def enqueue_raw_delivery_async(
    db: Any,
    *,
    endpoint: WebhookEndpoint,
    event_type: str,
    payload: Mapping[str, Any],
    natural_key: str,
    occurred_at: Optional[datetime] = None,
    subject_id: Optional[Any] = None,
) -> EnqueueResult:
    """Async twin of :func:`enqueue_raw_delivery` for the approval shim."""
    result = EnqueueResult()
    try:
        now = _utcnow()
        event_id = deterministic_event_id(natural_key)
        result.event_id = event_id
        result.endpoints_matched = 1
        pending = await _pending_count_async(db, endpoint.account_id)
        if pending >= settings.webhook_max_pending_per_account:
            result.skipped_queue_full = 1
            logger.warning(
                "Webhook outbox full for account %s: dropped shim delivery",
                endpoint.account_id,
            )
            return result
        row = _delivery_values(
            account_id=endpoint.account_id,
            endpoint=endpoint,
            event_id=event_id,
            event_type=event_type,
            occurred_at=_naive(occurred_at) or now,
            payload=payload,
            generation=0,
            now=now,
            subject_id=subject_id,
        )
        inserted = await db.execute(
            pg_insert(WebhookDelivery)
            .values([row])
            .on_conflict_do_nothing(constraint="uq_webhook_delivery_event")
            .returning(WebhookDelivery.id)
        )
        result.delivery_ids = [item[0] for item in inserted.fetchall()]
        return result
    except Exception:  # noqa: BLE001 - shim emit must never break approvals
        logger.warning("Failed to enqueue legacy approval webhook", exc_info=True)
        return result


def enqueue_event_detached(
    *,
    account_id: Any,
    event_type: str,
    data: Mapping[str, Any],
    occurred_at: Optional[datetime] = None,
    natural_key: Optional[str] = None,
    subject_id: Optional[Any] = None,
) -> EnqueueResult:
    """Enqueue on a short-lived session of our own, and commit it.

    For chokepoints that record a decision rather than change state (the
    policy evaluator's deny path), where there is no caller transaction worth
    joining. Never raises.

    Args:
        account_id: Owning account.
        event_type: One of the v1 types.
        data: Event body.
        occurred_at: When the fact happened.
        natural_key: Stable key for idempotency.

    Returns:
        What was enqueued.
    """
    result = EnqueueResult()
    try:
        from preloop.models.db.session import get_session_factory

        session = get_session_factory()()
    except Exception:  # noqa: BLE001 - no database, nothing to record
        logger.debug("No session factory for detached webhook emit", exc_info=True)
        return result
    try:
        result = enqueue_event(
            session,
            account_id=account_id,
            event_type=event_type,
            data=data,
            occurred_at=occurred_at,
            natural_key=natural_key,
            subject_id=subject_id,
        )
        session.commit()
    except Exception:  # noqa: BLE001 - emitting must never break the caller
        logger.warning("Detached webhook emit failed for %s", event_type, exc_info=True)
        session.rollback()
    finally:
        session.close()
    return result
