"""
Tests for new Jira tracker methods (get_issue and get_comments).
"""

from unittest.mock import ANY, AsyncMock, patch
from unittest import IsolatedAsyncioTestCase
from datetime import datetime

from preloop.sync.trackers.jira import JiraTracker
from preloop.sync.exceptions import TrackerResponseError


class TestJiraTrackerNewMethods(IsolatedAsyncioTestCase):
    """Test Jira tracker's new get_issue and get_comments methods."""

    def setUp(self):
        """Set up test fixtures."""
        self.connection_details = {
            "url": "https://test.atlassian.net",
            "username": "test@example.com",
            "project_key": "TEST",
        }
        # Mock the JIRA client to avoid real API calls during initialization
        with patch("preloop.sync.trackers.jira.JIRA"):
            self.tracker = JiraTracker(
                "tracker-1", "api-token", self.connection_details
            )
        # Set base_url attribute that is used in URL construction
        self.tracker.base_url = "https://test.atlassian.net"

    async def test_get_issue_success(self):
        """Test successful issue retrieval."""
        # Arrange
        issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Test Issue",
                "description": "Issue description",
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T11:00:00.000+0000",
                "labels": ["bug", "critical"],
                "assignee": {
                    "accountId": "123",
                    "displayName": "testuser",
                    "avatarUrls": {"48x48": "https://example.com/avatar.png"},
                },
                "reporter": {
                    "accountId": "456",
                    "displayName": "reporter",
                    "avatarUrls": {"48x48": "https://example.com/reporter.png"},
                },
            },
        }

        self.tracker._make_request = AsyncMock(return_value=issue_data)

        # Act
        result = await self.tracker.get_issue("TEST-123")

        # Assert - Now result is an Issue object, not a dict
        self.assertEqual(result.id, "12345")
        self.assertEqual(result.key, "TEST-123")
        self.assertEqual(result.title, "Test Issue")
        self.assertEqual(result.description, "Issue description")
        self.assertEqual(result.status.name, "Open")
        self.assertEqual(result.labels, ["bug", "critical"])
        self.assertIsNotNone(result.assignee)
        self.assertEqual(result.assignee.name, "testuser")

        # Verify API call
        self.tracker._make_request.assert_called_once_with(
            "GET", "issue/TEST-123", api_version="3"
        )

    async def test_get_issue_not_found(self):
        """Test error when issue is not found."""
        # Arrange
        self.tracker._make_request = AsyncMock(
            side_effect=TrackerResponseError("404 Not Found")
        )

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await self.tracker.get_issue("TEST-999")

        self.assertIn("Issue TEST-999 not found", str(context.exception))

    async def test_get_issue_no_assignee(self):
        """Test issue retrieval when no assignee is set."""
        # Arrange
        issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Test Issue",
                "description": "Issue description",
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T11:00:00.000+0000",
                "labels": [],
                "assignee": None,  # No assignee
            },
        }

        self.tracker._make_request = AsyncMock(return_value=issue_data)

        # Act
        result = await self.tracker.get_issue("TEST-123")

        # Assert - Now result is an Issue object
        self.assertIsNone(result.assignee)

    async def test_get_comments_success(self):
        """Test successful comments retrieval."""
        # Arrange
        comments_data = {
            "comments": [
                {
                    "id": "1001",
                    "body": "First comment",
                    "created": "2023-01-01T12:00:00.000+0000",
                    "updated": "2023-01-01T12:00:00.000+0000",
                    "author": {
                        "accountId": "101",
                        "displayName": "Test User 1",
                        "avatarUrls": {"48x48": "https://avatar1.png"},
                    },
                },
                {
                    "id": "1002",
                    "body": "Second comment",
                    "created": "2023-01-01T13:00:00.000+0000",
                    "updated": "2023-01-01T13:00:00.000+0000",
                    "author": {
                        "accountId": "102",
                        "displayName": "Test User 2",
                        "avatarUrls": {"48x48": "https://avatar2.png"},
                    },
                },
            ]
        }

        self.tracker._make_request = AsyncMock(return_value=comments_data)

        # Act
        result = await self.tracker.get_comments("TEST-123")

        # Assert
        self.assertEqual(len(result), 2)

        self.assertEqual(result[0].id, "1001")
        self.assertEqual(result[0].body, "First comment")
        self.assertEqual(result[0].author.id, "101")
        self.assertEqual(result[0].author.name, "Test User 1")
        self.assertEqual(result[0].author.avatar_url, "https://avatar1.png")
        self.assertEqual(
            result[0].url,
            "https://test.atlassian.net/browse/TEST-123?focusedCommentId=1001",
        )

        self.assertEqual(result[1].id, "1002")
        self.assertEqual(result[1].body, "Second comment")
        self.assertEqual(result[1].author.id, "102")
        self.assertEqual(result[1].author.name, "Test User 2")

        # Verify API call
        self.tracker._make_request.assert_called_once_with(
            "GET", "issue/TEST-123/comment", api_version="3"
        )

    async def test_get_comments_not_found(self):
        """Test error when issue is not found for comments."""
        # Arrange
        self.tracker._make_request = AsyncMock(
            side_effect=TrackerResponseError("404 Not Found")
        )

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await self.tracker.get_comments("TEST-999")

        self.assertIn("Issue TEST-999 not found", str(context.exception))

    async def test_get_comments_no_author(self):
        """Test comments retrieval when comment has no author."""
        # Arrange
        comments_data = {
            "comments": [
                {
                    "id": "1001",
                    "body": "Anonymous comment",
                    "created": "2023-01-01T12:00:00.000+0000",
                    "updated": "2023-01-01T12:00:00.000+0000",
                    "author": {},  # Empty author dict instead of None
                }
            ]
        }

        self.tracker._make_request = AsyncMock(return_value=comments_data)

        # Act
        result = await self.tracker.get_comments("TEST-123")

        # Assert
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, "1001")
        self.assertEqual(result[0].body, "Anonymous comment")
        # When author is empty dict, it should create an IssueUser with default "Anonymous" name
        self.assertIsNotNone(result[0].author)
        self.assertEqual(result[0].author.id, "")
        self.assertEqual(result[0].author.name, "Anonymous")

    async def test_get_comments_empty_list(self):
        """Test comments retrieval when no comments exist."""
        # Arrange
        comments_data = {"comments": []}

        self.tracker._make_request = AsyncMock(return_value=comments_data)

        # Act
        result = await self.tracker.get_comments("TEST-123")

        # Assert
        self.assertEqual(len(result), 0)

    async def test_get_issue_datetime_parsing_fallback(self):
        """Test datetime parsing fallback when date format is invalid."""
        # Arrange
        issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Test Issue",
                "description": "Issue description",
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "invalid-date",  # Invalid date format
                "updated": "also-invalid",  # Invalid date format
                "labels": [],
                "assignee": None,
            },
        }

        self.tracker._make_request = AsyncMock(return_value=issue_data)

        # Act - This should raise an error as our parser expects valid ISO format
        # The _parse_jira_datetime method will return None for invalid dates,
        # but _map_jira_issue expects valid dates and uses datetime.now() as fallback
        result = await self.tracker.get_issue("TEST-123")

        # Assert - should not raise error and use current datetime as fallback
        self.assertIsInstance(result.created_at, datetime)
        self.assertIsInstance(result.updated_at, datetime)

    async def test_create_issue_success(self):
        """Test successful issue creation."""
        # Arrange
        from preloop.schemas.tracker_models import IssueCreate

        issue_create = IssueCreate(
            title="New Issue",
            description="Issue description",
            priority="High",
            labels=["bug"],
        )

        creation_response = {"id": "12345", "key": "TEST-123"}
        issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "New Issue",
                "description": "Issue description",
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-01T10:00:00.000+0000",
                "labels": ["bug"],
                "priority": {"id": "2", "name": "High"},
                "project": {"key": "TEST"},
            },
        }

        self.tracker._make_request = AsyncMock()
        self.tracker._make_request.side_effect = [creation_response, issue_data]

        # Act
        result = await self.tracker.create_issue("TEST", issue_create)

        # Assert
        self.assertEqual(result.id, "12345")
        self.assertEqual(result.key, "TEST-123")
        self.assertEqual(result.title, "New Issue")

        # Verify create request was made
        self.tracker._make_request.assert_any_call(
            "POST", "issue", json_data={"fields": ANY}
        )

    async def test_update_issue_success(self):
        """Test successful issue update."""
        # Arrange
        from preloop.schemas.tracker_models import IssueUpdate

        issue_update = IssueUpdate(
            title="Updated Issue",
            description="Updated description",
            status="In Progress",
        )

        updated_issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Updated Issue",
                "description": "Updated description",
                "status": {
                    "id": "2",
                    "name": "In Progress",
                    "statusCategory": {"key": "indeterminate"},
                },
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T10:00:00.000+0000",
                "project": {"key": "TEST"},
            },
        }

        transitions_response = {
            "transitions": [
                {"id": "21", "to": {"name": "In Progress"}},
                {"id": "31", "to": {"name": "Done"}},
            ]
        }

        self.tracker._make_request = AsyncMock()
        self.tracker._make_request.side_effect = [
            None,  # PUT request
            transitions_response,  # GET transitions
            None,  # POST transition
            updated_issue_data,  # GET updated issue
        ]

        # Act
        result = await self.tracker.update_issue("TEST-123", issue_update)

        # Assert
        self.assertEqual(result.title, "Updated Issue")
        self.assertEqual(result.status.name, "In Progress")

    async def test_add_comment_success(self):
        """Test successful comment addition."""
        # Arrange
        comment_data = {
            "id": "1001",
            "body": {"type": "doc", "content": [{"type": "paragraph"}]},
            "created": "2023-01-01T12:00:00.000+0000",
            "updated": "2023-01-01T12:00:00.000+0000",
            "author": {
                "accountId": "123",
                "displayName": "Test User",
                "avatarUrls": {"48x48": "https://avatar.png"},
            },
        }

        self.tracker._make_request = AsyncMock(return_value=comment_data)

        # Act
        result = await self.tracker.add_comment("TEST-123", "Test comment")

        # Assert
        self.assertEqual(result.id, "1001")
        self.assertEqual(result.author.name, "Test User")

        # Verify API call
        self.tracker._make_request.assert_called_once_with(
            "POST", "issue/TEST-123/comment", json_data=ANY
        )

    async def test_add_relation_success(self):
        """Test successful issue relation creation."""
        # Arrange
        self.tracker._make_request = AsyncMock(return_value=None)

        # Act
        result = await self.tracker.add_relation("TEST-123", "TEST-124", "blocks")

        # Assert
        self.assertTrue(result)

        # Verify API call
        self.tracker._make_request.assert_called_once_with(
            "POST", "issueLink", json_data=ANY
        )

    async def test_search_issues_success(self):
        """Test successful issue search."""
        # Arrange
        from preloop.schemas.tracker_models import IssueFilter

        filter_params = IssueFilter(query="bug", status=["Open"], labels=["critical"])

        search_response = {
            "total": 1,
            "issues": [
                {
                    "id": "12345",
                    "key": "TEST-123",
                    "fields": {
                        "summary": "Bug Issue",
                        "description": "Bug description",
                        "status": {
                            "id": "1",
                            "name": "Open",
                            "statusCategory": {"key": "new"},
                        },
                        "created": "2023-01-01T10:00:00.000+0000",
                        "updated": "2023-01-02T11:00:00.000+0000",
                        "labels": ["critical"],
                        "project": {"key": "TEST"},
                    },
                }
            ],
        }

        self.tracker._make_request = AsyncMock(return_value=search_response)

        # Act
        issues, total = await self.tracker.search_issues(
            "TEST", filter_params, limit=10
        )

        # Assert
        self.assertEqual(total, 1)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].key, "TEST-123")

        # Verify JQL query was built correctly
        call_args = self.tracker._make_request.call_args
        jql_data = call_args[1]["json_data"]
        self.assertIn("project = 'TEST'", jql_data["jql"])
        self.assertIn("bug", jql_data["jql"])

    async def test_get_project_metadata_success(self):
        """Test successful project metadata retrieval."""
        # Arrange
        project_data = {
            "id": "10000",
            "key": "TEST",
            "name": "Test Project",
            "description": "Test project description",
        }

        statuses_data = [
            {
                "statuses": [
                    {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                    {
                        "id": "2",
                        "name": "In Progress",
                        "statusCategory": {"key": "indeterminate"},
                    },
                ]
            }
        ]

        priorities_data = [
            {"id": "1", "name": "Highest"},
            {"id": "2", "name": "High"},
        ]

        self.tracker._make_request = AsyncMock()
        self.tracker._make_request.side_effect = [
            project_data,
            statuses_data,
            priorities_data,
        ]

        # Act
        result = await self.tracker.get_project_metadata("TEST")

        # Assert
        self.assertEqual(result.key, "TEST")
        self.assertEqual(result.name, "Test Project")
        self.assertGreater(len(result.statuses), 0)
        self.assertGreater(len(result.priorities), 0)

    async def test_update_issue_with_nested_json_description(self):
        """Test updating issue with nested JSON in description (regression test for nested JSON bug)."""
        # Arrange
        from preloop.schemas.tracker_models import IssueUpdate

        # Simulate a description that comes from the database with nested JSON
        # This is the exact pattern that was causing the "Operation value must be a string" error
        nested_json_description = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "text",
                            "text": '{"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Actual description text"}]}]}',
                        }
                    ],
                }
            ],
        }

        # Create IssueUpdate with string, then patch it to have a dict
        # This simulates what happens when Pydantic/API parsing converts JSON strings to dicts
        issue_update = IssueUpdate(
            title="Test Issue",
            description="placeholder",  # Valid string for validation
        )
        # Now manually set it to the problematic dict (simulating post-parsing state)
        issue_update.description = nested_json_description

        updated_issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Test Issue",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Actual description text"}
                            ],
                        }
                    ],
                },
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T10:00:00.000+0000",
                "project": {"key": "TEST"},
            },
        }

        self.tracker._make_request = AsyncMock()
        self.tracker._make_request.side_effect = [
            None,  # PUT request
            updated_issue_data,  # GET updated issue
        ]

        # Act
        result = await self.tracker.update_issue("TEST-123", issue_update)

        # Assert
        self.assertEqual(result.title, "Test Issue")
        # The description should be unwrapped to just the actual text
        self.assertEqual(result.description, "Actual description text")

        # Verify the PUT request was called with properly unwrapped description
        put_call = self.tracker._make_request.call_args_list[0]
        fields_data = put_call[1]["json_data"]["fields"]

        # The description should be plain text (not ADF dict, not nested JSON strings)
        # Jira API v2 expects plain text, so the tracker converts ADF to plain text
        self.assertIn("description", fields_data)
        desc = fields_data["description"]
        self.assertIsInstance(desc, str)

        # Verify it's been unwrapped to just the actual text
        self.assertEqual(desc, "Actual description text")
        # Verify it's NOT still JSON (no escaped quotes or nested structures)
        self.assertNotIn('\\"', desc)
        self.assertNotIn('{"type":', desc)

    async def test_update_issue_with_string_nested_json_description(self):
        """Test updating issue when description is a JSON string (from API serialization)."""
        # Arrange
        from preloop.schemas.tracker_models import IssueUpdate

        # Simulate description as a JSON string (like what might come from Pydantic serialization)
        nested_json_string = '{"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": "{\\"type\\": \\"doc\\", \\"version\\": 1, \\"content\\": [{\\"type\\": \\"paragraph\\", \\"content\\": [{\\"type\\": \\"text\\", \\"text\\": \\"Real text\\"}]}]}"}]}]}'

        issue_update = IssueUpdate(
            title="Test Issue",
            description=nested_json_string,  # Pass as string
        )

        updated_issue_data = {
            "id": "12345",
            "key": "TEST-123",
            "fields": {
                "summary": "Test Issue",
                "description": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [{"type": "text", "text": "Real text"}],
                        }
                    ],
                },
                "status": {"id": "1", "name": "Open", "statusCategory": {"key": "new"}},
                "created": "2023-01-01T10:00:00.000+0000",
                "updated": "2023-01-02T10:00:00.000+0000",
                "project": {"key": "TEST"},
            },
        }

        self.tracker._make_request = AsyncMock()
        self.tracker._make_request.side_effect = [
            None,  # PUT request
            updated_issue_data,  # GET updated issue
        ]

        # Act
        result = await self.tracker.update_issue("TEST-123", issue_update)

        # Assert
        self.assertEqual(result.title, "Test Issue")
        self.assertEqual(result.description, "Real text")

        # Verify the PUT request unwrapped the nested JSON properly
        put_call = self.tracker._make_request.call_args_list[0]
        fields_data = put_call[1]["json_data"]["fields"]

        # Description should be plain text (Jira API v2 expects plain text)
        desc = fields_data["description"]
        self.assertIsInstance(desc, str)
        self.assertEqual(desc, "Real text")
        self.assertNotIn('\\"', desc)
