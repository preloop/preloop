"""Delivery worker: signing on the wire, outcome classification, write-back."""

import json
import time
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from preloop.models.models.webhook_endpoint import (
    DELIVERY_DEAD,
    DELIVERY_DELIVERED,
    DELIVERY_PENDING,
    SOURCE_APPROVAL_WORKFLOW,
    WebhookDelivery,
)
from preloop.services.event_webhooks import outbox, worker
from preloop.services.event_webhooks.events import EVENT_APPROVAL_CREATED
from preloop.services.event_webhooks.signing import (
    ATTEMPT_HEADER,
    DELIVERY_ID_HEADER,
    EVENT_ID_HEADER,
    EVENT_TYPE_HEADER,
    SIGNATURE_HEADER,
    USER_AGENT,
    verify_signature,
)


def _enqueue(db_session, account, event_type=EVENT_APPROVAL_CREATED, data=None):
    outbox.enqueue_event(
        db_session,
        account_id=account.id,
        event_type=event_type,
        data=data or {"approval_request_id": "1"},
    )
    db_session.flush()
    return (
        db_session.query(WebhookDelivery)
        .filter(WebhookDelivery.account_id == account.id)
        .one()
    )


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- preparing and signing -------------------------------------------------


def test_claim_batch_prepares_a_signed_body(db_session, account, make_endpoint):
    endpoint, secret = make_endpoint()
    delivery = _enqueue(db_session, account)

    prepared = worker.claim_batch(db_session)

    assert len(prepared) == 1
    item = prepared[0]
    assert item.url == endpoint.url
    assert item.attempt == 1
    assert json.loads(item.body)["id"] == str(delivery.event_id)
    headers = worker.build_headers(item)
    assert verify_signature(secret, headers[SIGNATURE_HEADER], item.body) is True


def test_headers_carry_the_documented_names(db_session, account, make_endpoint):
    make_endpoint()
    delivery = _enqueue(db_session, account)
    item = worker.claim_batch(db_session)[0]

    headers = worker.build_headers(item, timestamp=1_800_000_000)

    assert headers[EVENT_ID_HEADER] == str(delivery.event_id)
    assert headers[EVENT_TYPE_HEADER] == EVENT_APPROVAL_CREATED
    assert headers[DELIVERY_ID_HEADER] == str(delivery.id)
    assert headers[ATTEMPT_HEADER] == "1"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == USER_AGENT
    assert headers[SIGNATURE_HEADER].startswith("t=1800000000,v1=")


def test_each_attempt_signs_with_a_fresh_timestamp(db_session, account, make_endpoint):
    make_endpoint()
    _enqueue(db_session, account)
    item = worker.claim_batch(db_session)[0]

    first = worker.build_headers(item, timestamp=1_800_000_000)[SIGNATURE_HEADER]
    second = worker.build_headers(item, timestamp=1_800_000_600)[SIGNATURE_HEADER]

    assert first != second


def test_a_stale_signature_would_not_verify(db_session, account, make_endpoint):
    _, secret = make_endpoint()
    _enqueue(db_session, account)
    item = worker.claim_batch(db_session)[0]

    stale = worker.build_headers(item, timestamp=int(time.time()) - 3600)
    assert verify_signature(secret, stale[SIGNATURE_HEADER], item.body) is False
    fresh = worker.build_headers(item)
    assert verify_signature(secret, fresh[SIGNATURE_HEADER], item.body) is True


def test_an_undecryptable_secret_fails_the_attempt_rather_than_posting_unsigned(
    db_session, account, make_endpoint
):
    endpoint, _ = make_endpoint()
    endpoint.secret_encrypted = "not-a-fernet-token"
    db_session.flush()
    _enqueue(db_session, account)

    prepared = worker.claim_batch(db_session)

    assert prepared == []
    delivery = db_session.query(WebhookDelivery).one()
    assert delivery.attempt_count == 1
    assert "decrypt" in delivery.last_error


def test_claim_batch_releases_rows_for_an_inactive_endpoint(
    db_session, account, make_endpoint
):
    endpoint, _ = make_endpoint()
    _enqueue(db_session, account)
    endpoint.active = False
    db_session.flush()

    assert worker.claim_batch(db_session) == []
    delivery = db_session.query(WebhookDelivery).one()
    assert delivery.claimed_at is None
    assert delivery.status == DELIVERY_PENDING


def test_claim_batch_dead_letters_a_row_whose_endpoint_vanished(
    db_session, account, make_endpoint, monkeypatch
):
    """Defence in depth: the FK cascades, but a claimed row must never hang.

    A delivery whose endpoint has gone is unsendable forever, so it is
    dead-lettered rather than left claimed and invisible.
    """
    make_endpoint()
    _enqueue(db_session, account)
    monkeypatch.setattr(db_session, "get", lambda model, ident: None)

    assert worker.claim_batch(db_session) == []
    monkeypatch.undo()
    assert db_session.query(WebhookDelivery).one().status == DELIVERY_DEAD


# --- posting ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_2xx_is_a_success():
    prepared = worker.PreparedDelivery(
        delivery_id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        url="https://example.com/hook",
        secret="whsec_x",
        event_id=str(uuid.uuid4()),
        event_type=EVENT_APPROVAL_CREATED,
        attempt=1,
        body=b"{}",
    )

    async with _client(lambda request: httpx.Response(204)) as client:
        outcome = await worker.post_delivery(client, prepared)

    assert outcome.success is True
    assert outcome.response_status == 204
    assert outcome.error is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [301, 400, 401, 404, 429, 500, 503])
async def test_a_non_2xx_is_a_failure_without_the_response_body(status_code):
    prepared = worker.PreparedDelivery(
        delivery_id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        url="https://example.com/hook",
        secret="whsec_x",
        event_id=str(uuid.uuid4()),
        event_type=EVENT_APPROVAL_CREATED,
        attempt=1,
        body=b"{}",
    )

    def handler(request):
        return httpx.Response(status_code, text="leaky internal detail")

    async with _client(handler) as client:
        outcome = await worker.post_delivery(client, prepared)

    assert outcome.success is False
    assert outcome.response_status == status_code
    assert outcome.error == f"receiver returned HTTP {status_code}"
    assert "leaky" not in outcome.error


@pytest.mark.asyncio
async def test_a_transport_error_is_a_failure():
    prepared = worker.PreparedDelivery(
        delivery_id=uuid.uuid4(),
        endpoint_id=uuid.uuid4(),
        url="https://example.com/hook",
        secret="whsec_x",
        event_id=str(uuid.uuid4()),
        event_type=EVENT_APPROVAL_CREATED,
        attempt=1,
        body=b"{}",
    )

    def handler(request):
        raise httpx.ConnectError("no route to host")

    async with _client(handler) as client:
        outcome = await worker.post_delivery(client, prepared)

    assert outcome.success is False
    assert outcome.response_status is None
    assert "ConnectError" in outcome.error


@pytest.mark.asyncio
async def test_the_receiver_can_verify_what_the_worker_actually_sends(
    db_session, account, make_endpoint
):
    _, secret = make_endpoint()
    _enqueue(db_session, account)
    prepared = worker.claim_batch(db_session)[0]
    seen = {}

    def handler(request):
        seen["body"] = request.content
        seen["signature"] = request.headers[SIGNATURE_HEADER]
        return httpx.Response(200)

    async with _client(handler) as client:
        outcome = await worker.post_delivery(client, prepared)

    assert outcome.success is True
    assert verify_signature(secret, seen["signature"], seen["body"]) is True
    # A byte that differs from what was signed must fail.
    assert verify_signature(secret, seen["signature"], seen["body"] + b" ") is False


# --- applying outcomes -----------------------------------------------------


def test_apply_outcomes_marks_delivered(db_session, account, make_endpoint):
    endpoint, _ = make_endpoint()
    delivery = _enqueue(db_session, account)

    worker.apply_outcomes(
        db_session,
        [worker.AttemptOutcome(delivery.id, success=True, response_status=200)],
    )

    db_session.refresh(delivery)
    db_session.refresh(endpoint)
    assert delivery.status == DELIVERY_DELIVERED
    assert endpoint.last_delivery_status == DELIVERY_DELIVERED
    assert endpoint.last_response_code == 200


def test_apply_outcomes_reschedules_a_failure(db_session, account, make_endpoint):
    make_endpoint()
    delivery = _enqueue(db_session, account)

    worker.apply_outcomes(
        db_session,
        [worker.AttemptOutcome(delivery.id, success=False, response_status=500)],
    )

    db_session.refresh(delivery)
    assert delivery.status == DELIVERY_PENDING
    assert delivery.attempt_count == 1
    assert delivery.claimed_at is None


def test_apply_outcomes_ignores_a_delivery_that_disappeared(db_session, account):
    worker.apply_outcomes(db_session, [worker.AttemptOutcome(uuid.uuid4(), True, 200)])


# --- approval shim write-back ---------------------------------------------


class _StubApproval:
    def __init__(self):
        self.id = uuid.uuid4()
        self.webhook_posted_at = None
        self.webhook_error = None


def test_shim_write_back_stamps_posted_on_delivery(monkeypatch, db_session):
    request = _StubApproval()
    delivery = SimpleNamespace(
        subject_id=request.id, delivered_at=datetime(2026, 9, 8, 12, 0, 0)
    )
    monkeypatch.setattr(db_session, "get", lambda model, ident: request)

    worker._write_back_approval_state(
        db_session,
        delivery,
        DELIVERY_DELIVERED,
        worker.AttemptOutcome(uuid.uuid4(), True, 200),
    )

    assert request.webhook_posted_at == datetime(2026, 9, 8, 12, 0, 0)
    assert request.webhook_error is None


def test_shim_write_back_records_the_error_on_dead_letter(monkeypatch, db_session):
    request = _StubApproval()
    delivery = SimpleNamespace(subject_id=request.id, delivered_at=None)
    monkeypatch.setattr(db_session, "get", lambda model, ident: request)

    worker._write_back_approval_state(
        db_session,
        delivery,
        DELIVERY_DEAD,
        worker.AttemptOutcome(uuid.uuid4(), False, 500, "receiver returned HTTP 500"),
    )

    assert request.webhook_posted_at is None
    assert request.webhook_error == "receiver returned HTTP 500"


def test_shim_write_back_is_silent_while_retries_remain(monkeypatch, db_session):
    request = _StubApproval()
    delivery = SimpleNamespace(subject_id=request.id, delivered_at=None)
    monkeypatch.setattr(db_session, "get", lambda model, ident: request)

    worker._write_back_approval_state(
        db_session,
        delivery,
        DELIVERY_PENDING,
        worker.AttemptOutcome(uuid.uuid4(), False, 500, "boom"),
    )

    assert request.webhook_error is None


def test_v1_endpoints_do_not_write_back_to_approvals(
    db_session, account, make_endpoint
):
    endpoint, _ = make_endpoint()
    assert endpoint.source != SOURCE_APPROVAL_WORKFLOW


# --- one full pass ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_once_delivers_and_reports_the_count(
    db_session, account, make_endpoint, monkeypatch
):
    make_endpoint()
    delivery_id = _enqueue(db_session, account).id

    def handler(request):
        return httpx.Response(200)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        worker.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    # run_once owns and closes its session; the test session must survive.
    monkeypatch.setattr(db_session, "close", lambda: None)

    attempted = await worker.run_once(db_factory=lambda: db_session)

    assert attempted == 1
    assert db_session.get(WebhookDelivery, delivery_id).status == DELIVERY_DELIVERED


@pytest.mark.asyncio
async def test_run_once_with_an_empty_queue_does_no_http(
    db_session, account, monkeypatch
):
    monkeypatch.setattr(db_session, "close", lambda: None)
    assert await worker.run_once(db_factory=lambda: db_session) == 0
