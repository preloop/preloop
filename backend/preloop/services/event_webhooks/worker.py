"""Background worker that drains the webhook delivery outbox.

Modelled on ``OptimizationJobSweeper`` in
:mod:`preloop.services.session_optimization_jobs`: started from the app
lifespan, one pass now and then every tick. The database work runs in a
thread (it is synchronous CRUD) and the HTTP work runs on the event loop with
a bounded semaphore, so neither can starve the other.

Nothing is buffered in process memory: the queue is the ``webhook_delivery``
table, a pass claims a bounded batch, and terminal rows are purged on a
retention window.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import httpx

from preloop.config import settings
from preloop.models.models.webhook_endpoint import (
    SOURCE_APPROVAL_WORKFLOW,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks import outbox
from preloop.services.event_webhooks.signing import (
    ATTEMPT_HEADER,
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    SIGNATURE_HEADER,
    USER_AGENT,
    signature_header,
)
from preloop.utils.encryption import decrypt_value

logger = logging.getLogger(__name__)

# A receiver that answers with a body still tells us nothing we should store;
# only this much of an error string is kept, and never the response body.
_ERROR_SNIPPET_CHARS = 200


@dataclass
class PreparedDelivery:
    """One claimed delivery, detached from the ORM so it can cross threads."""

    delivery_id: Any
    endpoint_id: Any
    url: str
    secret: str
    event_id: str
    event_type: str
    attempt: int
    body: bytes


@dataclass
class AttemptOutcome:
    """The result of one POST, applied back to the outbox in a later pass."""

    delivery_id: Any
    success: bool
    response_status: Optional[int] = None
    error: Optional[str] = None


def _open_worker_session():
    """Open a session this worker owns and closes."""
    from preloop.models.db.session import get_session_factory

    return get_session_factory()()


def prepare_claimed(
    delivery: WebhookDelivery, endpoint: WebhookEndpoint
) -> Optional[PreparedDelivery]:
    """Render a claimed row into an immutable request description.

    Args:
        delivery: The claimed outbox row.
        endpoint: Its endpoint.

    Returns:
        The prepared delivery, or None when the secret cannot be decrypted
        (a deployment that lost its encryption key must not silently post
        unsigned bodies).
    """
    try:
        secret = decrypt_value(endpoint.secret_encrypted)
    except Exception:  # noqa: BLE001 - a broken secret is a delivery failure
        logger.warning("Webhook endpoint %s has an undecryptable secret", endpoint.id)
        return None
    body = json.dumps(delivery.payload, default=str, separators=(",", ":")).encode(
        "utf-8"
    )
    return PreparedDelivery(
        delivery_id=delivery.id,
        endpoint_id=endpoint.id,
        url=endpoint.url,
        secret=secret,
        event_id=str(delivery.event_id),
        event_type=delivery.event_type,
        attempt=delivery.attempt_count + 1,
        body=body,
    )


def build_headers(prepared: PreparedDelivery, timestamp: Optional[int] = None) -> dict:
    """Build the headers for one attempt, including a fresh signature.

    The timestamp is regenerated per attempt so a delivery retried an hour
    later still verifies inside the documented 5 minute tolerance.

    Args:
        prepared: The prepared delivery.
        timestamp: Unix seconds; defaults to now.

    Returns:
        The header mapping to send.
    """
    stamp = int(time.time()) if timestamp is None else int(timestamp)
    return {
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        EVENT_ID_HEADER: prepared.event_id,
        EVENT_TYPE_HEADER: prepared.event_type,
        DELIVERY_ID_HEADER: str(prepared.delivery_id),
        ATTEMPT_HEADER: str(prepared.attempt),
        SIGNATURE_HEADER: signature_header(prepared.secret, prepared.body, stamp),
    }


async def post_delivery(
    client: httpx.AsyncClient, prepared: PreparedDelivery
) -> AttemptOutcome:
    """POST one delivery and classify the outcome.

    Args:
        client: Shared HTTP client.
        prepared: The delivery to send.

    Returns:
        The outcome to record. Any non-2xx or transport error is a failure;
        no response body is retained.
    """
    try:
        response = await client.post(
            prepared.url,
            content=prepared.body,
            headers=build_headers(prepared),
        )
    except Exception as exc:  # noqa: BLE001 - transport errors are outcomes
        return AttemptOutcome(
            delivery_id=prepared.delivery_id,
            success=False,
            error=f"{type(exc).__name__}: {str(exc)[:_ERROR_SNIPPET_CHARS]}",
        )
    if 200 <= response.status_code < 300:
        return AttemptOutcome(
            delivery_id=prepared.delivery_id,
            success=True,
            response_status=response.status_code,
        )
    return AttemptOutcome(
        delivery_id=prepared.delivery_id,
        success=False,
        response_status=response.status_code,
        error=f"receiver returned HTTP {response.status_code}",
    )


def claim_batch(db, *, now: Optional[datetime] = None) -> list[PreparedDelivery]:
    """Claim due rows and prepare them for sending.

    Rows whose endpoint is inactive or circuit-open are released back to the
    queue (their claim is cleared) rather than attempted. An open circuit
    that has not reached its cooldown is parked at ``opened + cooldown`` so
    the next poll does not reclaim it. After cooldown, at most one probe
    per endpoint is prepared in this pass.

    Args:
        db: Worker-owned session.
        now: Naive UTC now, injectable by tests.

    Returns:
        The prepared deliveries for this pass.
    """
    moment = now or outbox._utcnow()
    claimed = outbox.claim_due_deliveries(db, now=moment)
    prepared: list[PreparedDelivery] = []
    probing: set[Any] = set()
    for delivery in claimed:
        endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None:
            delivery.status = "dead"
            delivery.last_error = "endpoint no longer exists"
            db.add(delivery)
            continue
        if not outbox.endpoint_is_deliverable(endpoint, moment):
            delivery.claimed_at = None
            probe_at = outbox.circuit_probe_at(endpoint)
            if probe_at is not None:
                delivery.next_attempt_at = probe_at
            db.add(delivery)
            continue
        if endpoint.circuit_opened_at is not None:
            if endpoint.id in probing:
                delivery.claimed_at = None
                db.add(delivery)
                continue
            probing.add(endpoint.id)
        item = prepare_claimed(delivery, endpoint)
        if item is None:
            outbox.record_attempt(
                db,
                delivery=delivery,
                endpoint=endpoint,
                success=False,
                error="endpoint secret could not be decrypted",
                now=moment,
            )
            continue
        prepared.append(item)
    db.commit()
    return prepared


def apply_outcomes(db, outcomes: list[AttemptOutcome]) -> None:
    """Write attempt outcomes back to the outbox.

    Args:
        db: Worker-owned session.
        outcomes: One per attempted delivery.
    """
    for outcome in outcomes:
        delivery = db.get(WebhookDelivery, outcome.delivery_id)
        if delivery is None:
            continue
        endpoint = db.get(WebhookEndpoint, delivery.endpoint_id)
        if endpoint is None:
            continue
        status = outbox.record_attempt(
            db,
            delivery=delivery,
            endpoint=endpoint,
            success=outcome.success,
            response_status=outcome.response_status,
            error=outcome.error,
        )
        if endpoint.source == SOURCE_APPROVAL_WORKFLOW:
            _write_back_approval_state(db, delivery, status, outcome)
    db.commit()


def _write_back_approval_state(
    db, delivery: WebhookDelivery, status: str, outcome: AttemptOutcome
) -> None:
    """Keep ``approval_request.webhook_posted_at`` / ``webhook_error`` honest.

    The legacy columns predate the outbox and are part of the approval API
    response, so they keep meaning what they always meant: posted means a
    receiver accepted it, error means it ultimately did not.

    Args:
        db: Worker-owned session.
        delivery: The shim delivery that just finished an attempt.
        status: The delivery's new status.
        outcome: The attempt outcome.
    """
    if delivery.subject_id is None:
        return
    try:
        from preloop.models.models.approval_request import ApprovalRequest

        request = db.get(ApprovalRequest, delivery.subject_id)
        if request is None:
            return
        if status == "delivered":
            request.webhook_posted_at = delivery.delivered_at
            request.webhook_error = None
        elif status == "dead":
            request.webhook_error = (
                outcome.error or "webhook delivery failed after retries"
            )[:500]
        else:
            return
        db.add(request)
    except Exception:  # noqa: BLE001 - bookkeeping must not break delivery
        logger.debug("Approval webhook write-back skipped", exc_info=True)


async def run_once(db_factory=_open_worker_session) -> int:
    """Run one full pass: claim, post, record.

    Args:
        db_factory: Session factory, injectable by tests.

    Returns:
        How many deliveries were attempted.
    """
    db = db_factory()
    try:
        prepared = await asyncio.to_thread(claim_batch, db)
    except Exception:  # noqa: BLE001 - a failed claim retries next tick
        logger.warning("Webhook delivery claim failed", exc_info=True)
        db.close()
        return 0
    if not prepared:
        db.close()
        return 0

    semaphore = asyncio.Semaphore(max(1, settings.webhook_delivery_concurrency))

    async def _send(item: PreparedDelivery, client: httpx.AsyncClient):
        async with semaphore:
            return await post_delivery(client, item)

    try:
        async with httpx.AsyncClient(
            timeout=settings.webhook_delivery_timeout_seconds,
            follow_redirects=False,
        ) as client:
            outcomes = await asyncio.gather(*(_send(item, client) for item in prepared))
        await asyncio.to_thread(apply_outcomes, db, list(outcomes))
    except Exception:  # noqa: BLE001 - claims lapse and the rows retry
        logger.warning("Webhook delivery pass failed", exc_info=True)
    finally:
        db.close()
    return len(prepared)


class WebhookDeliveryWorker:
    """Periodic outbox drain plus retention purge."""

    def __init__(self, poll_interval_seconds: Optional[int] = None) -> None:
        self.poll_interval = (
            poll_interval_seconds or settings.webhook_delivery_poll_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._passes = 0

    async def start(self) -> None:
        """Start the drain loop."""
        if self._running:
            logger.warning("Webhook delivery worker is already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Webhook delivery worker started (poll_interval=%ss)", self.poll_interval
        )

    async def stop(self) -> None:
        """Stop the drain loop."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected: stop() cancels the loop task.
                pass

    async def _loop(self) -> None:
        """Drain now, then on every tick, purging on a slow cadence."""
        while self._running:
            try:
                await run_once()
                self._passes += 1
                # Retention is cheap but pointless to run every few seconds.
                if self._passes % 120 == 1:
                    await asyncio.to_thread(self._purge)
            except Exception:  # noqa: BLE001 - the loop outlives one bad pass
                logger.error("Webhook delivery pass failed", exc_info=True)
            try:
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break

    @staticmethod
    def _purge() -> None:
        """Delete terminal rows past the retention window."""
        db = _open_worker_session()
        try:
            deleted = outbox.purge_terminal_deliveries(db)
            db.commit()
            if deleted:
                logger.info("Purged %d terminal webhook deliveries", deleted)
        except Exception:  # noqa: BLE001 - retention is best effort
            db.rollback()
            logger.warning("Webhook delivery purge failed", exc_info=True)
        finally:
            db.close()


_worker_instance: Optional[WebhookDeliveryWorker] = None


def get_webhook_delivery_worker() -> WebhookDeliveryWorker:
    """Get or create the process-wide delivery worker."""
    global _worker_instance
    if _worker_instance is None:
        _worker_instance = WebhookDeliveryWorker()
    return _worker_instance
