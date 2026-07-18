"""Tests for search endpoint."""

from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from preloop.models.models.user import User


class TestPerformSearch:
    """Test perform_search function."""

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    @patch("preloop.api.endpoints.search.crud_project")
    @patch("preloop.api.endpoints.search.crud_organization")
    async def test_similarity_search_success_with_issue(
        self,
        mock_crud_org,
        mock_crud_proj,
        mock_crud_embed_model,
        mock_crud_issue_embed,
        mock_get_accessible,
    ):
        """Test similarity search with Issue result."""
        from preloop.api.endpoints.search import perform_search
        from preloop.models import models as sm_models

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_project = MagicMock()
        mock_project.id = uuid4()
        mock_project.name = "Test Project"
        mock_project.identifier = "TP"
        mock_project.slug = "test-project"
        mock_project.organization_id = uuid4()
        mock_get_accessible.return_value = [mock_project]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]

        # Mock issue result
        mock_issue = MagicMock(spec=sm_models.Issue)
        mock_issue.id = uuid4()
        mock_issue.project_id = mock_project.id
        mock_issue.external_id = "EXT-123"
        mock_issue.key = "TP-1"
        mock_issue.title = "Test Issue"
        mock_issue.description = "Test description"
        mock_issue.status = "open"
        mock_issue.priority = "high"
        mock_issue.external_url = "https://example.com/issue"
        mock_issue.created_at = "2024-01-01T00:00:00"
        mock_issue.updated_at = "2024-01-02T00:00:00"
        mock_issue.meta_data = {"labels": ["bug", "urgent"]}

        mock_crud_issue_embed.similarity_search.return_value = [(mock_issue, 0.95)]

        # Mock project for result construction
        mock_crud_proj.get.return_value = mock_project

        # Mock organization for result construction
        mock_org = MagicMock()
        mock_org.name = "Test Org"
        mock_crud_org.get.return_value = mock_org

        # Call function
        mock_db = MagicMock()
        result = await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            limit=10,
            skip=0,
        )

        # Verify results
        assert len(result.results) == 1
        assert result.results[0].item_type == "issue"
        assert result.results[0].similarity == 0.95
        assert result.results[0].item.title == "Test Issue"
        assert result.results[0].item.organization == "Test Org"
        assert result.results[0].item.project == "Test Project"

        # Verify calls
        mock_get_accessible.assert_called_once()
        mock_crud_embed_model.get_active.assert_called_once_with(mock_db)
        mock_crud_issue_embed._generate_embedding_vector.assert_called_once()
        mock_crud_issue_embed.similarity_search.assert_called_once()

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_success_with_comment(
        self, mock_crud_embed_model, mock_crud_issue_embed, mock_get_accessible
    ):
        """Test similarity search with Comment result."""
        from preloop.api.endpoints.search import perform_search
        from preloop.models import models as sm_models

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_project = MagicMock()
        mock_project.id = uuid4()
        mock_get_accessible.return_value = [mock_project]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]

        # Mock comment result
        mock_comment = MagicMock(spec=sm_models.Comment)
        mock_comment.id = uuid4()
        mock_comment.body = "Test comment"
        mock_comment.author = "john_doe"
        mock_comment.created_at = "2024-01-01T00:00:00"
        mock_comment.updated_at = "2024-01-02T00:00:00"
        mock_comment.issue_id = uuid4()
        mock_comment.meta_data = {"source": "web"}

        mock_crud_issue_embed.similarity_search.return_value = [(mock_comment, 0.85)]

        # Call function
        mock_db = MagicMock()
        result = await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            limit=10,
            skip=0,
        )

        # Verify results
        assert len(result.results) == 1
        assert result.results[0].item_type == "comment"
        assert result.results[0].similarity == 0.85
        assert result.results[0].item.body == "Test comment"
        assert result.results[0].item.author == "john_doe"

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_no_active_model(
        self, mock_crud_embed_model, mock_get_accessible
    ):
        """Test similarity search with no active embedding model."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_get_accessible.return_value = []

        # Mock no active embedding model
        mock_crud_embed_model.get_active.return_value = []

        # Call function - should raise HTTPException
        mock_db = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await perform_search(
                query="test query",
                db=mock_db,
                current_user=mock_user,
                search_type="similarity",
                limit=10,
                skip=0,
            )

        assert exc_info.value.status_code == 500
        assert "No active embedding model" in exc_info.value.detail

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_embedding_generation_error(
        self, mock_crud_embed_model, mock_crud_issue_embed, mock_get_accessible
    ):
        """Test similarity search with embedding generation error."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_get_accessible.return_value = []

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation error
        mock_crud_issue_embed._generate_embedding_vector.side_effect = Exception(
            "API Error"
        )

        # Call function - should raise HTTPException
        mock_db = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await perform_search(
                query="test query",
                db=mock_db,
                current_user=mock_user,
                search_type="similarity",
                limit=10,
                skip=0,
            )

        assert exc_info.value.status_code == 500
        assert "Error generating query vector" in exc_info.value.detail

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_with_organization_id_filter(
        self, mock_crud_embed_model, mock_crud_issue_embed, mock_get_accessible
    ):
        """Test similarity search with organization_id filter."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects with different organizations
        mock_project1 = MagicMock()
        mock_project1.id = uuid4()
        mock_project1.organization_id = "org-1"

        mock_project2 = MagicMock()
        mock_project2.id = uuid4()
        mock_project2.organization_id = "org-2"

        mock_get_accessible.return_value = [mock_project1, mock_project2]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]
        mock_crud_issue_embed.similarity_search.return_value = []

        # Call function with organization_id filter
        mock_db = MagicMock()
        await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            organization_id="org-1",
            limit=10,
            skip=0,
        )

        # Verify similarity_search was called with only proj-1 (org-1)
        call_args = mock_crud_issue_embed.similarity_search.call_args
        assert call_args[1]["project_ids"] == [mock_project1.id]

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_with_organization_name_filter(
        self, mock_crud_embed_model, mock_crud_issue_embed, mock_get_accessible
    ):
        """Test similarity search with organization name filter."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects with different organizations
        mock_org1 = MagicMock()
        mock_org1.name = "Org Alpha"

        mock_org2 = MagicMock()
        mock_org2.name = "Org Beta"

        mock_project1 = MagicMock()
        mock_project1.id = uuid4()
        mock_project1.organization = mock_org1

        mock_project2 = MagicMock()
        mock_project2.id = uuid4()
        mock_project2.organization = mock_org2

        mock_get_accessible.return_value = [mock_project1, mock_project2]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]
        mock_crud_issue_embed.similarity_search.return_value = []

        # Call function with organization name filter
        mock_db = MagicMock()
        await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            organization="Org Alpha",
            limit=10,
            skip=0,
        )

        # Verify similarity_search was called with only proj-1 (Org Alpha)
        call_args = mock_crud_issue_embed.similarity_search.call_args
        assert call_args[1]["project_ids"] == [mock_project1.id]

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    async def test_similarity_search_with_project_name_filter(
        self, mock_crud_embed_model, mock_crud_issue_embed, mock_get_accessible
    ):
        """Test similarity search with project name filter."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_project1 = MagicMock()
        mock_project1.id = uuid4()
        mock_project1.name = "Project Alpha"

        mock_project2 = MagicMock()
        mock_project2.id = uuid4()
        mock_project2.name = "Project Beta"

        mock_get_accessible.return_value = [mock_project1, mock_project2]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]
        mock_crud_issue_embed.similarity_search.return_value = []

        # Call function with project name filter
        mock_db = MagicMock()
        await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            project="Project Alpha",
            limit=10,
            skip=0,
        )

        # Verify similarity_search was called with only proj-1 (Project Alpha)
        call_args = mock_crud_issue_embed.similarity_search.call_args
        assert call_args[1]["project_ids"] == [mock_project1.id]

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    async def test_fulltext_search_not_implemented(self, mock_get_accessible):
        """Test that fulltext search raises 501 Not Implemented."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_get_accessible.return_value = []

        # Call function with fulltext search type
        mock_db = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await perform_search(
                query="test query",
                db=mock_db,
                current_user=mock_user,
                search_type="fulltext",
                limit=10,
                skip=0,
            )

        assert exc_info.value.status_code == 501
        assert "not implemented" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    async def test_invalid_search_type(self, mock_get_accessible):
        """Test that invalid search_type raises 400 Bad Request."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_get_accessible.return_value = []

        # Call function with invalid search type
        mock_db = MagicMock()
        with pytest.raises(HTTPException) as exc_info:
            await perform_search(
                query="test query",
                db=mock_db,
                current_user=mock_user,
                search_type="invalid",
                limit=10,
                skip=0,
            )

        assert exc_info.value.status_code == 400
        assert "Invalid search_type" in exc_info.value.detail

    @pytest.mark.asyncio
    @patch("preloop.api.endpoints.search.get_accessible_projects")
    @patch("preloop.api.endpoints.search.crud_issue_embedding")
    @patch("preloop.api.endpoints.search.crud_embedding_model")
    @patch("preloop.api.endpoints.search.crud_project")
    async def test_similarity_search_with_status_filter(
        self,
        mock_crud_proj,
        mock_crud_embed_model,
        mock_crud_issue_embed,
        mock_get_accessible,
    ):
        """Test similarity search with status filter."""
        from preloop.api.endpoints.search import perform_search

        # Mock user
        mock_user = MagicMock(spec=User)
        mock_user.account_id = uuid4()

        # Mock accessible projects
        mock_project = MagicMock()
        mock_project.id = uuid4()
        mock_get_accessible.return_value = [mock_project]

        # Mock active embedding model
        mock_model = MagicMock()
        mock_model.id = uuid4()
        mock_crud_embed_model.get_active.return_value = [mock_model]

        # Mock embedding generation
        mock_crud_issue_embed._generate_embedding_vector.return_value = [0.1, 0.2, 0.3]
        mock_crud_issue_embed.similarity_search.return_value = []

        # Call function with status filter
        mock_db = MagicMock()
        await perform_search(
            query="test query",
            db=mock_db,
            current_user=mock_user,
            search_type="similarity",
            status="open",
            limit=10,
            skip=0,
        )

        # Verify similarity_search was called with status parameter
        call_args = mock_crud_issue_embed.similarity_search.call_args
        assert call_args[1]["status"] == "open"
