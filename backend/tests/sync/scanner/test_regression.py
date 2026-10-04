import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from sqlalchemy.orm import Session

from preloop.models.models import (
    Organization,
    Project,
    Tracker,
    Issue,
    Comment,
)
from preloop.sync.scanner.core import TrackerClient


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    return MagicMock(spec=Session)


@pytest.fixture
def mock_tracker():
    """Fixture for a mock tracker model instance."""
    tracker = MagicMock(spec=Tracker)
    tracker.id = 1
    tracker.tracker_type = "github"
    tracker.account_id = "acc_123"
    return tracker


@pytest.fixture
def mock_organization():
    """Fixture for a mock organization model instance."""
    org = MagicMock(spec=Organization)
    org.id = 101
    org.identifier = "test-org"
    return org


@pytest.fixture
def mock_project():
    """Fixture for a mock project model instance."""
    project = MagicMock(spec=Project)
    project.id = 1001
    project.identifier = "test-project"
    return project


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_issue")
@patch("preloop.sync.scanner.core.crud_comment")
@patch("preloop.sync.scanner.core.crud_issue_embedding")
async def test_scan_issues_handles_string_casting_and_identifier(
    mock_crud_embedding,
    mock_crud_comment,
    mock_crud_issue,
    mock_db_session,
    mock_tracker,
    mock_organization,
    mock_project,
):
    """
    Regression test: Ensures that organization/project identifiers are correctly handled
    as strings and that the `identifier` field is present in transformed project data.
    """
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        mock_internal_client = AsyncMock()
        # Simulate API response with integer IDs
        mock_internal_client.get_organizations.return_value = [
            {"id": 123, "name": "Test Org"}
        ]
        mock_internal_client.get_projects.return_value = [
            {"id": 456, "name": "Test Project"}
        ]
        mock_internal_client.get_issues.return_value = [
            {"id": "1", "updated_at": "2025-01-01T00:00:00Z"}
        ]

        # Mock the transform methods to ensure they behave as expected
        mock_internal_client.transform_organization = MagicMock(
            return_value={"identifier": "123", "name": "Test Org"}
        )
        mock_internal_client.transform_project = MagicMock(
            return_value={
                "identifier": "456",
                "name": "Test Project",
                "slug": "test-project",
            }
        )
        mock_internal_client.transform_issue = MagicMock(
            return_value={
                "external_id": "1",
                "updated_at": datetime.datetime(
                    2025, 1, 1, tzinfo=datetime.timezone.utc
                ),
                "comments": [],
            }
        )

        client = TrackerClient(mock_tracker)
        client.client = mock_internal_client

        mock_crud_issue.get_by_external_id.return_value = None
        mock_crud_issue.upsert.return_value = (Issue(id=1), True)

        # Act
        await client.scan_issues(mock_db_session, mock_organization, mock_project)

        # Assert
        # Verify that get_issues was called with string identifiers
        mock_internal_client.get_issues.assert_called_with(
            organization_id="test-org",
            project_id="test-project",
            since=None,
        )
        mock_crud_issue.upsert.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_comment")
@patch("preloop.sync.scanner.core.crud_issue")
async def test_scan_issues_handles_comments_correctly(
    mock_crud_issue,
    mock_crud_comment,
    mock_db_session,
    mock_tracker,
    mock_organization,
    mock_project,
):
    """
    Regression test: Ensures that comments are processed correctly without errors.
    """
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        mock_internal_client = AsyncMock()
        mock_internal_client.get_issues.return_value = [
            {"id": "1", "updated_at": "2025-01-01T00:00:00Z"}
        ]
        mock_internal_client.transform_issue = MagicMock(
            return_value={
                "external_id": "1",
                "updated_at": datetime.datetime(
                    2025, 1, 1, tzinfo=datetime.timezone.utc
                ),
                "comments": [{"id": "c1", "body": "a comment"}],
            }
        )
        mock_internal_client.transform_comment = MagicMock(
            return_value={
                "external_id": "c1",
                "body": "a comment",
                "updated_at": datetime.datetime(
                    2025, 1, 1, tzinfo=datetime.timezone.utc
                ),
            }
        )

        client = TrackerClient(mock_tracker)
        client.client = mock_internal_client

        mock_issue = Issue(
            id=1,
            external_id="1",
            updated_at=datetime.datetime(2024, 12, 31, tzinfo=datetime.timezone.utc),
            title="Test Title",
            description="Test Description",
        )
        mock_crud_issue.get_by_external_id.return_value = mock_issue
        mock_crud_issue.update.return_value = mock_issue
        mock_crud_comment.get_by_external_id.return_value = None

        created_comment = Comment(
            id="c1_db_id", issue_id=mock_issue.id, body="a comment"
        )
        mock_crud_comment.create.return_value = created_comment
        mock_db_session.get.side_effect = [created_comment, mock_issue]

        # Act
        await client.scan_issues(mock_db_session, mock_organization, mock_project)

        # Assert
        mock_crud_comment.create.assert_called_once()
