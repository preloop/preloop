"""Tests for GET /projects/{project_id}/pull-requests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints.pull_requests import clear_pull_request_list_cache
from preloop.models.crud import crud_organization, crud_project, crud_tracker
from preloop.models.models.user import User
from preloop.sync.exceptions import TrackerResponseError


@pytest.fixture(autouse=True)
def _clear_pr_cache() -> None:
    clear_pull_request_list_cache()
    yield
    clear_pull_request_list_cache()


@pytest.fixture
def pr_project_data(db_session: Session, test_user: User) -> dict:
    """Create a tracker, organization, and project for PR list tests."""
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "name": "PR Tracker",
            "tracker_type": "github",
            "url": "https://github.com/acme",
            "api_key": "test_key",
            "account_id": str(test_user.account_id),
            "is_active": True,
        },
    )
    db_session.flush()
    org = crud_organization.create(
        db_session,
        obj_in={
            "name": "acme",
            "identifier": "acme",
            "tracker_id": str(tracker.id),
            "is_active": True,
        },
    )
    db_session.flush()
    project = crud_project.create(
        db_session,
        obj_in={
            "name": "widgets",
            "identifier": "widgets",
            "slug": "acme/widgets",
            "organization_id": str(org.id),
            "is_active": True,
        },
    )
    db_session.flush()
    return {"tracker": tracker, "organization": org, "project": project}


def _listed_pr() -> dict:
    return {
        "number": 12,
        "iid": 12,
        "title": "Add login",
        "description": "Please review",
        "url": "https://github.com/acme/widgets/pull/12",
        "author": "janedoe",
        "source_branch": "feature",
        "target_branch": "main",
        "state": "open",
        "draft": False,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-02T00:00:00Z",
    }


@patch(
    "preloop.api.endpoints.pull_requests.get_tracker_client",
    new_callable=AsyncMock,
)
def test_list_prs_cached_within_ttl(
    mock_get_client: AsyncMock,
    client: TestClient,
    db_session: Session,
    test_user: User,
    pr_project_data: dict,
) -> None:
    """Two GETs within the TTL call the tracker client once."""
    del db_session, test_user
    tracker_client = MagicMock()
    tracker_client.list_pull_requests = AsyncMock(
        return_value={"items": [_listed_pr()], "has_more": False}
    )
    mock_get_client.return_value = tracker_client
    project = pr_project_data["project"]
    url = f"/api/v1/projects/{project.id}/pull-requests"

    first = client.get(url)
    second = client.get(url)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["items"][0]["number"] == 12
    assert second.json()["items"][0]["number"] == 12
    assert first.json()["supported"] is True
    assert tracker_client.list_pull_requests.await_count == 1
    mock_get_client.assert_awaited_once()


@patch(
    "preloop.api.endpoints.pull_requests.get_tracker_client",
    new_callable=AsyncMock,
)
def test_refresh_bypasses_cache(
    mock_get_client: AsyncMock,
    client: TestClient,
    db_session: Session,
    test_user: User,
    pr_project_data: dict,
) -> None:
    """?refresh=1 skips the TTL cache and fetches again."""
    del db_session, test_user
    tracker_client = MagicMock()
    tracker_client.list_pull_requests = AsyncMock(
        return_value={"items": [_listed_pr()], "has_more": True}
    )
    mock_get_client.return_value = tracker_client
    project = pr_project_data["project"]
    url = f"/api/v1/projects/{project.id}/pull-requests"

    assert client.get(url).status_code == 200
    refreshed = client.get(f"{url}?refresh=1")
    assert refreshed.status_code == 200
    assert refreshed.json()["has_more"] is True
    assert tracker_client.list_pull_requests.await_count == 2


def test_unsupported_tracker_returns_supported_false(
    client: TestClient,
    db_session: Session,
    test_user: User,
    pr_project_data: dict,
) -> None:
    """Jira projects return 200 with supported false and no items."""
    del test_user
    tracker = pr_project_data["tracker"]
    tracker.tracker_type = "jira"
    db_session.add(tracker)
    db_session.flush()
    project = pr_project_data["project"]

    with patch(
        "preloop.api.endpoints.pull_requests.get_tracker_client",
        new_callable=AsyncMock,
    ) as mock_get_client:
        response = client.get(f"/api/v1/projects/{project.id}/pull-requests")

    assert response.status_code == 200
    data = response.json()
    assert data["supported"] is False
    assert data["items"] == []
    assert data["has_more"] is False
    mock_get_client.assert_not_called()


@patch(
    "preloop.api.endpoints.pull_requests.get_tracker_client",
    new_callable=AsyncMock,
)
def test_tracker_error_returns_502(
    mock_get_client: AsyncMock,
    client: TestClient,
    db_session: Session,
    test_user: User,
    pr_project_data: dict,
) -> None:
    """Tracker failures become 502 with a stable detail string."""
    del db_session, test_user
    tracker_client = MagicMock()
    tracker_client.list_pull_requests = AsyncMock(
        side_effect=TrackerResponseError("GitHub API error: 502")
    )
    mock_get_client.return_value = tracker_client
    project = pr_project_data["project"]

    response = client.get(f"/api/v1/projects/{project.id}/pull-requests")
    assert response.status_code == 502
    assert response.json()["detail"] == "Tracker request failed"
