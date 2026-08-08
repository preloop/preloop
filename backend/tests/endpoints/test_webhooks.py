import hmac
import hashlib
import json
from unittest.mock import MagicMock, patch, AsyncMock
from typing import Optional

import pytest

from fastapi import HTTPException
from preloop.api.endpoints import webhooks
from preloop.models.crud.tracker import UnknownProjectState

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.models.db.session import get_db_session
from preloop.sync.services.event_bus import get_task_publisher


@pytest.fixture
def test_client():
    """Create a test client for the FastAPI app."""
    # This global fixture will be overridden by self.test_client in the class
    app = create_app()
    with TestClient(app) as client:
        yield client


@pytest.fixture
def configured_mock_org_fixture():
    """A basic mock organization instance. webhook_secret is configured per-test."""
    org = MagicMock(name="configured_org_instance_mock")
    org.id = 123
    org.name = "Test Organization"
    org.identifier = "test-org-fixture"
    org.last_webhook_update = None
    return org


def setup_mock_webhook_secret(
    mock_org_instance: MagicMock, secret_value: Optional[str] = "test-secret"
) -> None:
    """Configure the webhook_secret attribute on a mock organization instance."""
    mock_org_instance.webhook_secret = secret_value


class TestWebhooksEndpoint:
    """Test cases for the webhooks endpoint."""

    @classmethod
    def setup_class(cls):
        """Set up the test environment for the entire test class."""
        cls.connect_nats_patcher = patch(
            "preloop.api.app.connect_nats", new_callable=AsyncMock
        )
        cls.close_nats_patcher = patch(
            "preloop.api.app.close_nats", new_callable=AsyncMock
        )
        cls.mock_connect_nats = cls.connect_nats_patcher.start()
        cls.mock_close_nats = cls.close_nats_patcher.start()

        cls.app = create_app()
        cls.mock_session = MagicMock(spec=Session)
        cls.mock_task_publisher = AsyncMock()

        def override_get_db():
            try:
                yield cls.mock_session
            finally:
                pass

        def override_get_task_publisher():
            return cls.mock_task_publisher

        cls.app.dependency_overrides[get_db_session] = override_get_db
        cls.app.dependency_overrides[get_task_publisher] = override_get_task_publisher
        cls.test_client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls):
        """Clean up dependency overrides after the entire test class."""
        cls.app.dependency_overrides.clear()
        cls.connect_nats_patcher.stop()
        cls.close_nats_patcher.stop()

    def setup_method(self):
        """Reset mocks before each test method."""
        self.mock_session.reset_mock()
        self.mock_connect_nats.reset_mock()
        self.mock_close_nats.reset_mock()
        self.mock_task_publisher.reset_mock()

    def teardown_method(self):
        """No teardown needed after each method with the new setup."""
        pass

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_webhook_missing_organization(self, mock_crud_org):
        """Test webhook returns 404 if organization is not found."""
        # Mock the CRUD method to return None (organization not found)
        mock_crud_org.get_with_tracker.return_value = None

        response = self.test_client.post(
            "/api/v1/private/webhooks/github/123",  # Assuming github for this test case
            json={"event": "test"},
            headers={
                "X-Hub-Signature-256": "sha256=dummy"
            },  # Dummy signature, won't be checked if org not found
        )

        # Verify CRUD method was called
        mock_crud_org.get_with_tracker.assert_called_once_with(
            self.mock_session, id="123"
        )
        assert response.status_code == 404
        assert response.json()["detail"] == "Organization not found"

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_webhook_missing_secret(self, mock_crud_org, configured_mock_org_fixture):
        """Test webhook returns 403 if organization has no webhook_secret."""
        current_org_mock = configured_mock_org_fixture
        setup_mock_webhook_secret(current_org_mock, None)  # webhook_secret is None
        # Ensure the mock tracker on the org mock is also set up if accessed
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json={"event": "test"},
            headers={
                "X-Hub-Signature-256": "sha256=dummy"
            },  # Signature won't be checked if secret is missing
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Webhook not configured correctly"

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_github_webhook_missing_signature(
        self,
        mock_crud_org,
        configured_mock_org_fixture,
    ):
        """Test GitHub webhook returns 403 if X-Hub-Signature-256 header is missing."""
        current_org_mock = configured_mock_org_fixture
        setup_mock_webhook_secret(current_org_mock, "a-test-secret")
        # Ensure the mock tracker on the org mock is also set up
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json={"event": "test"},
            headers={},  # No signature header
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Missing GitHub signature"

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_github_webhook_invalid_signature_method(
        self, mock_crud_org, configured_mock_org_fixture
    ):
        """Test GitHub webhook returns 403 when signature method is invalid."""
        current_org_mock = configured_mock_org_fixture
        setup_mock_webhook_secret(current_org_mock, "a-secret")
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json={"event": "test"},
            headers={
                "X-Hub-Signature-256": "sha1=invalid-signature-format"
            },  # Invalid method
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Unsupported GitHub signature method"

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_github_webhook_invalid_signature(
        self, mock_crud_org, configured_mock_org_fixture
    ):
        """Test GitHub webhook returns 403 when signature is invalid."""
        current_org_mock = configured_mock_org_fixture
        secret_to_use = "test-secret"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        payload_dict = {"event": "test"}
        # TestClient's `json=` param will do `json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")`
        # So, the signature must match this.
        payload_bytes_for_signature = json.dumps(
            payload_dict, separators=(",", ":")
        ).encode("utf-8")

        # Signature generated with a different secret
        invalid_signature = hmac.new(
            b"wrong-secret", payload_bytes_for_signature, hashlib.sha256
        ).hexdigest()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json=payload_dict,
            headers={"X-Hub-Signature-256": f"sha256={invalid_signature}"},
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Invalid GitHub signature"

    @patch("preloop.api.endpoints.webhooks.crud_issue_embedding")
    @patch("preloop.api.endpoints.webhooks.crud_issue")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    @patch("preloop.api.endpoints.webhooks.TrackerClient")
    def test_github_webhook_valid_signature(
        self,
        mock_tracker_client,
        mock_crud_project,
        mock_crud_issue,
        mock_crud_issue_embedding,
        configured_mock_org_fixture,
    ):
        """Test GitHub webhook with a valid signature (200)."""
        current_org_mock = configured_mock_org_fixture
        secret_to_use = "valid-github-secret-for-this-test"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        current_org_mock.tracker = MagicMock(name="MockTracker")
        current_org_mock.tracker.id = "tracker-gh-valid"
        current_org_mock.tracker.is_active = True
        current_org_mock.tracker.subscribed_events = ["issues"]

        self.mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = current_org_mock

        mock_tracker_instance = MagicMock()
        mock_tracker_client.return_value = mock_tracker_instance

        mock_project = MagicMock()
        mock_project.id = "project-123"
        mock_project.slug = "test/repo"
        mock_crud_project.get_by_identifier.return_value = mock_project

        mock_crud_issue.get_by_external_id.return_value = None
        mock_crud_issue.create.return_value = MagicMock()

        payload_dict = {
            "action": "opened",
            "issue": {
                "id": 456,
                "number": 1,
                "title": "Test Issue",
                "body": "Test Body",
            },
            "repository": {"id": 123, "full_name": "test/repo"},
            "organization": {"id": "test-org-fixture"},
        }
        payload_bytes_for_signature = json.dumps(
            payload_dict, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(
            secret_to_use.encode("utf-8"), payload_bytes_for_signature, hashlib.sha256
        ).hexdigest()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json=payload_dict,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "issues",
            },
        )

        assert response.status_code == 200
        response_json = response.json()
        assert response_json["status"] == "success"
        assert response_json["tracker_id"] == current_org_mock.tracker.id
        self.mock_session.add.assert_called()
        self.mock_session.commit.assert_called()

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_gitlab_webhook_missing_token(
        self, mock_crud_org, configured_mock_org_fixture
    ):
        """Test GitLab webhook returns 403 if X-Gitlab-Token header is missing."""
        current_org_mock = configured_mock_org_fixture
        setup_mock_webhook_secret(current_org_mock, "a-gitlab-secret")
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        response = self.test_client.post(
            f"/api/v1/private/webhooks/gitlab/{current_org_mock.id}",
            json={"event": "test"},
            headers={},  # No token header
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Missing GitLab token"

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_gitlab_webhook_invalid_token(
        self, mock_crud_org, configured_mock_org_fixture
    ):
        """Test GitLab webhook returns 403 when token is invalid."""
        current_org_mock = configured_mock_org_fixture
        setup_mock_webhook_secret(
            current_org_mock, "correct-gitlab-secret"
        )  # This sets a string secret
        current_org_mock.tracker = MagicMock()
        current_org_mock.tracker.is_active = True

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        response = self.test_client.post(
            f"/api/v1/private/webhooks/gitlab/{current_org_mock.id}",
            json={"event": "test"},
            headers={"X-Gitlab-Token": "invalid-token"},
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 403
        assert response.json()["detail"] == "Invalid GitLab token"

    @patch("preloop.api.endpoints.webhooks.crud_issue_embedding")
    @patch("preloop.api.endpoints.webhooks.crud_issue")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    @patch("preloop.api.endpoints.webhooks.TrackerClient")
    def test_gitlab_webhook_valid_token(
        self,
        mock_tracker_client,
        mock_crud_project,
        mock_crud_issue,
        mock_crud_issue_embedding,
        configured_mock_org_fixture,
    ):
        """Test GitLab webhook succeeds with valid token."""
        current_org_mock = configured_mock_org_fixture
        secret_to_use = "valid-gitlab-secret"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        current_org_mock.tracker = MagicMock(name="MockTrackerGL")
        current_org_mock.tracker.id = "tracker-gl-valid"
        current_org_mock.tracker.is_active = True
        current_org_mock.tracker.subscribed_events = ["Issue Hook"]

        self.mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = current_org_mock

        mock_tracker_instance = MagicMock()
        mock_tracker_client.return_value = mock_tracker_instance

        mock_project = MagicMock()
        mock_project.id = "project-123"
        mock_project.slug = "test/repo"
        mock_crud_project.get_by_identifier.return_value = mock_project

        mock_crud_issue.get_by_external_id.return_value = None
        mock_crud_issue.create.return_value = MagicMock()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/gitlab/{current_org_mock.id}",
            json={
                "object_kind": "issue",
                "object_attributes": {
                    "id": 1,
                    "title": "Test Issue",
                    "description": "Test Body",
                    "iid": 1,
                },
                "project": {"id": 123, "path_with_namespace": "test/repo"},
                "group_id": "test-org-fixture",
            },
            headers={
                "X-Gitlab-Token": secret_to_use,
                "X-Gitlab-Event": "Issue Hook",
            },
        )

        assert response.status_code == 200
        response_json = response.json()
        assert response_json["status"] == "success"
        assert response_json["tracker_id"] == current_org_mock.tracker.id
        self.mock_session.add.assert_called()
        self.mock_session.commit.assert_called()

    @patch(
        "preloop.api.endpoints.webhooks.crud_organization"
    )  # Keep patch, though mock_crud_org not directly used
    def test_unsupported_tracker_type(
        self, mock_crud_org_unused_param, configured_mock_org_fixture
    ):  # Corrected fixture name
        """Test webhook returns 400 for an unsupported tracker type."""
        # current_org_mock = configured_mock_org_fixture # Not needed
        # setup_mock_webhook_secret is irrelevant

        # mock_crud_instance = MagicMock()
        # mock_crud_instance.get_by_identifier.return_value = current_org_mock
        # mock_crud_org.return_value = mock_crud_instance
        # No db query should happen for an unsupported type before the type check

        tracker_name = "unsupported-tracker"
        response = self.test_client.post(
            f"/api/v1/private/webhooks/{tracker_name}/123",  # Identifier doesn't matter
            json={"event": "test"},
        )
        # mock_crud_instance.get_by_identifier.assert_not_called() # DB query for org should not happen
        self.mock_session.query.assert_not_called()  # No SQLAlchemy query should be made
        assert response.status_code == 400
        assert (
            response.json()["detail"]
            == f"Unsupported tracker_type: {tracker_name}"  # Updated expected message
        )

    @patch("preloop.api.endpoints.webhooks.crud_organization")
    def test_invalid_json_payload(self, mock_crud_org, configured_mock_org_fixture):
        """Test webhook returns 400 when payload is not valid JSON, even with valid GitHub signature."""
        current_org_mock = configured_mock_org_fixture
        secret_to_use = "json-error-secret"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        # Mock the tracker relationship
        current_org_mock.tracker = MagicMock(name="MockTrackerJsonError")
        current_org_mock.tracker.id = "tracker-gh-json-error"
        current_org_mock.tracker.is_active = True
        # Subscribed events don't matter as much if JSON parsing fails before event type check
        current_org_mock.tracker.subscribed_events = ["some_event"]

        # Mock CRUD method to return the organization
        mock_crud_org.get_with_tracker.return_value = current_org_mock

        invalid_payload_bytes = b"this is not json {{{{,"
        signature = hmac.new(
            secret_to_use.encode("utf-8"), invalid_payload_bytes, hashlib.sha256
        ).hexdigest()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            content=invalid_payload_bytes,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "Content-Type": "application/json",  # Important for FastAPI to attempt JSON parsing
                "X-GitHub-Event": "some_event",  # Provide event header
            },
        )

        mock_crud_org.get_with_tracker.assert_called_once()
        assert response.status_code == 400
        assert "Invalid JSON payload" in response.json()["detail"]

    @patch("preloop.api.endpoints.webhooks.crud_issue_embedding")
    @patch("preloop.api.endpoints.webhooks.crud_issue")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    @patch("preloop.api.endpoints.webhooks.TrackerClient")
    def test_database_error_on_update(
        self,
        mock_tracker_client,
        mock_crud_project,
        mock_crud_issue,
        mock_crud_issue_embedding,
        configured_mock_org_fixture,
    ):
        """Test webhook handles database errors gracefully during DB update.

        With the new behavior, if database processing fails but NATS publishing
        succeeds, the webhook returns 200 with partial_success status to ensure
        that flows can still be triggered even if database operations fail.
        """
        current_org_mock = configured_mock_org_fixture
        current_org_mock.identifier = "db-error-org-final"
        secret_to_use = "db-error-secret-final"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        current_org_mock.tracker = MagicMock(name="MockTrackerDBError")
        current_org_mock.tracker.id = "tracker-gh-db-error"
        current_org_mock.tracker.is_active = True
        current_org_mock.tracker.subscribed_events = ["issues"]

        self.mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = current_org_mock

        mock_tracker_instance = MagicMock()
        mock_tracker_client.return_value = mock_tracker_instance
        mock_crud_project.get_by_identifier.side_effect = Exception(
            "Simulated DB error"
        )

        payload_dict = {
            "action": "opened",
            "issue": {"number": 1, "title": "Test Issue", "body": "Test Body"},
            "repository": {"id": 123, "full_name": "test/repo"},
            "organization": {"id": "db-error-org-final"},
        }
        payload_bytes_for_signature = json.dumps(
            payload_dict, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(
            secret_to_use.encode("utf-8"), payload_bytes_for_signature, hashlib.sha256
        ).hexdigest()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json=payload_dict,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "issues",
            },
        )

        # New behavior: DB fails but NATS succeeds -> return 200 with partial_success
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["status"] == "partial_success"
        assert response_data["nats_published"] is True
        assert response_data["db_processed"] is False

    @pytest.mark.skip(
        reason="Test is failing intermittently and needs to be refactored."
    )
    @patch("preloop.api.endpoints.webhooks.get_task_publisher")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    def test_webhook_project_not_found_triggers_sync(
        self,
        mock_crud_project,
        mock_get_task_publisher,
        configured_mock_org_fixture,
    ):
        mock_task_publisher = AsyncMock()
        mock_get_task_publisher.return_value = mock_task_publisher
        current_org_mock = configured_mock_org_fixture
        secret_to_use = "project-not-found-secret"
        setup_mock_webhook_secret(current_org_mock, secret_to_use)
        current_org_mock.tracker = MagicMock(name="MockTrackerProjectNotFound")
        current_org_mock.tracker.id = "tracker-gh-project-not-found"
        current_org_mock.tracker.is_active = True
        current_org_mock.tracker.tracker_type = "github"
        current_org_mock.tracker.subscribed_events = ["issues"]

        self.mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = current_org_mock
        mock_crud_project.get_by_identifier.return_value = None

        payload_dict = {
            "action": "opened",
            "issue": {
                "id": 789,
                "number": 2,
                "title": "Another Test Issue",
                "body": "Another Test Body",
            },
            "repository": {"id": 456, "full_name": "new/repo"},
            "organization": {"id": "test-org-fixture"},
        }
        payload_bytes_for_signature = json.dumps(
            payload_dict, separators=(",", ":")
        ).encode("utf-8")
        signature = hmac.new(
            secret_to_use.encode("utf-8"), payload_bytes_for_signature, hashlib.sha256
        ).hexdigest()

        response = self.test_client.post(
            f"/api/v1/private/webhooks/github/{current_org_mock.id}",
            json=payload_dict,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "issues",
            },
        )

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def _post_unknown_project_webhook(self, org_mock, secret):
        """POST an issues webhook whose repository id is unknown to us."""
        payload_dict = {
            "action": "opened",
            "issue": {"id": 789, "number": 2, "title": "T", "body": "B"},
            "repository": {"id": 1327729611, "full_name": "acme/unknown"},
            "organization": {"id": "test-org-fixture"},
        }
        body = json.dumps(payload_dict, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return self.test_client.post(
            f"/api/v1/private/webhooks/github/{org_mock.id}",
            json=payload_dict,
            headers={
                "X-Hub-Signature-256": f"sha256={signature}",
                "X-GitHub-Event": "issues",
            },
        )

    def _unknown_project_org(self, configured_mock_org_fixture, secret):
        """Wire up an org whose tracker will see an unresolvable project."""
        org = configured_mock_org_fixture
        setup_mock_webhook_secret(org, secret)
        org.tracker = MagicMock(name="MockTrackerUnknownProject")
        org.tracker.id = "tracker-gh-unknown-project"
        org.tracker.account_id = "account-1"
        org.tracker.is_active = True
        org.tracker.tracker_type = "github"
        org.tracker.subscribed_events = ["issues"]
        self.mock_session.query.return_value.options.return_value.filter.return_value.first.return_value = org
        return org

    @patch("preloop.api.endpoints.webhooks.crud_tracker")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    def test_unknown_project_backoff_suppresses_sync(
        self, mock_crud_project, mock_crud_tracker, configured_mock_org_fixture
    ):
        """While backoff is active no sync task is published (the loop fix)."""
        secret = "unknown-project-secret"
        org = self._unknown_project_org(configured_mock_org_fixture, secret)
        mock_crud_project.get_by_identifier.return_value = None
        mock_crud_tracker.record_unknown_project.return_value = UnknownProjectState(
            should_sync=False, attempts=2, degraded=False, retry_after_seconds=600
        )

        response = self._post_unknown_project_webhook(org, secret)

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
        published = [
            c.args[0] for c in self.mock_task_publisher.publish_task.call_args_list
        ]
        assert "poll_tracker" not in published

    @patch("preloop.api.endpoints.webhooks.crud_tracker")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    def test_unknown_project_degraded_reports_needs_attention(
        self, mock_crud_project, mock_crud_tracker, configured_mock_org_fixture
    ):
        """Once degraded we stop syncing and say the tracker needs attention."""
        secret = "unknown-project-degraded-secret"
        org = self._unknown_project_org(configured_mock_org_fixture, secret)
        mock_crud_project.get_by_identifier.return_value = None
        mock_crud_tracker.record_unknown_project.return_value = UnknownProjectState(
            should_sync=False, attempts=5, degraded=True, retry_after_seconds=3600
        )

        response = self._post_unknown_project_webhook(org, secret)

        assert response.status_code == 200
        assert response.json()["degraded"] is True
        published = [
            c.args[0] for c in self.mock_task_publisher.publish_task.call_args_list
        ]
        assert "poll_tracker" not in published

    @patch("preloop.api.endpoints.webhooks.crud_tracker")
    @patch("preloop.api.endpoints.webhooks.crud_project")
    def test_unknown_project_first_sighting_still_syncs(
        self, mock_crud_project, mock_crud_tracker, configured_mock_org_fixture
    ):
        """The first sighting must still trigger exactly one sync."""
        secret = "unknown-project-first-secret"
        org = self._unknown_project_org(configured_mock_org_fixture, secret)
        mock_crud_project.get_by_identifier.return_value = None
        mock_crud_tracker.record_unknown_project.return_value = UnknownProjectState(
            should_sync=True, attempts=1, degraded=False, retry_after_seconds=300
        )

        response = self._post_unknown_project_webhook(org, secret)

        assert response.status_code == 200
        published = [
            c.args[0] for c in self.mock_task_publisher.publish_task.call_args_list
        ]
        assert published.count("poll_tracker") == 1


@pytest.mark.asyncio
@patch("preloop.models.db.session.get_db_session")
@patch("preloop.sync.services.event_bus.get_task_publisher")
async def test_receive_webhook_unsupported_tracker(
    mock_get_task_publisher, mock_get_db_session
):
    """
    Tests the receive_webhook function with an unsupported tracker type.
    """
    request = AsyncMock()
    request.body = AsyncMock(return_value=b"{}")
    with pytest.raises(HTTPException):
        await webhooks.receive_webhook("invalid", "123", request)


@pytest.mark.asyncio
@patch("preloop.models.db.session.get_db_session")
@patch("preloop.sync.services.event_bus.get_task_publisher")
async def test_receive_webhook_github_success(
    mock_get_task_publisher, mock_get_db_session
):
    """
    Tests the receive_webhook function for a successful GitHub webhook.
    """
    request = AsyncMock()
    request.body = AsyncMock(return_value=b'{"test": "payload"}')
    request.headers = {"X-GitHub-Event": "push", "X-Hub-Signature-256": "sha256=test"}

    db_session = MagicMock()
    organization = MagicMock()
    organization.webhook_secret = "secret"
    db_session.query.return_value.options.return_value.filter.return_value.first.return_value = organization
    mock_get_db_session.return_value = db_session

    task_publisher = AsyncMock()
    mock_get_task_publisher.return_value = task_publisher

    with patch("hmac.compare_digest", return_value=True):
        result = await webhooks.receive_webhook(
            "github", "123", request, db_session, task_publisher
        )
        assert result["status"] == "success"
