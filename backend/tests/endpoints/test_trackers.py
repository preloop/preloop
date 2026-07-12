import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

from preloop.api.endpoints.trackers import _unique_tracker_name
from preloop.models.models.tracker import Tracker


@pytest.fixture(autouse=True)
def mock_event_bus_connect():
    """Auto-mock the event bus connect method to avoid NATS connection attempts."""
    with patch(
        "preloop.sync.services.event_bus.EventBus.connect", new_callable=AsyncMock
    ) as mock_connect:
        yield mock_connect


def test_list_trackers_empty(client: TestClient, db_session):
    """Test listing trackers when none exist."""
    response = client.get("/api/v1/trackers")
    assert response.status_code == 200
    assert response.json() == []


def test_list_trackers_with_data(client: TestClient, db_session, test_user):
    """Test listing trackers with existing data."""
    tracker = Tracker(
        name="Test Tracker",
        tracker_type="jira",
        url="https://test.jira.com",
        account_id=test_user.account_id,
        api_key="dummy_key",
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.get("/api/v1/trackers")
    assert response.status_code == 200
    response_json = response.json()
    assert len(response_json) == 1
    assert response_json[0]["name"] == "Test Tracker"


def test_get_tracker_not_found(client: TestClient, db_session, test_user):
    """Test getting a tracker that does not exist."""
    import uuid

    response = client.get(f"/api/v1/trackers/{uuid.uuid4()}")
    assert response.status_code == 404


def test_get_tracker_success(client: TestClient, db_session, test_user):
    """Test getting a tracker successfully."""
    tracker = Tracker(
        name="Test Tracker",
        tracker_type="jira",
        url="https://test.jira.com",
        account_id=test_user.account_id,
        api_key="dummy_key",
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.get(f"/api/v1/trackers/{tracker.id}")
    assert response.status_code == 200
    assert response.json()["name"] == "Test Tracker"


def test_delete_tracker(client: TestClient, db_session, test_user):
    """Test deleting a tracker."""
    tracker = Tracker(
        name="Test Tracker",
        tracker_type="jira",
        url="https://test.jira.com",
        account_id=test_user.account_id,
        api_key="dummy_key",
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.delete(f"/api/v1/trackers/{tracker.id}")
    assert response.status_code == 200
    assert response.json()["message"] == "Tracker soft deleted successfully"

    # Verify the tracker is marked as deleted
    db_session.refresh(tracker)
    deleted_tracker = db_session.query(Tracker).filter(Tracker.id == tracker.id).first()
    assert deleted_tracker.is_deleted is True


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_test_connection_and_list_orgs_uses_correct_args(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """Test that test_connection_and_list_orgs calls create_tracker_client with the correct arguments."""
    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_tracker_client.get_organizations.return_value = []
    mock_create_tracker_client.return_value = mock_tracker_client

    test_data = {
        "tracker_type": "gitlab",
        "url": "https://gitlab.com",
        "api_key": "test-key",
        "connection_details": {"project_id": "123"},
    }

    response = client.post("/api/v1/trackers/test-and-list-orgs", json=test_data)
    assert response.status_code == 200

    mock_create_tracker_client.assert_called_once_with(
        tracker_type="gitlab",
        tracker_id="test-connection",
        api_key="test-key",
        connection_details={"url": "https://gitlab.com", "project_id": "123"},
    )


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_list_projects_for_org_uses_correct_args(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """Test that list_projects_for_org calls create_tracker_client with the correct arguments."""
    mock_tracker_client = AsyncMock()
    mock_tracker_client.get_projects.return_value = []
    mock_create_tracker_client.return_value = mock_tracker_client

    test_data = {
        "tracker_type": "gitlab",
        "url": "https://gitlab.com",
        "api_key": "test-key",
        "connection_details": {"project_id": "123"},
        "organization_identifier": "test-org",
    }

    response = client.post("/api/v1/trackers/list-projects-for-org", json=test_data)
    assert response.status_code == 200

    mock_create_tracker_client.assert_called_once_with(
        tracker_type="gitlab",
        tracker_id="list-projects",
        api_key="test-key",
        connection_details={
            "url": "https://gitlab.com/",
            "project_id": "123",
        },
    )


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
async def test_register_tracker_success(
    mock_send_email,
    mock_publish_task,
    mock_create_tracker_client,
    client: TestClient,
    db_session,
    test_user,
):
    """Test successful tracker registration."""
    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_create_tracker_client.return_value = mock_tracker_client

    tracker_data = {
        "name": "New Test Tracker",
        "type": "jira",
        "url": "https://test.jira.com",
        "api_key": "new_dummy_key",
        "config": {"username": "testuser"},
    }

    response = client.post("/api/v1/trackers", json=tracker_data)
    assert response.status_code == 201
    response_json = response.json()
    assert "id" in response_json

    mock_publish_task.assert_called_once()
    mock_send_email.assert_called_once()

    # Security: the credential must be encrypted at rest, not in the plaintext
    # api_key column. It lives in a SecretReference and resolves back correctly.
    created = db_session.query(Tracker).filter(Tracker.id == response_json["id"]).one()
    assert created.api_key is None
    assert created.credentials_secret_id is not None
    assert created.credentials_secret is not None
    assert created.credentials_secret.encrypted_value not in (None, "new_dummy_key")
    assert created.resolved_api_key == "new_dummy_key"


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
async def test_update_tracker_success(
    mock_publish_task, client: TestClient, db_session, test_user
):
    """Test successful tracker update."""
    tracker = Tracker(
        name="Tracker to Update",
        tracker_type="jira",
        url="https://update.jira.com",
        account_id=test_user.account_id,
        api_key="update_key",
    )
    db_session.add(tracker)
    db_session.commit()

    update_data = {"name": "Updated Tracker Name"}

    response = client.put(f"/api/v1/trackers/{tracker.id}", json=update_data)
    assert response.status_code == 200
    response_json = response.json()
    assert response_json["name"] == "Updated Tracker Name"

    # UUID is converted to string for JSON serialization
    mock_publish_task.assert_called_once_with("poll_tracker", str(tracker.id))


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
async def test_sync_tracker_success(
    mock_publish_task, client: TestClient, db_session, test_user
):
    """Test queuing a tracker sync."""
    tracker = Tracker(
        name="Tracker to Sync",
        tracker_type="gitlab",
        url="https://gitlab.example.com",
        account_id=test_user.account_id,
        api_key="sync_key",
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.post(f"/api/v1/trackers/{tracker.id}/sync")
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    mock_publish_task.assert_called_once_with("poll_tracker", str(tracker.id))


def test_unique_tracker_name_appends_suffix(db_session, test_user):
    """OAuth tracker registration should pick a unique display name."""
    existing = Tracker(
        name="GitHub - preloop-agent",
        tracker_type="github",
        url="https://github.com/preloop-agent",
        account_id=test_user.account_id,
        api_key="oauth",
    )
    db_session.add(existing)
    db_session.commit()

    unique_name = _unique_tracker_name(
        db_session,
        base_name="GitHub - preloop-agent",
        account_id=str(test_user.account_id),
    )

    assert unique_name == "GitHub - preloop-agent (2)"
