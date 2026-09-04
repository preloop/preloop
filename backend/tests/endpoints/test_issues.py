"""Unit tests for issues API endpoints."""

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_issue,
    crud_organization,
    crud_project,
    crud_tracker,
)
from preloop.models.models.user import User
from preloop.schemas.tracker_models import Issue as TrackerIssue, IssueStatus


@pytest.fixture
def issue_test_data(db_session: Session, test_user: User) -> dict:
    """Create tracker, organization, project, and issue for issue endpoint tests."""
    tracker = crud_tracker.create(
        db_session,
        obj_in={
            "name": "Test Tracker",
            "tracker_type": "github",
            "url": "https://github.com/test",
            "api_key": "test_key",
            "account_id": str(test_user.account_id),
            "is_active": True,
        },
    )
    db_session.flush()

    org = crud_organization.create(
        db_session,
        obj_in={
            "name": "Test Org",
            "identifier": "test-org",
            "tracker_id": str(tracker.id),
            "is_active": True,
        },
    )
    db_session.flush()

    project = crud_project.create(
        db_session,
        obj_in={
            "name": "Test Project",
            "identifier": "test-proj",
            "slug": "test-proj",
            "organization_id": str(org.id),
            "is_active": True,
        },
    )
    db_session.flush()

    issue = crud_issue.create(
        db_session,
        obj_in={
            "title": "Test Issue",
            "description": "Test description",
            "project_id": str(project.id),
            "tracker_id": str(tracker.id),
            "external_id": "123",
            "key": "TEST-1",
            "status": "open",
            "external_url": "https://example.com/issues/123",
            "meta_data": {"labels": ["bug", "feature"], "assignee": "testuser"},
        },
    )
    db_session.flush()

    return {
        "tracker": tracker,
        "organization": org,
        "project": project,
        "issue": issue,
    }


class TestSearchIssues:
    """Tests for GET /api/v1/issues/search."""

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_fulltext_returns_matching_issues(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test fulltext search returns issues matching query."""
        response = client.get("/api/v1/issues/search?query=Test&search_type=fulltext")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert any(i["title"] == "Test Issue" for i in data)

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_empty_query_returns_list(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with empty query returns list (may be empty)."""
        response = client.get("/api/v1/issues/search?query=&search_type=fulltext")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_no_trackers_returns_empty(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test search with no trackers for user returns empty list."""
        response = client.get("/api/v1/issues/search?query=foo")
        assert response.status_code == 200
        assert response.json() == []

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_with_project_filter(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with organization and project filters."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]
        response = client.get(
            f"/api/v1/issues/search?query=Test&organization={org.name}&project={project.name}&search_type=fulltext"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["project"] == project.name

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_project_not_found_returns_404(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with non-existent project returns 404."""
        response = client.get(
            "/api/v1/issues/search?query=Test&project=nonexistent-project"
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_with_status_filter(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with status filter."""
        response = client.get(
            "/api/v1/issues/search?query=Test&status=open&search_type=fulltext"
        )
        assert response.status_code == 200
        data = response.json()
        assert all(i.get("status") == "open" for i in data if i.get("status"))

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_with_organization_id_and_project_id(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with organization_id and project_id filters."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]
        response = client.get(
            f"/api/v1/issues/search?query=Test&organization_id={org.id}&project_id={project.id}&search_type=fulltext"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0]["project"] == project.name

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_with_labels_filter(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with labels filter (post-fetch filtering)."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]
        response = client.get(
            f"/api/v1/issues/search?query=Test&organization={org.name}&project={project.name}&labels=bug&search_type=fulltext"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert "bug" in (data[0].get("labels") or [])

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_search_issues_invalid_search_type_returns_400(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test search with invalid search_type returns 400."""
        response = client.get("/api/v1/issues/search?query=Test&search_type=invalid")
        assert response.status_code == 400
        assert "Invalid search type" in response.json()["detail"]

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    @patch("preloop.api.endpoints.issues.crud_embedding_model")
    @patch("preloop.api.endpoints.issues.crud_issue_embedding")
    def test_search_issues_similarity_returns_results(
        self,
        mock_crud_issue_embedding: AsyncMock,
        mock_crud_embedding_model: AsyncMock,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test similarity search returns issues when embedding model is available."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]
        issue = issue_test_data["issue"]

        mock_model = type("Model", (), {"id": "model-1"})()
        mock_crud_embedding_model.get_active.return_value = [mock_model]
        mock_crud_issue_embedding._generate_embedding_vector.return_value = [0.1] * 1536
        mock_crud_issue_embedding.similarity_search.return_value = [
            (issue, 0.95),
        ]

        response = client.get(
            f"/api/v1/issues/search?query=Test&organization={org.name}&project={project.name}&search_type=similarity"
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 1
        assert data[0].get("score") == 0.95

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    @patch("preloop.api.endpoints.issues.crud_embedding_model")
    def test_search_issues_similarity_no_embedding_model_returns_500(
        self,
        mock_crud_embedding_model: AsyncMock,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test similarity search without embedding model returns 500."""
        mock_crud_embedding_model.get_active.return_value = []
        response = client.get("/api/v1/issues/search?query=Test&search_type=similarity")
        assert response.status_code == 500
        detail = response.json()["detail"].lower()
        assert "similarity search" in detail or "embedding model" in detail


class TestCreateIssue:
    """Tests for POST /api/v1/issues."""

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_create_issue_success(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test creating an issue successfully."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]

        mock_tracker_client = AsyncMock()
        mock_created = TrackerIssue(
            id="456",
            key="TEST-2",
            title="New Issue",
            description="New description",
            status=IssueStatus(id="1", name="open", category="todo"),
            priority=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            url="https://example.com/issues/456",
            tracker_type="github",
            project_key=project.slug or project.identifier,
            custom_fields={},
        )
        mock_tracker_client.create_issue = AsyncMock(return_value=mock_created)
        mock_get_tracker_client.return_value = mock_tracker_client

        payload = {
            "title": "New Issue",
            "description": "New description",
            "project": project.slug or project.identifier,
            "organization": org.identifier,
        }

        response = client.post("/api/v1/issues", json=payload)
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "New Issue"
        assert data["key"] == "TEST-2"
        assert data["external_id"] == "456"

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_create_issue_missing_project_returns_400(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test creating issue without project returns 400."""
        response = client.post(
            "/api/v1/issues",
            json={"title": "Test", "description": "Desc"},
        )
        assert response.status_code == 422  # Validation error

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_create_issue_project_not_found_returns_404(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test creating issue with non-existent project returns 404."""
        response = client.post(
            "/api/v1/issues",
            json={
                "title": "Test",
                "description": "Desc",
                "project": "nonexistent",
            },
        )
        assert response.status_code == 404

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_create_issue_by_organization_id_and_project_id(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test creating issue with organization_id and project_id (IDs take precedence)."""
        org = issue_test_data["organization"]
        project = issue_test_data["project"]

        mock_tracker_client = AsyncMock()
        mock_created = TrackerIssue(
            id="789",
            key="TEST-3",
            title="Issue by IDs",
            description="Created via org/project IDs",
            status=IssueStatus(id="1", name="open", category="todo"),
            priority=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            url="https://example.com/issues/789",
            tracker_type="github",
            project_key=project.slug or project.identifier,
            custom_fields={},
        )
        mock_tracker_client.create_issue = AsyncMock(return_value=mock_created)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.post(
            "/api/v1/issues",
            json={
                "title": "Issue by IDs",
                "description": "Created via org/project IDs",
                "organization_id": str(org.id),
                "project_id": str(project.id),
                "project": project.slug,
            },
        )
        assert response.status_code == 201
        data = response.json()
        assert data["title"] == "Issue by IDs"
        assert data["key"] == "TEST-3"

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_create_issue_organization_not_found_returns_404(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test creating issue with non-existent organization returns 404."""
        project = issue_test_data["project"]
        fake_org_id = str(uuid4())
        response = client.post(
            "/api/v1/issues",
            json={
                "title": "Test",
                "description": "Desc",
                "organization_id": fake_org_id,
                "project": project.slug,
            },
        )
        assert response.status_code == 404


class TestGetIssue:
    """Tests for GET /api/v1/issues/{id}."""

    def test_get_issue_by_id_success(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting an issue by internal ID."""
        issue = issue_test_data["issue"]
        response = client.get(f"/api/v1/issues/{issue.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Issue"
        assert data["key"] == "TEST-1"
        assert data["external_id"] == "123"

    def test_get_issue_by_external_id_success(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting an issue by combined key (project_slug#external_id)."""
        issue = issue_test_data["issue"]
        project = issue_test_data["project"]
        # URL-encode '#' as %23 so server receives full path
        combined_key = f"{project.slug}%23{issue.external_id}"
        response = client.get(f"/api/v1/issues/{combined_key}")
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Issue"

    def test_get_issue_by_key_success(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting an issue by key (e.g. TEST-1)."""
        issue = issue_test_data["issue"]
        response = client.get(f"/api/v1/issues/{issue.key}")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "TEST-1"

    def test_get_issue_not_found_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting non-existent issue returns 404."""
        response = client.get("/api/v1/issues/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    def test_get_issue_no_trackers_returns_404(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test getting issue when user has no trackers returns 404."""
        response = client.get("/api/v1/issues/some-id")
        assert response.status_code == 404

    def test_get_issue_includes_comments(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting issue includes comments."""
        issue = issue_test_data["issue"]
        response = client.get(f"/api/v1/issues/{issue.id}")
        assert response.status_code == 200
        data = response.json()
        assert "comments" in data
        assert isinstance(data["comments"], list)

    def test_get_issue_by_external_id_with_project_query_param(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test getting issue by external_id with project query param."""
        issue = issue_test_data["issue"]
        project = issue_test_data["project"]
        response = client.get(
            f"/api/v1/issues/{issue.external_id}?project={project.slug}"
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Issue"

    def test_get_issue_response_has_required_fields(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test get issue response contains all required fields."""
        issue = issue_test_data["issue"]
        response = client.get(f"/api/v1/issues/{issue.id}")
        assert response.status_code == 200
        data = response.json()
        required = [
            "id",
            "key",
            "external_id",
            "title",
            "organization",
            "project",
            "url",
        ]
        for field in required:
            assert field in data, f"Missing required field: {field}"


class TestUpdateIssue:
    """Tests for PUT /api/v1/issues/{id}."""

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_success(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test updating an issue successfully."""
        issue = issue_test_data["issue"]

        mock_tracker_client = AsyncMock()
        mock_tracker_client.update_issue = AsyncMock(return_value=None)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.put(
            f"/api/v1/issues/{issue.id}",
            json={"title": "Updated Title", "description": "Updated description"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated Title"
        assert data["description"] == "Updated description"

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_partial_update(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test partial update (only title)."""
        issue = issue_test_data["issue"]

        mock_tracker_client = AsyncMock()
        mock_tracker_client.update_issue = AsyncMock(return_value=None)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.put(
            f"/api/v1/issues/{issue.id}",
            json={"title": "Only Title Changed"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Only Title Changed"
        assert data["description"] == "Test description"

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_not_found_returns_404(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test updating non-existent issue returns 404."""
        response = client.put(
            "/api/v1/issues/00000000-0000-0000-0000-000000000000",
            json={"title": "Updated"},
        )
        assert response.status_code == 404

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_no_trackers_returns_403(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test updating issue when user has no trackers returns 403."""
        response = client.put(
            "/api/v1/issues/some-id",
            json={"title": "Updated"},
        )
        assert response.status_code == 403

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_by_combined_key(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test updating issue by combined key (project_slug#external_id)."""
        issue = issue_test_data["issue"]
        project = issue_test_data["project"]
        combined_key = f"{project.slug}%23{issue.external_id}"

        mock_tracker_client = AsyncMock()
        mock_tracker_client.update_issue = AsyncMock(return_value=None)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.put(
            f"/api/v1/issues/{combined_key}",
            json={"title": "Updated via Key", "status": "in_progress"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Updated via Key"
        assert data["status"] == "in_progress"

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_empty_payload_returns_current_state(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test update with empty payload returns current issue state."""
        issue = issue_test_data["issue"]

        mock_tracker_client = AsyncMock()
        mock_tracker_client.update_issue = AsyncMock(return_value=None)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.put(
            f"/api/v1/issues/{issue.id}",
            json={},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["title"] == "Test Issue"
        assert data["key"] in ("TEST-1", "test-proj#123")

    @patch("preloop.api.endpoints.issues.get_tracker_client", new_callable=AsyncMock)
    def test_update_issue_status_only(
        self,
        mock_get_tracker_client: AsyncMock,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test updating only status field."""
        issue = issue_test_data["issue"]

        mock_tracker_client = AsyncMock()
        mock_tracker_client.update_issue = AsyncMock(return_value=None)
        mock_get_tracker_client.return_value = mock_tracker_client

        response = client.put(
            f"/api/v1/issues/{issue.id}",
            json={"status": "closed"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "closed"
        assert data["title"] == "Test Issue"


class TestGetIssueCount:
    """Tests for GET /api/v1/issues-count."""

    def test_get_issue_count_returns_total(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
        issue_test_data: dict,
    ):
        """Test get issue count returns total_issues."""
        response = client.get("/api/v1/issues-count")
        assert response.status_code == 200
        data = response.json()
        assert "total_issues" in data
        assert isinstance(data["total_issues"], int)
        assert data["total_issues"] >= 1

    def test_get_issue_count_no_trackers_returns_zero(
        self,
        client: TestClient,
        db_session: Session,
        test_user: User,
    ):
        """Test get issue count when user has no trackers returns 0."""
        response = client.get("/api/v1/issues-count")
        assert response.status_code == 200
        data = response.json()
        assert data["total_issues"] == 0


class TestSearchIssuesRateLimit:
    """Transient 429 handling for the similarity-search query embedding (#269)."""

    @staticmethod
    def _openai_rate_limit_error(retry_after: str = "3") -> Exception:
        import httpx
        import openai

        request = httpx.Request("POST", "https://example.com/v1/embeddings")
        response = httpx.Response(
            429, headers={"retry-after": retry_after}, request=request
        )
        return openai.RateLimitError(
            "Error code: 429 - Too Many Requests", response=response, body=None
        )

    @staticmethod
    def _similarity_patches(
        vector_side_effect: object,
    ) -> tuple[Any, ...]:
        return (
            patch(
                "preloop.api.endpoints.issues.crud_tracker.get_for_account",
                return_value=[MagicMock()],
            ),
            patch(
                "preloop.api.endpoints.issues.crud_issue.get_for_trackers",
                return_value=MagicMock(),
            ),
            patch(
                "preloop.api.endpoints.issues.crud_embedding_model.get_active",
                return_value=[MagicMock(id="model-1", provider="openai")],
            ),
            patch(
                "preloop.api.endpoints.issues.crud_issue_embedding._generate_embedding_vector",
                side_effect=vector_side_effect,
            ),
            patch(
                "preloop.api.endpoints.issues.crud_issue_embedding.similarity_search",
                return_value=[],
            ),
            patch(
                "preloop.services.aux_model_retry.asyncio.sleep",
                new=AsyncMock(return_value=None),
            ),
        )

    @staticmethod
    async def _search_similarity() -> Any:
        from preloop.api.endpoints import issues as issues_endpoint

        return await issues_endpoint.search_issues(
            db=MagicMock(),
            current_user=MagicMock(),
            organization_id=None,
            organization=None,
            project_id=None,
            project=None,
            query="test query",
            limit=10,
            search_type="similarity",
            status=None,
            labels=None,
            assignee=None,
            priority=None,
            last_updated_before=None,
            last_updated_after=None,
        )

    @pytest.mark.asyncio
    async def test_search_issues_similarity_embedding_429_is_retried(self) -> None:
        """One provider 429 during query embedding must not fail the search."""
        patches = self._similarity_patches(
            [self._openai_rate_limit_error(), [0.1] * 1536]
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3] as mock_vector,
            patches[4],
            patches[5],
        ):
            result = await self._search_similarity()

        assert result == []
        assert mock_vector.call_count == 2

    @pytest.mark.asyncio
    async def test_search_issues_similarity_embedding_429_surfaces_retry_after(
        self,
    ) -> None:
        """An exhausted transient 429 surfaces as 429 with a Retry-After header."""
        patches = self._similarity_patches(
            self._openai_rate_limit_error(retry_after="3")
        )
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            with pytest.raises(HTTPException) as exc_info:
                await self._search_similarity()

        assert exc_info.value.status_code == 429
        assert exc_info.value.headers is not None
        assert exc_info.value.headers.get("Retry-After") == "3"
