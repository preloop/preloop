import pytest
import unittest
from unittest.mock import patch, MagicMock
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
class TestGitHubTrackerDependencies(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_parses_dependencies_from_body_and_comments(
        self, mock_requests_get
    ):
        """Test that dependency parsing is integrated into issue fetching."""
        # Mock API responses
        mock_issues_response = {
            "json": MagicMock(
                return_value=[
                    {
                        "id": 1,
                        "number": 1,
                        "title": "Test issue",
                        "body": "This closes #123 and fixes owner/other-repo#456",
                        "state": "open",
                        "created_at": "2023-01-01T00:00:00Z",
                        "updated_at": "2023-01-01T00:00:00Z",
                        "user": {
                            "id": 1,
                            "login": "testuser",
                            "avatar_url": "https://avatar.url",
                        },
                    }
                ]
            ),
            "status_code": 200,
            "links": {},
        }

        mock_comments_response = {
            "json": MagicMock(
                return_value=[
                    {
                        "id": 1,
                        "body": "This relates to #789",
                        "created_at": "2023-01-01T01:00:00Z",
                        "updated_at": "2023-01-01T01:00:00Z",
                        "html_url": "https://github.com/owner/repo/issues/1#issuecomment-1",
                        "user": {
                            "id": 1,
                            "login": "testuser",
                            "avatar_url": "https://avatar.url",
                        },
                    }
                ]
            ),
            "status_code": 200,
            "links": {},
        }

        # Set up mock responses
        mock_requests_get.side_effect = [
            MagicMock(**mock_issues_response),  # Issues response
            MagicMock(**mock_comments_response),  # Comments response
        ]

        # Create tracker instance
        tracker = GitHubTracker("test-tracker", "test-token", {})

        # Call get_issues
        issues = await tracker.get_issues("test-org", "owner/repo")

        # Verify the issue was processed and dependencies were parsed
        assert len(issues) == 1
        issue = issues[0]

        # Check that dependencies were parsed and added
        assert "dependencies" in issue
        dependencies = issue["dependencies"]

        # Should have 3 dependencies: 2 from body + 1 from comment
        assert len(dependencies) == 3

        # Check dependencies from body
        body_deps = [
            d
            for d in dependencies
            if d["target_key"] in ["owner/repo#123", "owner/other-repo#456"]
        ]
        assert len(body_deps) == 2
        assert any(
            d["type"] == "closes" and d["target_key"] == "owner/repo#123"
            for d in body_deps
        )
        assert any(
            d["type"] == "closes" and d["target_key"] == "owner/other-repo#456"
            for d in body_deps
        )

        # Check dependency from comment
        comment_deps = [d for d in dependencies if d["target_key"] == "owner/repo#789"]
        assert len(comment_deps) == 1
        assert comment_deps[0]["type"] == "related"


@pytest.mark.asyncio
class TestGitHubTrackerComments(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_fetches_and_transforms_comments(self, mock_requests_get):
        # --- Mock API Responses ---
        # Response for repo details (if project_id is not full_name)
        mock_repo_details_response = MagicMock()
        mock_repo_details_response.status_code = 200
        mock_repo_details_response.json.return_value = {
            "id": 1296269,
            "full_name": "octocat/Hello-World",
            "name": "Hello-World",
            # ... other fields if needed by the tracker
        }

        # Response for issues list
        mock_issues_list_response = MagicMock()
        mock_issues_list_response.status_code = 200
        mock_issues_list_response.json.return_value = [
            {
                "id": 1,
                "number": 1347,
                "title": "Found a bug",
                "body": "I'm having a problem with this.",
                "state": "open",
                "user": {"login": "octocat", "id": 1},
                "labels": [{"name": "bug"}],
                "assignees": [{"login": "octocat", "id": 1}],
                "created_at": "2011-04-22T13:33:48Z",
                "updated_at": "2011-04-22T13:33:48Z",
                "html_url": "https://github.com/octocat/Hello-World/issues/1347",
            }
        ]

        # Response for issue comments
        mock_comments_response = MagicMock()
        mock_comments_response.status_code = 200
        mock_comments_response.json.return_value = [
            {
                "id": 101,
                "user": {"login": "commenter", "id": 2, "avatar_url": ""},
                "body": "This is a comment on the issue.",
                "created_at": "2011-04-22T14:00:00Z",
                "updated_at": "2011-04-22T14:00:00Z",
                "html_url": "https://github.com/octocat/Hello-World/issues/1347#issuecomment-101",
            }
        ]

        # Configure mock_requests_get to return different responses based on URL
        async def side_effect_requests_get(url, headers, params=None):
            if "repositories/project-id-123" in url:
                return mock_repo_details_response  # If project_id is an ID
            if (
                "repos/octocat/Hello-World/issues"
                == url.split("?")[0].replace(GitHubTracker.API_BASE_URL + "/", "")
                and params.get("state") == "all"
            ):
                return mock_issues_list_response
            if "repos/octocat/Hello-World/issues/1347/comments" in url:
                return mock_comments_response
            # Fallback for unexpected calls
            fallback_response = MagicMock()
            fallback_response.status_code = 404
            fallback_response.json.return_value = {"message": "Not Found"}
            print(
                f"UNMOCKED URL in test: {url} with params {params}"
            )  # For debugging tests
            return fallback_response

        mock_requests_get.side_effect = side_effect_requests_get

        # --- Initialize Tracker ---
        tracker = GitHubTracker(
            tracker_id="test-github-tracker",
            api_key="fake_token",
            connection_details={},
        )

        # --- Call get_issues ---
        # Using 'octocat/Hello-World' directly as project_id to simplify one mock path
        raw_issues = await tracker.get_issues(
            organization_id="octocat", project_id="octocat/Hello-World"
        )

        # --- Assertions ---
        self.assertEqual(len(raw_issues), 1, "Should return one issue")

        project_mock = MagicMock()
        project_mock.id = "proj-123"
        project_mock.slug = "octocat/Hello-World"
        transformed_issue = tracker.transform_issue(raw_issues[0], project_mock)

        self.assertEqual(transformed_issue["external_id"], "1")
        self.assertEqual(transformed_issue["key"], "octocat/Hello-World#1347")
        self.assertEqual(transformed_issue["title"], "Found a bug")
        self.assertEqual(
            len(transformed_issue["comments"]), 1, "Should include one comment"
        )

        comment_data = transformed_issue["comments"][0]
        self.assertEqual(comment_data["id"], 101)
        self.assertEqual(comment_data["body"], "This is a comment on the issue.")
        self.assertEqual(comment_data["user"]["login"], "commenter")
        self.assertEqual(
            comment_data["created_at"],
            "2011-04-22T14:00:00Z",
        )

        # Verify API calls (simplified check of calls made)
        # Check that requests.get was called at least for issues and comments
        # A more specific check would involve asserting call_args_list
        self.assertTrue(mock_requests_get.call_count >= 2)


@pytest.mark.asyncio
class TestGitHubTracker(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_organizations(self, mock_requests_get):
        mock_user_response = MagicMock()
        mock_user_response.status_code = 200
        mock_user_response.json.return_value = {
            "login": "octocat",
            "html_url": "https://github.com/octocat",
        }

        mock_orgs_response = MagicMock()
        mock_orgs_response.status_code = 200
        mock_orgs_response.json.return_value = [
            {
                "id": 1,
                "login": "github",
                "url": "https://api.github.com/orgs/github",
            }
        ]

        mock_requests_get.side_effect = [mock_user_response, mock_orgs_response]

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        orgs = await tracker.get_organizations()

        self.assertEqual(len(orgs), 2)
        self.assertEqual(orgs[0]["name"], "octocat")
        self.assertEqual(orgs[1]["name"], "github")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_projects(self, mock_requests_get):
        mock_repos_response = MagicMock()
        mock_repos_response.status_code = 200
        mock_repos_response.json.return_value = [
            {
                "id": 1296269,
                "name": "Hello-World",
                "full_name": "octocat/Hello-World",
                "description": "My first repository on GitHub!",
                "html_url": "https://github.com/octocat/Hello-World",
                "default_branch": "main",
                "language": "JavaScript",
                "created_at": "2011-01-26T19:01:12Z",
                "pushed_at": "2011-01-26T19:14:43Z",
                "stargazers_count": 80,
            }
        ]
        mock_requests_get.return_value = mock_repos_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        projects = await tracker.get_projects("octocat")

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "Hello-World")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_projects_personal(self, mock_requests_get):
        mock_repos_response = MagicMock()
        mock_repos_response.status_code = 200
        mock_repos_response.json.return_value = [
            {
                "id": 1296269,
                "name": "Hello-World",
                "full_name": "octocat/Hello-World",
                "description": "My first repository on GitHub!",
                "html_url": "https://github.com/octocat/Hello-World",
                "default_branch": "main",
                "language": "JavaScript",
                "created_at": "2011-01-26T19:01:12Z",
                "pushed_at": "2011-01-26T19:14:43Z",
                "stargazers_count": 80,
            }
        ]
        mock_requests_get.return_value = mock_repos_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        projects = await tracker.get_projects("personal")

        self.assertEqual(len(projects), 1)
        self.assertEqual(projects[0]["name"], "Hello-World")
        mock_requests_get.assert_called_with(
            "https://api.github.com/user/repos",
            headers=tracker.headers,
            params={"per_page": 100, "sort": "updated", "direction": "desc"},
        )

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_repo_details_error(self, mock_requests_get):
        mock_requests_get.side_effect = TrackerResponseError("Failed to fetch")

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        issues = await tracker.get_issues("octocat", "12345")

        self.assertEqual(len(issues), 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_fetch_error(self, mock_requests_get):
        mock_repo_details_response = MagicMock()
        mock_repo_details_response.status_code = 200
        mock_repo_details_response.json.return_value = {
            "full_name": "octocat/Hello-World"
        }

        async def side_effect(*args, **kwargs):
            if "repositories" in args[0]:
                return mock_repo_details_response
            else:
                raise TrackerResponseError("Failed to fetch")

        mock_requests_get.side_effect = side_effect

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        issues = await tracker.get_issues("octocat", "12345")

        self.assertEqual(len(issues), 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_skips_pull_requests(self, mock_requests_get):
        mock_issues_response = MagicMock()
        mock_issues_response.status_code = 200
        mock_issues_response.json.return_value = [
            {
                "id": 1,
                "number": 1347,
                "title": "Found a bug",
                "pull_request": {},
            }
        ]
        mock_requests_get.return_value = mock_issues_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        issues = await tracker.get_issues("octocat", "octocat/Hello-World")

        self.assertEqual(len(issues), 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_get_issues_no_comments(self, mock_requests_get):
        mock_issues_response = MagicMock()
        mock_issues_response.status_code = 200
        mock_issues_response.json.return_value = [
            {
                "id": 1,
                "number": 1347,
                "title": "Found a bug",
                "body": "I'm having a problem with this.",
                "state": "open",
                "user": {"login": "octocat"},
                "labels": [],
                "assignees": [],
                "created_at": "2011-04-22T13:33:48Z",
                "updated_at": "2011-04-22T13:33:48Z",
                "html_url": "https://github.com/octocat/Hello-World/issues/1347",
            }
        ]

        mock_comments_response = MagicMock()
        mock_comments_response.status_code = 200
        mock_comments_response.json.return_value = []

        async def side_effect(*args, **kwargs):
            if "comments" in args[0]:
                return mock_comments_response
            return mock_issues_response

        mock_requests_get.side_effect = side_effect

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        issues = await tracker.get_issues("octocat", "octocat/Hello-World")

        self.assertEqual(len(issues), 1)
        self.assertEqual(len(issues[0]["comments"]), 0)


@pytest.mark.asyncio
class TestGitHubTrackerRequestErrors(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_make_request_authentication_error(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_requests_get.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerAuthenticationError):
            await tracker._make_request("user")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_make_request_connection_error(self, mock_requests_get):
        mock_requests_get.side_effect = httpx.RequestError(
            "Connection failed", request=MagicMock()
        )

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerConnectionError):
            await tracker._make_request("user")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_make_request_response_error(self, mock_requests_get):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_requests_get.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerResponseError):
            await tracker._make_request("user")


@pytest.mark.asyncio
class TestGitHubTrackerDeleteRequest(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_make_request_delete_success(self, mock_requests_delete):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_requests_delete.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        self.assertTrue(await tracker._make_request_delete("hooks/1"))

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_make_request_delete_not_found(self, mock_requests_delete):
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_requests_delete.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        self.assertTrue(await tracker._make_request_delete("hooks/1"))

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_make_request_delete_authentication_error(self, mock_requests_delete):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_requests_delete.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerAuthenticationError):
            await tracker._make_request_delete("hooks/1")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_make_request_delete_connection_error(self, mock_requests_delete):
        mock_requests_delete.side_effect = httpx.RequestError(
            "Connection failed", request=MagicMock()
        )

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerConnectionError):
            await tracker._make_request_delete("hooks/1")

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_make_request_delete_response_error(self, mock_requests_delete):
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_requests_delete.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        with self.assertRaises(TrackerResponseError):
            await tracker._make_request_delete("hooks/1")


@pytest.mark.asyncio
class TestGitHubTrackerWebhooks(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.logger.error")
    async def test_register_webhook_requires_all_arguments(self, mock_logger_error):
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        result = await tracker.register_webhook(
            db=None,
            organization=None,
            webhook_url=None,
            secret=None,
        )

        self.assertFalse(result)
        mock_logger_error.assert_called_once_with(
            "GitHub webhook registration requires db, organization, URL, and secret."
        )

    @patch("preloop.sync.trackers.github.logger.error")
    async def test_unregister_webhook_requires_db_and_webhook(self, mock_logger_error):
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        result = await tracker.unregister_webhook(db=None, webhook=None)

        self.assertFalse(result)
        mock_logger_error.assert_called_once_with(
            "GitHub webhook unregistration requires db and webhook."
        )

    @patch("preloop.sync.trackers.github.logger.error")
    async def test_get_webhooks_requires_organization_identifier(
        self, mock_logger_error
    ):
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        webhooks = await tracker.get_webhooks()

        self.assertEqual(webhooks, [])
        mock_logger_error.assert_called_once_with(
            "GitHub webhook listing requires an organization identifier."
        )

    @patch("preloop.sync.trackers.github.GitHubTracker.get_projects")
    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_get_webhooks(self, mock_make_request, mock_get_projects):
        mock_get_projects.return_value = [
            {"meta_data": {"full_name": "octocat/Hello-World"}}
        ]
        mock_make_request.return_value = [
            {
                "id": 1,
                "url": "https://example.com/webhook",
                "config": {"url": "https://example.com/webhook"},
            }
        ]

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        webhooks = await tracker.get_webhooks("octocat")

        self.assertEqual(len(webhooks), 1)
        self.assertEqual(webhooks[0]["id"], 1)
        mock_make_request.assert_called_with(
            "repos/octocat/Hello-World/hooks", params={"per_page": 100}
        )

    @patch("preloop.sync.trackers.github.httpx.AsyncClient.post")
    @patch("preloop.sync.trackers.github.crud_webhook.create")
    async def test_register_webhook(self, mock_crud_create, mock_requests_post):
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "id": 1,
            "url": "https://example.com/webhook",
            "config": {"url": "https://example.com/webhook"},
        }
        mock_requests_post.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        db_mock = MagicMock()
        organization_mock = MagicMock()
        organization_mock.identifier = "octocat"
        result = await tracker.register_webhook(
            db=db_mock,
            organization=organization_mock,
            webhook_url="https://example.com/webhook",
            secret="secret",
        )

        self.assertTrue(result)

    @patch("preloop.sync.trackers.github.crud_webhook.remove")
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.delete")
    async def test_unregister_webhook(self, mock_requests_delete, mock_crud_remove):
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_requests_delete.return_value = mock_response

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        db_mock = MagicMock()
        webhook_mock = MagicMock()
        webhook_mock.id = "webhook-db-id"
        webhook_mock.external_id = "1"
        webhook_mock.project.slug = "octocat/Hello-World"
        webhook_mock.organization.identifier = "octocat"
        result = await tracker.unregister_webhook(db=db_mock, webhook=webhook_mock)

        self.assertTrue(result)
        mock_requests_delete.assert_called_with(
            "https://api.github.com/repos/octocat/Hello-World/hooks/1",
            headers=tracker.headers,
        )
        mock_crud_remove.assert_called_with(db_mock, id="webhook-db-id")

    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_is_webhook_registered_for_project(self, mock_make_request):
        mock_make_request.return_value = [
            {"config": {"url": "https://example.com/webhook"}}
        ]

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        project_mock = MagicMock()
        project_mock.slug = "octocat/Hello-World"
        is_registered = await tracker.is_webhook_registered_for_project(
            project_mock, "https://example.com/webhook"
        )

        self.assertTrue(is_registered)
        mock_make_request.assert_called_with("repos/octocat/Hello-World/hooks")

    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_is_webhook_not_registered_for_project(self, mock_make_request):
        mock_make_request.return_value = [
            {"config": {"url": "https://example.com/other-webhook"}}
        ]

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        project_mock = MagicMock()
        project_mock.slug = "octocat/Hello-World"
        is_registered = await tracker.is_webhook_registered_for_project(
            project_mock, "https://example.com/webhook"
        )

        self.assertFalse(is_registered)
        mock_make_request.assert_called_with("repos/octocat/Hello-World/hooks")


class TestGitHubTrackerPagination(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.httpx.AsyncClient.get")
    async def test_make_request_pagination(self, mock_requests_get):
        mock_response_1 = MagicMock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = [{"id": 1}]
        mock_response_1.links = {
            "next": {"url": "https://api.github.com/user/repos?page=2"}
        }

        mock_response_2 = MagicMock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = [{"id": 2}]
        mock_response_2.links = {}

        mock_requests_get.side_effect = [mock_response_1, mock_response_2]

        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        results = await tracker._make_request("user/repos")

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], 1)
        self.assertEqual(results[1]["id"], 2)
        self.assertEqual(mock_requests_get.call_count, 2)


@pytest.mark.asyncio
class TestGitHubTrackerUnregisterAllWebhooks(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.crud_project.get_for_organization")
    @patch("preloop.sync.trackers.github.crud_organization.get_multi")
    @patch("preloop.sync.trackers.github.GitHubTracker.unregister_webhook")
    async def test_unregister_all_webhooks_success(
        self,
        mock_unregister_webhook,
        mock_crud_organization_get_multi,
        mock_crud_project_get_for_organization,
    ):
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        db_mock = MagicMock()

        mock_org = MagicMock()
        mock_org.id = "org-id-1"
        mock_crud_organization_get_multi.return_value = [mock_org]

        mock_project = MagicMock()
        mock_project.id = "project-id-1"
        mock_crud_project_get_for_organization.return_value = [mock_project]

        mock_webhook = MagicMock()
        db_mock.query.return_value.filter.return_value.all.return_value = [
            mock_webhook,
            mock_webhook,
        ]
        mock_unregister_webhook.return_value = True

        # Act
        results = await tracker.unregister_all_webhooks(db_mock)

        # Assert
        self.assertEqual(results["unregistered"], 2)
        self.assertEqual(results["failed"], 0)

    @patch("preloop.sync.trackers.github.crud_project.get_for_organization")
    @patch("preloop.sync.trackers.github.crud_organization.get_multi")
    @patch("preloop.sync.trackers.github.GitHubTracker.unregister_webhook")
    async def test_unregister_all_webhooks_failure(
        self,
        mock_unregister_webhook,
        mock_crud_organization_get_multi,
        mock_crud_project_get_for_organization,
    ):
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        db_mock = MagicMock()

        mock_org = MagicMock()
        mock_org.id = "org-id-1"
        mock_crud_organization_get_multi.return_value = [mock_org]

        mock_project = MagicMock()
        mock_project.id = "project-id-1"
        mock_crud_project_get_for_organization.return_value = [mock_project]

        mock_webhook = MagicMock()
        db_mock.query.return_value.filter.return_value.all.return_value = [
            mock_webhook,
            mock_webhook,
        ]
        mock_unregister_webhook.side_effect = [True, False]

        # Act
        results = await tracker.unregister_all_webhooks(db_mock)

        # Assert
        self.assertEqual(results["unregistered"], 1)
        self.assertEqual(results["failed"], 1)

    @patch("preloop.sync.trackers.github.crud_project.get_for_organization")
    @patch("preloop.sync.trackers.github.crud_organization.get_multi")
    async def test_unregister_all_webhooks_no_webhooks(
        self, mock_crud_organization_get_multi, mock_crud_project_get_for_organization
    ):
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        db_mock = MagicMock()

        mock_org = MagicMock()
        mock_org.id = "org-id-1"
        mock_crud_organization_get_multi.return_value = [mock_org]

        mock_project = MagicMock()
        mock_project.id = "project-id-1"
        mock_crud_project_get_for_organization.return_value = [mock_project]

        db_mock.query.return_value.filter.return_value.all.return_value = []

        # Act
        results = await tracker.unregister_all_webhooks(db_mock)

        # Assert
        self.assertEqual(results["unregistered"], 0)
        self.assertEqual(results["failed"], 0)


@pytest.mark.asyncio
class TestGitHubTrackerCleanupStaleWebhooks(unittest.IsolatedAsyncioTestCase):
    @patch("preloop.sync.trackers.github.GitHubTracker._make_request_delete")
    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_cleanup_stale_webhooks_org_hooks(
        self, mock_make_request, mock_make_request_delete
    ):
        """
        Test that cleanup_stale_webhooks only deletes webhooks that:
        1. Point to our preloop_url
        2. Are NOT in our database

        This test has a webhook pointing to preloop_url that's not in DB, so it should be deleted.
        """
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        mock_make_request.side_effect = [
            {"login": "user", "html_url": ""},
            [{"login": "org1", "url": "...", "id": "org1", "name": "org1"}],
            [{"config": {"url": "http://my-preloop.com/webhook"}, "id": 1}],
        ]
        mock_make_request_delete.return_value = True

        # Act
        results = await tracker.cleanup_stale_webhooks("http://my-preloop.com")

        # Assert: Should delete the stale webhook (points to preloop but not in DB)
        self.assertEqual(results["unregistered"], 1)
        self.assertEqual(results["failed"], 0)
        mock_make_request_delete.assert_called_with("orgs/org1/hooks/1")

    @patch("preloop.sync.trackers.github.GitHubTracker._make_request_delete")
    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_cleanup_stale_webhooks_project_hooks(
        self, mock_make_request, mock_make_request_delete
    ):
        """
        Test cleanup for project-level webhooks.
        A webhook pointing to preloop_url that's not in DB should be deleted.
        """
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        repo_meta = {
            "full_name": "org1/repo1",
            "id": "123",
            "name": "repo1",
            "description": "",
            "html_url": "",
            "default_branch": "main",
            "created_at": "2021-01-01T00:00:00Z",
            "pushed_at": "2021-01-01T00:00:00Z",
            "stargazers_count": 0,
            "meta_data": {"full_name": "org1/repo1"},
        }
        mock_make_request.side_effect = [
            {"login": "user", "html_url": ""},
            [{"login": "org1", "url": "...", "id": "org1", "name": "org1"}],
            [repo_meta],
            [{"config": {"url": "http://my-preloop.com/webhook"}, "id": 2}],
            [],
        ]
        mock_make_request_delete.return_value = True

        # Act
        results = await tracker.cleanup_stale_webhooks(
            "http://my-preloop.com", cleanup_projects=True
        )

        # Assert: Should delete the stale webhook (points to preloop but not in DB)
        self.assertEqual(results["unregistered"], 1)
        self.assertEqual(results["failed"], 0)
        mock_make_request_delete.assert_called_with("repos/org1/repo1/hooks/2")

    @patch("preloop.sync.trackers.github.GitHubTracker._make_request_delete")
    @patch("preloop.sync.trackers.github.GitHubTracker._make_request")
    async def test_cleanup_stale_webhooks_no_stale_hooks(
        self, mock_make_request, mock_make_request_delete
    ):
        """
        Test that webhooks pointing to OTHER URLs (not our preloop_url) are ignored.
        A webhook at "http://other-service.com/webhook" should NOT be deleted
        even though it's not in our DB, because it doesn't point to our Preloop.
        """
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})
        mock_make_request.side_effect = [
            {"login": "user", "html_url": ""},
            [{"login": "org1", "url": "...", "id": "org1", "name": "org1"}],
            [{"config": {"url": "http://other-service.com/webhook"}, "id": 1}],
        ]

        # Act
        results = await tracker.cleanup_stale_webhooks("http://my-preloop.com")

        # Assert: Should NOT delete webhooks that don't point to our Preloop
        self.assertEqual(results["unregistered"], 0)
        self.assertEqual(results["failed"], 0)
        mock_make_request_delete.assert_not_called()


class TestGitHubTrackerIssueOperations(IsolatedAsyncioTestCase):
    """Test GitHub tracker issue-related operations."""

    async def test_get_issue_success(self):
        """Test successful issue retrieval."""
        # Arrange
        issue_data = {
            "id": 12345,
            "number": 1,
            "title": "Test Issue",
            "body": "Issue description",
            "state": "open",
            "created_at": "2023-01-01T10:00:00Z",
            "updated_at": "2023-01-02T11:00:00Z",
            "html_url": "https://github.com/owner/repo/issues/1",
            "url": "https://api.github.com/repos/owner/repo/issues/1",
            "labels": [{"name": "bug"}, {"name": "critical"}],
            "assignees": [{"login": "user1"}, {"login": "user2"}],
            "assignee": {
                "id": 123,
                "login": "user1",
                "avatar_url": "https://example.com/avatar.png",
            },
            "user": {
                "id": 456,
                "login": "reporter",
                "avatar_url": "https://example.com/reporter.png",
            },
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Mock the _make_request method directly
        from unittest.mock import AsyncMock

        tracker._make_request = AsyncMock(return_value=issue_data)

        # Act
        result = await tracker.get_issue("1")

        # Assert - Now result is an Issue object, not a dict
        self.assertEqual(result.id, "12345")
        self.assertEqual(result.key, "testowner/testrepo#1")
        self.assertEqual(result.title, "Test Issue")
        self.assertEqual(result.description, "Issue description")
        self.assertEqual(result.status.id, "open")
        self.assertEqual(result.status.name, "Open")
        self.assertEqual(result.labels, ["bug", "critical"])
        self.assertEqual(result.url, "https://github.com/owner/repo/issues/1")
        self.assertIsNotNone(result.assignee)
        self.assertEqual(result.assignee.name, "user1")
        self.assertIsNotNone(result.reporter)
        self.assertEqual(result.reporter.name, "reporter")

        tracker._make_request.assert_called_once_with(
            "repos/testowner/testrepo/issues/1"
        )

    async def test_get_issue_pull_request_error(self):
        """Test error when trying to get a pull request as an issue."""
        # Arrange
        issue_data = {
            "id": 12345,
            "number": 1,
            "title": "Test PR",
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/1"},
        }

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Mock the _make_request method directly
        from unittest.mock import AsyncMock

        tracker._make_request = AsyncMock(return_value=issue_data)

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_issue("1")

        self.assertIn("is a pull request, not an issue", str(context.exception))

    async def test_get_issue_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_issue("1")

        self.assertIn(
            "Owner/repo not found in connection details", str(context.exception)
        )

    async def test_get_comments_success(self):
        """Test successful comments retrieval."""
        # Arrange
        comments_data = [
            {
                "id": 1001,
                "body": "First comment",
                "user": {
                    "id": 101,
                    "login": "commenter1",
                    "avatar_url": "https://avatars.github.com/u/101",
                },
                "created_at": "2023-01-01T12:00:00Z",
                "updated_at": "2023-01-01T12:00:00Z",
                "html_url": "https://github.com/owner/repo/issues/1#issuecomment-1001",
            },
            {
                "id": 1002,
                "body": "Second comment",
                "user": {
                    "id": 102,
                    "login": "commenter2",
                    "avatar_url": "https://avatars.github.com/u/102",
                },
                "created_at": "2023-01-01T13:00:00Z",
                "updated_at": "2023-01-01T13:00:00Z",
                "html_url": "https://github.com/owner/repo/issues/1#issuecomment-1002",
            },
        ]

        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Mock the _make_request method directly
        from unittest.mock import AsyncMock

        tracker._make_request = AsyncMock(return_value=comments_data)

        # Act
        result = await tracker.get_comments("1")

        # Assert
        self.assertEqual(len(result), 2)

        self.assertEqual(result[0].id, "1001")
        self.assertEqual(result[0].body, "First comment")
        self.assertEqual(result[0].author.id, "101")
        self.assertEqual(result[0].author.name, "commenter1")
        self.assertEqual(
            result[0].url, "https://github.com/owner/repo/issues/1#issuecomment-1001"
        )

        self.assertEqual(result[1].id, "1002")
        self.assertEqual(result[1].body, "Second comment")
        self.assertEqual(result[1].author.id, "102")
        self.assertEqual(result[1].author.name, "commenter2")

        tracker._make_request.assert_called_once_with(
            "repos/testowner/testrepo/issues/1/comments", params={"per_page": 100}
        )

    async def test_get_comments_missing_connection_details(self):
        """Test error when connection details are missing."""
        # Arrange
        tracker = GitHubTracker(str(uuid4()), "api-key", {})

        # Act & Assert
        with self.assertRaises(TrackerResponseError) as context:
            await tracker.get_comments("1")

        self.assertIn(
            "Owner/repo not found in connection details", str(context.exception)
        )

    async def test_get_comments_api_error(self):
        """Test handling of API errors when fetching comments."""
        # Arrange
        connection_details = {"owner": "testowner", "repo": "testrepo"}
        tracker = GitHubTracker(str(uuid4()), "api-key", connection_details)

        # Mock the _make_request method to raise an exception
        from unittest.mock import AsyncMock

        tracker._make_request = AsyncMock(
            side_effect=TrackerResponseError("404 Not Found")
        )

        # Act
        result = await tracker.get_comments("1")

        # Assert - should return empty list on error
        self.assertEqual(result, [])


@pytest.mark.asyncio
class TestGitHubTrackerTokenValidation(unittest.IsolatedAsyncioTestCase):
    """Tests for GitHub token permission validation."""

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_validate_token_permissions_valid_token_with_all_scopes(
        self, mock_client_class
    ):
        """Test validation with a token that has all required scopes."""
        from unittest.mock import AsyncMock

        # Setup mock response with all required scopes
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-OAuth-Scopes": "repo, admin:org_hook, user"}
        mock_response.json.return_value = {"login": "testuser"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker and validate
        tracker = GitHubTracker(str(uuid4()), "test-token", {})
        result = await tracker.validate_token_permissions()

        # Assert
        self.assertTrue(result["valid"])
        self.assertIn("repo", result["scopes"])
        self.assertIn("admin:org_hook", result["scopes"])
        self.assertEqual(len(result["warnings"]), 0)
        self.assertEqual(len(result["errors"]), 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_validate_token_permissions_missing_org_hook_scope(
        self, mock_client_class
    ):
        """Test validation warns when admin:org_hook scope is missing."""
        from unittest.mock import AsyncMock

        # Setup mock response without org_hook scope
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"X-OAuth-Scopes": "repo, user"}
        mock_response.json.return_value = {"login": "testuser"}

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker and validate
        tracker = GitHubTracker(str(uuid4()), "test-token", {})
        result = await tracker.validate_token_permissions()

        # Assert - should be valid but with warning
        self.assertTrue(result["valid"])
        self.assertTrue(
            any("admin:org_hook" in w for w in result["warnings"]),
            "Should warn about missing admin:org_hook scope",
        )

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_validate_token_permissions_invalid_token(self, mock_client_class):
        """Test validation fails with invalid token."""
        from unittest.mock import AsyncMock

        # Setup mock response for unauthorized
        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker and validate
        tracker = GitHubTracker(str(uuid4()), "invalid-token", {})
        result = await tracker.validate_token_permissions()

        # Assert - should not be valid
        self.assertFalse(result["valid"])
        self.assertTrue(len(result["errors"]) > 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_validate_token_permissions_with_org_admin_check(
        self, mock_client_class
    ):
        """Test validation checks org admin status when org_identifier provided."""
        from unittest.mock import AsyncMock

        # Setup mock responses
        user_response = MagicMock()
        user_response.status_code = 200
        user_response.headers = {"X-OAuth-Scopes": "repo, admin:org_hook"}
        user_response.json.return_value = {"login": "testuser"}

        membership_response = MagicMock()
        membership_response.status_code = 200
        membership_response.json.return_value = {"role": "admin", "state": "active"}

        mock_client = AsyncMock()
        mock_client.get.side_effect = [user_response, membership_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker and validate with org
        tracker = GitHubTracker(str(uuid4()), "test-token", {})
        result = await tracker.validate_token_permissions(org_identifier="test-org")

        # Assert - should be valid and user is admin
        self.assertTrue(result["valid"])
        self.assertTrue(result["is_org_admin"])
        self.assertEqual(len(result["warnings"]), 0)

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_validate_token_permissions_non_admin_user(self, mock_client_class):
        """Test validation warns when user is not an org admin."""
        from unittest.mock import AsyncMock

        # Setup mock responses
        user_response = MagicMock()
        user_response.status_code = 200
        user_response.headers = {"X-OAuth-Scopes": "repo, admin:org_hook"}
        user_response.json.return_value = {"login": "testuser"}

        membership_response = MagicMock()
        membership_response.status_code = 200
        membership_response.json.return_value = {"role": "member", "state": "active"}

        mock_client = AsyncMock()
        mock_client.get.side_effect = [user_response, membership_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker and validate with org
        tracker = GitHubTracker(str(uuid4()), "test-token", {})
        result = await tracker.validate_token_permissions(org_identifier="test-org")

        # Assert - should be valid but not admin with warning
        self.assertTrue(result["valid"])
        self.assertFalse(result["is_org_admin"])
        self.assertTrue(
            any("not an admin" in w for w in result["warnings"]),
            "Should warn that user is not an admin",
        )


@pytest.mark.asyncio
class TestGitHubTrackerReactions(unittest.IsolatedAsyncioTestCase):
    """Tests for reaction add/remove functionality."""

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_remove_issue_reaction_with_user_auth(self, mock_client_class):
        """Test removing a reaction with user token auth."""
        from unittest.mock import AsyncMock

        # Setup mock responses
        reactions_response = MagicMock()
        reactions_response.status_code = 200
        reactions_response.json.return_value = [
            {"id": 123, "content": "eyes", "user": {"login": "testuser"}},
            {"id": 456, "content": "eyes", "user": {"login": "otheruser"}},
        ]

        user_response = MagicMock()
        user_response.status_code = 200
        user_response.json.return_value = {"login": "testuser"}

        delete_response = MagicMock()
        delete_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.get.side_effect = [reactions_response, user_response]
        mock_client.delete.return_value = delete_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker with owner/repo
        tracker = GitHubTracker(
            str(uuid4()), "test-token", {"owner": "testowner", "repo": "testrepo"}
        )
        result = await tracker.remove_issue_reaction("42", "eyes")

        # Should delete the reaction owned by testuser (id 123), not otheruser
        self.assertTrue(result)
        mock_client.delete.assert_called_once()
        delete_call = mock_client.delete.call_args
        self.assertIn("/reactions/123", delete_call[0][0])

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_remove_issue_reaction_with_app_slug_in_connection_details(
        self, mock_client_class
    ):
        """Test removing a reaction with GitHub App when app_slug is in connection_details."""
        from unittest.mock import AsyncMock

        # Setup mock responses
        reactions_response = MagicMock()
        reactions_response.status_code = 200
        reactions_response.json.return_value = [
            {"id": 789, "content": "eyes", "user": {"login": "my-app[bot]"}},
            {"id": 456, "content": "eyes", "user": {"login": "humanuser"}},
        ]

        # GET /user returns 403 for app installation tokens
        user_response = MagicMock()
        user_response.status_code = 403

        delete_response = MagicMock()
        delete_response.status_code = 204

        mock_client = AsyncMock()
        mock_client.get.side_effect = [reactions_response, user_response]
        mock_client.delete.return_value = delete_response
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker with owner/repo AND app_slug in connection_details
        tracker = GitHubTracker(
            str(uuid4()),
            "test-token",
            {"owner": "testowner", "repo": "testrepo", "app_slug": "my-app"},
        )
        result = await tracker.remove_issue_reaction("42", "eyes")

        # Should delete the reaction owned by my-app[bot] (id 789)
        self.assertTrue(result)
        mock_client.delete.assert_called_once()
        delete_call = mock_client.delete.call_args
        self.assertIn("/reactions/789", delete_call[0][0])

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_remove_issue_reaction_installation_token_without_app_slug(
        self, mock_client_class
    ):
        """Test that reaction removal fails for installation tokens without app_slug.

        GitHub App installation tokens cannot call GET /user or GET /app.
        Without app_slug in connection_details, we cannot determine the bot identity.
        """
        from unittest.mock import AsyncMock

        # Setup mock responses
        reactions_response = MagicMock()
        reactions_response.status_code = 200
        reactions_response.json.return_value = [
            {"id": 789, "content": "eyes", "user": {"login": "my-app[bot]"}},
        ]

        # GET /user returns 403 for app installation tokens
        user_response = MagicMock()
        user_response.status_code = 403

        # GET /app also returns 403 for installation tokens (requires JWT)
        app_response = MagicMock()
        app_response.status_code = 403

        mock_client = AsyncMock()
        mock_client.get.side_effect = [reactions_response, user_response, app_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker WITHOUT app_slug in connection_details
        tracker = GitHubTracker(
            str(uuid4()),
            "test-token",
            {"owner": "testowner", "repo": "testrepo"},
        )
        result = await tracker.remove_issue_reaction("42", "eyes")

        # Should return False - cannot determine identity
        self.assertFalse(result)
        mock_client.delete.assert_not_called()

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_remove_issue_reaction_refuses_without_auth(self, mock_client_class):
        """Test that reaction removal refuses when auth cannot be determined."""
        from unittest.mock import AsyncMock

        # Setup mock responses
        reactions_response = MagicMock()
        reactions_response.status_code = 200
        reactions_response.json.return_value = [
            {"id": 123, "content": "eyes", "user": {"login": "someuser"}},
        ]

        # GET /user fails
        user_response = MagicMock()
        user_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get.side_effect = [reactions_response, user_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        # Create tracker with owner/repo
        tracker = GitHubTracker(
            str(uuid4()), "test-token", {"owner": "testowner", "repo": "testrepo"}
        )
        result = await tracker.remove_issue_reaction("42", "eyes")

        # Should return False and not attempt to delete
        self.assertFalse(result)
        mock_client.delete.assert_not_called()


@pytest.mark.asyncio
class TestGitHubTrackerPullRequestUpdates(unittest.IsolatedAsyncioTestCase):
    """Tests for PR update functionality including assignees/reviewers."""

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_update_pull_request_empty_assignees_clears_all(
        self, mock_client_class
    ):
        """Test that assignees=[] removes all existing assignees."""
        from unittest.mock import AsyncMock

        # PR data with existing assignees
        pr_data = {
            "id": 123,
            "number": 42,
            "title": "Test PR",
            "body": "Description",
            "state": "open",
            "html_url": "https://github.com/owner/repo/pull/42",
            "draft": False,
            "assignees": [{"login": "user1"}, {"login": "user2"}],
            "requested_reviewers": [],
        }

        patch_response = MagicMock()
        patch_response.status_code = 200
        patch_response.json.return_value = pr_data

        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.json.return_value = {}

        mock_client = AsyncMock()
        # PATCH for PR update, DELETE for clearing assignees
        mock_client.request.side_effect = [patch_response, delete_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        tracker = GitHubTracker(
            str(uuid4()), "test-token", {"owner": "testowner", "repo": "testrepo"}
        )
        await tracker.update_pull_request(pr_identifier="42", assignees=[])

        # Verify DELETE was called with existing assignees
        calls = mock_client.request.call_args_list
        self.assertEqual(len(calls), 2)

        # Second call should be DELETE for assignees
        delete_call = calls[1]
        self.assertEqual(delete_call[0][0], "DELETE")
        self.assertIn("/assignees", delete_call[0][1])
        self.assertEqual(delete_call[1]["json"]["assignees"], ["user1", "user2"])

    @patch("preloop.sync.trackers.github.httpx.AsyncClient")
    async def test_update_pull_request_empty_reviewers_clears_all(
        self, mock_client_class
    ):
        """Test that reviewers=[] removes all existing reviewers."""
        from unittest.mock import AsyncMock

        # PR data with existing reviewers
        pr_data = {
            "id": 123,
            "number": 42,
            "title": "Test PR",
            "body": "Description",
            "state": "open",
            "html_url": "https://github.com/owner/repo/pull/42",
            "draft": False,
            "assignees": [],
            "requested_reviewers": [{"login": "reviewer1"}, {"login": "reviewer2"}],
        }

        patch_response = MagicMock()
        patch_response.status_code = 200
        patch_response.json.return_value = pr_data

        delete_response = MagicMock()
        delete_response.status_code = 200
        delete_response.json.return_value = {}

        mock_client = AsyncMock()
        mock_client.request.side_effect = [patch_response, delete_response]
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = None
        mock_client_class.return_value = mock_client

        tracker = GitHubTracker(
            str(uuid4()), "test-token", {"owner": "testowner", "repo": "testrepo"}
        )
        await tracker.update_pull_request(pr_identifier="42", reviewers=[])

        # Verify DELETE was called with existing reviewers
        calls = mock_client.request.call_args_list
        self.assertEqual(len(calls), 2)

        # Second call should be DELETE for reviewers
        delete_call = calls[1]
        self.assertEqual(delete_call[0][0], "DELETE")
        self.assertIn("/requested_reviewers", delete_call[0][1])
        self.assertEqual(
            delete_call[1]["json"]["reviewers"], ["reviewer1", "reviewer2"]
        )
