import uuid
from datetime import datetime, UTC

import httpx
import openai
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException

from preloop.api.endpoints import comments
from preloop.schemas.comment import CommentCreate
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.upstream_errors import ERROR_CLASS_UPSTREAM_RATE_LIMITED


def _openai_rate_limit_error(retry_after: str = "3") -> openai.RateLimitError:
    request = httpx.Request("POST", "https://example.com/v1/embeddings")
    response = httpx.Response(
        429, headers={"retry-after": retry_after}, request=request
    )
    return openai.RateLimitError(
        "Error code: 429 - Too Many Requests", response=response, body=None
    )


@pytest.fixture
def mock_user():
    """Create a mock user."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = str(uuid.uuid4())
    return user


@pytest.fixture
def mock_tracker():
    """Create a mock tracker."""
    tracker = MagicMock()
    tracker.id = str(uuid.uuid4())
    return tracker


@pytest.fixture
def mock_issue(mock_tracker):
    """Create a mock issue."""
    issue = MagicMock()
    issue.id = str(uuid.uuid4())
    issue.project_id = str(uuid.uuid4())
    issue.tracker_id = mock_tracker.id
    return issue


@pytest.fixture
def mock_project():
    """Create a mock project."""
    project = MagicMock()
    project.id = str(uuid.uuid4())
    project.organization_id = str(uuid.uuid4())
    return project


@pytest.fixture
def mock_comment(mock_issue):
    """Create a mock comment."""
    comment = MagicMock()
    comment.id = str(uuid.uuid4())
    comment.issue_id = mock_issue.id
    comment.body = "Test comment"
    comment.author = "testuser"
    comment.created_at = datetime.now(UTC)
    comment.updated_at = datetime.now(UTC)
    return comment


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_list_issue_comments_success(mock_get_tracker_client):
    """
    Tests the list_issue_comments function for a successful request.
    """
    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = MagicMock()
    tracker_client.get_comments.return_value = []
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()

    result = await comments.list_issue_comments(
        "123",
        "org",
        "proj",
        limit=10,
        offset=0,
        db=db_session,
        current_user=current_user,
    )
    assert result.total == 0


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_list_issue_comments_with_data(mock_get_tracker_client):
    """Test listing comments with actual comment data."""
    mock_comment = MagicMock()
    mock_comment.id = "comment-1"
    mock_comment.author = "testuser"
    mock_comment.body = "Test comment body"
    mock_comment.created_at = datetime.now(UTC)
    mock_comment.updated_at = datetime.now(UTC)
    mock_comment.metadata = {}

    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = MagicMock()
    tracker_client.get_comments.return_value = [mock_comment]
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()

    result = await comments.list_issue_comments(
        "123",
        "org",
        "proj",
        limit=10,
        offset=0,
        db=db_session,
        current_user=current_user,
    )
    assert result.total == 1
    assert len(result.items) == 1
    assert result.items[0].body == "Test comment body"


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_list_issue_comments_issue_not_found(mock_get_tracker_client):
    """Test 404 when issue is not found."""
    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = None
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await comments.list_issue_comments(
            "non-existent",
            "org",
            "proj",
            limit=10,
            offset=0,
            db=db_session,
            current_user=current_user,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Issue not found"


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_list_issue_comments_pagination(mock_get_tracker_client):
    """Test pagination in list_issue_comments."""
    mock_comments = [
        MagicMock(
            id=f"comment-{i}",
            author="testuser",
            body=f"Comment {i}",
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            metadata={},
        )
        for i in range(5)
    ]

    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = MagicMock()
    tracker_client.get_comments.return_value = mock_comments
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()

    # Test with offset
    result = await comments.list_issue_comments(
        "123",
        "org",
        "proj",
        limit=2,
        offset=2,
        db=db_session,
        current_user=current_user,
    )
    assert result.total == 5
    assert len(result.items) == 2


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_list_issue_comments_error(mock_get_tracker_client):
    """Test error handling in list_issue_comments."""
    tracker_client = AsyncMock()
    tracker_client.get_issue.side_effect = Exception("Connection error")
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()

    with pytest.raises(HTTPException) as exc_info:
        await comments.list_issue_comments(
            "123",
            "org",
            "proj",
            limit=10,
            offset=0,
            db=db_session,
            current_user=current_user,
        )

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_add_issue_comment_success(mock_get_tracker_client):
    """
    Tests the add_issue_comment function for a successful request.
    """
    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = MagicMock()
    tracker_client.add_comment.return_value = MagicMock(
        id="456",
        author="test",
        body="Test comment",
        created_at="2025-09-22T00:19:39.601Z",
        updated_at="2025-09-22T00:19:39.601Z",
        meta_data={},
    )
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()
    comment_create = CommentCreate(body="Test comment")

    result = await comments.add_issue_comment(
        "123", comment_create, "org", "proj", db=db_session, current_user=current_user
    )
    assert result.id == "456"


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_add_issue_comment_issue_not_found(mock_get_tracker_client):
    """Test 404 when issue is not found."""
    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = None
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()
    comment_create = CommentCreate(body="Test comment")

    with pytest.raises(HTTPException) as exc_info:
        await comments.add_issue_comment(
            "non-existent",
            comment_create,
            "org",
            "proj",
            db=db_session,
            current_user=current_user,
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.get_tracker_client")
async def test_add_issue_comment_error(mock_get_tracker_client):
    """Test error handling in add_issue_comment."""
    tracker_client = AsyncMock()
    tracker_client.get_issue.return_value = MagicMock()
    tracker_client.add_comment.side_effect = Exception("Failed to add comment")
    mock_get_tracker_client.return_value = tracker_client

    db_session = MagicMock()
    current_user = MagicMock()
    comment_create = CommentCreate(body="Test comment")

    with pytest.raises(HTTPException) as exc_info:
        await comments.add_issue_comment(
            "123",
            comment_create,
            "org",
            "proj",
            db=db_session,
            current_user=current_user,
        )

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.crud_comment.get_multi_by_issue")
async def test_search_comments_by_issue_id(mock_get_multi_by_issue):
    """
    Tests the search_comments function when searching by issue_id.
    """
    mock_get_multi_by_issue.return_value = []
    db_session = MagicMock()
    current_user = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[MagicMock()],
    ):
        result = await comments.search_comments(
            db=db_session,
            current_user=current_user,
            issue_id="123",
            search_type="fulltext",
            query="",
            author=None,
        )
        assert result.total == 0
        mock_get_multi_by_issue.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.api.endpoints.comments.crud_comment.get_multi_by_author")
async def test_search_comments_by_author(mock_get_multi_by_author):
    """
    Tests the search_comments function when searching by author.
    """
    mock_get_multi_by_author.return_value = []
    db_session = MagicMock()
    current_user = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[MagicMock()],
    ):
        result = await comments.search_comments(
            db=db_session,
            current_user=current_user,
            author="test",
            search_type="fulltext",
            query="",
            issue_id=None,
        )
        assert result.total == 0
        mock_get_multi_by_author.assert_called_once()


@pytest.mark.asyncio
async def test_search_comments_no_trackers(mock_user):
    """Test search returns empty when user has no trackers."""
    db_session = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[],
    ):
        result = await comments.search_comments(
            db=db_session,
            current_user=mock_user,
            query="test",
            search_type="fulltext",
        )

        assert result.total == 0
        assert result.items == []


@pytest.mark.asyncio
async def test_search_comments_similarity_no_model(mock_user, mock_tracker):
    """Test similarity search fails when no embedding model is configured."""
    db_session = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[],
        ):
            with pytest.raises(HTTPException) as exc_info:
                await comments.search_comments(
                    db=db_session,
                    current_user=mock_user,
                    query="test",
                    search_type="similarity",
                )

            assert exc_info.value.status_code == 500
            assert "No active embedding model" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_comments_similarity_success(
    mock_user, mock_tracker, mock_issue, mock_project, mock_comment
):
    """Test successful similarity search."""
    db_session = MagicMock()
    db_session.get.side_effect = lambda model, id: {
        mock_issue.id: mock_issue,
        mock_project.id: mock_project,
    }.get(id)

    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())

    # Update mock_issue to match tracker
    mock_issue.tracker_id = mock_tracker.id
    mock_issue.project_id = mock_project.id

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                return_value=[0.1] * 1536,
            ):
                with patch(
                    "preloop.api.endpoints.comments.crud_issue_embedding.similarity_search",
                    return_value=[(mock_comment, 0.95)],
                ):
                    result = await comments.search_comments(
                        db=db_session,
                        current_user=mock_user,
                        query="test query",
                        search_type="similarity",
                    )

                    assert result.total == 1


@pytest.mark.asyncio
async def test_search_comments_invalid_search_type(mock_user, mock_tracker):
    """Test error for invalid search type."""
    db_session = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with pytest.raises(HTTPException) as exc_info:
            await comments.search_comments(
                db=db_session,
                current_user=mock_user,
                query="test",
                search_type="invalid",
            )

        assert exc_info.value.status_code == 400
        assert "Invalid search_type" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_comments_fulltext_no_filters(mock_user, mock_tracker):
    """Test fulltext search returns empty when no filters are provided."""
    db_session = MagicMock()

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        result = await comments.search_comments(
            db=db_session,
            current_user=mock_user,
            query="test",
            search_type="fulltext",
            author=None,
            issue_id=None,
        )

        assert result.total == 0
        assert result.items == []


@pytest.mark.asyncio
async def test_search_comments_similarity_vector_error(mock_user, mock_tracker):
    """Test error handling when vector generation fails."""
    db_session = MagicMock()

    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                side_effect=Exception("Vector generation failed"),
            ):
                with pytest.raises(HTTPException) as exc_info:
                    await comments.search_comments(
                        db=db_session,
                        current_user=mock_user,
                        query="test query",
                        search_type="similarity",
                    )

                assert exc_info.value.status_code == 500
                assert "Error generating query vector" in exc_info.value.detail


@pytest.mark.asyncio
async def test_search_comments_fulltext_with_results(
    mock_user, mock_tracker, mock_issue, mock_project, mock_comment
):
    """Test fulltext search with actual results."""
    db_session = MagicMock()
    db_session.get.side_effect = lambda model, id: {
        mock_issue.id: mock_issue,
        mock_project.id: mock_project,
    }.get(id)

    mock_issue.tracker_id = mock_tracker.id
    mock_issue.project_id = mock_project.id

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_comment.get_multi_by_author",
            return_value=[mock_comment],
        ):
            result = await comments.search_comments(
                db=db_session,
                current_user=mock_user,
                author="testuser",
                search_type="fulltext",
                query="",  # Must pass a string, not use default Query() object
            )

            assert result.total == 1
            assert len(result.items) == 1


# --- Transient 429 handling on the similarity-search query embedding (#269) --


@pytest.mark.asyncio
async def test_search_comments_similarity_embedding_429_is_retried(
    mock_user: MagicMock,
    mock_tracker: MagicMock,
) -> None:
    """One provider 429 during query embedding must not fail the search."""
    db_session = MagicMock()
    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())
    mock_model.provider = "openai"

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                side_effect=[_openai_rate_limit_error(), [0.1] * 1536],
            ) as mock_vector:
                with patch(
                    "preloop.api.endpoints.comments.crud_issue_embedding.similarity_search",
                    return_value=[],
                ):
                    with patch(
                        "preloop.services.aux_model_retry.asyncio.sleep",
                        new=AsyncMock(return_value=None),
                    ):
                        result = await comments.search_comments(
                            db=db_session,
                            current_user=mock_user,
                            query="test query",
                            search_type="similarity",
                        )

    assert result.total == 0
    assert mock_vector.call_count == 2


@pytest.mark.asyncio
async def test_search_comments_similarity_embedding_429_surfaces_retry_after(
    mock_user: MagicMock,
    mock_tracker: MagicMock,
) -> None:
    """An exhausted transient 429 surfaces as 429 with a Retry-After header."""
    db_session = MagicMock()
    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())
    mock_model.provider = "openai"

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                side_effect=_openai_rate_limit_error(retry_after="3"),
            ) as mock_vector:
                with patch(
                    "preloop.services.aux_model_retry.asyncio.sleep",
                    new=AsyncMock(return_value=None),
                ):
                    with pytest.raises(HTTPException) as exc_info:
                        await comments.search_comments(
                            db=db_session,
                            current_user=mock_user,
                            query="test query",
                            search_type="similarity",
                        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert exc_info.value.headers.get("Retry-After") == "3"
    assert mock_vector.call_count == 2


@pytest.mark.asyncio
async def test_search_comments_similarity_embedding_non_retryable_fails_fast(
    mock_user: MagicMock,
    mock_tracker: MagicMock,
) -> None:
    """A non-transient embedding failure is not retried and stays a 500."""
    db_session = MagicMock()
    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())
    mock_model.provider = "openai"

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                side_effect=ValueError("Unsupported provider: fake"),
            ) as mock_vector:
                with pytest.raises(HTTPException) as exc_info:
                    await comments.search_comments(
                        db=db_session,
                        current_user=mock_user,
                        query="test query",
                        search_type="similarity",
                    )

    assert exc_info.value.status_code == 500
    assert mock_vector.call_count == 1


@pytest.mark.asyncio
async def test_search_comments_similarity_gateway_rate_limit_surfaces_429(
    mock_user: MagicMock,
    mock_tracker: MagicMock,
) -> None:
    """A classified ModelGatewayAPIError maps to its own status + headers."""
    db_session = MagicMock()
    mock_model = MagicMock()
    mock_model.id = str(uuid.uuid4())
    mock_model.provider = "openai"
    gateway_error = ModelGatewayAPIError(
        provider="openai",
        status_code=429,
        message="rate limited",
        error_class=ERROR_CLASS_UPSTREAM_RATE_LIMITED,
        retry_after_seconds=2,
    )

    with patch(
        "preloop.api.endpoints.comments.crud_tracker.get_for_account",
        return_value=[mock_tracker],
    ):
        with patch(
            "preloop.api.endpoints.comments.crud_embedding_model.get_active",
            return_value=[mock_model],
        ):
            with patch(
                "preloop.api.endpoints.comments.crud_issue_embedding._generate_embedding_vector",
                side_effect=gateway_error,
            ):
                with patch(
                    "preloop.services.aux_model_retry.asyncio.sleep",
                    new=AsyncMock(return_value=None),
                ):
                    with pytest.raises(HTTPException) as exc_info:
                        await comments.search_comments(
                            db=db_session,
                            current_user=mock_user,
                            query="test query",
                            search_type="similarity",
                        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers is not None
    assert exc_info.value.headers.get("Retry-After") == "2"
