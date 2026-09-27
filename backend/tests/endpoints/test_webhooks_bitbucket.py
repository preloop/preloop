"""Tests for Bitbucket Cloud webhook signature checks and event queueing."""

import json
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import get_task_publisher
from preloop.utils.bitbucket import compute_signature

SECRET = "bitbucket-test-secret"
PAYLOAD: Dict[str, Any] = {
    "pullrequest": {"id": 7, "title": "Add parser"},
    "repository": {"full_name": "ws/repo", "uuid": "{r-1}"},
}


@pytest.fixture
def client_and_mocks():
    with (
        patch("preloop.api.app.connect_nats", new_callable=AsyncMock),
        patch("preloop.api.app.close_nats", new_callable=AsyncMock),
    ):
        app = create_app()
        session = MagicMock(spec=Session)
        publisher = AsyncMock()

        def override_db():
            yield session

        app.dependency_overrides[get_db_session] = override_db
        app.dependency_overrides[get_task_publisher] = lambda: publisher
        yield TestClient(app), session, publisher
        app.dependency_overrides.clear()


def _org() -> MagicMock:
    org = MagicMock()
    org.id = 321
    org.identifier = "ws"
    org.webhook_secret = SECRET
    org.tracker = MagicMock()
    org.tracker.id = "tracker-bb"
    org.tracker.is_active = True
    org.tracker.subscribed_events = None
    return org


def _body() -> bytes:
    return json.dumps(PAYLOAD, separators=(",", ":")).encode()


def _published(publisher: AsyncMock, name: str) -> list:
    return [c for c in publisher.publish_task.await_args_list if c.args[0] == name]


@patch("preloop.api.endpoints.webhooks.crud_organization")
def test_missing_signature_is_rejected(crud_org, client_and_mocks) -> None:
    client, _, publisher = client_and_mocks
    crud_org.get_with_tracker.return_value = _org()
    response = client.post(
        "/api/v1/private/webhooks/bitbucket/321",
        content=_body(),
        headers={"X-Event-Key": "pullrequest:created"},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing Bitbucket signature"
    assert not _published(publisher, "process_webhook_event")


@patch("preloop.api.endpoints.webhooks.crud_organization")
def test_wrong_signature_is_rejected(crud_org, client_and_mocks) -> None:
    client, _, publisher = client_and_mocks
    crud_org.get_with_tracker.return_value = _org()
    response = client.post(
        "/api/v1/private/webhooks/bitbucket/321",
        content=_body(),
        headers={
            "X-Event-Key": "pullrequest:created",
            "X-Hub-Signature": compute_signature("wrong-secret", _body()),
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Invalid Bitbucket signature"
    assert not _published(publisher, "process_webhook_event")


@patch("preloop.api.endpoints.webhooks.crud_organization")
def test_valid_signature_queues_event(crud_org, client_and_mocks) -> None:
    client, _, publisher = client_and_mocks
    crud_org.get_with_tracker.return_value = _org()
    response = client.post(
        "/api/v1/private/webhooks/bitbucket/321",
        content=_body(),
        headers={
            "X-Event-Key": "pullrequest:created",
            "X-Request-UUID": "delivery-1",
            "X-Hub-Signature": compute_signature(SECRET, _body()),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "success"
    calls = _published(publisher, "process_webhook_event")
    assert len(calls) == 1
    kwargs = calls[0].kwargs
    assert kwargs["tracker_type"] == "bitbucket"
    assert kwargs["event_type"] == "pullrequest:created"
    assert kwargs["delivery_id"] == "delivery-1"
    assert kwargs["payload"] == PAYLOAD


@patch("preloop.api.endpoints.webhooks.crud_organization")
def test_unsubscribed_event_is_skipped(crud_org, client_and_mocks) -> None:
    client, _, publisher = client_and_mocks
    crud_org.get_with_tracker.return_value = _org()
    response = client.post(
        "/api/v1/private/webhooks/bitbucket/321",
        content=_body(),
        headers={
            "X-Event-Key": "repo:fork",
            "X-Hub-Signature": compute_signature(SECRET, _body()),
        },
    )
    assert response.status_code == 200
    assert response.json()["message"] == "Event not subscribed"
    assert not _published(publisher, "process_webhook_event")
