"""Tests for embedding endpoints."""

from unittest.mock import MagicMock, patch

import pytest
from preloop.models.models.user import User


class TestGetRawEmbeddings:
    """Test get_raw_embeddings endpoint."""

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_success(self, mock_crud_class, mock_db_session):
        """Test getting raw embeddings successfully."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"
        mock_user.account_id = "account-456"

        # Mock CRUD instance
        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = [
            (
                "issue-1",
                [0.1, 0.2, 0.3],
                "Test Issue",
                "proj-1",
                "bug",
                "2024-01-01T00:00:00",
            ),
            (
                "issue-2",
                [0.4, 0.5, 0.6],
                "Another Issue",
                "proj-2",
                "feature",
                "2024-01-02T00:00:00",
            ),
        ]
        mock_crud_class.return_value = mock_crud

        result = get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id=None,
            project_ids=None,
            project_names=None,
            organization_ids=None,
            organization_names=None,
            skip=0,
            limit=1000,
        )

        # Verify CRUD was called correctly
        mock_crud.get_raw_embeddings.assert_called_once_with(
            db=mock_db_session,
            account_id="account-456",
            embedding_model_id=None,
            project_ids=None,
            project_names=None,
            organization_ids=None,
            organization_names=None,
            skip=0,
            limit=1000,
        )

        # Verify response structure
        assert len(result.data) == 2
        assert result.data[0].issue_id == "issue-1"
        assert result.data[0].embedding == [0.1, 0.2, 0.3]
        assert result.data[0].issue_title == "Test Issue"
        assert result.data[1].issue_id == "issue-2"

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_with_project_ids(
        self, mock_crud_class, mock_db_session
    ):
        """Test getting embeddings filtered by project IDs."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id="model-1",
            project_ids="proj-1,proj-2",
            project_names=None,
            organization_ids=None,
            organization_names=None,
            skip=0,
            limit=1000,
        )

        # Verify project_ids were split correctly
        mock_crud.get_raw_embeddings.assert_called_once()
        call_kwargs = mock_crud.get_raw_embeddings.call_args[1]
        assert call_kwargs["project_ids"] == ["proj-1", "proj-2"]
        assert call_kwargs["embedding_model_id"] == "model-1"

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_with_project_names(
        self, mock_crud_class, mock_db_session
    ):
        """Test getting embeddings filtered by project names."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id=None,
            project_ids=None,
            project_names="project-a,project-b",
            organization_ids=None,
            organization_names=None,
            skip=10,
            limit=500,
        )

        call_kwargs = mock_crud.get_raw_embeddings.call_args[1]
        assert call_kwargs["project_names"] == ["project-a", "project-b"]
        assert call_kwargs["skip"] == 10
        assert call_kwargs["limit"] == 500

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_with_organization_ids(
        self, mock_crud_class, mock_db_session
    ):
        """Test getting embeddings filtered by organization IDs."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id=None,
            project_ids=None,
            project_names=None,
            organization_ids="org-1,org-2,org-3",
            organization_names=None,
            skip=0,
            limit=1000,
        )

        call_kwargs = mock_crud.get_raw_embeddings.call_args[1]
        assert call_kwargs["organization_ids"] == ["org-1", "org-2", "org-3"]

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_with_organization_names(
        self, mock_crud_class, mock_db_session
    ):
        """Test getting embeddings filtered by organization names."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id=None,
            project_ids=None,
            project_names=None,
            organization_ids=None,
            organization_names="org-alpha,org-beta",
            skip=0,
            limit=1000,
        )

        call_kwargs = mock_crud.get_raw_embeddings.call_args[1]
        assert call_kwargs["organization_names"] == ["org-alpha", "org-beta"]

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_with_all_filters(
        self, mock_crud_class, mock_db_session
    ):
        """Test getting embeddings with all filters applied."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id="model-x",
            project_ids="p1,p2",
            project_names="pn1,pn2",
            organization_ids="o1,o2",
            organization_names="on1,on2",
            skip=100,
            limit=2000,
        )

        call_kwargs = mock_crud.get_raw_embeddings.call_args[1]
        assert call_kwargs["embedding_model_id"] == "model-x"
        assert call_kwargs["project_ids"] == ["p1", "p2"]
        assert call_kwargs["project_names"] == ["pn1", "pn2"]
        assert call_kwargs["organization_ids"] == ["o1", "o2"]
        assert call_kwargs["organization_names"] == ["on1", "on2"]
        assert call_kwargs["skip"] == 100
        assert call_kwargs["limit"] == 2000

    @patch("preloop.api.endpoints.embedding.CRUDIssueEmbedding")
    def test_get_raw_embeddings_empty_result(self, mock_crud_class, mock_db_session):
        """Test getting embeddings with no results."""
        from preloop.api.endpoints.embedding import get_raw_embeddings

        mock_user = MagicMock(spec=User)
        mock_user.id = "user-123"

        mock_crud = MagicMock()
        mock_crud.get_raw_embeddings.return_value = []
        mock_crud_class.return_value = mock_crud

        result = get_raw_embeddings(
            db=mock_db_session,
            current_user=mock_user,
            embedding_model_id=None,
            project_ids=None,
            project_names=None,
            organization_ids=None,
            organization_names=None,
            skip=0,
            limit=1000,
        )

        assert len(result.data) == 0


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return MagicMock()


class TestEmbeddingHTTP:
    """HTTP integration tests for embedding endpoint."""

    def test_get_embeddings_success(self, client):
        """Test GET /api/v1/embeddings returns 200 and valid structure."""
        response = client.get("/api/v1/embeddings")
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
        assert isinstance(data["data"], list)

    def test_get_embeddings_with_query_params(self, client):
        """Test GET /api/v1/embeddings with optional query parameters (skip, limit)."""
        response = client.get(
            "/api/v1/embeddings",
            params={"skip": 10, "limit": 50},
        )
        assert response.status_code == 200
        data = response.json()
        assert "data" in data
