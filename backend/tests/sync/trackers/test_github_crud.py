"""
Tests for GitHub tracker CRUD methods.
"""

from unittest.mock import ANY, AsyncMock
from unittest import IsolatedAsyncioTestCase

from preloop.sync.trackers.github import GitHubTracker
from preloop.sync.exceptions import TrackerResponseError


class TestGitHubTrackerCRUD(IsolatedAsyncioTestCase):
    """Test GitHub tracker's CRUD methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.connection_details = {
            "owner": "testowner",
            "repo": "testrepo",
        }
        self.tracker = GitHubTracker("tracker-1", "api-token", self.connection_details)

    async def test_create_issue_success(self):
        """Test successful issue creation."""
        # Arrange
        from preloop.schemas.tracker_models import IssueCreate

        issue_create = IssueCreate(
            title="New Issue",
            description="Issue description",
            labels=["bug"],
        )

        created_issue_data = {
            "id": 12345,
            "number": 1,
            "title": "New Issue",
            "body": "Issue description",
            "state": "open",
            "created_at": "2023-01-01T10:00:00Z",
            "updated_at": "2023-01-01T10:00:00Z",
            "html_url": "https://github.com/testowner/testrepo/issues/1",
            "url": "https://api.github.com/repos/testowner/testrepo/issues/1",
            "labels": [{"name": "bug"}],
            "assignees": [],
            "user": {
                "id": 123,
                "login": "testuser",
                "avatar_url": "https://avatar.png",
            },
        }

        self.tracker._request = AsyncMock(return_value=created_issue_data)

        # Act
        result = await self.tracker.create_issue("testowner/testrepo", issue_create)

        # Assert
        self.assertEqual(result.id, "12345")
        self.assertEqual(result.title, "New Issue")
        self.assertEqual(result.labels, ["bug"])

        # Verify API call
        self.tracker._request.assert_called_once_with(
            "POST", "/repos/testowner/testrepo/issues", data=ANY
        )

    async def test_update_issue_success(self):
        """Test successful issue update."""
        # Arrange
        from preloop.schemas.tracker_models import IssueUpdate

        issue_update = IssueUpdate(
            title="Updated Issue",
            description="Updated description",
            status="closed",
        )

        updated_issue_data = {
            "id": 12345,
            "number": 1,
            "title": "Updated Issue",
            "body": "Updated description",
            "state": "closed",
            "created_at": "2023-01-01T10:00:00Z",
            "updated_at": "2023-01-02T10:00:00Z",
            "html_url": "https://github.com/testowner/testrepo/issues/1",
            "url": "https://api.github.com/repos/testowner/testrepo/issues/1",
            "labels": [],
            "assignees": [],
            "user": {
                "id": 123,
                "login": "testuser",
                "avatar_url": "https://avatar.png",
            },
        }

        self.tracker._request = AsyncMock(return_value=updated_issue_data)

        # Act
        result = await self.tracker.update_issue("1", issue_update)

        # Assert
        self.assertEqual(result.title, "Updated Issue")
        self.assertEqual(result.status.id, "closed")

        # Verify API call
        self.tracker._request.assert_called_once_with(
            "PATCH", "/repos/testowner/testrepo/issues/1", data=ANY
        )

    async def test_add_comment_success(self):
        """Test successful comment addition."""
        # Arrange
        comment_data = {
            "id": 1001,
            "body": "Test comment",
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:00:00Z",
            "html_url": "https://github.com/testowner/testrepo/issues/1#issuecomment-1001",
            "user": {
                "id": 123,
                "login": "testuser",
                "avatar_url": "https://avatar.png",
            },
        }

        self.tracker._request = AsyncMock(return_value=comment_data)

        # Act
        result = await self.tracker.add_comment("1", "Test comment")

        # Assert
        self.assertEqual(result.id, "1001")
        self.assertEqual(result.body, "Test comment")
        self.assertEqual(result.author.name, "testuser")

        # Verify API call
        self.tracker._request.assert_called_once_with(
            "POST",
            "/repos/testowner/testrepo/issues/1/comments",
            data={"body": "Test comment"},
        )

    async def test_add_relation_success(self):
        """Test successful issue relation creation (via comment)."""
        # Arrange
        comment_data = {
            "id": 1002,
            "body": "Relates to #2",
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:00:00Z",
            "html_url": "https://github.com/testowner/testrepo/issues/1#issuecomment-1002",
            "user": {
                "id": 123,
                "login": "testuser",
                "avatar_url": "https://avatar.png",
            },
        }

        self.tracker._request = AsyncMock(return_value=comment_data)

        # Act
        result = await self.tracker.add_relation("1", "2", "relates_to")

        # Assert
        self.assertTrue(result)

        # Verify API call
        self.tracker._request.assert_called_once()

    async def test_search_issues_success(self):
        """Test successful issue search."""
        # Arrange
        from preloop.schemas.tracker_models import IssueFilter

        filter_params = IssueFilter(query="bug", status=["open"], labels=["critical"])

        search_response = {
            "total_count": 1,
            "items": [
                {
                    "id": 12345,
                    "number": 1,
                    "title": "Bug Issue",
                    "body": "Bug description",
                    "state": "open",
                    "created_at": "2023-01-01T10:00:00Z",
                    "updated_at": "2023-01-02T11:00:00Z",
                    "html_url": "https://github.com/testowner/testrepo/issues/1",
                    "url": "https://api.github.com/repos/testowner/testrepo/issues/1",
                    "labels": [{"name": "critical"}],
                    "assignees": [],
                    "user": {
                        "id": 123,
                        "login": "testuser",
                        "avatar_url": "https://avatar.png",
                    },
                }
            ],
        }

        self.tracker._request = AsyncMock(return_value=search_response)

        # Act
        issues, total = await self.tracker.search_issues(
            "testowner/testrepo", filter_params, limit=10
        )

        # Assert
        self.assertEqual(total, 1)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].key, "testowner/testrepo#1")

        # Verify search query was built
        self.tracker._request.assert_called_once()
        call_args = self.tracker._request.call_args
        self.assertEqual(call_args[0][0], "GET")
        self.assertIn("search/issues", call_args[0][1])

    async def test_get_project_metadata_success(self):
        """Test successful project metadata retrieval."""
        # Arrange
        repo_data = {
            "id": 12345,
            "name": "testrepo",
            "full_name": "testowner/testrepo",
            "description": "Test repository",
            "html_url": "https://github.com/testowner/testrepo",
            "default_branch": "main",
        }

        self.tracker._make_request = AsyncMock(return_value=repo_data)

        # Act
        result = await self.tracker.get_project_metadata("testowner/testrepo")

        # Assert
        self.assertEqual(result.key, "testowner/testrepo")
        self.assertEqual(result.name, "testrepo")
        self.assertEqual(result.description, "Test repository")
        self.assertGreater(len(result.statuses), 0)

        # Verify API call
        self.tracker._make_request.assert_called_once_with("repos/testowner/testrepo")

    async def test_create_issue_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker("tracker-1", "api-key", {})
        from preloop.schemas.tracker_models import IssueCreate

        issue_create = IssueCreate(title="New Issue")

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.create_issue("testowner/testrepo", issue_create)

        self.assertIn("Owner/repo not found", str(context.exception))

    async def test_add_comment_with_issue_number_format(self):
        """Test comment addition with issue number in various formats."""
        # Arrange
        comment_data = {
            "id": 1001,
            "body": "Test comment",
            "created_at": "2023-01-01T12:00:00Z",
            "updated_at": "2023-01-01T12:00:00Z",
            "html_url": "https://github.com/testowner/testrepo/issues/1#issuecomment-1001",
            "user": {
                "id": 123,
                "login": "testuser",
                "avatar_url": "https://avatar.png",
            },
        }

        self.tracker._request = AsyncMock(return_value=comment_data)

        # Act - test with format "owner/repo#123"
        result = await self.tracker.add_comment("testowner/testrepo#1", "Test comment")

        # Assert
        self.assertEqual(result.id, "1001")

        # Verify the issue number was extracted correctly
        call_args = self.tracker._request.call_args
        self.assertIn("/issues/1/comments", call_args[0][1])
