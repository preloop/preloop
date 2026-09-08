"""Outbox behaviour: routing, idempotency, retries, dead-letter, replay."""

import random
import uuid
from datetime import datetime, timedelta

import pytest

from preloop.config import settings
from preloop.models.models.webhook_endpoint import (
    DELIVERY_DEAD,
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    SOURCE_APPROVAL_WORKFLOW,
    WebhookDelivery,
)
from preloop.services.event_webhooks import outbox
from preloop.services.event_webhooks.events import (
    EVENT_APPROVAL_CREATED,
    EVENT_POLICY_DENIED,
    EVENT_TEST,
)


def _deliveries(db_session, account_id):
    return (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == account_id)
        .all()
    )


# --- routing and filters ---------------------------------------------------


def test_event_reaches_every_unfiltered_endpoint(db_session, account, make_endpoint):
    make_endpoint(url="https://example.com/a")
    make_endpoint(url="https://example.com/b")

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={"approval_request_id": "1"},
    )

    assert result.endpoints_matched == 2
    assert len(result.delivery_ids) == 2


def test_filter_selects_only_subscribed_endpoints(db_session, account, make_endpoint):
    make_endpoint(url="https://example.com/a", event_types=[EVENT_APPROVAL_CREATED])
    make_endpoint(url="https://example.com/b", event_types=[EVENT_POLICY_DENIED])

    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_POLICY_DENIED,
        data={"tool_name": "shell"},
    )

    rows = _deliveries(db_session, account.id)
    assert len(rows) == 1
    assert rows[0].payload["type"] == EVENT_POLICY_DENIED


def test_inactive_endpoints_are_not_enqueued(db_session, account, make_endpoint):
    make_endpoint(active=False)

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
    )

    assert result.endpoints_matched == 0
    assert _deliveries(db_session, account.id) == []


def test_shim_endpoints_are_skipped_by_v1_routing(db_session, account, make_endpoint):
    make_endpoint(source=SOURCE_APPROVAL_WORKFLOW)

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
    )

    assert result.endpoints_matched == 0


def test_test_send_ignores_the_event_filter(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint(event_types=[EVENT_POLICY_DENIED])

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_TEST,
        data={"message": "hello"},
        endpoints=[endpoint],
    )

    assert len(result.delivery_ids) == 1


def test_other_accounts_are_never_targeted(db_session, account, make_endpoint):
    make_endpoint()
    other_account_id = uuid.uuid4()

    result = outbox.enqueue_event(
        db_session,
        account_id=other_account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
    )

    assert result.endpoints_matched == 0


# --- idempotency -----------------------------------------------------------


def test_same_natural_key_enqueues_once(db_session, account, make_endpoint):
    make_endpoint()
    first = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="approval.created:abc",
    )
    second = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="approval.created:abc",
    )

    assert first.event_id == second.event_id
    assert len(first.delivery_ids) == 1
    assert second.delivery_ids == []
    assert len(_deliveries(db_session, account.id)) == 1


def test_events_without_a_natural_key_are_distinct(db_session, account, make_endpoint):
    make_endpoint()
    first = outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_POLICY_DENIED, data={}
    )
    second = outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_POLICY_DENIED, data={}
    )

    assert first.event_id != second.event_id
    assert len(_deliveries(db_session, account.id)) == 2


# --- retry schedule --------------------------------------------------------


def test_retry_schedule_is_the_documented_backoff():
    rng = random.Random(0)
    assert outbox.RETRY_BASE_DELAYS == (10, 60, 300, 900, 2400)
    for attempt, base in enumerate(outbox.RETRY_BASE_DELAYS, start=1):
        delay = outbox.retry_delay_seconds(attempt, rng=rng)
        assert base * outbox.JITTER_MIN <= delay <= base * outbox.JITTER_MAX


def test_retry_schedule_spans_about_an_hour():
    assert sum(outbox.RETRY_BASE_DELAYS) == 3670
    assert outbox.MAX_ATTEMPTS == len(outbox.RETRY_BASE_DELAYS) + 1


def test_no_delay_after_the_last_attempt():
    assert outbox.retry_delay_seconds(outbox.MAX_ATTEMPTS) is None
    assert outbox.retry_delay_seconds(0) is None


def test_failed_attempt_schedules_the_next_one(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    now = datetime(2026, 9, 8, 12, 0, 0)

    status = outbox.record_attempt(
        db_session,
        delivery=delivery,
        endpoint=endpoint,
        success=False,
        response_status=500,
        error="receiver returned HTTP 500",
        now=now,
    )

    assert status == DELIVERY_PENDING
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at > now
    assert delivery.next_attempt_at <= now + timedelta(seconds=12)
    assert endpoint.consecutive_failures == 1
    assert endpoint.last_delivery_status == "failed"


def test_delivery_dies_after_six_attempts(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]

    statuses = [
        outbox.record_attempt(
            db_session,
            delivery=delivery,
            endpoint=endpoint,
            success=False,
            response_status=503,
            error="down",
        )
        for _ in range(outbox.MAX_ATTEMPTS)
    ]

    assert statuses[:-1] == [DELIVERY_PENDING] * (outbox.MAX_ATTEMPTS - 1)
    assert statuses[-1] == DELIVERY_DEAD
    assert delivery.attempt_count == outbox.MAX_ATTEMPTS
    assert endpoint.last_delivery_status == DELIVERY_DEAD


def test_success_clears_endpoint_failure_state(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    outbox.record_attempt(
        db_session, delivery=delivery, endpoint=endpoint, success=False, error="boom"
    )

    status = outbox.record_attempt(
        db_session,
        delivery=delivery,
        endpoint=endpoint,
        success=True,
        response_status=204,
    )

    assert status == DELIVERY_DELIVERED
    assert delivery.delivered_at is not None
    assert endpoint.consecutive_failures == 0
    assert endpoint.circuit_opened_at is None
    assert endpoint.last_error is None


def test_stored_error_is_truncated(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]

    outbox.record_attempt(
        db_session,
        delivery=delivery,
        endpoint=endpoint,
        success=False,
        error="x" * 5000,
    )

    assert len(delivery.last_error) == outbox.ERROR_MAX_CHARS


# --- circuit breaker -------------------------------------------------------


def test_circuit_opens_after_the_threshold(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    now = datetime(2026, 9, 8, 12, 0, 0)

    for _ in range(settings.webhook_circuit_failure_threshold):
        delivery.status = DELIVERY_PENDING
        delivery.attempt_count = 0
        outbox.record_attempt(
            db_session,
            delivery=delivery,
            endpoint=endpoint,
            success=False,
            error="down",
            now=now,
        )

    assert endpoint.circuit_opened_at == now
    assert outbox.endpoint_is_deliverable(endpoint, now) is False


def test_open_circuit_allows_a_probe_after_the_cooldown(
    db_session, account, make_endpoint
):
    endpoint, _ = make_endpoint()
    now = datetime(2026, 9, 8, 12, 0, 0)
    endpoint.circuit_opened_at = now

    cooldown = timedelta(seconds=settings.webhook_circuit_cooldown_seconds)
    assert outbox.endpoint_is_deliverable(endpoint, now + cooldown / 2) is False
    assert outbox.endpoint_is_deliverable(endpoint, now + cooldown) is True


def test_inactive_endpoint_is_never_deliverable(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint(active=False)
    assert outbox.endpoint_is_deliverable(endpoint, datetime(2026, 9, 8)) is False


# --- bounded queue ---------------------------------------------------------


def test_enqueue_is_refused_when_the_account_queue_is_full(
    db_session, account, make_endpoint, monkeypatch
):
    endpoint, _ = make_endpoint()
    monkeypatch.setattr(settings, "webhook_max_pending_per_account", 1)
    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="first",
    )

    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="second",
    )

    assert result.skipped_queue_full == 1
    assert result.delivery_ids == []
    assert len(_deliveries(db_session, account.id)) == 1
    assert "queue_full" in endpoint.last_error


def test_oversized_payloads_are_truncated_not_stored(
    db_session, account, make_endpoint
):
    make_endpoint()
    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={"blob": "x" * 200_000},
    )

    payload = _deliveries(db_session, account.id)[0].payload
    assert payload["data"] == {
        "truncated": True,
        "reason": "payload exceeded 65536 bytes",
    }


# --- claiming, replay, retention -------------------------------------------


def test_claim_returns_due_rows_and_stamps_them(db_session, account, make_endpoint):
    make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )

    claimed = outbox.claim_due_deliveries(db_session)

    assert len(claimed) == 1
    assert claimed[0].claimed_at is not None


def test_claim_skips_rows_that_are_not_due_yet(db_session, account, make_endpoint):
    make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    delivery.next_attempt_at = outbox._utcnow() + timedelta(minutes=5)
    db_session.flush()

    assert outbox.claim_due_deliveries(db_session) == []


def test_claim_reclaims_an_expired_lease(db_session, account, make_endpoint):
    make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    delivery.claimed_at = outbox._utcnow() - timedelta(
        seconds=outbox.CLAIM_LEASE_SECONDS + 60
    )
    db_session.flush()

    assert len(outbox.claim_due_deliveries(db_session)) == 1


def test_claim_skips_a_live_lease(db_session, account, make_endpoint):
    make_endpoint()
    outbox.enqueue_event(
        db_session, account_id=account.id, event_type=EVENT_APPROVAL_CREATED, data={}
    )
    delivery = _deliveries(db_session, account.id)[0]
    delivery.claimed_at = outbox._utcnow()
    db_session.flush()

    assert outbox.claim_due_deliveries(db_session) == []


def test_replay_creates_a_new_generation_and_keeps_history(
    db_session, account, make_endpoint
):
    endpoint, _ = make_endpoint()
    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={"approval_request_id": "1"},
        natural_key="approval.created:1",
    )
    original = _deliveries(db_session, account.id)[0]
    original.status = DELIVERY_DEAD
    db_session.flush()

    replayed = outbox.replay_event(
        db_session, account_id=account.id, event_id=result.event_id
    )
    db_session.flush()

    rows = sorted(_deliveries(db_session, account.id), key=lambda row: row.generation)
    assert len(replayed) == 1
    assert [row.generation for row in rows] == [0, 1]
    assert rows[0].status == DELIVERY_DEAD
    assert rows[1].status == DELIVERY_PENDING
    assert rows[1].payload == rows[0].payload
    assert rows[1].endpoint_id == endpoint.id


def test_replay_of_an_unknown_event_does_nothing(db_session, account):
    assert (
        outbox.replay_event(db_session, account_id=account.id, event_id=uuid.uuid4())
        == []
    )


def test_replay_is_account_scoped(db_session, account, make_endpoint):
    make_endpoint()
    result = outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="scoped",
    )

    assert (
        outbox.replay_event(
            db_session, account_id=uuid.uuid4(), event_id=result.event_id
        )
        == []
    )


def test_retention_purges_only_old_terminal_rows(db_session, account, make_endpoint):
    make_endpoint()
    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="old",
    )
    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="fresh",
    )
    rows = _deliveries(db_session, account.id)
    stale = outbox._utcnow() - timedelta(
        days=settings.webhook_delivery_retention_days + 1
    )
    rows[0].status = DELIVERY_DELIVERED
    rows[0].updated_at = stale
    rows[1].updated_at = stale
    db_session.flush()

    deleted = outbox.purge_terminal_deliveries(db_session)

    assert deleted == 1
    assert len(_deliveries(db_session, account.id)) == 1


@pytest.mark.parametrize(
    "event_types,event_type,expected",
    [
        ([], EVENT_APPROVAL_CREATED, True),
        (None, EVENT_APPROVAL_CREATED, True),
        ([EVENT_APPROVAL_CREATED], EVENT_APPROVAL_CREATED, True),
        ([EVENT_POLICY_DENIED], EVENT_APPROVAL_CREATED, False),
        ("garbage", EVENT_APPROVAL_CREATED, False),
    ],
)
def test_endpoint_filter_matching(event_types, event_type, expected):
    from preloop.services.event_webhooks.events import endpoint_wants

    assert endpoint_wants(event_types, event_type) is expected
