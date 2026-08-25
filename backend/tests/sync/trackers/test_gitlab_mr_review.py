"""Tests for GitLab MR review methods in GitLabTracker.

This module tests the MR review functionality:
- approve_merge_request
- unapprove_merge_request
- get_mr_discussions
- update_mr_note
- resolve_mr_discussion
- create_mr_discussion
"""

import unittest
from unittest.mock import MagicMock, patch

from preloop.sync.trackers.gitlab import GitLabTracker
from preloop.sync.exceptions import TrackerResponseError


class _AttrsStrippingMR:
    """Stands in for python-gitlab's ProjectMergeRequest post-approval.

    approve()/unapprove() call ``_update_attrs`` with the approvals payload,
    which carries no ``id`` field, so the attribute genuinely disappears
    after a successful call (#246).
    """

    def __init__(self, mr_id: int, iid: int) -> None:
        self.id = mr_id
        self.iid = iid
        self.approve_called = False
        self.unapprove_called = False

    def approve(self, *args, **kwargs):
        self.approve_called = True
        del self.id
        return {"approved": True}

    def unapprove(self, *args, **kwargs):
        self.unapprove_called = True
        del self.id


class TestApproveMergeRequest(unittest.IsolatedAsyncioTestCase):
    """Tests for approve_merge_request method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_approve_merge_request_success(self, mock_gitlab_constructor):
        """Test successful MR approval."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.id = 12345
        mock_mr.iid = 1
        mock_mr.approve.return_value = {"approved": True, "user": {"id": 1}}
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.approve_merge_request("1")

        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["iid"], 1)
        self.assertTrue(result["approved"])
        mock_mr.approve.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_approve_merge_request_no_project_id(self, mock_gitlab_constructor):
        """Test approval fails without project_id in connection details."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.approve_merge_request("1")

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_approve_merge_request_api_error(self, mock_gitlab_constructor):
        """Test approval handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.approve.side_effect = Exception("API Error")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.approve_merge_request("1")

        self.assertIn("Failed to approve merge request", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_approve_merge_request_survives_attrs_replacement(
        self, mock_gitlab_constructor
    ):
        """Approving must not crash when python-gitlab replaces MR attrs.

        python-gitlab's approve() updates the object's attrs with the
        approvals payload, which carries no `id` field. The tracker captured
        the identity before mutating so a successful approval does not blow
        up afterwards with AttributeError (#246).
        """
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = _AttrsStrippingMR(mr_id=12345, iid=6)
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.approve_merge_request("6")

        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["iid"], 6)
        self.assertTrue(result["approved"])
        self.assertTrue(mock_mr.approve_called)


class TestUnapproveMergeRequest(unittest.IsolatedAsyncioTestCase):
    """Tests for unapprove_merge_request method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unapprove_merge_request_success(self, mock_gitlab_constructor):
        """Test successful MR unapproval."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.id = 12345
        mock_mr.iid = 1
        mock_mr.unapprove.return_value = None
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.unapprove_merge_request("1")

        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["iid"], 1)
        self.assertFalse(result["approved"])
        mock_mr.unapprove.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unapprove_merge_request_no_project_id(self, mock_gitlab_constructor):
        """Test unapproval fails without project_id in connection details."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.unapprove_merge_request("1")

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unapprove_merge_request_404_is_noop(self, mock_gitlab_constructor):
        """A 404 from the unapprove call (no approval to revoke, or approvals
        unavailable on this plan) is treated as a successful no-op instead of
        raising (GlitchTip issues 3270-3273)."""
        import gitlab as gitlab_lib

        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.id = 12345
        mock_mr.iid = 1
        mock_mr.unapprove.side_effect = gitlab_lib.exceptions.GitlabHttpError(
            "404 Not Found", response_code=404
        )
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.unapprove_merge_request("1")

        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["iid"], 1)
        self.assertFalse(result["approved"])
        self.assertTrue(result["skipped"])

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unapprove_merge_request_survives_attrs_replacement(
        self, mock_gitlab_constructor
    ):
        """Unapproving must not crash when python-gitlab replaces MR attrs
        with the approvals payload, which has no `id` field (#246)."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = _AttrsStrippingMR(mr_id=12345, iid=6)
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.unapprove_merge_request("6")

        self.assertEqual(result["id"], "12345")
        self.assertEqual(result["iid"], 6)
        self.assertFalse(result["approved"])
        self.assertTrue(mock_mr.unapprove_called)

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unapprove_merge_request_api_error(self, mock_gitlab_constructor):
        """Test unapproval handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.unapprove.side_effect = Exception("API Error")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.unapprove_merge_request("1")

        self.assertIn("Failed to unapprove merge request", str(context.exception))


class TestGetMrDiscussions(unittest.IsolatedAsyncioTestCase):
    """Tests for get_mr_discussions method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_success(self, mock_gitlab_constructor):
        """Test successful retrieval of MR discussions."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        # Create mock discussions
        mock_discussion = MagicMock()
        mock_discussion.id = "discussion-1"
        mock_discussion.individual_note = False
        mock_discussion.notes = [
            {
                "id": 101,
                "author": {"username": "user1"},
                "body": "This is a comment",
                "created_at": "2023-01-01T10:00:00Z",
                "updated_at": "2023-01-01T10:00:00Z",
                "resolvable": True,
                "resolved": False,
                "system": False,
            }
        ]
        mock_mr.discussions.list.return_value = [mock_discussion]

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.get_mr_discussions("1")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "discussion-1")
        self.assertEqual(len(result[0]["notes"]), 1)
        self.assertEqual(result[0]["notes"][0]["author"], "user1")
        self.assertEqual(result[0]["notes"][0]["body"], "This is a comment")

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_with_author_filter(self, mock_gitlab_constructor):
        """Test MR discussions filtered by author."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        # Create mock discussions with different authors
        mock_discussion1 = MagicMock()
        mock_discussion1.id = "discussion-1"
        mock_discussion1.individual_note = False
        mock_discussion1.notes = [
            {
                "id": 101,
                "author": {"username": "user1"},
                "body": "Comment from user1",
                "created_at": "2023-01-01T10:00:00Z",
                "updated_at": "2023-01-01T10:00:00Z",
                "resolvable": True,
                "resolved": False,
                "system": False,
            }
        ]

        mock_discussion2 = MagicMock()
        mock_discussion2.id = "discussion-2"
        mock_discussion2.individual_note = False
        mock_discussion2.notes = [
            {
                "id": 102,
                "author": {"username": "user2"},
                "body": "Comment from user2",
                "created_at": "2023-01-01T11:00:00Z",
                "updated_at": "2023-01-01T11:00:00Z",
                "resolvable": True,
                "resolved": False,
                "system": False,
            }
        ]

        mock_mr.discussions.list.return_value = [mock_discussion1, mock_discussion2]

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.get_mr_discussions("1", filter_author="user1")

        # Only discussion-1 should be included (has notes from user1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "discussion-1")
        self.assertEqual(result[0]["notes"][0]["author"], "user1")

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_no_project_id(self, mock_gitlab_constructor):
        """Test get discussions fails without project_id."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_mr_discussions("1")

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_api_error(self, mock_gitlab_constructor):
        """Test get discussions handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.discussions.list.side_effect = Exception("API Error")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_mr_discussions("1")

        self.assertIn("Failed to get MR discussions", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_with_notes_manager(self, mock_gitlab_constructor):
        """Notes managers from python-gitlab should be listed, not iterated directly."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "discussion-1"
        mock_discussion.individual_note = False
        mock_notes_manager = MagicMock()
        mock_notes_manager.list.return_value = [
            {
                "id": 201,
                "author": {"username": "reviewer"},
                "body": "Please update tests",
                "created_at": "2023-01-02T10:00:00Z",
                "updated_at": "2023-01-02T10:00:00Z",
                "resolvable": True,
                "resolved": False,
                "system": False,
            }
        ]
        mock_discussion.notes = mock_notes_manager
        mock_mr.discussions.list.return_value = [mock_discussion]

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.get_mr_discussions("10")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["notes"][0]["author"], "reviewer")
        mock_notes_manager.list.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_get_mr_discussions_empty(self, mock_gitlab_constructor):
        """Test get discussions with no discussions."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.discussions.list.return_value = []
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.get_mr_discussions("1")

        self.assertEqual(result, [])


class TestUpdateMrNote(unittest.IsolatedAsyncioTestCase):
    """Tests for update_mr_note method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_update_mr_note_success(self, mock_gitlab_constructor):
        """Test successful MR note update."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_note = MagicMock()
        mock_note.id = 101
        mock_note.body = "Updated body"
        mock_note.author = {"username": "user1"}
        mock_note.updated_at = "2023-01-02T10:00:00Z"
        mock_mr.notes.get.return_value = mock_note

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.update_mr_note("1", "101", "Updated body")

        self.assertEqual(result["id"], "101")
        self.assertEqual(result["body"], "Updated body")
        self.assertEqual(result["author"], "user1")
        mock_note.save.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_update_mr_note_no_project_id(self, mock_gitlab_constructor):
        """Test note update fails without project_id."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.update_mr_note("1", "101", "New body")

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_update_mr_note_api_error(self, mock_gitlab_constructor):
        """Test note update handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.notes.get.side_effect = Exception("Note not found")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.update_mr_note("1", "999", "New body")

        self.assertIn("Failed to update MR note", str(context.exception))


class TestResolveMrDiscussion(unittest.IsolatedAsyncioTestCase):
    """Tests for resolve_mr_discussion method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_resolve_mr_discussion_success(self, mock_gitlab_constructor):
        """Test successful discussion resolution."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "discussion-1"
        mock_discussion.resolved = False
        mock_mr.discussions.get.return_value = mock_discussion

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.resolve_mr_discussion("1", "discussion-1", True)

        self.assertEqual(result["id"], "discussion-1")
        self.assertTrue(result["resolved"])
        mock_discussion.save.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_unresolve_mr_discussion_success(self, mock_gitlab_constructor):
        """Test successful discussion unresolve."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "discussion-1"
        mock_discussion.resolved = True
        mock_mr.discussions.get.return_value = mock_discussion

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.resolve_mr_discussion("1", "discussion-1", False)

        self.assertEqual(result["id"], "discussion-1")
        self.assertFalse(result["resolved"])
        mock_discussion.save.assert_called_once()

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_resolve_mr_discussion_no_project_id(self, mock_gitlab_constructor):
        """Test resolve fails without project_id."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.resolve_mr_discussion("1", "discussion-1", True)

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_resolve_mr_discussion_api_error(self, mock_gitlab_constructor):
        """Test resolve handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.discussions.get.side_effect = Exception("Discussion not found")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.resolve_mr_discussion("1", "invalid-id", True)

        self.assertIn("Failed to resolve MR discussion", str(context.exception))


class TestCreateMrDiscussion(unittest.IsolatedAsyncioTestCase):
    """Tests for create_mr_discussion method."""

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_create_mr_discussion_success(self, mock_gitlab_constructor):
        """Test successful MR discussion creation."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "new-discussion-1"
        mock_discussion.individual_note = True
        mock_discussion.notes = [
            {
                "id": 201,
                "body": "This is a new comment",
                "author": {"username": "user1"},
            }
        ]
        mock_mr.discussions.create.return_value = mock_discussion

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.create_mr_discussion("1", "This is a new comment")

        self.assertEqual(result["id"], "new-discussion-1")
        self.assertTrue(result["individual_note"])
        self.assertEqual(len(result["notes"]), 1)
        self.assertEqual(result["notes"][0]["body"], "This is a new comment")
        mock_mr.discussions.create.assert_called_once_with(
            {"body": "This is a new comment"}
        )

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_create_mr_discussion_with_position(self, mock_gitlab_constructor):
        """Test MR discussion creation with diff position."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "new-discussion-2"
        mock_discussion.individual_note = False
        mock_discussion.notes = [
            {
                "id": 202,
                "body": "Line comment",
                "author": {"username": "reviewer"},
            }
        ]
        mock_mr.discussions.create.return_value = mock_discussion

        position = {
            "base_sha": "abc123",
            "start_sha": "def456",
            "head_sha": "ghi789",
            "position_type": "text",
            "new_path": "src/main.py",
            "new_line": 42,
            "old_path": "src/main.py",
            "old_line": 40,
        }

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.create_mr_discussion("1", "Line comment", position)

        self.assertEqual(result["id"], "new-discussion-2")
        self.assertEqual(result["notes"][0]["body"], "Line comment")

        # Verify the position was included in the create call
        create_call_args = mock_mr.discussions.create.call_args[0][0]
        self.assertEqual(create_call_args["body"], "Line comment")
        self.assertIn("position", create_call_args)
        self.assertEqual(create_call_args["position"]["base_sha"], "abc123")
        self.assertEqual(create_call_args["position"]["new_path"], "src/main.py")
        self.assertEqual(create_call_args["position"]["new_line"], 42)
        self.assertEqual(create_call_args["position"]["old_path"], "src/main.py")
        self.assertEqual(create_call_args["position"]["old_line"], 40)

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_create_mr_discussion_no_project_id(self, mock_gitlab_constructor):
        """Test create discussion fails without project_id."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        tracker = GitLabTracker("tracker-1", "api-key", {})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.create_mr_discussion("1", "Comment body")

        self.assertIn("Project ID not found", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_create_mr_discussion_api_error(self, mock_gitlab_constructor):
        """Test create discussion handles API errors."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_mr.discussions.create.side_effect = Exception("API Error")
        mock_project.mergerequests.get.return_value = mock_mr

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})

        with self.assertRaises(TrackerResponseError) as context:
            await tracker.create_mr_discussion("1", "Comment body")

        self.assertIn("Failed to create MR discussion", str(context.exception))

    @patch("preloop.sync.trackers.gitlab.gitlab.Gitlab")
    async def test_create_mr_discussion_with_minimal_position(
        self, mock_gitlab_constructor
    ):
        """Test MR discussion creation with minimal position (no old_path/old_line)."""
        mock_gl_instance = MagicMock()
        mock_gitlab_constructor.return_value = mock_gl_instance
        mock_gl_instance.auth.return_value = None

        mock_project = MagicMock()
        mock_gl_instance.projects.get.return_value = mock_project

        mock_mr = MagicMock()
        mock_project.mergerequests.get.return_value = mock_mr

        mock_discussion = MagicMock()
        mock_discussion.id = "new-discussion-3"
        mock_discussion.individual_note = False
        mock_discussion.notes = []  # Empty notes to test edge case
        mock_mr.discussions.create.return_value = mock_discussion

        position = {
            "base_sha": "abc123",
            "start_sha": "def456",
            "head_sha": "ghi789",
            "new_path": "src/new_file.py",
            "new_line": 10,
        }

        tracker = GitLabTracker("tracker-1", "api-key", {"project_id": "proj-1"})
        result = await tracker.create_mr_discussion("1", "New file comment", position)

        self.assertEqual(result["id"], "new-discussion-3")

        # Verify position doesn't include old_path/old_line when not provided
        create_call_args = mock_mr.discussions.create.call_args[0][0]
        self.assertNotIn("old_path", create_call_args["position"])
        self.assertNotIn("old_line", create_call_args["position"])


if __name__ == "__main__":
    unittest.main()
