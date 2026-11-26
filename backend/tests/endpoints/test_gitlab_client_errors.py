"""GitLab MR requests preserve actionable client initialization failures."""

from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import gitlab
import pytest
import requests
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.endpoints.pull_requests import clear_pull_request_list_cache
from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase


@pytest.fixture
def gitlab_project(db_session: Session, test_user: models.User) -> models.Project:
    """Persist a scoped GitLab project with an encrypted token."""
    tracker = crud.crud_tracker.create(
        db_session,
        obj_in={
            "name": "GitLab tracker",
            "tracker_type": "gitlab",
            "url": "https://gitlab.example.com/",
            "api_key": "private-test-token",
            "account_id": test_user.account_id,
            "connection_details": {"private_setting": "private-config-detail"},
        },
    )
    organization = crud.crud_organization.create(
        db_session,
        obj_in={"name": "Example", "identifier": "42", "tracker_id": tracker.id},
    )
    crud.crud_tracker_scope_rule.create(
        db_session,
        obj_in={
            "tracker_id": tracker.id,
            "scope_type": "ORGANIZATION",
            "rule_type": "INCLUDE",
            "identifier": organization.identifier,
        },
    )
    return crud.crud_project.create(
        db_session,
        obj_in={
            "name": "Example project",
            "identifier": "123",
            "slug": "example/project",
            "organization_id": organization.id,
        },
    )


@pytest.fixture
def gitlab_api() -> Iterator[MagicMock]:
    """Mock only GitLab's HTTP client, retaining the real factory and adapter."""
    clear_pull_request_list_cache()
    with patch("preloop.sync.trackers.gitlab.gitlab.Gitlab") as constructor:
        constructor.return_value.projects.get.return_value.mergerequests.list.return_value = []
        yield constructor
    clear_pull_request_list_cache()


@pytest.mark.parametrize(
    ("failure", "status_code", "message"),
    [
        (
            gitlab.exceptions.GitlabAuthenticationError(
                "private-provider-detail", response_code=401
            ),
            400,
            "token",
        ),
        (
            gitlab.exceptions.GitlabGetError(
                "private-provider-detail", response_code=403
            ),
            403,
            "api scope",
        ),
        (
            requests.ConnectionError("private-provider-detail"),
            502,
            "Tracker request failed",
        ),
        (ValueError("private-provider-detail"), 400, "configuration"),
        (RuntimeError("private-provider-detail"), 500, "tracker client"),
    ],
)
def test_client_construction_errors_are_actionable(
    client: TestClient,
    gitlab_project: models.Project,
    gitlab_api: MagicMock,
    failure: Exception,
    status_code: int,
    message: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Factory failures retain their category without exposing config or bodies."""
    caplog.set_level("DEBUG", logger="preloop.api.common")
    gitlab_api.return_value.auth.side_effect = failure
    response = client.get(f"/api/v1/projects/{gitlab_project.id}/pull-requests")
    assert response.status_code == status_code
    assert message in response.json()["detail"]
    assert "private-" not in response.text
    assert "private-" not in caplog.text


@pytest.mark.parametrize("status_code", [401, 403])
def test_permission_changes_after_construction_are_actionable(
    client: TestClient,
    gitlab_project: models.Project,
    gitlab_api: MagicMock,
    status_code: int,
) -> None:
    """A token can pass /user authentication but fail project access later."""
    gitlab_api.return_value.projects.get.side_effect = gitlab.exceptions.GitlabGetError(
        "private-provider-detail", response_code=status_code
    )
    response = client.get(f"/api/v1/projects/{gitlab_project.id}/pull-requests")
    assert response.status_code == (400 if status_code == 401 else 403)
    assert "token" in response.json()["detail"]
    assert "private-" not in response.text


@pytest.mark.parametrize("legacy", [False, True])
def test_valid_client_uses_saved_credentials_and_project(
    client: TestClient,
    db_session: Session,
    gitlab_project: models.Project,
    gitlab_api: MagicMock,
    legacy: bool,
) -> None:
    """The complete route builds a client from the secret and discovered identity."""
    if legacy:
        # Represent a row from before credentials moved to SecretReference.
        CRUDBase(models.Tracker).update(
            db_session,
            db_obj=gitlab_project.organization.tracker,
            obj_in={"credentials_secret_id": None, "api_key": "private-test-token"},
        )
    response = client.get(f"/api/v1/projects/{gitlab_project.id}/pull-requests")
    assert response.status_code == 200
    assert response.json()["items"] == []
    gitlab_api.assert_called_once_with(
        "https://gitlab.example.com", private_token="private-test-token"
    )
    gitlab_api.return_value.auth.assert_called_once()
    gitlab_api.return_value.projects.get.assert_called_once_with("123")


def test_failed_client_is_not_cached(
    client: TestClient,
    gitlab_project: models.Project,
    gitlab_api: MagicMock,
) -> None:
    """Retry after restoring access builds a fresh client and reaches GitLab."""
    gitlab_api.return_value.auth.side_effect = [
        gitlab.exceptions.GitlabAuthenticationError("rejected", response_code=401),
        None,
    ]
    url = f"/api/v1/projects/{gitlab_project.id}/pull-requests"
    assert client.get(url).status_code == 400
    assert client.get(url).status_code == 200
    assert gitlab_api.call_count == 2


def test_unreadable_saved_credential_returns_configuration_error(
    client: TestClient,
    gitlab_project: models.Project,
    gitlab_api: MagicMock,
) -> None:
    """A credential decryption failure tells the user to re-enter the token."""
    with patch(
        "preloop.services.secret_service.SecretService.resolve_secret_reference",
        side_effect=ValueError("private-provider-detail"),
    ):
        response = client.get(f"/api/v1/projects/{gitlab_project.id}/pull-requests")
    assert response.status_code == 400
    assert "re-enter an access token" in response.json()["detail"]
    assert "private-" not in response.text
    gitlab_api.assert_not_called()
