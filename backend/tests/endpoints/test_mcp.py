"""
Tests for the MCP API endpoints.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import patch, AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from preloop.api.endpoints import mcp
from preloop.api.endpoints.search import SearchResponse as ApiSearchResponse
from preloop.schemas.issue import IssueResponse
from preloop.schemas.mcp import (
    SuggestedUpdate,
    UpdateIssueRequest,
    GetIssueResponse,
)
from preloop.models.models import (
    Organization,
    Project,
    Issue,
    Tracker,
    EmbeddingModel,
)
from preloop.models.models.user import User


@pytest.mark.asyncio
async def test_mcp_get_issue_success(db_session: Session, test_user: User):
    """
    Tests successful retrieval of an issue via the MCP get_issue tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    issue = Issue(
        title="Test Issue",
        description="A test issue.",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id="123",
        key="TP-1",
        meta_data={
            "labels": [
                {"id": 1, "title": "feature"},
                {"id": 2, "name": "gitlab"},
                "plain-label",
            ],
            "assignee": {"username": "alice"},
        },
    )
    db_session.add(issue)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        response = await mcp.get_issue(issue=str(issue.id))

    # response.id is a UUID object, need to compare with issue.id directly
    assert response.id == issue.id
    assert response.key == "TP-1"
    assert response.title == "Test Issue"
    assert response.project == "test-proj"
    assert response.organization == "test-org"
    assert response.labels == ["feature", "gitlab", "plain-label"]
    assert response.assignee == "alice"


@pytest.mark.asyncio
async def test_mcp_create_issue_success(db_session: Session, test_user: User):
    """
    Tests successful creation of an issue via the MCP create_issue tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch(
            "preloop.api.endpoints.mcp.api_create_issue", new_callable=AsyncMock
        ) as mock_api_create,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])
        mock_api_create.return_value = IssueResponse(
            id=str(uuid.uuid4()),
            external_id="ext-123",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            url="http://example.com/issue/1",
            key="TP-1",
            title="New Issue",
            project="test-proj",
            project_id=str(project.id),
            organization="test-org",
        )

        response = await mcp.create_issue(
            project="test-proj",
            title="New Issue",
            description="A new test issue.",
            prevent_duplicates=False,
        )

    assert response.status == "created"
    assert response.message == "Successfully created new issue."
    mock_api_create.assert_called_once()


@pytest.mark.asyncio
async def test_mcp_update_issue_success(db_session: Session, test_user: User):
    """
    Tests successful update of an issue via the MCP update_issue tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    issue = Issue(
        title="Original Title",
        description="Original description.",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id="456",
        key="TP-2",
        status="open",
    )
    db_session.add(issue)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client", new_callable=AsyncMock
        ) as mock_get_tracker,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])
        mock_tracker_client = AsyncMock()
        mock_get_tracker.return_value = mock_tracker_client

        response = await mcp.update_issue(issue=str(issue.id), title="Updated Title")

    assert isinstance(response, GetIssueResponse)
    assert response.title == "Updated Title"
    mock_tracker_client.update_issue.assert_called_once()
    db_session.refresh(issue)
    assert issue.title == "Updated Title"


@pytest.mark.asyncio
async def test_mcp_search_success(db_session: Session, test_user: User):
    """
    Tests the MCP search tool.
    """
    embedding_model = EmbeddingModel(
        name="test-embedding-model",
        provider="openai",
        version="text-embedding-ada-002",
        dimensions=1536,
        is_active=True,
    )
    db_session.add(embedding_model)
    db_session.commit()
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch(
            "preloop.api.endpoints.mcp.perform_search", new_callable=AsyncMock
        ) as mock_perform_search,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_perform_search.return_value = ApiSearchResponse(results=[])
        mock_get_db.return_value = iter([db_session])

        response = await mcp.search(query="test query", project="test-proj")

    assert isinstance(response, ApiSearchResponse)


@pytest.mark.asyncio
async def test_mcp_estimate_compliance_success(db_session: Session, test_user: User):
    """
    Tests the MCP estimate_compliance tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    issue = Issue(
        title="Test Issue",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id="789",
        key="TP-3",
    )
    db_session.add(issue)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request"),
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp._process_single_issue_estimate",
            new_callable=AsyncMock,
        ) as mock_process,
    ):
        mock_auth.return_value = (db_session, test_user)
        mock_process.return_value = MagicMock(
            success=True,
            data={
                "id": str(uuid.uuid4()),
                "compliance_factor": 0.8,
                "reason": "Looks good.",
                "issue_id": str(issue.id),
                "prompt_id": "test",
                "name": "Test Compliance",
                "suggestion": "No suggestion",
                "short_name": "test",
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
            },
        )

        response = await mcp.estimate_compliance(issues=[str(issue.id)])

    assert len(response.results) == 1
    assert response.results[0].compliance_factor == 0.8


@pytest.mark.asyncio
async def test_mcp_improve_compliance_success(db_session: Session, test_user: User):
    """
    Tests the MCP improve_compliance tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    issue = Issue(
        title="Test Issue",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id="101",
        key="TP-4",
    )
    db_session.add(issue)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request"),
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp._process_single_issue_compliance",
            new_callable=AsyncMock,
        ) as mock_process,
    ):
        mock_auth.return_value = (db_session, test_user)
        mock_process.return_value = MagicMock(
            success=True,
            data=SuggestedUpdate(
                issue_identifier=str(issue.id),
                arguments=UpdateIssueRequest(
                    issue=str(issue.id), title="Improved Title"
                ),
            ),
        )

        response = await mcp.improve_compliance(issues=[str(issue.id)])

    assert len(response.suggested_updates) == 1
    assert response.suggested_updates[0].arguments.title == "Improved Title"


@pytest.mark.asyncio
async def test_mcp_add_comment_success(db_session: Session, test_user: User):
    """
    Tests successful addition of a comment via the MCP add_comment tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org", identifier="test-org", tracker_id=tracker.id
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="test-proj",
        identifier="test-proj",
        slug="test-proj",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    issue = Issue(
        title="Test Issue",
        description="A test issue.",
        project_id=project.id,
        tracker_id=tracker.id,
        external_id="123",
        key="TP-1",
    )
    db_session.add(issue)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_comment = MagicMock()
        mock_comment.id = "comment-123"
        mock_comment.meta_data = {
            "url": "https://github.com/owner/repo/issues/1#comment-123"
        }
        mock_tracker.add_comment = AsyncMock(return_value=mock_comment)
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.add_comment(target=str(issue.id), comment="Test comment")

    assert response.status == "created"
    assert "successfully added comment" in response.message.lower()


@pytest.mark.asyncio
async def test_mcp_get_pull_request_success(db_session: Session, test_user: User):
    """
    Tests successful retrieval of a GitHub pull request via the MCP get_pull_request tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.client = MagicMock()
        mock_tracker.client.get_pull_request = AsyncMock(
            return_value={
                "id": "pr-123",
                "number": 123,
                "title": "Test PR",
                "description": "Test PR description",
                "state": "open",
                "author": "testuser",
                "url": "https://github.com/owner/repo/pull/123",
            }
        )
        mock_tracker.get_pull_request = mock_tracker.client.get_pull_request
        # Mock get_pull_request_comments as it's called when include_comments=True (default)
        mock_tracker.get_pull_request_comments = AsyncMock(return_value=[])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.get_pull_request(pull_request="owner/repo#123")

    assert response.number == 123
    assert response.title == "Test PR"
    assert response.state == "open"


@pytest.mark.asyncio
async def test_mcp_update_pull_request_success(db_session: Session, test_user: User):
    """
    Tests successful update of a GitHub pull request via the MCP update_pull_request tool.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.client = MagicMock()
        mock_tracker.client.update_pull_request = AsyncMock(
            return_value={
                "id": "pr-123",
                "number": 123,
                "title": "Updated PR Title",
                "description": "Updated PR description",
                "state": "open",
                "url": "https://github.com/owner/repo/pull/123",
            }
        )
        mock_tracker.update_pull_request = mock_tracker.client.update_pull_request
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="owner/repo#123", title="Updated PR Title"
        )

    assert response.status == "updated"
    assert response.pull_request_id == "pr-123"
    assert "Successfully performed" in response.message
    assert "metadata update" in response.message


# =============================================================================
# add_comment tests - inline comments, threaded replies, validation
# =============================================================================


@pytest.mark.asyncio
async def test_add_comment_inline_github_pr(db_session: Session, test_user: User):
    """
    Tests adding an inline comment to a GitHub PR at a specific file/line.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.submit_pull_request_review = AsyncMock(
            return_value={
                "id": "review-456",
                "html_url": "https://github.com/owner/repo/pull/123#pullrequestreview-456",
            }
        )
        mock_tracker.get_review_comments = AsyncMock(
            return_value=[
                {
                    "id": "comment-789",
                    "html_url": "https://github.com/owner/repo/pull/123#discussion_r789",
                }
            ]
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.add_comment(
            target="https://github.com/owner/repo/pull/123",
            comment="This line has a bug",
            path="src/main.py",
            line=42,
            side="RIGHT",
        )

    assert response.status == "created"
    assert "inline comment" in response.message.lower()
    assert response.comment_id == "comment-789"
    mock_tracker.submit_pull_request_review.assert_called_once()
    call_args = mock_tracker.submit_pull_request_review.call_args
    assert call_args.kwargs["event"] == "COMMENT"
    assert call_args.kwargs["comments"][0]["path"] == "src/main.py"
    assert call_args.kwargs["comments"][0]["line"] == 42


@pytest.mark.asyncio
async def test_add_comment_inline_gitlab_mr(db_session: Session, test_user: User):
    """
    Tests that inline comments on GitLab MRs degrade gracefully to a general
    discussion referencing the file/line (GlitchTip issue 3274).

    GitLab inline diff comments require position data (base_sha, start_sha, head_sha)
    which is not available through this API, so we fall back instead of erroring.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.create_mr_discussion = AsyncMock(return_value={"id": "disc-456"})
        mock_get_tracker.return_value = mock_tracker

        # GitLab inline comments (with path/line) fall back to a general
        # discussion that references the file/line in the body.
        response = await mcp.add_comment(
            target="https://gitlab.com/group/project/-/merge_requests/5",
            comment="Consider refactoring this",
            path="lib/utils.py",
            line=100,
        )

    assert response.status == "created"
    assert response.comment_id == "disc-456"
    assert "lib/utils.py:100" in response.message
    mock_tracker.create_mr_discussion.assert_called_once()
    call_kwargs = mock_tracker.create_mr_discussion.call_args.kwargs
    assert call_kwargs["mr_iid"] == "5"
    assert "**lib/utils.py:100**" in call_kwargs["body"]
    assert "Consider refactoring this" in call_kwargs["body"]


@pytest.mark.asyncio
async def test_add_comment_threaded_reply_github(db_session: Session, test_user: User):
    """
    Tests replying to an existing GitHub review comment (threaded reply).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.reply_to_review_comment = AsyncMock(
            return_value={
                "id": "reply-999",
                "html_url": "https://github.com/owner/repo/pull/123#discussion_r999",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.add_comment(
            target="owner/repo#123",
            comment="Good point, I'll fix this",
            in_reply_to="original-comment-123",
        )

    assert response.status == "created"
    assert "replied to comment" in response.message.lower()
    mock_tracker.reply_to_review_comment.assert_called_once_with(
        pr_number="123",
        comment_id="original-comment-123",
        body="Good point, I'll fix this",
    )


@pytest.mark.asyncio
async def test_add_comment_threaded_reply_gitlab(db_session: Session, test_user: User):
    """
    Tests replying to an existing GitLab MR discussion (threaded reply).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.reply_to_mr_discussion = AsyncMock(return_value={"id": "note-456"})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.add_comment(
            target="group/project#5",
            comment="Thanks for the feedback",
            in_reply_to="discussion-123",
        )

    assert response.status == "created"
    assert "replied to discussion" in response.message.lower()
    mock_tracker.reply_to_mr_discussion.assert_called_once_with(
        mr_iid="5",
        discussion_id="discussion-123",
        body="Thanks for the feedback",
    )


@pytest.mark.asyncio
async def test_add_comment_validation_path_without_line(
    db_session: Session, test_user: User
):
    """
    Tests that providing path without line raises a validation error.
    """
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.add_comment(
                target="owner/repo#123",
                comment="Test comment",
                path="src/main.py",  # path provided
                line=None,  # line NOT provided
            )

        assert exc_info.value.status_code == 400
        assert "both 'path' and 'line' must be provided together" in str(
            exc_info.value.detail
        )


@pytest.mark.asyncio
async def test_add_comment_validation_line_without_path(
    db_session: Session, test_user: User
):
    """
    Tests that providing line without path raises a validation error.
    """
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.add_comment(
                target="owner/repo#123",
                comment="Test comment",
                path=None,  # path NOT provided
                line=42,  # line provided
            )

        assert exc_info.value.status_code == 400
        assert "both 'path' and 'line' must be provided together" in str(
            exc_info.value.detail
        )


@pytest.mark.asyncio
async def test_add_comment_validation_invalid_side(
    db_session: Session, test_user: User
):
    """
    Tests that providing an invalid side parameter raises a validation error.
    Side validation only occurs for inline comments (when both path and line are provided).
    """
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.add_comment(
                target="owner/repo#123",
                comment="Test comment",
                path="src/main.py",  # Path is required for side validation
                line=42,  # Line is required for side validation
                side="INVALID",  # Invalid side parameter
            )

        assert exc_info.value.status_code == 400
        assert "Must be 'LEFT' or 'RIGHT'" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_add_comment_inline_with_left_side(db_session: Session, test_user: User):
    """
    Tests adding an inline comment on the LEFT side of a diff.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch(
            "preloop.api.endpoints.mcp.get_user_from_token_if_valid",
            new_callable=AsyncMock,
        ) as mock_get_user,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_user.return_value = test_user
        mock_get_db.return_value = iter([db_session])

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.submit_pull_request_review = AsyncMock(
            return_value={"id": "review-456", "html_url": "https://example.com"}
        )
        mock_tracker.get_review_comments = AsyncMock(return_value=[{"id": "c-123"}])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.add_comment(
            target="owner/repo#123",
            comment="This old code was correct",
            path="src/main.py",
            line=10,
            side="LEFT",
        )

    assert response.status == "created"
    call_args = mock_tracker.submit_pull_request_review.call_args
    assert call_args.kwargs["comments"][0]["side"] == "LEFT"


# =============================================================================
# update_comment tests - body updates, thread resolution, validation
# =============================================================================


@pytest.mark.asyncio
async def test_update_comment_body_github(db_session: Session, test_user: User):
    """
    Tests updating the body of a GitHub review comment.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.update_review_comment = AsyncMock(
            return_value={
                "id": "comment-123",
                "html_url": "https://github.com/owner/repo/pull/123#discussion_r123",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="comment-123",
            body="Updated comment text",
        )

    assert response.status == "updated"
    assert response.comment_id == "comment-123"
    assert "body updated" in response.message.lower()
    mock_tracker.update_review_comment.assert_called_once_with(
        comment_id="comment-123",
        body="Updated comment text",
    )


@pytest.mark.asyncio
async def test_update_comment_body_gitlab(db_session: Session, test_user: User):
    """
    Tests updating the body of a GitLab MR note.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.update_mr_note = AsyncMock(return_value={"id": "note-456"})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="https://gitlab.com/group/project/-/merge_requests/10",
            comment_id="note-456",
            body="Updated note text",
        )

    assert response.status == "updated"
    assert "body updated" in response.message.lower()
    mock_tracker.update_mr_note.assert_called_once_with(
        mr_iid="10",
        note_id="note-456",
        body="Updated note text",
    )


@pytest.mark.asyncio
async def test_update_comment_resolve_thread_github(
    db_session: Session, test_user: User
):
    """
    Tests resolving a GitHub review thread.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.resolve_review_thread = AsyncMock(return_value={})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="comment-123",
            resolved=True,
            thread_id="PRRT_kwDOCjXy1M5abc123",
        )

    assert response.status == "updated"
    assert "resolved" in response.message.lower()
    mock_tracker.resolve_review_thread.assert_called_once_with(
        thread_id="PRRT_kwDOCjXy1M5abc123",
        resolved=True,
    )


@pytest.mark.asyncio
async def test_update_comment_unresolve_thread_gitlab(
    db_session: Session, test_user: User
):
    """
    Tests unresolving a GitLab MR discussion.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.resolve_mr_discussion = AsyncMock(return_value={})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="group/project#10",
            comment_id="note-456",
            resolved=False,
            thread_id="discussion-789",
        )

    assert response.status == "updated"
    assert "unresolved" in response.message.lower()
    mock_tracker.resolve_mr_discussion.assert_called_once_with(
        mr_iid="10",
        discussion_id="discussion-789",
        resolved=False,
    )


@pytest.mark.asyncio
async def test_update_comment_body_and_resolve(db_session: Session, test_user: User):
    """
    Tests updating both body and resolving a thread in one call.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.update_review_comment = AsyncMock(
            return_value={"id": "comment-123", "html_url": "https://example.com"}
        )
        mock_tracker.resolve_review_thread = AsyncMock(return_value={})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="comment-123",
            body="Fixed the issue",
            resolved=True,
            thread_id="PRRT_thread123",
        )

    assert response.status == "updated"
    assert "body updated" in response.message.lower()
    assert "resolved" in response.message.lower()
    mock_tracker.update_review_comment.assert_called_once()
    mock_tracker.resolve_review_thread.assert_called_once()


@pytest.mark.asyncio
async def test_update_comment_validation_missing_body_and_resolved(
    db_session: Session, test_user: User
):
    """
    Tests that providing neither body nor resolved raises a validation error.
    """
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_comment(
                target="owner/repo#123",
                comment_id="comment-123",
                body=None,
                resolved=None,
            )

        assert exc_info.value.status_code == 400
        assert "body" in str(exc_info.value.detail)
        assert "resolved" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_update_comment_invalid_target_format(
    db_session: Session, test_user: User
):
    """
    Tests that an invalid target format raises a validation error.
    """
    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_comment(
                target="invalid-target-no-hash",
                comment_id="comment-123",
                body="Updated text",
            )

        assert exc_info.value.status_code == 400
        assert "Invalid target format" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_update_comment_issue_comment_explicit(
    db_session: Session, test_user: User
):
    """
    Tests updating an issue comment (PR conversation comment) with explicit comment_type.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.update_issue_comment = AsyncMock(
            return_value={
                "id": "issue-comment-456",
                "html_url": "https://github.com/owner/repo/pull/123#issuecomment-456",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="456",
            body="Updated issue comment text",
            comment_type="issue_comment",
        )

    assert response.status == "updated"
    assert "body updated" in response.message.lower()
    mock_tracker.update_issue_comment.assert_called_once_with(
        comment_id="456",
        body="Updated issue comment text",
    )


@pytest.mark.asyncio
async def test_update_comment_fallback_to_issue_comment(
    db_session: Session, test_user: User
):
    """
    Tests auto-detection fallback: when review_comment 404s, tries issue_comment.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        # First call (review_comment) raises 404
        mock_tracker.update_review_comment = AsyncMock(
            side_effect=Exception("404 Not Found")
        )
        # Second call (issue_comment) succeeds
        mock_tracker.update_issue_comment = AsyncMock(
            return_value={
                "id": "issue-comment-789",
                "html_url": "https://github.com/owner/repo/pull/123#issuecomment-789",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="789",
            body="Updated via fallback",
        )

    assert response.status == "updated"
    mock_tracker.update_review_comment.assert_called_once()
    mock_tracker.update_issue_comment.assert_called_once()


@pytest.mark.asyncio
async def test_update_comment_resolve_issue_comment_fails(
    db_session: Session, test_user: User
):
    """
    Tests that resolving an issue_comment returns 400 (not supported).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_comment(
                target="owner/repo#123",
                comment_id="456",
                resolved=True,
                comment_type="issue_comment",
            )

        assert exc_info.value.status_code == 400
        assert (
            "not supported for github issue comments"
            in str(exc_info.value.detail).lower()
        )


@pytest.mark.asyncio
async def test_update_comment_issue_comment_body_and_resolve_fails_upfront(
    db_session: Session, test_user: User
):
    """
    Tests that issue_comment + body + resolved fails upfront without partial update.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        # These should NOT be called - validation should fail upfront
        mock_tracker.update_issue_comment = AsyncMock()
        mock_tracker.update_review_comment = AsyncMock()
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_comment(
                target="owner/repo#123",
                comment_id="456",
                body="New body text",
                resolved=True,
                comment_type="issue_comment",
            )

        assert exc_info.value.status_code == 400
        assert (
            "not supported for github issue comments"
            in str(exc_info.value.detail).lower()
        )
        # Verify no update calls were made (failed upfront)
        mock_tracker.update_issue_comment.assert_not_called()
        mock_tracker.update_review_comment.assert_not_called()


@pytest.mark.asyncio
async def test_update_comment_autodetect_body_and_resolve_precheck_fails(
    db_session: Session, test_user: User
):
    """
    Tests that auto-detect mode with body + resolved pre-checks the comment type
    and fails upfront if it's an issue comment (avoids partial success).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
        patch("httpx.AsyncClient") as mock_httpx,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.connection_details = {"owner": "testowner", "repo": "testrepo"}
        mock_tracker._get_auth_headers = AsyncMock(
            return_value={"Authorization": "Bearer token"}
        )
        # These should NOT be called - pre-check should fail upfront
        mock_tracker.update_issue_comment = AsyncMock()
        mock_tracker.update_review_comment = AsyncMock()
        mock_get_tracker.return_value = mock_tracker

        # Mock httpx to return 404 (comment is an issue comment)
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_httpx.return_value = mock_client

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_comment(
                target="owner/repo#123",
                comment_id="456",
                body="New body text",
                resolved=True,
                # comment_type not specified - auto-detect mode
            )

        assert exc_info.value.status_code == 400
        assert (
            "not supported for github issue comments"
            in str(exc_info.value.detail).lower()
        )
        # Verify no update calls were made (failed upfront via pre-check)
        mock_tracker.update_issue_comment.assert_not_called()
        mock_tracker.update_review_comment.assert_not_called()


@pytest.mark.asyncio
async def test_update_comment_not_found_maps_to_404_with_warning_log(
    db_session: Session, test_user: User, caplog
):
    """
    Tests that an expected not-found failure (e.g. GraphQL node could not be
    resolved because the id is stale/wrong) maps to HTTP 404 and logs at
    warning level without exc_info (no GlitchTip exception event).
    """
    import logging

    from preloop.sync.exceptions import TrackerResponseError

    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.resolve_review_thread = AsyncMock(
            side_effect=TrackerResponseError(
                "GraphQL errors: Could not resolve to a node with the "
                "global id of '3755981585'"
            )
        )
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with caplog.at_level(logging.WARNING):
            with pytest.raises(HTTPException) as exc_info:
                await mcp.update_comment(
                    target="owner/repo#123",
                    comment_id="3755981585",
                    resolved=True,
                    thread_id="PRRT_kwDOCjXy1M5abc123",
                )

    assert exc_info.value.status_code == 404
    assert "not found" in str(exc_info.value.detail).lower()

    warning_records = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "not found" in r.getMessage().lower()
    ]
    assert warning_records, "expected a warning-level log for the not-found case"
    for record in warning_records:
        assert record.exc_info in (None, (None, None, None)), (
            "not-found failures must not log with exc_info"
        )
    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "updating comment" in r.getMessage()
    ]
    assert not error_records, "not-found failures must not be logged as errors"


@pytest.mark.asyncio
async def test_update_comment_genuine_api_failure_maps_to_502_with_error_log(
    db_session: Session, test_user: User, caplog
):
    """
    Tests that a genuine tracker/API failure still maps to HTTP 502 and is
    logged as an error with exc_info.
    """
    import logging

    from preloop.sync.exceptions import TrackerResponseError

    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.resolve_review_thread = AsyncMock(
            side_effect=TrackerResponseError(
                "GitHub GraphQL API error: 500 - internal server error"
            )
        )
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with caplog.at_level(logging.ERROR):
            with pytest.raises(HTTPException) as exc_info:
                await mcp.update_comment(
                    target="owner/repo#123",
                    comment_id="comment-123",
                    resolved=True,
                    thread_id="PRRT_kwDOCjXy1M5abc123",
                )

    assert exc_info.value.status_code == 502
    error_records = [
        r
        for r in caplog.records
        if r.levelno >= logging.ERROR and "Error updating comment" in r.getMessage()
    ]
    assert error_records, "genuine API failures must be logged as errors"
    assert any(r.exc_info for r in error_records), (
        "error-level logs should include exc_info"
    )


@pytest.mark.asyncio
async def test_update_comment_resolve_numeric_comment_id_maps_to_thread_node_id(
    db_session: Session, test_user: User
):
    """
    Tests that resolving with a numeric REST comment id looks up and uses the
    containing thread's GraphQL node id instead of passing the numeric id to
    the GraphQL node lookup (issue #220).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.get_thread_id_for_comment = AsyncMock(
            return_value="PRRT_kwDOCjXy1M5abc123"
        )
        mock_tracker.resolve_review_thread = AsyncMock(return_value={})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="3755981585",
            resolved=True,
        )

    assert response.status == "updated"
    mock_tracker.get_thread_id_for_comment.assert_called_once_with(
        pr_number="123",
        comment_id="3755981585",
    )
    mock_tracker.resolve_review_thread.assert_called_once_with(
        thread_id="PRRT_kwDOCjXy1M5abc123",
        resolved=True,
    )


@pytest.mark.asyncio
async def test_update_comment_resolve_numeric_thread_id_is_mapped_via_lookup(
    db_session: Session, test_user: User
):
    """
    Tests that a non-PRRT_ thread_id (e.g. a numeric REST id mistakenly passed
    as thread_id) is mapped through the thread lookup instead of being fed to
    the GraphQL node lookup (issue #220).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.get_thread_id_for_comment = AsyncMock(
            return_value="PRRT_kwDOCjXy1M5abc123"
        )
        mock_tracker.resolve_review_thread = AsyncMock(return_value={})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_comment(
            target="owner/repo#123",
            comment_id="3755981585",
            resolved=True,
            thread_id="3755981585",
        )

    assert response.status == "updated"
    mock_tracker.get_thread_id_for_comment.assert_called_once_with(
        pr_number="123",
        comment_id="3755981585",
    )
    mock_tracker.resolve_review_thread.assert_called_once_with(
        thread_id="PRRT_kwDOCjXy1M5abc123",
        resolved=True,
    )


# =============================================================================
# get_pull_request tests - include_comments, include_diff flags
# =============================================================================


@pytest.mark.asyncio
async def test_get_pull_request_without_comments(db_session: Session, test_user: User):
    """
    Tests getting a PR with include_comments=False.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.get_pull_request = AsyncMock(
            return_value={
                "id": "pr-123",
                "number": 123,
                "title": "Test PR",
                "state": "open",
                "url": "https://github.com/owner/repo/pull/123",
            }
        )
        mock_tracker.get_pull_request_comments = AsyncMock(return_value=[])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.get_pull_request(
            pull_request="owner/repo#123",
            include_comments=False,
        )

    assert response.number == 123
    # get_pull_request_comments should NOT be called
    mock_tracker.get_pull_request_comments.assert_not_called()


@pytest.mark.asyncio
async def test_get_pull_request_without_diff(db_session: Session, test_user: User):
    """
    Tests getting a PR with include_diff=False removes changes from response.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.get_pull_request = AsyncMock(
            return_value={
                "id": "pr-123",
                "number": 123,
                "title": "Test PR",
                "state": "open",
                "url": "https://github.com/owner/repo/pull/123",
                "changes": [{"file": "main.py", "additions": 10}],
            }
        )
        mock_tracker.get_pull_request_comments = AsyncMock(return_value=[])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.get_pull_request(
            pull_request="owner/repo#123",
            include_diff=False,
        )

    assert response.number == 123
    assert response.changes is None


@pytest.mark.asyncio
async def test_get_pull_request_gitlab_mr(db_session: Session, test_user: User):
    """
    Tests getting a GitLab merge request via URL.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.get_merge_request = AsyncMock(
            return_value={
                "id": "999",
                "iid": 5,
                "title": "Test MR",
                "description": "MR description",
                "state": "opened",
                "author": "testuser",
                "url": "https://gitlab.com/group/project/-/merge_requests/5",
                "source_branch": "feature",
                "target_branch": "main",
            }
        )
        mock_tracker.get_mr_discussions = AsyncMock(return_value=[])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.get_pull_request(
            pull_request="https://gitlab.com/group/project/-/merge_requests/5"
        )

    assert response.number == 5
    assert response.title == "Test MR"
    assert response.state == "opened"
    mock_tracker.get_merge_request.assert_called_once_with("5")


@pytest.mark.asyncio
async def test_get_pull_request_from_github_url(db_session: Session, test_user: User):
    """
    Tests getting a PR via full GitHub URL.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.get_pull_request = AsyncMock(
            return_value={
                "id": "pr-456",
                "number": 456,
                "title": "Feature PR",
                "state": "open",
                "url": "https://github.com/owner/repo/pull/456",
            }
        )
        mock_tracker.get_pull_request_comments = AsyncMock(return_value=[])
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.get_pull_request(
            pull_request="https://github.com/owner/repo/pull/456"
        )

    assert response.number == 456
    mock_tracker.get_pull_request.assert_called_once_with("456")


# =============================================================================
# update_pull_request tests - review actions, validation
# =============================================================================


@pytest.mark.asyncio
async def test_update_pull_request_approve_github(db_session: Session, test_user: User):
    """
    Tests approving a GitHub PR via review_action.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.submit_pull_request_review = AsyncMock(
            return_value={
                "id": "review-789",
                "html_url": "https://github.com/owner/repo/pull/123#pullrequestreview-789",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="owner/repo#123",
            review_action="approve",
            review_body="LGTM!",
        )

    assert response.status == "updated"
    assert "review" in response.message.lower()
    mock_tracker.submit_pull_request_review.assert_called_once()
    call_args = mock_tracker.submit_pull_request_review.call_args
    assert call_args.kwargs["event"] == "APPROVE"
    assert call_args.kwargs["body"] == "LGTM!"


@pytest.mark.asyncio
async def test_update_pull_request_request_changes_github(
    db_session: Session, test_user: User
):
    """
    Tests requesting changes on a GitHub PR.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.submit_pull_request_review = AsyncMock(
            return_value={"id": "review-999", "html_url": "https://example.com"}
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="owner/repo#123",
            review_action="request_changes",
            review_body="Please fix the security issue",
        )

    assert response.status == "updated"
    call_args = mock_tracker.submit_pull_request_review.call_args
    assert call_args.kwargs["event"] == "REQUEST_CHANGES"


@pytest.mark.asyncio
async def test_update_pull_request_comment_review_github(
    db_session: Session, test_user: User
):
    """
    Tests adding a comment review on GitHub with inline comments.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_tracker.submit_pull_request_review = AsyncMock(
            return_value={"id": "review-111", "html_url": "https://example.com"}
        )
        mock_get_tracker.return_value = mock_tracker

        inline_comments = [
            {"path": "src/main.py", "line": 10, "body": "Consider using async here"},
            {"path": "src/utils.py", "line": 25, "body": "This could be simplified"},
        ]

        response = await mcp.update_pull_request(
            pull_request="owner/repo#123",
            review_action="comment",
            review_body="A few suggestions",
            review_comments=inline_comments,
        )

    assert response.status == "updated"
    call_args = mock_tracker.submit_pull_request_review.call_args
    assert call_args.kwargs["event"] == "COMMENT"
    assert call_args.kwargs["comments"] == inline_comments


@pytest.mark.asyncio
async def test_update_pull_request_comment_without_content_github(
    db_session: Session, test_user: User
):
    """
    Tests that comment review without body or comments raises validation error.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_pull_request(
                pull_request="owner/repo#123",
                review_action="comment",
                # No review_body and no review_comments
            )

        assert exc_info.value.status_code == 400
        assert "review_body" in str(exc_info.value.detail)
        assert "review_comments" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_update_pull_request_invalid_review_action(
    db_session: Session, test_user: User
):
    """
    Tests that an invalid review_action raises validation error.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_pull_request(
                pull_request="owner/repo#123",
                review_action="invalid_action",
            )

        assert exc_info.value.status_code == 400
        assert "Invalid review_action" in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_update_pull_request_approve_gitlab(db_session: Session, test_user: User):
    """
    Tests approving a GitLab MR.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.approve_merge_request = AsyncMock(return_value={"id": 123})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="group/project#10",
            review_action="approve",
        )

    assert response.status == "updated"
    mock_tracker.approve_merge_request.assert_called_once_with("10")


@pytest.mark.asyncio
async def test_update_pull_request_request_changes_gitlab(
    db_session: Session, test_user: User
):
    """
    Tests requesting changes on a GitLab MR (unapprove + comment).
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.unapprove_merge_request = AsyncMock(return_value={})
        mock_tracker.create_mr_discussion = AsyncMock(return_value={"id": "disc-123"})
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="group/project#10",
            review_action="request_changes",
            review_body="Please fix these issues",
        )

    assert response.status == "updated"
    mock_tracker.unapprove_merge_request.assert_called_once_with("10")
    mock_tracker.create_mr_discussion.assert_called_once()
    call_body = mock_tracker.create_mr_discussion.call_args.kwargs["body"]
    assert "Changes Requested" in call_body


@pytest.mark.asyncio
async def test_update_pull_request_gitlab_assignee_warning(
    db_session: Session, test_user: User
):
    """
    Tests that GitLab MR update with assignees shows a warning about user IDs.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.update_merge_request = AsyncMock(
            return_value={
                "id": 999,
                "url": "https://gitlab.com/group/project/-/merge_requests/10",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="group/project#10",
            title="Updated Title",
            assignees=["username1", "username2"],  # Will trigger warning
        )

    assert response.status == "updated"
    assert "assignees not applied" in response.message.lower()


@pytest.mark.asyncio
async def test_update_pull_request_gitlab_empty_list_clears_assignees_reviewers(
    db_session: Session, test_user: User
):
    """
    Tests that passing empty lists for assignees/reviewers clears them on GitLab MRs.
    This is distinct from passing None (which means "don't change").
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_tracker.update_merge_request = AsyncMock(
            return_value={
                "id": 999,
                "url": "https://gitlab.com/group/project/-/merge_requests/10",
            }
        )
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="group/project#10",
            title="Updated Title",
            assignees=[],  # Empty list should clear assignees
            reviewers=[],  # Empty list should clear reviewers
        )

    assert response.status == "updated"

    # Verify that update_merge_request was called with empty lists (not None)
    mock_tracker.update_merge_request.assert_called_once()
    call_kwargs = mock_tracker.update_merge_request.call_args.kwargs

    # Empty lists should be passed through, not converted to None
    assert call_kwargs.get("assignee_ids") == [], (
        "Empty assignees list should pass empty list to update_merge_request"
    )
    assert call_kwargs.get("reviewer_ids") == [], (
        "Empty reviewers list should pass empty list to update_merge_request"
    )


@pytest.mark.asyncio
async def test_update_pull_request_comment_without_content_gitlab(
    db_session: Session, test_user: User
):
    """
    Tests that GitLab comment review without body or comments raises validation error.
    """
    tracker = Tracker(
        name="test-tracker",
        account_id=test_user.account_id,
        tracker_type="gitlab",
        api_key="test_key",
        url="https://gitlab.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org",
        identifier="test-org",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="group/project",
        identifier="group/project",
        slug="group/project",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "gitlab"
        mock_get_tracker.return_value = mock_tracker

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc_info:
            await mcp.update_pull_request(
                pull_request="group/project#10",
                review_action="comment",
                # No review_body and no review_comments
            )

        assert exc_info.value.status_code == 400
        assert "review_body" in str(exc_info.value.detail)


# =============================================================================
# Helper function tests - _detect_platform_from_url
# =============================================================================


def test_detect_platform_from_github_url():
    """Tests GitHub URL detection."""
    assert mcp._detect_platform_from_url("https://github.com/owner/repo") == "github"
    assert (
        mcp._detect_platform_from_url("https://github.com/owner/repo/pull/123")
        == "github"
    )


def test_detect_platform_from_gitlab_url():
    """Tests GitLab URL detection."""
    assert mcp._detect_platform_from_url("https://gitlab.com/group/project") == "gitlab"
    assert (
        mcp._detect_platform_from_url(
            "https://gitlab.com/group/project/-/merge_requests/5"
        )
        == "gitlab"
    )
    assert (
        mcp._detect_platform_from_url("https://gitlab.mycompany.com/group/project")
        == "gitlab"
    )


def test_detect_platform_from_url_with_patterns():
    """Tests platform detection via URL path patterns."""
    # PR path pattern -> GitHub
    assert (
        mcp._detect_platform_from_url("https://example.com/owner/repo/pull/1")
        == "github"
    )
    # MR path pattern -> GitLab
    assert (
        mcp._detect_platform_from_url(
            "https://example.com/group/project/-/merge_requests/1"
        )
        == "gitlab"
    )


def test_detect_platform_from_url_unknown():
    """Tests that unknown URLs raise ValueError."""
    with pytest.raises(ValueError) as exc_info:
        mcp._detect_platform_from_url("https://example.com/unknown/path")
    assert "Cannot determine platform" in str(exc_info.value)


@pytest.mark.asyncio
async def test_update_pull_request_remove_absent_reaction_is_success(
    db_session: Session, test_user: User
):
    """Removing a reaction that is already gone must report success.

    Regression: the tool reported "FAILED: remove reaction (eyes)" when the
    reaction was not present. Agents then retried the identical call until the
    orchestrator's repeated-MCP-tool-loop guard killed the whole execution,
    after the review had already been posted.
    """
    tracker = Tracker(
        name="test-tracker-absent-reaction",
        account_id=test_user.account_id,
        tracker_type="github",
        api_key="test_key",
        url="https://github.com",
    )
    db_session.add(tracker)
    db_session.commit()

    organization = Organization(
        name="test-org-absent-reaction",
        identifier="test-org-absent-reaction",
        tracker_id=tracker.id,
    )
    db_session.add(organization)
    db_session.commit()

    project = Project(
        name="owner/repo",
        identifier="owner/repo",
        slug="owner/repo",
        organization_id=organization.id,
    )
    db_session.add(project)
    db_session.commit()

    with (
        patch("preloop.api.endpoints.mcp.get_http_request") as mock_get_request,
        patch("preloop.api.endpoints.mcp.get_db") as mock_get_db,
        patch(
            "preloop.api.endpoints.mcp._get_authenticated_user",
            new_callable=AsyncMock,
        ) as mock_auth,
        patch(
            "preloop.api.endpoints.mcp.get_tracker_client",
            new_callable=AsyncMock,
        ) as mock_get_tracker,
    ):
        mock_get_request.return_value.headers = {"authorization": "Bearer testtoken"}
        mock_get_db.return_value = iter([db_session])
        mock_auth.return_value = (db_session, test_user)

        mock_tracker = MagicMock()
        mock_tracker.tracker_type = "github"
        # False == "no such reaction found to remove"
        mock_tracker.remove_issue_reaction = AsyncMock(return_value=False)
        mock_get_tracker.return_value = mock_tracker

        response = await mcp.update_pull_request(
            pull_request="owner/repo#123",
            remove_reaction="eyes",
        )

    assert response.status == "updated"
    assert "FAILED" not in response.message
    assert "already absent" in response.message
