import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from sqlalchemy import event, inspect as sa_inspect
from sqlalchemy.engine import Engine

from preloop.api.endpoints.trackers import _unique_tracker_name
from preloop.models.crud import crud_tracker
from preloop.models.models.github_app_installation import OAuthAppInstallation
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


def _make_github_app_tracker(
    db_session,
    test_user,
    *,
    name: str = "GitHub App Tracker",
    external_id: int = 4242,
    target_id: int = 9001,
    target_name: str = "example-org",
) -> Tracker:
    """Persist a GitHub App tracker bound to a synthetic installation."""
    installation = OAuthAppInstallation(
        provider="github",
        external_id=external_id,
        target_type="Organization",
        target_id=target_id,
        target_name=target_name,
        account_id=test_user.account_id,
    )
    db_session.add(installation)
    db_session.flush()
    tracker = Tracker(
        name=name,
        tracker_type="github",
        url="https://github.com",
        account_id=test_user.account_id,
        api_key=None,
        auth_type="github_app",
        oauth_installation_id=installation.id,
    )
    db_session.add(tracker)
    db_session.commit()
    return tracker


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_test_connection_and_list_orgs_uses_installation_for_app_tracker(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """Editing a GitHub App tracker must list orgs through its installation.

    The modal sends ``api_key: "unchanged"`` with the tracker id. For an App
    tracker there is no PAT to substitute, so the client has to be built with
    the installation binding (as the scanner does) instead of an empty token.
    """
    tracker = _make_github_app_tracker(db_session, test_user)
    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_tracker_client.get_organizations.return_value = [
        {"id": "9001", "name": "example-org", "type": "Organization"}
    ]
    mock_tracker_client.get_projects.return_value = []
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "unchanged",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert [org["id"] for org in body["orgs"]] == ["9001"]

    mock_create_tracker_client.assert_called_once_with(
        tracker_type="github",
        tracker_id="test-connection",
        api_key="",
        connection_details={
            "url": "https://github.com",
            "auth_type": "github_app",
            "github_installation_id": 4242,
        },
    )


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_test_connection_and_list_orgs_rejects_app_tracker_without_installation(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """An App tracker whose installation row was removed gets a clear 400.

    The FK is ``ondelete="SET NULL"``, so a deleted installation leaves the
    tracker with ``auth_type="github_app"`` and no ``oauth_installation``.
    """
    tracker = Tracker(
        name="Orphaned GitHub App Tracker",
        tracker_type="github",
        url="https://github.com",
        account_id=test_user.account_id,
        api_key=None,
        auth_type="github_app",
        oauth_installation_id=None,
    )
    db_session.add(tracker)
    db_session.commit()

    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "unchanged",
        },
    )
    assert response.status_code == 400
    assert "no longer exists" in response.json()["detail"]
    mock_create_tracker_client.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_test_connection_and_list_orgs_keeps_pat_for_token_tracker(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """PAT trackers keep substituting the stored token on edit."""
    tracker = Tracker(
        name="GitHub PAT Tracker",
        tracker_type="github",
        url="https://github.com",
        account_id=test_user.account_id,
        api_key="stored-token",
    )
    db_session.add(tracker)
    db_session.commit()
    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_tracker_client.get_organizations.return_value = []
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers/test-and-list-orgs",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "unchanged",
        },
    )
    assert response.status_code == 200

    mock_create_tracker_client.assert_called_once_with(
        tracker_type="github",
        tracker_id="test-connection",
        api_key="stored-token",
        connection_details={"url": "https://github.com"},
    )


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
async def test_list_projects_for_org_uses_installation_for_app_tracker(
    mock_create_tracker_client, client: TestClient, db_session, test_user
):
    """Project listing for an App tracker must also use installation auth."""
    tracker = _make_github_app_tracker(db_session, test_user)
    mock_tracker_client = AsyncMock()
    mock_tracker_client.get_projects.return_value = []
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers/list-projects-for-org",
        json={
            "tracker_id": str(tracker.id),
            "tracker_type": "github",
            "url": "https://github.com",
            "api_key": "unchanged",
            "organization_identifier": "9001",
        },
    )
    assert response.status_code == 200

    mock_create_tracker_client.assert_called_once_with(
        tracker_type="github",
        tracker_id="list-projects",
        api_key="",
        connection_details={
            "url": "https://github.com/",
            "auth_type": "github_app",
            "github_installation_id": 4242,
        },
    )
    mock_tracker_client.get_projects.assert_awaited_once_with("9001")


def test_tracker_response_exposes_auth_binding(
    client: TestClient, db_session, test_user
):
    """List and detail responses expose the auth type and installation binding."""
    app_tracker = _make_github_app_tracker(db_session, test_user)
    pat_tracker = Tracker(
        name="GitHub PAT Tracker",
        tracker_type="github",
        url="https://github.com",
        account_id=test_user.account_id,
        api_key="stored-token",
    )
    db_session.add(pat_tracker)
    db_session.commit()

    detail = client.get(f"/api/v1/trackers/{app_tracker.id}")
    assert detail.status_code == 200
    detail_json = detail.json()
    assert detail_json["auth_type"] == "github_app"
    assert detail_json["oauth_installation_id"] == str(
        app_tracker.oauth_installation_id
    )
    assert detail_json["github_installation_target_login"] == "example-org"
    assert "api_key" not in detail_json

    listing = client.get("/api/v1/trackers")
    assert listing.status_code == 200
    by_id = {item["id"]: item for item in listing.json()}
    assert by_id[str(app_tracker.id)]["auth_type"] == "github_app"
    assert by_id[str(pat_tracker.id)]["auth_type"] == "api_token"
    assert by_id[str(pat_tracker.id)]["oauth_installation_id"] is None
    assert by_id[str(pat_tracker.id)]["github_installation_target_login"] is None


def test_get_for_account_eager_loads_oauth_installation(db_session, test_user):
    """List CRUD must load the installation so TrackerResponse does not N+1."""
    first = _make_github_app_tracker(
        db_session,
        test_user,
        name="GitHub App Tracker A",
        external_id=4242,
        target_id=9001,
        target_name="example-org",
    )
    second = _make_github_app_tracker(
        db_session,
        test_user,
        name="GitHub App Tracker B",
        external_id=4343,
        target_id=9002,
        target_name="other-org",
    )

    trackers = crud_tracker.get_for_account(
        db_session, account_id=str(test_user.account_id)
    )
    by_id = {str(tracker.id): tracker for tracker in trackers}
    assert "oauth_installation" not in sa_inspect(by_id[str(first.id)]).unloaded
    assert "oauth_installation" not in sa_inspect(by_id[str(second.id)]).unloaded
    assert by_id[str(first.id)].github_installation_target_login == "example-org"
    assert by_id[str(second.id)].github_installation_target_login == "other-org"

    detail = crud_tracker.get_by_id_and_account(
        db_session, id=str(first.id), account_id=test_user.account_id
    )
    assert detail is not None
    assert "oauth_installation" not in sa_inspect(detail).unloaded


def test_list_trackers_does_not_n_plus_1_installations(
    client: TestClient, db_session, test_user
):
    """GET /trackers must not issue a per-tracker installation SELECT."""
    _make_github_app_tracker(
        db_session,
        test_user,
        name="GitHub App Tracker A",
        external_id=4242,
        target_id=9001,
        target_name="example-org",
    )
    _make_github_app_tracker(
        db_session,
        test_user,
        name="GitHub App Tracker B",
        external_id=4343,
        target_id=9002,
        target_name="other-org",
    )

    statements: list[str] = []

    def _capture(
        conn: object,
        cursor: object,
        statement: str,
        parameters: object,
        context: object,
        executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _capture)
    try:
        response = client.get("/api/v1/trackers")
    finally:
        event.remove(Engine, "before_cursor_execute", _capture)

    assert response.status_code == 200
    logins = {item["github_installation_target_login"] for item in response.json()}
    assert logins == {"example-org", "other-org"}

    lazy_installation_lookups = [
        statement
        for statement in statements
        if "oauth_app_installation" in statement.lower()
        and "tracker" not in statement.lower()
    ]
    assert lazy_installation_lookups == []


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
    # Additive field: first Jira unlocks any-tracker default-enabled builtins.
    assert "unlocked_tool_names" in response_json
    assert set(response_json["unlocked_tool_names"]) == {
        "get_issue",
        "create_issue",
        "update_issue",
        "search",
        "add_comment",
    }
    assert "estimate_compliance" not in response_json["unlocked_tool_names"]
    assert "improve_compliance" not in response_json["unlocked_tool_names"]

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


ANY_TRACKER_UNLOCKED = {
    "get_issue",
    "create_issue",
    "update_issue",
    "search",
    "add_comment",
}
GITHUB_GITLAB_UNLOCKED = {
    "update_comment",
    "get_pull_request",
    "update_pull_request",
    "create_pull_request",
    "get_issue_triage_context",
    "apply_issue_triage",
}


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
async def test_register_github_tracker_unlocks_pr_tools(
    mock_send_email,
    mock_publish_task,
    mock_create_tracker_client,
    client: TestClient,
    db_session,
    test_user,
):
    """First GitHub tracker unlocks any-tracker + github/gitlab tools."""
    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_tracker_client.validate_token_permissions = AsyncMock(
        return_value={"valid": True, "warnings": [], "errors": []}
    )
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers",
        json={
            "name": "GitHub Tracker",
            "type": "github",
            "url": "https://github.com",
            "api_key": "ghp_test",
            "config": {},
        },
    )
    assert response.status_code == 201
    unlocked = set(response.json()["unlocked_tool_names"])
    assert unlocked == ANY_TRACKER_UNLOCKED | GITHUB_GITLAB_UNLOCKED


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
async def test_register_jira_when_github_exists_unlocks_nothing(
    mock_send_email,
    mock_publish_task,
    mock_create_tracker_client,
    client: TestClient,
    db_session,
    test_user,
):
    """Adding Jira when GitHub already exists unlocks no additional tools."""
    existing = Tracker(
        name="Existing GitHub",
        tracker_type="github",
        url="https://github.com",
        account_id=test_user.account_id,
        api_key="ghp_existing",
    )
    db_session.add(existing)
    db_session.commit()

    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers",
        json={
            "name": "Jira After GitHub",
            "type": "jira",
            "url": "https://test.jira.com",
            "api_key": "jira_key",
            "config": {"username": "testuser"},
        },
    )
    assert response.status_code == 201
    assert response.json()["unlocked_tool_names"] == []


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
async def test_register_github_when_jira_exists_unlocks_pr_tools(
    mock_send_email,
    mock_publish_task,
    mock_create_tracker_client,
    client: TestClient,
    db_session,
    test_user,
):
    """Adding GitHub when only Jira exists unlocks github/gitlab-gated tools."""
    existing = Tracker(
        name="Existing Jira",
        tracker_type="jira",
        url="https://test.jira.com",
        account_id=test_user.account_id,
        api_key="jira_existing",
    )
    db_session.add(existing)
    db_session.commit()

    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_tracker_client.validate_token_permissions = AsyncMock(
        return_value={"valid": True, "warnings": [], "errors": []}
    )
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers",
        json={
            "name": "GitHub After Jira",
            "type": "github",
            "url": "https://github.com",
            "api_key": "ghp_test",
            "config": {},
        },
    )
    assert response.status_code == 201
    assert set(response.json()["unlocked_tool_names"]) == GITHUB_GITLAB_UNLOCKED


@pytest.mark.asyncio
@patch("preloop.api.endpoints.trackers.create_tracker_client")
@patch("preloop.api.endpoints.trackers.event_bus_service.publish_task")
@patch("preloop.api.endpoints.trackers.send_tracker_registered_email")
async def test_register_tracker_excludes_explicitly_disabled_tool(
    mock_send_email,
    mock_publish_task,
    mock_create_tracker_client,
    client: TestClient,
    db_session,
    test_user,
):
    """Tools with ToolConfiguration.is_enabled=False are not listed as unlocked."""
    from preloop.models.models.tool_configuration import ToolConfiguration

    db_session.add(
        ToolConfiguration(
            tool_name="add_comment",
            tool_source="builtin",
            account_id=test_user.account_id,
            is_enabled=False,
        )
    )
    db_session.commit()

    mock_tracker_client = AsyncMock()
    mock_tracker_client.test_connection.return_value.connected = True
    mock_create_tracker_client.return_value = mock_tracker_client

    response = client.post(
        "/api/v1/trackers",
        json={
            "name": "Jira With Disabled Tool",
            "type": "jira",
            "url": "https://test.jira.com",
            "api_key": "jira_key",
            "config": {"username": "testuser"},
        },
    )
    assert response.status_code == 201
    unlocked = response.json()["unlocked_tool_names"]
    assert "add_comment" not in unlocked
    assert set(unlocked) == ANY_TRACKER_UNLOCKED - {"add_comment"}


def test_tracker_create_response_unlocked_field_is_additive():
    """Existing clients that ignore unlocked_tool_names remain valid."""
    from preloop.api.endpoints.trackers import TrackerCreateResponse

    minimal = TrackerCreateResponse(id="abc")
    assert minimal.unlocked_tool_names == []
    assert minimal.model_dump()["id"] == "abc"
    with_field = TrackerCreateResponse(
        id="abc", warnings=["w"], unlocked_tool_names=["get_issue"]
    )
    assert with_field.unlocked_tool_names == ["get_issue"]
