import pytest
from unittest.mock import Mock
from datetime import datetime

from preloop.sync.trackers.base import BaseTracker


class ConcreteTracker(BaseTracker):
    async def get_organizations(self):
        return []

    async def get_projects(self, organization_id):
        return []

    async def get_issues(self, organization_id, project_id, since=None):
        return []

    async def register_webhook(self, **kwargs):
        return True

    async def unregister_webhook(self, **kwargs):
        return True

    async def is_webhook_registered(self, webhook):
        return True

    async def get_webhooks(self):
        return []

    async def delete_webhook(self, webhook):
        return True

    async def unregister_all_webhooks(self, webhook_url_pattern=None):
        return {"unregistered": 0, "failed": 0, "not_found": 0}

    async def is_webhook_registered_for_project(self, project, webhook_url):
        return True

    async def is_webhook_registered_for_organization(self, organization, webhook_url):
        return True

    async def test_connection(self):
        pass

    async def get_project_metadata(self, project_key):
        pass

    async def search_issues(self, project_key, filter_params, limit=10, offset=0):
        pass

    async def get_issue(self, issue_id):
        pass

    async def get_comments(self, issue_id):
        return []

    async def create_issue(self, project_key, issue_data):
        pass

    async def update_issue(self, issue_id, issue_data):
        pass

    async def add_comment(self, issue_id, comment):
        pass

    async def add_relation(self, issue_id, related_issue_id, relation_type):
        pass


@pytest.fixture
def tracker():
    return ConcreteTracker("tracker-1", "api-key", {})


def test_transform_organization(tracker):
    org_data = {"id": "org-1", "name": "Test Org"}
    transformed = tracker.transform_organization(org_data)
    assert transformed["identifier"] == "org-1"
    assert transformed["name"] == "Test Org"
    assert transformed["tracker_id"] == "tracker-1"


def test_transform_project(tracker):
    proj_data = {"id": "proj-1", "name": "Test Proj", "description": "A test project"}
    transformed = tracker.transform_project(proj_data, "org-db-id")
    assert transformed["identifier"] == "proj-1"
    assert transformed["name"] == "Test Proj"
    assert transformed["organization_id"] == "org-db-id"


def test_transform_issue(tracker):
    issue_data = {
        "id": "issue-1",
        "title": "Test Issue",
        "description": "A test issue",
        "state": "open",
        "created_at": datetime.now(),
        "updated_at": datetime.now(),
    }
    mock_project = Mock()
    mock_project.id = "project-db-id"
    transformed = tracker.transform_issue(issue_data, mock_project)
    assert transformed["external_id"] == "issue-1"
    assert transformed["title"] == "Test Issue"
    assert transformed["project_id"] == "project-db-id"


def test_transform_comment(tracker):
    comment_data = {"id": "comment-1", "body": "A test comment"}
    transformed = tracker.transform_comment(comment_data, "issue-db-id")
    assert transformed["external_id"] == "comment-1"
    assert transformed["body"] == "A test comment"
    assert transformed["issue_id"] == "issue-db-id"


def test_transform_issue_keeps_native_estimate_fields(tracker):
    issue_data = {
        "id": "issue-2",
        "title": "Estimated",
        "state": "opened",
        "time_stats": {"time_estimate": 7200, "total_time_spent": 60},
        "weight": 3,
    }
    mock_project = Mock()
    mock_project.id = "project-db-id"
    transformed = tracker.transform_issue(issue_data, mock_project)
    assert transformed["meta_data"]["estimate_fields"] == {
        "time_estimate": 7200,
        "weight": 3,
    }


def test_transform_issue_without_estimate_stores_nothing(tracker):
    mock_project = Mock()
    mock_project.id = "project-db-id"
    transformed = tracker.transform_issue(
        {"id": "issue-3", "title": "Plain", "state": "open"}, mock_project
    )
    assert transformed["meta_data"]["estimate_fields"] == {}
