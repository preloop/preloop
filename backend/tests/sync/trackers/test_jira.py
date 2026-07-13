import unittest
import pytest
from unittest.mock import patch, MagicMock
from jira import JIRAError
from uuid import uuid4

from preloop.sync.trackers.jira import JiraTracker
from preloop.models.models import Webhook


@pytest.mark.asyncio
class TestJiraTrackerWebhooks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mock_jira_patcher = patch("preloop.sync.trackers.jira.JIRA")
        self.mock_crud_patcher = patch("preloop.sync.trackers.jira.crud_webhook")

        self.mock_jira_class = self.mock_jira_patcher.start()
        self.mock_crud_webhook = self.mock_crud_patcher.start()

        self.mock_jira_client = MagicMock()
        self.mock_jira_class.return_value = self.mock_jira_client

        self.mock_db_session = MagicMock()
        self.test_tracker_id = str(uuid4())

        self.tracker = JiraTracker(
            tracker_id=self.test_tracker_id,
            api_key="fake_key",
            connection_details={
                "jira_url": "https://myjira.atlassian.net",
                "username": "user@example.com",
            },
        )

    def tearDown(self):
        self.mock_jira_patcher.stop()
        self.mock_crud_patcher.stop()

    async def test_register_webhook_success(self):
        """Test successful webhook registration when none exists in DB."""
        self.mock_crud_webhook.get_by_project_id.return_value = None
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": "12345"}
        self.mock_jira_client._session.post.return_value = mock_response
        mock_project = MagicMock()
        mock_project.id = "proj-db-id"
        mock_project.identifier = "TEST"
        result = self.tracker.register_webhook(
            db=self.mock_db_session,
            project=mock_project,
            webhook_url="https://example.com/webhook",
            secret="mysecret",
        )

        self.assertTrue(result)
        self.mock_crud_webhook.get_by_project_id.assert_called_once_with(
            self.mock_db_session, project_id="proj-db-id"
        )
        self.mock_jira_client._session.post.assert_called_once()
        self.mock_crud_webhook.create.assert_called_once()

    @patch("preloop.sync.trackers.jira.logger.error")
    async def test_register_webhook_requires_all_arguments(self, mock_logger_error):
        """Test registration fails fast when required args are missing."""
        result = self.tracker.register_webhook(
            db=None,
            project=None,
            webhook_url=None,
            secret=None,
        )

        self.assertFalse(result)
        mock_logger_error.assert_called_once_with(
            "Jira webhook registration requires db, project, URL, and secret."
        )
        self.mock_jira_client._session.post.assert_not_called()
        self.mock_crud_webhook.get_by_project_id.assert_not_called()

    async def test_register_webhook_already_in_db(self):
        """Test webhook registration is skipped if already in DB."""
        self.mock_crud_webhook.get_by_project_id.return_value = Webhook(
            id="wh-id", project_id="proj-db-id", external_id="123"
        )
        mock_project = MagicMock()
        mock_project.id = "proj-db-id"
        mock_project.identifier = "TEST"
        result = self.tracker.register_webhook(
            db=self.mock_db_session,
            project=mock_project,
            webhook_url="https://example.com/webhook",
            secret="mysecret",
        )

        self.assertTrue(result)
        self.mock_crud_webhook.get_by_project_id.assert_called_once_with(
            self.mock_db_session, project_id="proj-db-id"
        )
        self.mock_jira_client._session.post.assert_not_called()
        self.mock_crud_webhook.create.assert_not_called()

    async def test_unregister_webhook_success(self):
        """Test successful unregistration of an existing webhook."""
        mock_webhook = Webhook(
            id="wh-db-id", project_id="proj-db-id", external_id="12345"
        )
        self.mock_jira_client._session.delete.return_value = MagicMock(status_code=204)
        result = self.tracker.unregister_webhook(
            db=self.mock_db_session, webhook=mock_webhook
        )

        self.assertTrue(result)
        self.mock_jira_client._session.delete.assert_called_once_with(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/12345"
        )
        self.mock_crud_webhook.remove.assert_called_once_with(
            self.mock_db_session, id="wh-db-id"
        )

    @patch("preloop.sync.trackers.jira.logger.error")
    async def test_unregister_webhook_requires_db_and_webhook(self, mock_logger_error):
        """Test unregistration fails fast when required args are missing."""
        result = self.tracker.unregister_webhook(db=None, webhook=None)

        self.assertFalse(result)
        mock_logger_error.assert_called_once_with(
            "Jira webhook unregistration requires db and webhook."
        )
        self.mock_jira_client._session.delete.assert_not_called()
        self.mock_crud_webhook.remove.assert_not_called()

    async def test_unregister_webhook_not_in_jira(self):
        """Test unregistration when webhook is in DB but not in Jira (404)."""
        mock_webhook = Webhook(
            id="wh-db-id", project_id="proj-db-id", external_id="12345"
        )
        self.mock_jira_client._session.delete.side_effect = JIRAError(
            status_code=404, text="Not Found"
        )
        result = self.tracker.unregister_webhook(
            db=self.mock_db_session, webhook=mock_webhook
        )

        self.assertTrue(result)
        self.mock_jira_client._session.delete.assert_called_once_with(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/12345"
        )
        self.mock_crud_webhook.remove.assert_called_once_with(
            self.mock_db_session, id="wh-db-id"
        )

    async def test_cleanup_stale_webhooks(self):
        """Test cleaning up stale webhooks."""
        preloop_url = "https://stale-preloop.com"
        mock_webhooks_data = [
            {"id": "1", "url": f"{preloop_url}/webhook"},
            {"id": "2", "url": "https://another-service.com/webhook"},
            {"id": "3", "url": f"{preloop_url}/another_webhook"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = mock_webhooks_data
        self.mock_jira_client._session.get.return_value = mock_response
        result = self.tracker.cleanup_stale_webhooks(preloop_url)
        self.assertEqual(result, {"unregistered": 2, "failed": 0})
        self.mock_jira_client._session.get.assert_called_once_with(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook"
        )
        self.mock_jira_client._session.delete.assert_any_call(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/1"
        )
        self.mock_jira_client._session.delete.assert_any_call(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/3"
        )
        self.assertEqual(self.mock_jira_client._session.delete.call_count, 2)

    async def test_cleanup_stale_webhooks_with_failures(self):
        """Test cleanup with some deletions failing."""
        preloop_url = "https://stale-preloop.com"
        mock_webhooks_data = [
            {"id": "1", "url": f"{preloop_url}/webhook"},
            {"id": "2", "url": f"{preloop_url}/failing"},
        ]
        mock_response = MagicMock()
        mock_response.json.return_value = mock_webhooks_data
        self.mock_jira_client._session.get.return_value = mock_response
        self.mock_jira_client._session.delete.side_effect = [
            MagicMock(status_code=204),
            JIRAError(status_code=500, text="Internal Server Error"),
        ]
        result = self.tracker.cleanup_stale_webhooks(preloop_url)
        self.assertEqual(result, {"unregistered": 1, "failed": 1})
        self.mock_jira_client._session.get.assert_called_once_with(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook"
        )
        self.mock_jira_client._session.delete.assert_any_call(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/1"
        )
        self.mock_jira_client._session.delete.assert_any_call(
            "https://myjira.atlassian.net/rest/webhooks/1.0/webhook/2"
        )
        self.assertEqual(self.mock_jira_client._session.delete.call_count, 2)


@pytest.mark.asyncio
class TestJiraTracker(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.jira.JIRA")
    async def test_get_organizations(self, mock_jira_class):
        tracker = JiraTracker(
            "tracker-1",
            "api-key",
            {"url": "https://test.jira.com", "username": "testuser"},
        )
        orgs = await tracker.get_organizations()

        self.assertEqual(len(orgs), 1)
        self.assertEqual(orgs[0]["name"], "test.jira.com")

    @patch("preloop.sync.trackers.jira.JIRA")
    async def test_get_projects(self, mock_jira_class):
        tracker = JiraTracker(
            "tracker-1",
            "api-key",
            {"url": "https://test.jira.com", "username": "testuser"},
        )
        with patch.object(
            tracker,
            "_make_request",
            return_value=[
                {
                    "id": "10000",
                    "key": "PROJ",
                    "name": "Test Project",
                    "description": "A test project",
                }
            ],
        ) as mock_make_request:
            projects = await tracker.get_projects("org-1")

            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["name"], "Test Project")
            mock_make_request.assert_called_once_with("GET", "project")
