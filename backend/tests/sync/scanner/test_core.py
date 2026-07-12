import datetime
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from sqlalchemy.orm import Session

from preloop.models.models import (
    Organization,
    Project,
    Tracker,
    TrackerScopeRule,
    Issue,
    Account,
)
from preloop.sync.scanner.core import (
    scan_tracker,
    TrackerClient,
    _process_organization,
    scan_account,
    scan_all_accounts,
)


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    return MagicMock(spec=Session)


@pytest.fixture
def mock_tracker():
    """Fixture for a mock tracker model instance."""
    tracker = MagicMock(spec=Tracker)
    tracker.id = 1
    tracker.is_deleted = False
    tracker.tracker_type = "github"
    return tracker


@pytest.fixture
def mock_organization():
    """Fixture for a mock organization model instance."""
    org = MagicMock(spec=Organization)
    org.id = 101
    org.identifier = "test-org"
    org.last_webhook_update = None
    org.last_polling_update = None
    return org


@pytest.fixture
def mock_project():
    """Fixture for a mock project model instance."""
    project = MagicMock(spec=Project)
    project.id = 1001
    project.identifier = "test-project"
    return project


@pytest.fixture
def mock_account():
    """Fixture for a mock account model instance."""
    account = MagicMock(spec=Account)
    account.id = 1
    return account


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core._process_organization")
@patch("preloop.sync.scanner.core.TrackerClient")
async def test_scan_tracker_happy_path(
    mock_tracker_client_class,
    mock_process_org,
    mock_db_session,
    mock_tracker,
    mock_organization,
):
    """
    Test that scan_tracker initializes TrackerClient, gets organizations,
    and processes them.
    """
    # Arrange
    mock_client_instance = AsyncMock()
    mock_client_instance.scan_organizations.return_value = [mock_organization]
    mock_tracker_client_class.return_value = mock_client_instance
    mock_process_org.return_value = {
        "projects": 1,
        "issues": 5,
        "embeddings_updated": 5,
        "organizations": {"errors": 0},
    }

    # Act
    stats = await scan_tracker(db=mock_db_session, tracker=mock_tracker)

    # Assert
    mock_tracker_client_class.assert_called_once_with(mock_tracker)
    mock_client_instance.scan_organizations.assert_called_once_with(mock_db_session)
    mock_process_org.assert_called_once_with(
        mock_db_session, mock_client_instance, mock_organization, None, False
    )
    assert stats["organizations"]["total"] == 1
    assert stats["organizations"]["processed"] == 1
    assert stats["projects"] == 1
    assert stats["issues"] == 5


@pytest.mark.asyncio
async def test_scan_tracker_skips_deleted_tracker(mock_db_session, mock_tracker):
    """Test that a deleted tracker is skipped."""
    # Arrange
    mock_tracker.is_deleted = True

    # Act
    stats = await scan_tracker(db=mock_db_session, tracker=mock_tracker)

    # Assert
    assert stats["organizations"]["total"] == 0


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core._process_organization")
@patch("preloop.sync.scanner.core.TrackerClient")
async def test_scan_tracker_skips_recently_polled_org(
    mock_tracker_client_class,
    mock_process_org,
    mock_db_session,
    mock_tracker,
    mock_organization,
):
    """Test that an organization that was recently polled is skipped."""
    # Arrange
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_organization.last_polling_update = now - datetime.timedelta(minutes=10)

    mock_client_instance = AsyncMock()
    mock_client_instance.scan_organizations.return_value = [mock_organization]
    mock_tracker_client_class.return_value = mock_client_instance

    # Act
    stats = await scan_tracker(db=mock_db_session, tracker=mock_tracker)

    # Assert
    mock_process_org.assert_not_called()
    assert stats["organizations"]["total"] == 1
    assert stats["organizations"]["processed"] == 0
    assert stats["organizations"]["skipped_polling"] == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.TrackerClient")
async def test_scan_tracker_force_update_ignores_polling_time(
    mock_tracker_client_class,
    mock_db_session,
    mock_tracker,
    mock_organization,
):
    """Test that force_update=True processes an org regardless of polling time."""
    # Arrange
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_organization.last_polling_update = now - datetime.timedelta(minutes=10)

    mock_client_instance = AsyncMock()
    mock_client_instance.scan_organizations.return_value = [mock_organization]
    mock_tracker_client_class.return_value = mock_client_instance

    with patch("preloop.sync.scanner.core._process_organization") as mock_process_org:
        mock_process_org.return_value = {
            "projects": 1,
            "issues": 5,
            "embeddings_updated": 5,
            "organizations": {"errors": 0},
        }
        # Act
        stats = await scan_tracker(
            db=mock_db_session, tracker=mock_tracker, force_update=True
        )

        # Assert
        mock_process_org.assert_called_once()
        assert stats["organizations"]["processed"] == 1
        assert stats["organizations"]["skipped_polling"] == 0


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
async def test_tracker_client_scan_organizations(
    mock_crud_org, mock_db_session, mock_tracker
):
    """Test that TrackerClient.scan_organizations correctly processes organizations."""
    # Arrange
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        mock_client = AsyncMock()
        mock_client.get_organizations.return_value = [
            {"id": "test-org", "name": "Test Org"}
        ]
        mock_client.transform_organization = MagicMock(
            return_value={
                "identifier": "test-org",
                "name": "Test Org",
            }
        )
        client = TrackerClient(mock_tracker)
        client.client = mock_client
        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            TrackerScopeRule(
                scope_type="ORGANIZATION", rule_type="INCLUDE", identifier="test-org"
            )
        ]
        mock_crud_org.get_by_identifier.return_value = None

        # Act
        await client.scan_organizations(mock_db_session)

        # Assert
        mock_crud_org.get_by_identifier.assert_called_once()
        mock_crud_org.create.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_project")
async def test_tracker_client_scan_projects(
    mock_crud_project, mock_db_session, mock_tracker, mock_organization
):
    """Test that TrackerClient.scan_projects correctly processes projects."""
    # Arrange
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        mock_client = AsyncMock()
        mock_client.get_projects.return_value = [
            {"id": "test-project", "name": "Test Project"}
        ]
        mock_client.transform_project = MagicMock(
            return_value={
                "identifier": "test-project",
                "name": "Test Project",
            }
        )
        client = TrackerClient(mock_tracker)
        client.client = mock_client
        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            TrackerScopeRule(
                scope_type="ORGANIZATION", rule_type="INCLUDE", identifier="test-org"
            )
        ]
        mock_crud_project.get_by_slug_or_identifier.return_value = None

        # Act
        await client.scan_projects(mock_db_session, mock_organization)

        # Assert
        mock_crud_project.get_by_slug_or_identifier.assert_called_once()
        mock_crud_project.create.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_issue")
@patch("preloop.sync.scanner.core.crud_comment")
@patch("preloop.sync.scanner.core.crud_issue_embedding")
async def test_tracker_client_scan_issues_new_issue(
    mock_crud_embedding,
    mock_crud_comment,
    mock_crud_issue,
    mock_db_session,
    mock_tracker,
    mock_organization,
    mock_project,
):
    """Test that TrackerClient.scan_issues correctly processes a new issue."""
    # Arrange
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
                "comments": [],
            }
        )
        client = TrackerClient(mock_tracker)
        client.client = mock_internal_client
        mock_crud_issue.get_by_external_id.return_value = None  # New issue
        mock_crud_issue.create.return_value = Issue(id=1)

        # Act
        issues, embeddings = await client.scan_issues(
            mock_db_session, mock_organization, mock_project
        )

        # Assert
        mock_crud_issue.create.assert_called_once()
        mock_crud_embedding.create_embeddings.assert_called_once()
        assert len(issues) == 1
        assert embeddings == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_issue")
@patch("preloop.sync.scanner.core.crud_comment")
@patch("preloop.sync.scanner.core.crud_issue_embedding")
async def test_tracker_client_scan_issues_updated_issue(
    mock_crud_embedding,
    mock_crud_comment,
    mock_crud_issue,
    mock_db_session,
    mock_tracker,
    mock_organization,
    mock_project,
):
    """Test that TrackerClient.scan_issues correctly processes an updated issue."""
    # Arrange
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        mock_internal_client = AsyncMock()
        mock_internal_client.get_issues.return_value = [
            {"id": "1", "updated_at": "2025-01-02T00:00:00Z"}
        ]
        mock_internal_client.transform_issue = MagicMock(
            return_value={
                "external_id": "1",
                "updated_at": datetime.datetime(
                    2025, 1, 2, tzinfo=datetime.timezone.utc
                ),
                "comments": [],
            }
        )
        client = TrackerClient(mock_tracker)
        client.client = mock_internal_client
        existing_issue = Issue(
            id=1, updated_at=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)
        )
        mock_crud_issue.get_by_external_id.return_value = existing_issue

        # Act
        await client.scan_issues(mock_db_session, mock_organization, mock_project)

        # Assert
        mock_crud_issue.update.assert_called_once()
        mock_crud_embedding.create_embeddings.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
@patch("os.getenv")
async def test_process_organization_github(
    mock_getenv,
    mock_crud_org,
    mock_db_session,
    mock_organization,
    mock_project,
):
    """Test the _process_organization function for GitHub."""
    # Arrange
    mock_getenv.return_value = "http://test.com"
    mock_client = AsyncMock(spec=TrackerClient)
    mock_client.client = AsyncMock()
    mock_client.scan_projects.return_value = [mock_project]
    mock_client.scan_issues.return_value = ([], 0)
    mock_client.tracker_type = "github"
    mock_client.client.is_webhook_registered_for_organization = AsyncMock(
        return_value=False
    )
    mock_client.client.register_webhook = AsyncMock()

    # Act
    stats = await _process_organization(
        db=mock_db_session,
        client=mock_client,
        org=mock_organization,
        since=None,
        force_update=False,
    )

    # Assert
    mock_client.client.register_webhook.assert_called_once()
    assert stats["projects"] == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.scan_tracker")
@patch("preloop.sync.scanner.core.crud_account")
async def test_scan_account(
    mock_crud_account, mock_scan_tracker, mock_db_session, mock_account
):
    """Test scanning all trackers for a single account."""
    # Arrange
    mock_crud_account.get.return_value = mock_account
    mock_account.trackers = [MagicMock(spec=Tracker)]
    mock_scan_tracker.return_value = {
        "trackers_scanned": 1,
        "trackers_with_errors": 0,
        "organizations": {
            "total": 1,
            "processed": 1,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 1,
        "issues": 1,
        "embeddings_updated": 1,
    }

    # Act
    await scan_account(mock_db_session, account_id=mock_account.id)

    # Assert
    mock_scan_tracker.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_account")
@patch("preloop.sync.scanner.core.scan_account")
async def test_scan_all_accounts(mock_scan_account, mock_crud_account, mock_db_session):
    """Test scanning all active accounts."""
    # Arrange
    mock_account = MagicMock(spec=Account)
    mock_account.is_active = True
    mock_account.trackers = [MagicMock(spec=Tracker)]
    mock_crud_account.get_multi.return_value = [mock_account]
    mock_scan_account.return_value = {
        "trackers": 1,
        "organizations": {
            "total": 0,
            "processed": 0,
            "skipped_webhook": 0,
            "skipped_polling": 0,
            "errors": 0,
        },
        "projects": 0,
        "issues": 0,
        "embeddings_updated": 0,
        "errors": 0,
    }

    # Act
    await scan_all_accounts(mock_db_session)

    # Assert
    mock_scan_account.assert_called_once()


@pytest.fixture
def mock_tracker_gitlab():
    """Fixture for a mock gitlab tracker model instance."""
    tracker = MagicMock(spec=Tracker)
    tracker.id = 2
    tracker.is_deleted = False
    tracker.tracker_type = "gitlab"
    tracker.resolved_api_key = "gl-key"
    tracker.connection_details = {}
    tracker.url = "https://gitlab.com"
    return tracker


@pytest.fixture
def mock_tracker_jira():
    """Fixture for a mock jira tracker model instance."""
    tracker = MagicMock(spec=Tracker)
    tracker.id = 3
    tracker.is_deleted = False
    tracker.tracker_type = "jira"
    tracker.resolved_api_key = "jira-key"
    tracker.connection_details = {}
    tracker.url = "https://jira.atlassian.com"
    return tracker


@pytest.mark.asyncio
async def test_tracker_client_init_gitlab(mock_tracker_gitlab):
    """Test TrackerClient initialization for GitLab."""
    with patch("preloop.sync.trackers.gitlab.GitLabTracker") as mock_gitlab_tracker:
        client = TrackerClient(mock_tracker_gitlab)
        mock_gitlab_tracker.assert_called_once_with(
            mock_tracker_gitlab.id,
            mock_tracker_gitlab.resolved_api_key,
            {"url": mock_tracker_gitlab.url},
        )
        assert client.tracker_type == "gitlab"


@pytest.mark.asyncio
async def test_tracker_client_init_jira(mock_tracker_jira):
    """Test TrackerClient initialization for Jira."""
    with patch("preloop.sync.trackers.jira.JiraTracker") as mock_jira_tracker:
        client = TrackerClient(mock_tracker_jira)
        mock_jira_tracker.assert_called_once_with(
            mock_tracker_jira.id,
            mock_tracker_jira.resolved_api_key,
            {"url": mock_tracker_jira.url},
        )
        assert client.tracker_type == "jira"


@pytest.mark.asyncio
async def test_tracker_client_init_unsupported():
    """Test TrackerClient initialization with an unsupported tracker type."""
    tracker = MagicMock(spec=Tracker)
    tracker.tracker_type = "unsupported"
    with pytest.raises(ValueError, match="Unsupported tracker type: unsupported"):
        TrackerClient(tracker)


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_project")
async def test_scan_projects_skips_excluded_project(
    mock_crud_project, mock_db_session, mock_organization
):
    """Test that a project in the exclusion list is skipped."""
    # Arrange
    tracker = MagicMock(spec=Tracker)
    tracker.id = 1
    tracker.tracker_type = "github"

    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(tracker)
        client.client = AsyncMock()
        client.client.get_projects.return_value = [
            {"id": "proj-1", "name": "Project 1"},
            {"id": "proj-2", "name": "Project 2"},
        ]
        client.client.transform_project.side_effect = lambda p, o: {
            "identifier": p["id"],
            "name": p["name"],
        }

        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            TrackerScopeRule(
                scope_type="ORGANIZATION", rule_type="INCLUDE", identifier="test-org"
            ),
            TrackerScopeRule(
                scope_type="PROJECT", rule_type="EXCLUDE", identifier="proj-2"
            ),
        ]

        mock_crud_project.get_by_slug_or_identifier.return_value = None
        mock_crud_project.create.return_value = Project(identifier="proj-1")

        # Act
        projects = await client.scan_projects(mock_db_session, mock_organization)

        # Assert
        assert len(projects) == 0
        mock_crud_project.create.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_project")
async def test_scan_projects_skips_unincluded_project(
    mock_crud_project, mock_db_session, mock_organization
):
    """Test that a project not in an inclusion list is skipped."""
    # Arrange
    tracker = MagicMock(spec=Tracker)
    tracker.id = 1
    tracker.tracker_type = "github"

    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(tracker)
        client.client = AsyncMock()
        client.client.get_projects.return_value = [
            {"id": "proj-1", "name": "Project 1"},
            {"id": "proj-2", "name": "Project 2"},
        ]
        client.client.transform_project.side_effect = lambda p, o: {
            "identifier": p["id"],
            "name": p["name"],
        }

        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            TrackerScopeRule(
                scope_type="PROJECT", rule_type="INCLUDE", identifier="proj-1"
            )
        ]

        mock_crud_project.get_by_slug_or_identifier.return_value = None
        mock_crud_project.create.return_value = Project(identifier="proj-1")

        # Act
        projects = await client.scan_projects(mock_db_session, mock_organization)

        # Assert
        assert len(projects) == 0
        mock_crud_project.create.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_project")
async def test_scan_projects_handles_exception(
    mock_crud_project, mock_db_session, mock_organization
):
    """Test that scan_projects handles exceptions gracefully."""
    # Arrange
    tracker = MagicMock(spec=Tracker)
    tracker.id = 1
    tracker.tracker_type = "github"

    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(tracker)
        client.client = AsyncMock()
        client.client.get_projects.side_effect = Exception("API Error")

        # Act
        projects = await client.scan_projects(mock_db_session, mock_organization)

        # Assert
        assert len(projects) == 0
        mock_crud_project.create.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
@patch("os.getenv")
async def test_process_organization_gitlab(
    mock_getenv,
    mock_crud_org,
    mock_db_session,
    mock_organization,
    mock_project,
):
    """Test the _process_organization function for GitLab."""
    # Arrange
    mock_getenv.return_value = "http://test.com"
    mock_client = AsyncMock(spec=TrackerClient)
    mock_client.client = AsyncMock()
    mock_client.scan_projects.return_value = [mock_project]
    mock_client.scan_issues.return_value = ([], 0)
    mock_client.tracker_type = "gitlab"
    mock_client.tracker = MagicMock()
    mock_client.tracker.meta_data = {}  # Empty meta_data means not GitLab CE
    mock_client.client.is_webhook_registered_for_organization.return_value = False

    # Act
    stats = await _process_organization(
        db=mock_db_session,
        client=mock_client,
        org=mock_organization,
        since=None,
        force_update=False,
    )

    # Assert
    mock_client.client.register_group_webhook.assert_called_once()
    assert stats["projects"] == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
@patch("os.getenv")
async def test_process_organization_jira(
    mock_getenv,
    mock_crud_org,
    mock_db_session,
    mock_organization,
    mock_project,
):
    """Test the _process_organization function for Jira."""
    # Arrange
    mock_getenv.return_value = "http://test.com"
    mock_client = AsyncMock(spec=TrackerClient)
    mock_client.client = AsyncMock()
    mock_client.scan_projects.return_value = [mock_project]
    mock_client.scan_issues.return_value = ([], 0)
    mock_client.tracker_type = "jira"
    mock_client.client.is_webhook_registered_for_project = MagicMock(return_value=False)
    mock_client.client.register_webhook = MagicMock()

    # Act
    stats = await _process_organization(
        db=mock_db_session,
        client=mock_client,
        org=mock_organization,
        since=None,
        force_update=False,
    )

    # Assert
    mock_client.client.register_webhook.assert_called_once()
    assert stats["projects"] == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.scan_tracker")
@patch("preloop.sync.scanner.core.crud_account")
async def test_scan_account_no_trackers(
    mock_crud_account, mock_scan_tracker, mock_db_session
):
    """Test that scan_account handles an account with no trackers."""
    # Arrange
    account = MagicMock()
    account.id = 1
    account.trackers = []
    mock_crud_account.get.return_value = account

    # Act
    await scan_account(mock_db_session, account_id=1)

    # Assert
    mock_scan_tracker.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.scan_account")
@patch("preloop.sync.scanner.core.crud_account")
async def test_scan_all_accounts_no_active_accounts(
    mock_crud_account, mock_scan_account, mock_db_session
):
    """Test that scan_all_accounts handles the case with no active accounts."""
    # Arrange
    mock_crud_account.get_multi.return_value = []

    # Act
    await scan_all_accounts(mock_db_session)

    # Assert
    mock_scan_account.assert_not_called()


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_issue")
@patch("preloop.sync.scanner.core.crud_comment")
@patch("preloop.sync.scanner.core.crud_issue_embedding")
async def test_scan_issues_with_new_comment(
    mock_crud_embedding,
    mock_crud_comment,
    mock_crud_issue,
    mock_db_session,
    mock_tracker,
    mock_organization,
    mock_project,
):
    """Test scanning an issue that has a new comment."""
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(mock_tracker)
        client.client = AsyncMock()
        client.client.get_issues.return_value = [
            {"id": "1", "updated_at": "2025-01-01T00:00:00Z"}
        ]
        client.client.transform_issue = MagicMock(
            return_value={
                "external_id": "1",
                "updated_at": datetime.datetime(
                    2025, 1, 1, tzinfo=datetime.timezone.utc
                ),
                "comments": [
                    {
                        "external_id": "c1",
                        "updated_at": "2025-01-01T00:00:00Z",
                        "body": "new comment",
                    }
                ],
            }
        )
        client.client.transform_comment = MagicMock(
            return_value={
                "external_id": "c1",
                "updated_at": datetime.datetime(
                    2025, 1, 1, tzinfo=datetime.timezone.utc
                ),
                "body": "new comment",
            }
        )
        existing_issue = Issue(
            id=1,
            updated_at=datetime.datetime(2024, 12, 31, tzinfo=datetime.timezone.utc),
        )
        mock_crud_issue.get_by_external_id.return_value = existing_issue
        mock_crud_comment.get_by_external_id.return_value = None

        # Act
        await client.scan_issues(mock_db_session, mock_organization, mock_project)

        # Assert
        mock_crud_comment.create.assert_called_once()


@pytest.mark.asyncio
@patch("preloop.models.crud.crud_tracker")
@patch("os.getenv")
async def test_process_organization_gitlab_group_hooks_not_supported(
    mock_getenv, mock_crud_tracker, mock_db_session, mock_organization, mock_project
):
    """Test _process_organization for GitLab when group hooks are not supported."""
    mock_getenv.return_value = "http://test.com"
    mock_client = AsyncMock(spec=TrackerClient)
    mock_client.client = AsyncMock()
    mock_client.scan_projects.return_value = [mock_project]
    mock_client.scan_issues.return_value = ([], 0)
    mock_client.tracker_type = "gitlab"
    mock_client.tracker = MagicMock()
    mock_client.tracker.id = "test-tracker-id"
    mock_client.tracker.meta_data = {}  # Empty meta_data means not GitLab CE initially
    mock_client.client.is_webhook_registered_for_organization.return_value = False
    mock_client.client.register_group_webhook.return_value = "group_hooks_not_supported"
    mock_client.client.is_webhook_registered_for_project.return_value = False

    await _process_organization(
        db=mock_db_session,
        client=mock_client,
        org=mock_organization,
        since=None,
        force_update=False,
    )

    mock_client.client.register_project_webhook.assert_called_once()
    mock_crud_tracker.update.assert_called_once()  # Verify tracker was marked as GitLab CE


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core._process_organization")
async def test_scan_tracker_skips_recently_updated_by_webhook_org(
    mock_process_org, mock_db_session, mock_tracker, mock_organization
):
    """Test that an org recently updated by webhook is skipped."""
    # Arrange
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_organization.last_webhook_update = now - datetime.timedelta(minutes=5)
    mock_organization.last_polling_update = now - datetime.timedelta(minutes=30)

    mock_client_instance = AsyncMock()
    mock_client_instance.scan_organizations.return_value = [mock_organization]
    with patch("preloop.sync.scanner.core.TrackerClient") as mock_tracker_client_class:
        mock_tracker_client_class.return_value = mock_client_instance

        # Act
        stats = await scan_tracker(db=mock_db_session, tracker=mock_tracker)

        # Assert
        mock_process_org.assert_not_called()
        assert stats["organizations"]["skipped_webhook"] == 1


@pytest.mark.asyncio
@patch("preloop.sync.scanner.core.crud_organization")
async def test_scan_organizations_skips_unincluded(
    mock_crud_org, mock_db_session, mock_tracker
):
    """Test that scan_organizations skips an org not in the inclusion list."""
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        client = TrackerClient(mock_tracker)
        client.client = AsyncMock()
        client.client.get_organizations.return_value = [
            {"id": "org-1", "name": "Org 1"},
            {"id": "org-2", "name": "Org 2"},
        ]
        mock_db_session.query.return_value.filter.return_value.all.return_value = [
            TrackerScopeRule(
                scope_type="ORGANIZATION", rule_type="INCLUDE", identifier="org-1"
            )
        ]
        mock_crud_org.get_by_identifier.return_value = None
        mock_crud_org.create.return_value = Organization(identifier="org-1")
        client.client.transform_organization = MagicMock(
            side_effect=lambda d: {"identifier": d["id"], "name": d["name"]}
        )

        # Act
        orgs = await client.scan_organizations(mock_db_session)

        # Assert
        assert len(orgs) == 1
        assert orgs[0].identifier == "org-1"


@pytest.mark.asyncio
async def test_scan_projects_skips_missing_identifier(
    mock_db_session, mock_organization
):
    """Test that scan_projects skips a project with a missing identifier."""
    with patch("preloop.sync.trackers.github.GitHubTracker"):
        tracker = MagicMock(spec=Tracker)
        tracker.tracker_type = "github"
        client = TrackerClient(tracker)
        client.client = AsyncMock()
        client.client.get_projects.return_value = [{"name": "Project without ID"}]

        # Act
        projects = await client.scan_projects(mock_db_session, mock_organization)

        # Assert
        assert len(projects) == 0
