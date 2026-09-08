"""API surface for outbound event webhooks."""

import uuid

import pytest

from preloop.models.models.tool_configuration import ApprovalWorkflow
from preloop.models.models.webhook_endpoint import (
    DELIVERY_DEAD,
    SOURCE_ACCOUNT,
    SOURCE_APPROVAL_WORKFLOW,
    WebhookDelivery,
    WebhookEndpoint,
)
from preloop.services.event_webhooks import outbox
from preloop.services.event_webhooks.events import (
    EVENT_APPROVAL_CREATED,
    EVENT_TEST,
    EVENT_TYPES_V1,
)
from preloop.services.event_webhooks.signing import SECRET_PREFIX, generate_secret
from preloop.utils.encryption import decrypt_value, encrypt_value

BASE = "/api/v1/event-webhooks"


@pytest.fixture
def endpoint_row(db_session, test_user):
    """An account endpoint owned by the authenticated test user's account."""
    secret = generate_secret()
    row = WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://siem.example.com/hook",
        secret_encrypted=encrypt_value(secret),
        secret_hint=secret[-6:],
        event_types=[],
        source=SOURCE_ACCOUNT,
    )
    db_session.add(row)
    db_session.flush()
    return row


# --- catalogue -------------------------------------------------------------


def test_catalogue_lists_the_v1_events_and_the_delivery_contract(client):
    response = client.get(f"{BASE}/catalogue")

    assert response.status_code == 200
    body = response.json()
    assert [item["name"] for item in body["event_types"]] == list(EVENT_TYPES_V1)
    assert all(item["description"] for item in body["event_types"])
    assert body["version"] == "1"
    assert body["signature_header"] == "X-Preloop-Signature"
    assert body["tolerance_seconds"] == 300
    assert body["max_attempts"] == 6
    assert body["retry_delays_seconds"] == [10, 60, 300, 900, 2400]


# --- create ----------------------------------------------------------------


def test_create_returns_the_secret_exactly_once(client, db_session, test_user):
    response = client.post(
        f"{BASE}/endpoints",
        json={
            "url": "https://siem.example.com/hook",
            "description": "SIEM",
            "event_types": ["approval.decided"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    secret = body["secret"]
    assert secret.startswith(SECRET_PREFIX)
    assert body["secret_hint"] and secret.endswith(body["secret_hint"])
    assert body["event_types"] == ["approval.decided"]
    assert body["source"] == SOURCE_ACCOUNT

    # The secret is not readable again anywhere.
    listed = client.get(f"{BASE}/endpoints").json()
    assert "secret" not in listed[0]
    row = db_session.get(WebhookEndpoint, uuid.UUID(body["id"]))
    assert row.secret_encrypted != secret
    assert decrypt_value(row.secret_encrypted) == secret


def test_create_rejects_a_non_http_url(client):
    response = client.post(f"{BASE}/endpoints", json={"url": "ftp://siem/hook"})
    assert response.status_code == 422


def test_create_rejects_an_unknown_event_type(client):
    response = client.post(
        f"{BASE}/endpoints",
        json={"url": "https://siem.example.com/hook", "event_types": ["approval.made"]},
    )
    assert response.status_code == 422
    assert "approval.made" in response.text


def test_create_is_capped_per_account(client, db_session, test_user, monkeypatch):
    from preloop.api.endpoints import event_webhooks

    monkeypatch.setattr(event_webhooks, "MAX_ENDPOINTS_PER_ACCOUNT", 1)
    first = client.post(f"{BASE}/endpoints", json={"url": "https://a.example.com/h"})
    second = client.post(f"{BASE}/endpoints", json={"url": "https://b.example.com/h"})

    assert first.status_code == 201
    assert second.status_code == 400
    assert "at most 1" in second.json()["detail"]


# --- list ------------------------------------------------------------------


def test_list_shows_endpoints_with_their_last_delivery_state(
    client, db_session, endpoint_row
):
    endpoint_row.last_delivery_status = "dead"
    endpoint_row.last_response_code = 500
    endpoint_row.consecutive_failures = 10
    endpoint_row.circuit_opened_at = outbox._utcnow()
    db_session.flush()

    body = client.get(f"{BASE}/endpoints").json()

    assert len(body) == 1
    assert body[0]["last_delivery_status"] == "dead"
    assert body[0]["last_response_code"] == 500
    assert body[0]["circuit_open"] is True


def test_list_never_shows_another_accounts_endpoints(client, db_session, test_user):
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    db_session.add(
        WebhookEndpoint(
            account_id=other.id,
            url="https://elsewhere.example.com/hook",
            secret_encrypted=encrypt_value(generate_secret()),
            event_types=[],
            source=SOURCE_ACCOUNT,
        )
    )
    db_session.flush()

    assert client.get(f"{BASE}/endpoints").json() == []


# --- update and delete -----------------------------------------------------


def test_update_changes_the_filter(client, endpoint_row):
    response = client.patch(
        f"{BASE}/endpoints/{endpoint_row.id}",
        json={"event_types": ["policy.denied", "session.ended"]},
    )

    assert response.status_code == 200
    assert response.json()["event_types"] == ["policy.denied", "session.ended"]


def test_updating_the_url_closes_the_circuit_breaker(client, db_session, endpoint_row):
    endpoint_row.consecutive_failures = 10
    endpoint_row.circuit_opened_at = outbox._utcnow()
    db_session.flush()

    body = client.patch(
        f"{BASE}/endpoints/{endpoint_row.id}",
        json={"url": "https://fixed.example.com/hook"},
    ).json()

    assert body["circuit_open"] is False
    assert body["consecutive_failures"] == 0


def test_update_of_a_missing_endpoint_is_404(client):
    response = client.patch(f"{BASE}/endpoints/{uuid.uuid4()}", json={"active": False})
    assert response.status_code == 404


def test_shim_endpoints_are_read_only(client, db_session, test_user):
    workflow = ApprovalWorkflow(
        account_id=test_user.account_id,
        name="Deploy approvals",
        approval_type="webhook",
        approval_config={"webhook_url": "https://chat.example.com/hook"},
    )
    db_session.add(workflow)
    db_session.flush()
    row = WebhookEndpoint(
        account_id=test_user.account_id,
        url="https://chat.example.com/hook",
        secret_encrypted=encrypt_value(generate_secret()),
        event_types=[],
        source=SOURCE_APPROVAL_WORKFLOW,
        approval_workflow_id=workflow.id,
    )
    db_session.add(row)
    db_session.flush()

    patched = client.patch(f"{BASE}/endpoints/{row.id}", json={"active": False})
    deleted = client.delete(f"{BASE}/endpoints/{row.id}")
    tested = client.post(f"{BASE}/endpoints/{row.id}/test")

    assert patched.status_code == 400
    assert deleted.status_code == 400
    assert tested.status_code == 400
    assert "approval workflow" in patched.json()["detail"]
    # Still listed, so the operator can see it failing.
    assert len(client.get(f"{BASE}/endpoints").json()) == 1


def test_delete_removes_the_endpoint_and_its_deliveries(
    client, db_session, endpoint_row
):
    outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
    )
    db_session.flush()

    response = client.delete(f"{BASE}/endpoints/{endpoint_row.id}")

    assert response.status_code == 204
    assert client.get(f"{BASE}/endpoints").json() == []
    assert db_session.query(WebhookDelivery).count() == 0


# --- test send -------------------------------------------------------------


def test_test_send_queues_an_event_even_when_filtered_out(
    client, db_session, endpoint_row
):
    endpoint_row.event_types = ["policy.denied"]
    db_session.flush()

    response = client.post(f"{BASE}/endpoints/{endpoint_row.id}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["queued"] == 1
    row = db_session.get(WebhookDelivery, uuid.UUID(body["delivery_ids"][0]))
    assert row.event_type == EVENT_TEST
    assert row.payload["data"]["message"] == "Test event from Preloop."


def test_test_send_on_a_missing_endpoint_is_404(client):
    assert client.post(f"{BASE}/endpoints/{uuid.uuid4()}/test").status_code == 404


# --- deliveries ------------------------------------------------------------


def test_deliveries_list_is_filterable_by_endpoint_and_status(
    client, db_session, endpoint_row
):
    outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="one",
    )
    outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="two",
    )
    rows = db_session.query(WebhookDelivery).all()
    rows[0].status = DELIVERY_DEAD
    db_session.flush()

    assert len(client.get(f"{BASE}/deliveries").json()) == 2
    assert (
        len(client.get(f"{BASE}/deliveries?endpoint_id={endpoint_row.id}").json()) == 2
    )
    assert len(client.get(f"{BASE}/deliveries?delivery_status=dead").json()) == 1
    assert len(client.get(f"{BASE}/deliveries?endpoint_id={uuid.uuid4()}").json()) == 0


def test_dead_letter_list_shows_only_exhausted_deliveries(
    client, db_session, endpoint_row
):
    outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="dead-one",
    )
    row = db_session.query(WebhookDelivery).one()
    row.status = DELIVERY_DEAD
    row.attempt_count = 6
    row.last_error = "receiver returned HTTP 500"
    db_session.flush()

    body = client.get(f"{BASE}/deliveries/dead-letter").json()

    assert len(body) == 1
    assert body[0]["attempt_count"] == 6
    assert body[0]["last_error"] == "receiver returned HTTP 500"


def test_delivery_rows_do_not_leak_the_payload_or_the_secret(
    client, db_session, endpoint_row
):
    outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={"approval_request_id": "1"},
    )
    db_session.flush()

    body = client.get(f"{BASE}/deliveries").json()[0]

    assert "payload" not in body
    assert "secret" not in str(body)


# --- replay ----------------------------------------------------------------


def test_replay_requeues_a_dead_event_as_a_new_generation(
    client, db_session, endpoint_row
):
    result = outbox.enqueue_event(
        db_session,
        account_id=endpoint_row.account_id,
        event_type=EVENT_APPROVAL_CREATED,
        data={},
        natural_key="replay-me",
    )
    row = db_session.query(WebhookDelivery).one()
    row.status = DELIVERY_DEAD
    db_session.flush()

    response = client.post(f"{BASE}/deliveries/{result.event_id}/replay")

    assert response.status_code == 200
    assert response.json()["queued"] == 1
    generations = sorted(r.generation for r in db_session.query(WebhookDelivery).all())
    assert generations == [0, 1]


def test_replay_of_an_unknown_event_is_404(client):
    assert client.post(f"{BASE}/deliveries/{uuid.uuid4()}/replay").status_code == 404


def test_create_refuses_an_internal_target_when_the_guard_is_on(client, monkeypatch):
    """Multi-tenant hosting turns this on so an admin cannot aim at 169.254."""
    from preloop.services.event_webhooks import targets

    monkeypatch.setattr(targets.settings, "webhook_block_private_targets", True)

    response = client.post(
        f"{BASE}/endpoints",
        json={"url": "https://169.254.169.254/latest/meta-data", "event_types": []},
    )

    assert response.status_code == 400
    assert "link-local" in response.json()["detail"]


def test_update_refuses_an_internal_target_when_the_guard_is_on(
    client, endpoint_row, monkeypatch
):
    """The URL is checked on edit too, not only at creation."""
    from preloop.services.event_webhooks import targets

    monkeypatch.setattr(targets.settings, "webhook_block_private_targets", True)

    response = client.patch(
        f"{BASE}/endpoints/{endpoint_row.id}",
        json={"url": "http://127.0.0.1:9999/hook"},
    )

    assert response.status_code == 400
    assert "loopback" in response.json()["detail"]
