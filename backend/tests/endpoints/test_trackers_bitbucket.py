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


# ---------------------------------------------------------------------------
# Managed Bitbucket Cloud grants (issue #1065)
# ---------------------------------------------------------------------------

MANAGED_DETAILS = {
    "workspace": "ws",
    "repository": "repo",
    "managed_oauth": True,
    "auth_type": "oauth_token",
    "token_kind": "access_token",
    "managed_state": "connected",
    "actor": {"uuid": "{u}", "display_name": "Jane Doe", "nickname": "jane"},
}


def _managed_tracker(db_session, test_user) -> Tracker:
    tracker = Tracker(
        name="Bitbucket (managed)",
        tracker_type="bitbucket",
        url="https://bitbucket.org",
        account_id=test_user.account_id,
        auth_type="managed_oauth",
        api_key=None,
        connection_details=dict(MANAGED_DETAILS),
    )
    db_session.add(tracker)
    db_session.commit()
    return tracker


@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_register_refuses_managed_auth_type(
    mock_create, client: TestClient, db_session, test_user
) -> None:
    response = client.post(
        "/api/v1/trackers",
        json=_register_body(auth_type="managed_oauth", api_key="pasted"),
    )
    assert response.status_code == 400
    assert "consent flow" in response.json()["detail"]
    mock_create.assert_not_called()


@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_test_connection_refuses_managed_auth_without_tracker(
    mock_create, client: TestClient, db_session, test_user
) -> None:
    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_type": "bitbucket",
            "url": "https://bitbucket.org",
            "api_key": "",
            "auth_type": "managed_oauth",
            "connection_details": {"workspace": "ws"},
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    mock_create.assert_not_called()


@patch("preloop.api.endpoints.trackers.create_tracker_client")
def test_scope_preview_uses_bound_resolver_for_managed_tracker(
    mock_create, client: TestClient, db_session, test_user
) -> None:
    """The existing scope preview reuses the grant: no token leaves the row."""
    tracker = _managed_tracker(db_session, test_user)
    mock_client = AsyncMock()
    mock_client.test_connection.return_value.connected = True
    mock_client.get_organizations.return_value = [{"id": "ws", "name": "Workspace"}]
    mock_client.get_projects.return_value = [
        {"id": "r-1", "name": "repo", "identifier": "r-1"}
    ]
    mock_create.return_value = mock_client

    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "bitbucket",
            "url": "https://bitbucket.org",
            "api_key": "unchanged",
            "connection_details": {"workspace": "ws"},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    kwargs = mock_create.call_args.kwargs
    assert kwargs["api_key"] == ""
    assert kwargs["connection_details"]["auth_type"] == "managed_oauth"
    source = kwargs["credential_source"]
    assert source is not None
    assert (source.account_id, source.tracker_id) == (
        test_user.account_id,
        tracker.id,
    )
    assert source.repository == "repo"

    response = client.post(
        "/api/v1/trackers/list-projects-for-org",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "bitbucket",
            "api_key": "unchanged",
            "organization_identifier": "ws",
            "connection_details": {"workspace": "ws"},
        },
    )
    assert response.status_code == 200, response.text
    assert mock_create.call_args.kwargs["credential_source"].tracker_id == tracker.id


@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
def test_update_cannot_paste_a_token_over_a_managed_grant(
    mock_publish, client: TestClient, db_session, test_user
) -> None:
    tracker = _managed_tracker(db_session, test_user)
    response = client.put(
        f"/api/v1/trackers/{tracker.id}", json={"api_key": "ATATT-fresh"}
    )
    assert response.status_code == 409
    assert "reconnect or disconnect" in response.json()["detail"]
    mock_publish.assert_not_called()
    db_session.refresh(tracker)
    assert tracker.auth_type == "managed_oauth"
    assert tracker.api_key is None


@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
def test_update_keeps_managed_identity_and_drops_manual_expiry(
    mock_publish, client: TestClient, db_session, test_user
) -> None:
    tracker = _managed_tracker(db_session, test_user)
    response = client.put(
        f"/api/v1/trackers/{tracker.id}",
        json={
            "name": "Renamed",
            "api_key": "unchanged",
            "connection_details": {
                "workspace": "ws",
                "repository": "repo",
                "token_expires_at": "2027-01-01",
                "email": "jane@example.com",
            },
            "scope_rules": [
                {
                    "scope_type": "ORGANIZATION",
                    "rule_type": "INCLUDE",
                    "identifier": "ws",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_type"] == "managed_oauth"
    assert body["managed"] is True
    assert body["token_expires_at"] is None
    details = body["connection_details"]
    assert details["managed_oauth"] is True
    assert details["managed_state"] == "connected"
    assert details["actor"]["display_name"] == "Jane Doe"
    assert "token_expires_at" not in details
    assert "email" not in details


@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
def test_update_rejects_custom_origin_for_managed_grant(
    mock_publish, client: TestClient, db_session, test_user
) -> None:
    tracker = _managed_tracker(db_session, test_user)
    response = client.put(
        f"/api/v1/trackers/{tracker.id}",
        json={
            "connection_details": {
                "workspace": "ws",
                "api_url": "https://api.example.com/2.0",
            }
        },
    )
    assert response.status_code == 400
    assert "pinned" in response.json()["detail"]


@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
def test_editing_a_pasted_oauth_token_does_not_convert_it(
    mock_publish, client: TestClient, db_session, test_user
) -> None:
    tracker = Tracker(
        name="Bitbucket (pasted)",
        tracker_type="bitbucket",
        url="https://bitbucket.org",
        account_id=test_user.account_id,
        auth_type="oauth_token",
        api_key="old-token",
        connection_details={"workspace": "ws", "token_expires_at": "2026-12-01"},
    )
    db_session.add(tracker)
    db_session.commit()
    response = client.put(
        f"/api/v1/trackers/{tracker.id}",
        json={
            "api_key": "new-token",
            "connection_details": {"workspace": "ws", "token_expires_at": "2027-06-01"},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["auth_type"] == "oauth_token"
    assert body["managed"] is False
    assert body["token_expires_at"] == "2027-06-01"


def test_tracker_response_for_managed_grant_hides_manual_expiry() -> None:
    now = datetime.now(timezone.utc)
    response = TrackerResponse(
        id=uuid.uuid4(),
        name="Bitbucket",
        tracker_type="bitbucket",
        url="https://bitbucket.org",
        is_active=True,
        auth_type="managed_oauth",
        connection_details={"workspace": "ws", "token_expires_at": "2020-01-01"},
        created=now,
        last_updated=now,
        account_id=uuid.uuid4(),
    ).model_dump()
    assert response["managed"] is True
    assert response["token_expires_at"] is None
    assert response["token_expiry_status"] is None
