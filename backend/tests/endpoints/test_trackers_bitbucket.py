"""Tests for Bitbucket Cloud tracker registration, validation and responses."""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from preloop.models.models.tracker import Tracker
from preloop.schemas.tracker import TrackerResponse

WORKSPACE_DETAILS = {"workspace": "ws", "token_kind": "api_token"}


@pytest.fixture(autouse=True)
def mock_event_bus_connect():
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ) as mock_connect:
        yield mock_connect


def _register_body(**overrides):
    body = {
        "name": "Bitbucket",
        "type": "bitbucket",
        "url": "https://bitbucket.org",
        "api_key": "bb-token",
        "auth_type": "api_token",
        "config": dict(WORKSPACE_DETAILS),
    }
    body.update(overrides)
    return body


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"api_key": "ATBB-legacy"}, "app passwords are not supported"),
        ({"auth_type": "app_password"}, "app passwords are not supported"),
        (
            {"config": {"workspace": "ws", "token_kind": "access_token"}},
            "set 'repository'",
        ),
        ({"config": {}}, "requires 'workspace'"),
        (
            {"config": {"workspace": "ws", "username": "dev@example.com"}},
            "must not be an email",
        ),
    ],
)
@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_register_rejects_invalid_config(
    mock_create, overrides, fragment, client: TestClient, db_session, test_user
) -> None:
    response = client.post("/api/v1/trackers", json=_register_body(**overrides))
    assert response.status_code == 400
    assert fragment in response.json()["detail"]
    mock_create.assert_not_called()


@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
def test_register_passes_auth_type_to_client(
    mock_email,
    mock_publish,
    mock_create,
    client: TestClient,
    db_session,
    test_user,
) -> None:
    mock_client = AsyncMock()
    mock_client.test_connection.return_value.connected = True
    mock_create.return_value = mock_client

    response = client.post(
        "/api/v1/trackers",
        json=_register_body(
            auth_type="oauth_token",
            config={"workspace": "ws", "token_expires_at": "2099-01-01"},
        ),
    )
    assert response.status_code == 201, response.text
    kwargs = mock_create.call_args.kwargs
    assert kwargs["tracker_type"] == "bitbucket"
    assert kwargs["connection_details"]["auth_type"] == "oauth_token"
    assert kwargs["connection_details"]["workspace"] == "ws"

    created = (
        db_session.query(Tracker).filter(Tracker.id == response.json()["id"]).one()
    )
    assert created.tracker_type == "bitbucket"
    assert created.auth_type == "oauth_token"


@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_test_connection_reports_invalid_config(
    mock_create, client: TestClient, db_session, test_user
) -> None:
    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_type": "bitbucket",
            "url": "https://bitbucket.org",
            "api_key": "ATBBsomething",
            "connection_details": WORKSPACE_DETAILS,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is False
    assert "app passwords are not supported" in body["message"]
    mock_create.assert_not_called()


@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_test_connection_returns_grouped_repositories(
    mock_create, client: TestClient, db_session, test_user
) -> None:
    mock_client = AsyncMock()
    mock_client.test_connection.return_value.connected = True
    mock_client.get_organizations.return_value = [{"id": "ws", "name": "Workspace"}]
    mock_client.get_projects.return_value = [
        {"id": "r-1", "name": "repo", "group": "Platform"}
    ]
    mock_create.return_value = mock_client

    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_type": "bitbucket",
            "url": "https://bitbucket.org",
            "api_key": "bb-token",
            "auth_type": "oauth_token",
            "connection_details": WORKSPACE_DETAILS,
        },
    )
    assert response.status_code == 200, response.text
    assert mock_create.call_args.kwargs["connection_details"]["auth_type"] == (
        "oauth_token"
    )
    orgs = response.json()["orgs"]
    assert orgs[0]["children"][0]["group"] == "Platform"


@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
def test_update_rejects_app_password(
    mock_publish, client: TestClient, db_session, test_user
) -> None:
    tracker = Tracker(
        name="Bitbucket",
        tracker_type="bitbucket",
        url="https://bitbucket.org",
        account_id=test_user.account_id,
        api_key="bb-token",
        connection_details=dict(WORKSPACE_DETAILS),
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.put(f"/api/v1/trackers/{tracker.id}", json={"api_key": "ATBBnew"})
    assert response.status_code == 400
    assert "app passwords are not supported" in response.json()["detail"]
    mock_publish.assert_not_called()


def _tracker_response(details):
    now = datetime.now(timezone.utc)
    return TrackerResponse(
        id=uuid.uuid4(),
        name="Bitbucket",
        tracker_type="bitbucket",
        url="https://bitbucket.org",
        is_active=True,
        connection_details=details,
        created=now,
        last_updated=now,
        account_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(
    ("offset_days", "expected"),
    [(-1, "expired"), (5, "expiring"), (60, "ok")],
)
def test_tracker_response_token_expiry(offset_days: int, expected: str) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=offset_days)).isoformat()
    response = _tracker_response({"workspace": "ws", "token_expires_at": expires})
    dumped = response.model_dump()
    assert dumped["token_expires_at"] == expires
    assert dumped["token_expiry_status"] == expected


def test_tracker_response_without_expiry() -> None:
    dumped = _tracker_response({"workspace": "ws"}).model_dump()
    assert dumped["token_expires_at"] is None
    assert dumped["token_expiry_status"] is None
