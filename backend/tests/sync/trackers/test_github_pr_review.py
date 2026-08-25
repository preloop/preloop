"""
Tests for GitHub PR review methods in GitHubTracker.

Tests cover:
- submit_pull_request_review
- get_pull_request_comments
- update_review_comment
- resolve_review_thread
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from unittest import IsolatedAsyncioTestCase
import httpx
from uuid import uuid4

from preloop.sync.exceptions import (
    TrackerAuthenticationError,
    TrackerConnectionError,
    TrackerResponseError,
)
from preloop.sync.trackers.github import GitHubTracker


@pytest.mark.asyncio
class TestSubmitPullRequestReview(IsolatedAsyncioTestCase):
    """Tests for submit_pull_request_review method."""

    async def test_submit_review_approve_success(self):
        """Test successfully submitting an approval review."""
        # Arrange
        review_response = {
            "id": 12345,
            "node_id": "PRR_kwDOAbcdef",
            "state": "APPROVED",
            "body": "Looks good to me!",
            "user": {"login": "reviewer"},
            "submitted_at": "2023-01-15T10:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-12345",
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=review_response)

        # Act
        result = await tracker.submit_pull_request_review(
            pr_number="1",
            body="Looks good to me!",
            event="APPROVE",
        )

        # Assert
        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["state"], "APPROVED")
        self.assertEqual(result["body"], "Looks good to me!")
        self.assertEqual(result["user"], "reviewer")
        tracker._request.assert_called_once_with(
            "POST",
            "/repos/testowner/testrepo/pulls/1/reviews",
            data={"body": "Looks good to me!", "event": "APPROVE"},
        )

    async def test_submit_review_request_changes_with_comments(self):
        """Test submitting a request changes review with inline comments."""
        # Arrange
        review_response = {
            "id": 12346,
            "node_id": "PRR_kwDOAbcdefg",
            "state": "CHANGES_REQUESTED",
            "body": "Please fix these issues.",
            "user": {"login": "reviewer"},
            "submitted_at": "2023-01-15T11:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-12346",
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=review_response)

        comments = [
            {
                "path": "src/main.py",
                "line": 10,
                "body": "This variable name is unclear",
            },
            {
                "path": "src/utils.py",
                "line": 25,
                "body": "Missing docstring",
                "side": "LEFT",
            },
        ]

        # Act
        result = await tracker.submit_pull_request_review(
            pr_number="1",
            body="Please fix these issues.",
            event="REQUEST_CHANGES",
            comments=comments,
        )

        # Assert
        self.assertEqual(result["id"], "12346")
        self.assertEqual(result["state"], "CHANGES_REQUESTED")

        # Verify the request was made with properly formatted comments
        call_args = tracker._request.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertEqual(call_args[0][1], "/repos/testowner/testrepo/pulls/1/reviews")

        request_data = call_args[1]["data"]
        self.assertEqual(request_data["event"], "REQUEST_CHANGES")
        self.assertEqual(len(request_data["comments"]), 2)

        # Check first comment (default side: RIGHT)
        self.assertEqual(request_data["comments"][0]["path"], "src/main.py")
        self.assertEqual(request_data["comments"][0]["line"], 10)
        self.assertEqual(request_data["comments"][0]["side"], "RIGHT")

        # Check second comment (explicit side: LEFT)
        self.assertEqual(request_data["comments"][1]["side"], "LEFT")

    async def test_submit_review_comment_only(self):
        """Test submitting a comment-only review (no approval/rejection)."""
        # Arrange
        review_response = {
            "id": 12347,
            "node_id": "PRR_kwDOAbcdefgh",
            "state": "COMMENTED",
            "body": "Just some observations.",
            "user": {"login": "reviewer"},
            "submitted_at": "2023-01-15T12:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/1#pullrequestreview-12347",
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=review_response)

        # Act
        result = await tracker.submit_pull_request_review(
            pr_number="1",
            body="Just some observations.",
            event="COMMENT",
        )

        # Assert
        self.assertEqual(result["state"], "COMMENTED")

    async def test_submit_review_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.submit_pull_request_review(
                pr_number="1",
                body="Test",
                event="APPROVE",
            )

        self.assertIn("Owner/repo not found", str(context.exception))

    async def test_submit_review_extracts_pr_number_from_formats(self):
        """Test that PR number is correctly extracted from various formats."""
        # Arrange
        review_response = {
            "id": 12345,
            "node_id": "PRR_kwDOAbcdef",
            "state": "APPROVED",
            "body": "LGTM",
            "user": {"login": "reviewer"},
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=review_response)

        # Test with "owner/repo#123" format
        await tracker.submit_pull_request_review(
            pr_number="testowner/testrepo#42",
            body="LGTM",
            event="APPROVE",
        )

        call_args = tracker._request.call_args
        self.assertIn("/pulls/42/", call_args[0][1])

    async def test_submit_review_api_error(self):
        """Test handling of API errors."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(
            side_effect=TrackerResponseError("422 Unprocessable Entity")
        )

        # Act & Assert
        with self.assertRaises(TrackerResponseError):
            await tracker.submit_pull_request_review(
                pr_number="1",
                body="Test",
                event="APPROVE",
            )


@pytest.mark.asyncio
class TestGetPullRequestComments(IsolatedAsyncioTestCase):
    """Tests for get_pull_request_comments method."""

    async def test_get_comments_success(self):
        """Test successfully fetching PR comments."""
        # Arrange
        review_comments = [
            {
                "id": 1001,
                "node_id": "PRRC_1001",
                "user": {"login": "reviewer1"},
                "body": "Inline comment",
                "path": "src/main.py",
                "line": 10,
                "original_line": 8,
                "side": "RIGHT",
                "diff_hunk": "@@ -5,10 +5,15 @@",
                "commit_id": "abc123",
                "in_reply_to_id": None,
                "created_at": "2023-01-15T10:00:00Z",
                "updated_at": "2023-01-15T10:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/1#discussion_r1001",
            }
        ]

        issue_comments = [
            {
                "id": 2001,
                "node_id": "IC_2001",
                "user": {"login": "commenter1"},
                "body": "General comment",
                "created_at": "2023-01-15T11:00:00Z",
                "updated_at": "2023-01-15T11:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/1#issuecomment-2001",
            }
        ]

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._make_request = AsyncMock(side_effect=[review_comments, issue_comments])

        # Act
        result = await tracker.get_pull_request_comments(pr_number="1")

        # Assert
        self.assertEqual(len(result), 2)

        # Check review comment
        review_comment = next(c for c in result if c["type"] == "review_comment")
        self.assertEqual(review_comment["id"], "1001")
        self.assertEqual(review_comment["author"], "reviewer1")
        self.assertEqual(review_comment["body"], "Inline comment")
        self.assertEqual(review_comment["path"], "src/main.py")
        self.assertEqual(review_comment["line"], 10)
        self.assertEqual(review_comment["side"], "RIGHT")

        # Check issue comment
        issue_comment = next(c for c in result if c["type"] == "issue_comment")
        self.assertEqual(issue_comment["id"], "2001")
        self.assertEqual(issue_comment["author"], "commenter1")
        self.assertEqual(issue_comment["body"], "General comment")
        self.assertIsNone(issue_comment["path"])
        self.assertIsNone(issue_comment["line"])

    async def test_get_comments_with_author_filter(self):
        """Test filtering comments by author."""
        # Arrange
        review_comments = [
            {
                "id": 1001,
                "node_id": "PRRC_1001",
                "user": {"login": "reviewer1"},
                "body": "Comment from reviewer1",
                "path": "src/main.py",
                "line": 10,
                "created_at": "2023-01-15T10:00:00Z",
                "updated_at": "2023-01-15T10:00:00Z",
            },
            {
                "id": 1002,
                "node_id": "PRRC_1002",
                "user": {"login": "reviewer2"},
                "body": "Comment from reviewer2",
                "path": "src/utils.py",
                "line": 20,
                "created_at": "2023-01-15T10:30:00Z",
                "updated_at": "2023-01-15T10:30:00Z",
            },
        ]

        issue_comments = [
            {
                "id": 2001,
                "node_id": "IC_2001",
                "user": {"login": "reviewer1"},
                "body": "Another from reviewer1",
                "created_at": "2023-01-15T11:00:00Z",
                "updated_at": "2023-01-15T11:00:00Z",
            }
        ]

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._make_request = AsyncMock(side_effect=[review_comments, issue_comments])

        # Act
        result = await tracker.get_pull_request_comments(
            pr_number="1",
            filter_author="reviewer1",
        )

        # Assert - should only have comments from reviewer1
        self.assertEqual(len(result), 2)
        for comment in result:
            self.assertEqual(comment["author"], "reviewer1")

    async def test_get_comments_no_comments(self):
        """Test when PR has no comments."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._make_request = AsyncMock(side_effect=[[], []])

        # Act
        result = await tracker.get_pull_request_comments(pr_number="1")

        # Assert
        self.assertEqual(len(result), 0)

    async def test_get_comments_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_pull_request_comments(pr_number="1")

        self.assertIn("Owner/repo not found", str(context.exception))

    async def test_get_comments_api_error(self):
        """Test handling of API errors."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._make_request = AsyncMock(
            side_effect=TrackerResponseError("404 Not Found")
        )

        # Act & Assert
        with self.assertRaises(TrackerResponseError):
            await tracker.get_pull_request_comments(pr_number="1")


@pytest.mark.asyncio
class TestUpdateReviewComment(IsolatedAsyncioTestCase):
    """Tests for update_review_comment method."""

    async def test_update_comment_success(self):
        """Test successfully updating a review comment."""
        # Arrange
        updated_comment = {
            "id": 1001,
            "node_id": "PRRC_1001",
            "user": {"login": "reviewer1"},
            "body": "Updated comment text",
            "path": "src/main.py",
            "line": 10,
            "side": "RIGHT",
            "created_at": "2023-01-15T10:00:00Z",
            "updated_at": "2023-01-15T12:00:00Z",
            "html_url": "https://github.com/owner/repo/pull/1#discussion_r1001",
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=updated_comment)

        # Act
        result = await tracker.update_review_comment(
            comment_id="1001",
            body="Updated comment text",
        )

        # Assert
        self.assertEqual(result["id"], "1001")
        self.assertEqual(result["body"], "Updated comment text")
        self.assertEqual(result["author"], "reviewer1")
        self.assertEqual(result["path"], "src/main.py")
        self.assertEqual(result["line"], 10)

        tracker._request.assert_called_once_with(
            "PATCH",
            "/repos/testowner/testrepo/pulls/comments/1001",
            data={"body": "Updated comment text"},
        )

    async def test_update_comment_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.update_review_comment(
                comment_id="1001",
                body="Updated text",
            )

        self.assertIn("Owner/repo not found", str(context.exception))

    async def test_update_comment_not_found(self):
        """Test error when comment doesn't exist."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(side_effect=TrackerResponseError("404 Not Found"))

        # Act & Assert
        with self.assertRaises(TrackerResponseError):
            await tracker.update_review_comment(
                comment_id="99999",
                body="Updated text",
            )


@pytest.mark.asyncio
class TestResolveReviewThread(IsolatedAsyncioTestCase):
    """Tests for resolve_review_thread method."""

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_resolve_thread_success(self, mock_client_class):
        """Test successfully resolving a review thread."""
        # Arrange
        graphql_response = {
            "data": {
                "resolveReviewThread": {
                    "thread": {
                        "id": "PRRT_kwDOAbcdef",
                        "isResolved": True,
                        "viewerCanResolve": False,
                        "viewerCanUnresolve": True,
                    }
                }
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Act
        result = await tracker.resolve_review_thread(
            thread_id="PRRT_kwDOAbcdef",
            resolved=True,
        )

        # Assert
        self.assertEqual(result["id"], "PRRT_kwDOAbcdef")
        self.assertTrue(result["is_resolved"])
        self.assertFalse(result["viewer_can_resolve"])
        self.assertTrue(result["viewer_can_unresolve"])

        # Verify GraphQL mutation was called
        call_args = mock_client.post.call_args
        self.assertEqual(call_args[0][0], "https://api.github.com/graphql")
        request_body = call_args[1]["json"]
        self.assertIn("resolveReviewThread", request_body["query"])
        self.assertEqual(request_body["variables"]["threadId"], "PRRT_kwDOAbcdef")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_unresolve_thread_success(self, mock_client_class):
        """Test successfully unresolving a review thread."""
        # Arrange
        graphql_response = {
            "data": {
                "unresolveReviewThread": {
                    "thread": {
                        "id": "PRRT_kwDOAbcdef",
                        "isResolved": False,
                        "viewerCanResolve": True,
                        "viewerCanUnresolve": False,
                    }
                }
            }
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Act
        result = await tracker.resolve_review_thread(
            thread_id="PRRT_kwDOAbcdef",
            resolved=False,
        )

        # Assert
        self.assertEqual(result["id"], "PRRT_kwDOAbcdef")
        self.assertFalse(result["is_resolved"])

        # Verify GraphQL mutation was called with unresolve
        call_args = mock_client.post.call_args
        request_body = call_args[1]["json"]
        self.assertIn("unresolveReviewThread", request_body["query"])

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_resolve_thread_authentication_error(self, mock_client_class):
        """Test error when authentication fails."""
        # Arrange
        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Act & Assert
        with self.assertRaises(TrackerAuthenticationError):
            await tracker.resolve_review_thread(
                thread_id="PRRT_kwDOAbcdef",
                resolved=True,
            )

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_resolve_thread_graphql_error(self, mock_client_class):
        """Test handling of GraphQL errors."""
        # Arrange
        graphql_response = {
            "errors": [{"message": "Could not resolve to a PullRequestReviewThread"}]
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = graphql_response

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.resolve_review_thread(
                thread_id="invalid_thread",
                resolved=True,
            )

        self.assertIn("GraphQL errors", str(context.exception))

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_resolve_thread_connection_error(self, mock_client_class):
        """Test handling of connection errors."""
        # Arrange
        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.RequestError(
            "Connection failed", request=MagicMock()
        )
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Act & Assert
        with self.assertRaises(TrackerConnectionError):
            await tracker.resolve_review_thread(
                thread_id="PRRT_kwDOAbcdef",
                resolved=True,
            )


def _make_threads_response(threads, has_next_page=False, end_cursor=None):
    """Build a mock GraphQL response for the reviewThreads query."""
    page_info = {"hasNextPage": has_next_page}
    if end_cursor:
        page_info["endCursor"] = end_cursor
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": page_info,
                        "nodes": threads,
                    }
                }
            }
        }
    }


@pytest.mark.asyncio
class TestGetThreadIdForComment(IsolatedAsyncioTestCase):
    """Tests for get_thread_id_for_comment method."""

    def _make_tracker(self):
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        return GitHubTracker(str(uuid4()), "api-key", connection_details)

    @staticmethod
    def _mock_client(mock_client_class, responses):
        mock_client = AsyncMock()
        mock_client.post.side_effect = responses
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client
        return mock_client

    @staticmethod
    def _graphql_response(payload):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = payload
        return mock_response

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_found_by_numeric_database_id(self, mock_client_class):
        """Test matching a comment by its numeric REST id on the first page."""
        response = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOthread1",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_node1", "databaseId": 111}],
                        },
                    },
                    {
                        "id": "PRRT_kwDOthread2",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_node2", "databaseId": 222}],
                        },
                    },
                ]
            )
        )
        mock_client = self._mock_client(mock_client_class, [response])
        tracker = self._make_tracker()

        result = await tracker.get_thread_id_for_comment(
            pr_number="42", comment_id="222"
        )

        self.assertEqual(result, "PRRT_kwDOthread2")
        self.assertEqual(mock_client.post.call_count, 1)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_paginates_through_comment_pages(self, mock_client_class):
        """Test that comments beyond the first page of a thread are found."""
        page1 = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOlongthread",
                        "comments": {
                            "pageInfo": {
                                "hasNextPage": True,
                                "endCursor": "cursor-c2",
                            },
                            "nodes": [{"id": "PRRC_early", "databaseId": 1}],
                        },
                    }
                ]
            )
        )
        page2 = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOlongthread",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_late", "databaseId": 999}],
                        },
                    }
                ]
            )
        )
        mock_client = self._mock_client(mock_client_class, [page1, page2])
        tracker = self._make_tracker()

        result = await tracker.get_thread_id_for_comment(
            pr_number="42", comment_id="999"
        )

        self.assertEqual(result, "PRRT_kwDOlongthread")
        self.assertEqual(mock_client.post.call_count, 2)
        second_call_vars = mock_client.post.call_args_list[1][1]["json"]["variables"]
        self.assertIsNone(second_call_vars["threadsCursor"])
        self.assertEqual(second_call_vars["commentsCursor"], "cursor-c2")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_paginates_through_thread_pages(self, mock_client_class):
        """Test that threads beyond the first page are searched."""
        page1 = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOearly",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_a", "databaseId": 1}],
                        },
                    }
                ],
                has_next_page=True,
                end_cursor="cursor-t2",
            )
        )
        page2 = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOlate",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_b", "databaseId": 777}],
                        },
                    }
                ]
            )
        )
        mock_client = self._mock_client(mock_client_class, [page1, page2])
        tracker = self._make_tracker()

        result = await tracker.get_thread_id_for_comment(
            pr_number="42", comment_id="777"
        )

        self.assertEqual(result, "PRRT_kwDOlate")
        self.assertEqual(mock_client.post.call_count, 2)
        second_call_vars = mock_client.post.call_args_list[1][1]["json"]["variables"]
        self.assertEqual(second_call_vars["threadsCursor"], "cursor-t2")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_returns_none_when_comment_not_found(self, mock_client_class):
        """Test that None is returned when no thread contains the comment."""
        response = self._graphql_response(
            _make_threads_response(
                [
                    {
                        "id": "PRRT_kwDOthread1",
                        "comments": {
                            "pageInfo": {"hasNextPage": False},
                            "nodes": [{"id": "PRRC_node1", "databaseId": 111}],
                        },
                    }
                ]
            )
        )
        self._mock_client(mock_client_class, [response])
        tracker = self._make_tracker()

        result = await tracker.get_thread_id_for_comment(
            pr_number="42", comment_id="404404"
        )

        self.assertIsNone(result)


@pytest.mark.asyncio
class TestPRReviewIntegration(IsolatedAsyncioTestCase):
    """Integration-style tests for PR review workflow."""

    async def test_full_review_workflow(self):
        """Test a complete review workflow: get comments, update, submit review."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Mock responses for the workflow
        existing_comments = [
            {
                "id": 1001,
                "node_id": "PRRC_1001",
                "user": {"login": "bot"},
                "body": "Original comment",
                "path": "src/main.py",
                "line": 10,
                "created_at": "2023-01-15T10:00:00Z",
                "updated_at": "2023-01-15T10:00:00Z",
            }
        ]

        updated_comment = {
            "id": 1001,
            "node_id": "PRRC_1001",
            "user": {"login": "bot"},
            "body": "Updated comment",
            "path": "src/main.py",
            "line": 10,
            "created_at": "2023-01-15T10:00:00Z",
            "updated_at": "2023-01-15T11:00:00Z",
        }

        review_response = {
            "id": 12345,
            "node_id": "PRR_kwDOAbcdef",
            "state": "APPROVED",
            "body": "All issues addressed!",
            "user": {"login": "reviewer"},
        }

        # Set up mocks
        tracker._make_request = AsyncMock(side_effect=[existing_comments, []])
        tracker._request = AsyncMock(side_effect=[updated_comment, review_response])

        # Act - Step 1: Get existing comments by bot
        comments = await tracker.get_pull_request_comments(
            pr_number="1",
            filter_author="bot",
        )

        # Act - Step 2: Update the bot's comment
        if comments:
            await tracker.update_review_comment(
                comment_id=comments[0]["id"],
                body="Updated comment",
            )

        # Act - Step 3: Submit final review
        result = await tracker.submit_pull_request_review(
            pr_number="1",
            body="All issues addressed!",
            event="APPROVE",
        )

        # Assert
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["author"], "bot")
        self.assertEqual(result["state"], "APPROVED")


@pytest.mark.asyncio
class TestGetReviewComments(IsolatedAsyncioTestCase):
    """Tests for get_review_comments method."""

    async def test_get_review_comments_success(self):
        """Test successfully fetching comments for a specific review."""
        # Arrange
        review_comments = [
            {
                "id": 1001,
                "node_id": "PRRC_1001",
                "user": {"login": "reviewer1"},
                "body": "Inline comment on review",
                "path": "src/main.py",
                "line": 10,
                "position": 5,
                "created_at": "2023-01-15T10:00:00Z",
                "updated_at": "2023-01-15T10:00:00Z",
                "html_url": "https://github.com/owner/repo/pull/1#discussion_r1001",
            }
        ]

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=review_comments)

        # Act
        result = await tracker.get_review_comments(
            pr_number="1",
            review_id="12345",
        )

        # Assert
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "1001")
        self.assertEqual(result[0]["body"], "Inline comment on review")
        self.assertEqual(result[0]["author"], "reviewer1")
        self.assertEqual(result[0]["path"], "src/main.py")
        self.assertEqual(result[0]["line"], 10)

        # Verify API call
        tracker._request.assert_called_once_with(
            "GET", "/repos/testowner/testrepo/pulls/1/reviews/12345/comments"
        )

    async def test_get_review_comments_empty(self):
        """Test fetching comments when review has no comments."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=[])

        # Act
        result = await tracker.get_review_comments(
            pr_number="1",
            review_id="12345",
        )

        # Assert
        self.assertEqual(len(result), 0)

    async def test_get_review_comments_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_review_comments(
                pr_number="1",
                review_id="12345",
            )

        self.assertIn("Owner/repo not found", str(context.exception))

    async def test_get_review_comments_api_error(self):
        """Test handling of API errors."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(side_effect=TrackerResponseError("404 Not Found"))

        # Act & Assert
        with self.assertRaises(TrackerResponseError):
            await tracker.get_review_comments(
                pr_number="1",
                review_id="99999",
            )

    async def test_get_review_comments_extracts_pr_number_from_formats(self):
        """Test that PR number is correctly extracted from various formats."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)
        tracker._request = AsyncMock(return_value=[])

        # Test with "owner/repo#123" format
        await tracker.get_review_comments(
            pr_number="testowner/testrepo#42",
            review_id="12345",
        )

        call_args = tracker._request.call_args
        self.assertIn("/pulls/42/", call_args[0][1])
