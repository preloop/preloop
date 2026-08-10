"""Tests for projects API endpoints.

Note: These tests call the underlying endpoint functions directly, bypassing
the require_permission decorator. This allows testing endpoint logic
independently of RBAC. The RBAC permission checking is tested separately
in plugin-specific tests.

If the EE RBAC plugin is linked (via link_ee_plugins.sh), the decorator
wraps functions in an async wrapper. To handle this, tests access the
original unwrapped function via __wrapped__ when available.
"""

import asyncio
import inspect
import uuid
from datetime import datetime, UTC
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import projects
from preloop.schemas.project import (
    ProjectCreate,
    ProjectUpdate,
    TestConnectionRequest,
)


def call_endpoint(func, **kwargs):
    """Call an endpoint function, handling both sync and async wrappers.

    When the EE RBAC plugin is linked, endpoints are wrapped in an async
    decorator. This helper unwraps to get the original sync function,
    or runs the async version if needed.
    """
    # Try to get the original unwrapped function
    original_func = func
    while hasattr(original_func, "__wrapped__"):
        original_func = original_func.__wrapped__

    # If original is sync, call it directly
    if not inspect.iscoroutinefunction(original_func):
        return original_func(**kwargs)

    # If we get here, both wrapper and original are async - run it
    return asyncio.get_event_loop().run_until_complete(func(**kwargs))


@pytest.fixture
def mock_user():
    """Create a mock user with account_id."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.account_id = str(uuid.uuid4())
    user.username = "testuser"
    user.is_active = True
    return user


@pytest.fixture
def mock_organization():
    """Create a mock organization."""
    org = MagicMock()
    org.id = str(uuid.uuid4())
    org.name = "Test Organization"
    org.identifier = "test-org"
    org.description = "A test organization"
    org.tracker_id = str(uuid.uuid4())
    org.settings = {}
    org.meta_data = {}
    org.is_active = True
    org.tracker = MagicMock()
    org.tracker.id = org.tracker_id
    org.tracker.tracker_type = "github"
    org.tracker.url = "https://github.com"
    org.tracker.api_key = "test_key"
    org.tracker.connection_details = {}
    return org


@pytest.fixture
def mock_project(mock_organization):
    """Create a mock project."""
    project = MagicMock()
    project.id = str(uuid.uuid4())
    project.name = "Test Project"
    project.identifier = "test-project"
    project.description = "A test project"
    project.organization_id = mock_organization.id
    project.settings = {}
    project.tracker_settings = {"repo": "test/repo"}
    project.meta_data = {}
    project.is_active = True
    project.created_at = datetime.now(UTC)
    project.updated_at = datetime.now(UTC)
    return project


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return MagicMock()


class TestCreateProject:
    """Tests for the create_project endpoint."""

    def test_create_project_success(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test successful project creation."""
        project_create = ProjectCreate(
            name="Test Project",
            identifier="test-project",
            description="A test project",
            organization_id=mock_organization.id,
            settings={},
            tracker_configurations={"repo": "test/repo"},
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects.crud_project, "get_by_identifier", return_value=None
            ):
                with patch.object(
                    projects.crud_project, "create", return_value=mock_project
                ):
                    result = call_endpoint(
                        projects.create_project,
                        project=project_create,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                    assert result["name"] == "Test Project"
                    assert result["identifier"] == "test-project"

    def test_create_project_duplicate_check_matches_crud_signature(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """The duplicate-check call must match CRUDProject.get_by_identifier's signature.

        Regression: create_project passed organization_id= to
        get_by_identifier, which did not accept it, so every create_project
        request 500ed with a TypeError before reaching the duplicate check.
        autospec=True makes the mock enforce the real method signature.
        """
        project_create = ProjectCreate(
            name="Test Project",
            identifier="test-project",
            description="A test project",
            organization_id=mock_organization.id,
            settings={},
            tracker_configurations={"repo": "test/repo"},
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects.crud_project,
                "get_by_identifier",
                autospec=True,
                return_value=None,
            ):
                with patch.object(
                    projects.crud_project, "create", return_value=mock_project
                ):
                    result = call_endpoint(
                        projects.create_project,
                        project=project_create,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                    assert result["identifier"] == "test-project"

    def test_create_project_organization_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        project_create = ProjectCreate(
            name="Test Project",
            identifier="test-project",
            organization_id=str(uuid.uuid4()),
        )

        with patch.object(projects.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.create_project,
                    project=project_create,
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found"

    def test_create_project_duplicate_identifier(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test 400 when project identifier already exists."""
        project_create = ProjectCreate(
            name="Test Project",
            identifier="test-project",
            organization_id=mock_organization.id,
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects.crud_project, "get_by_identifier", return_value=mock_project
            ):
                with pytest.raises(HTTPException) as exc_info:
                    call_endpoint(
                        projects.create_project,
                        project=project_create,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                assert exc_info.value.status_code == 400
                assert "already exists" in exc_info.value.detail


class TestListProjects:
    """Tests for the list_projects endpoint."""

    def test_list_projects_success(self, mock_user, mock_project, mock_db_session):
        """Test successful project listing."""
        with patch.object(
            projects, "get_accessible_projects", return_value=[mock_project]
        ):
            result = call_endpoint(
                projects.list_projects,
                organization_id=None,
                limit=100,
                offset=0,
                db=mock_db_session,
                current_user=mock_user,
            )

            assert len(result) == 1
            assert result[0]["name"] == "Test Project"

    def test_list_projects_with_organization_filter(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test listing with organization filter."""
        with patch.object(
            projects, "get_accessible_projects", return_value=[mock_project]
        ):
            with patch.object(
                projects.crud_organization, "get", return_value=mock_organization
            ):
                result = call_endpoint(
                    projects.list_projects,
                    organization_id=mock_organization.id,
                    limit=100,
                    offset=0,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert len(result) == 1

    def test_list_projects_organization_not_found(
        self, mock_user, mock_project, mock_db_session
    ):
        """Test 404 when organization filter is invalid."""
        with patch.object(
            projects, "get_accessible_projects", return_value=[mock_project]
        ):
            with patch.object(projects.crud_organization, "get", return_value=None):
                with pytest.raises(HTTPException) as exc_info:
                    call_endpoint(
                        projects.list_projects,
                        organization_id=str(uuid.uuid4()),
                        limit=100,
                        offset=0,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                assert exc_info.value.status_code == 404

    def test_list_projects_empty(self, mock_user, mock_db_session):
        """Test listing when no projects exist."""
        with patch.object(projects, "get_accessible_projects", return_value=[]):
            result = call_endpoint(
                projects.list_projects,
                organization_id=None,
                limit=100,
                offset=0,
                db=mock_db_session,
                current_user=mock_user,
            )

            assert len(result) == 0


class TestListOrganizationProjects:
    """Tests for the list_organization_projects endpoint."""

    def test_list_organization_projects_success(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test successful listing of organization projects."""
        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects, "get_accessible_projects", return_value=[mock_project]
            ):
                result = call_endpoint(
                    projects.list_organization_projects,
                    organization_id=mock_organization.id,
                    limit=100,
                    offset=0,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert "items" in result
                assert result["total"] == 1

    def test_list_organization_projects_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        with patch.object(projects.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.list_organization_projects,
                    organization_id=str(uuid.uuid4()),
                    limit=100,
                    offset=0,
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404


class TestGetProject:
    """Tests for the get_project endpoint."""

    def test_get_project_success(self, mock_user, mock_project, mock_db_session):
        """Test successful project retrieval."""
        with patch.object(projects.crud_project, "get", return_value=mock_project):
            result = call_endpoint(
                projects.get_project,
                project_id=mock_project.id,
                db=mock_db_session,
                current_user=mock_user,
            )

            assert result["id"] == mock_project.id
            assert result["name"] == "Test Project"

    def test_get_project_not_found(self, mock_user, mock_db_session):
        """Test 404 when project is not found."""
        with patch.object(projects.crud_project, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.get_project,
                    project_id=str(uuid.uuid4()),
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Project not found"


class TestGetProjectByIdentifier:
    """Tests for the get_project_by_identifier endpoint."""

    def test_get_project_by_identifier_success(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test successful project retrieval by identifier."""
        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects.crud_project,
                "get_by_slug_or_identifier",
                return_value=mock_project,
            ):
                result = call_endpoint(
                    projects.get_project_by_identifier,
                    organization_id=mock_organization.id,
                    identifier="test-project",
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result["identifier"] == "test-project"

    def test_get_project_by_identifier_org_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        with patch.object(projects.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.get_project_by_identifier,
                    organization_id=str(uuid.uuid4()),
                    identifier="test-project",
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found"

    def test_get_project_by_identifier_not_found(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test 404 when project is not found."""
        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                projects.crud_project, "get_by_slug_or_identifier", return_value=None
            ):
                with pytest.raises(HTTPException) as exc_info:
                    call_endpoint(
                        projects.get_project_by_identifier,
                        organization_id=mock_organization.id,
                        identifier="non-existent",
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                assert exc_info.value.status_code == 404
                assert exc_info.value.detail == "Project not found"


class TestUpdateProject:
    """Tests for the update_project endpoint."""

    def test_update_project_success(self, mock_user, mock_project, mock_db_session):
        """Test successful project update."""
        project_update = ProjectUpdate(
            name="Updated Project",
            description="Updated description",
        )

        updated_project = MagicMock()
        updated_project.id = mock_project.id
        updated_project.name = "Updated Project"
        updated_project.identifier = mock_project.identifier
        updated_project.description = "Updated description"
        updated_project.organization_id = mock_project.organization_id
        updated_project.settings = {}
        updated_project.tracker_settings = {}
        updated_project.created_at = datetime.now(UTC)
        updated_project.updated_at = datetime.now(UTC)

        with patch.object(projects.crud_project, "get", return_value=mock_project):
            with patch.object(
                projects.crud_project, "update", return_value=updated_project
            ):
                result = call_endpoint(
                    projects.update_project,
                    project_id=mock_project.id,
                    project_update=project_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result["name"] == "Updated Project"

    def test_update_project_not_found(self, mock_user, mock_db_session):
        """Test 404 when project is not found."""
        project_update = ProjectUpdate(name="Updated Project")

        with patch.object(projects.crud_project, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.update_project,
                    project_id=str(uuid.uuid4()),
                    project_update=project_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404

    def test_update_project_tracker_configurations(
        self, mock_user, mock_project, mock_db_session
    ):
        """Test updating project with tracker_configurations field."""
        project_update = ProjectUpdate(
            tracker_configurations={"new_repo": "test/new-repo"}
        )

        updated_project = MagicMock()
        updated_project.id = mock_project.id
        updated_project.name = mock_project.name
        updated_project.identifier = mock_project.identifier
        updated_project.description = mock_project.description
        updated_project.organization_id = mock_project.organization_id
        updated_project.settings = {}
        updated_project.tracker_settings = {"new_repo": "test/new-repo"}
        updated_project.created_at = datetime.now(UTC)
        updated_project.updated_at = datetime.now(UTC)

        with patch.object(projects.crud_project, "get", return_value=mock_project):
            with patch.object(
                projects.crud_project, "update", return_value=updated_project
            ) as mock_update:
                call_endpoint(
                    projects.update_project,
                    project_id=mock_project.id,
                    project_update=project_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                # Verify tracker_configurations was mapped to tracker_settings
                call_args = mock_update.call_args
                update_data = call_args[1]["obj_in"]
                assert "tracker_settings" in update_data


class TestDeleteProject:
    """Tests for the delete_project endpoint."""

    def test_delete_project_success(self, mock_user, mock_project, mock_db_session):
        """Test successful project deletion."""
        with patch.object(projects.crud_project, "get", return_value=mock_project):
            with patch.object(projects.crud_project, "delete") as mock_delete:
                result = call_endpoint(
                    projects.delete_project,
                    project_id=mock_project.id,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result is None
                mock_delete.assert_called_once_with(mock_db_session, id=mock_project.id)

    def test_delete_project_not_found(self, mock_user, mock_db_session):
        """Test 404 when project is not found."""
        with patch.object(projects.crud_project, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    projects.delete_project,
                    project_id=str(uuid.uuid4()),
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404


class TestTestProjectConnection:
    """Tests for the test_project_connection endpoint."""

    @pytest.mark.asyncio
    async def test_connection_success(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test successful connection test."""
        request = TestConnectionRequest(
            organization=mock_organization.id,
            project=mock_project.id,
        )

        mock_connection_result = MagicMock()
        mock_connection_result.success = True
        mock_connection_result.message = "Connection successful"
        mock_connection_result.details = {}

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(projects.crud_project, "get", return_value=mock_project):
                with patch.object(
                    projects, "create_tracker_client", new_callable=AsyncMock
                ) as mock_create_client:
                    mock_client = AsyncMock()
                    mock_client.test_connection.return_value = mock_connection_result
                    mock_create_client.return_value = mock_client

                    result = await projects.test_project_connection(
                        request=request,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                    assert result.success is True
                    assert result.message == "Connection successful"

    @pytest.mark.asyncio
    async def test_connection_organization_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        request = TestConnectionRequest(
            organization=str(uuid.uuid4()),
            project=str(uuid.uuid4()),
        )

        with patch.object(projects.crud_organization, "get", return_value=None):
            with patch.object(
                projects.crud_organization, "get_by_identifier", return_value=None
            ):
                with pytest.raises(HTTPException) as exc_info:
                    await projects.test_project_connection(
                        request=request,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                assert exc_info.value.status_code == 404
                assert exc_info.value.detail == "Organization not found"

    @pytest.mark.asyncio
    async def test_connection_no_tracker(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test failure when organization has no tracker."""
        mock_organization.tracker = None

        request = TestConnectionRequest(
            organization=mock_organization.id,
            project=mock_project.id,
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(projects.crud_project, "get", return_value=mock_project):
                result = await projects.test_project_connection(
                    request=request,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result.success is False
                assert "no associated tracker" in result.message

    @pytest.mark.asyncio
    async def test_connection_project_not_found(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test 404 when project is not found."""
        request = TestConnectionRequest(
            organization=mock_organization.id,
            project=str(uuid.uuid4()),
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(projects.crud_project, "get", return_value=None):
                with patch.object(
                    projects.crud_project, "get_by_identifier", return_value=None
                ):
                    with pytest.raises(HTTPException) as exc_info:
                        await projects.test_project_connection(
                            request=request,
                            db=mock_db_session,
                            current_user=mock_user,
                        )

                    assert exc_info.value.status_code == 404
                    assert exc_info.value.detail == "Project not found"

    @pytest.mark.asyncio
    async def test_connection_client_creation_error(
        self, mock_user, mock_organization, mock_project, mock_db_session
    ):
        """Test failure when tracker client creation fails."""
        request = TestConnectionRequest(
            organization=mock_organization.id,
            project=mock_project.id,
        )

        with patch.object(
            projects.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(projects.crud_project, "get", return_value=mock_project):
                with patch.object(
                    projects,
                    "create_tracker_client",
                    side_effect=Exception("Client error"),
                ):
                    result = await projects.test_project_connection(
                        request=request,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                    assert result.success is False
                    assert "error" in result.details
