"""Tests for organizations API endpoints.

Note: These tests call the underlying endpoint functions directly, bypassing
the require_permission decorator. This allows testing endpoint logic
independently of RBAC. Wrapped OSS authorization is covered in
test_tracker_project_permissions.py; plugin checks have their own tests.

If the EE RBAC plugin is linked (via link_ee_plugins.sh), the decorator
wraps functions in an async wrapper. To handle this, tests access the
original unwrapped function via __wrapped__ when available.
"""

import asyncio
import inspect
import uuid
from datetime import datetime, UTC
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from preloop.api.endpoints import organizations
from preloop.schemas.organization import (
    OrganizationCreate,
    OrganizationUpdate,
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
    user.is_superuser = True
    return user


@pytest.fixture
def mock_tracker(mock_user):
    """Create a mock tracker."""
    tracker = MagicMock()
    tracker.id = str(uuid.uuid4())
    tracker.name = "Test Tracker"
    tracker.tracker_type = "github"
    tracker.url = "https://github.com"
    tracker.account_id = mock_user.account_id
    return tracker


@pytest.fixture
def mock_organization(mock_tracker):
    """Create a mock organization."""
    org = MagicMock()
    org.id = str(uuid.uuid4())
    org.name = "Test Organization"
    org.identifier = "test-org"
    org.description = "A test organization"
    org.tracker_id = mock_tracker.id
    org.settings = {}
    org.meta_data = {}
    org.is_active = True
    org.created_at = datetime.now(UTC)
    org.updated_at = datetime.now(UTC)
    return org


@pytest.fixture
def mock_db_session():
    """Create a mock database session."""
    return MagicMock()


class TestCreateOrganization:
    """Tests for the create_organization endpoint."""

    def test_create_organization_success(
        self, mock_user, mock_tracker, mock_organization, mock_db_session
    ):
        """Test successful organization creation."""
        org_create = OrganizationCreate(
            name="Test Organization",
            identifier="test-org",
            description="A test organization",
            settings={},
        )

        with patch.object(
            organizations.crud_organization, "get_by_identifier", return_value=None
        ):
            with patch.object(
                organizations.crud_tracker,
                "get_for_account",
                return_value=[mock_tracker],
            ):
                with patch.object(
                    organizations.crud_organization,
                    "create",
                    return_value=mock_organization,
                ):
                    result = call_endpoint(
                        organizations.create_organization,
                        organization=org_create,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                    assert result == mock_organization

    def test_create_organization_duplicate_identifier(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test 400 when organization identifier already exists."""
        org_create = OrganizationCreate(
            name="Test Organization",
            identifier="test-org",
        )

        with patch.object(
            organizations.crud_organization,
            "get_by_identifier",
            return_value=mock_organization,
        ):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    organizations.create_organization,
                    organization=org_create,
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 400
            assert "already exists" in exc_info.value.detail

    def test_create_organization_no_tracker(self, mock_user, mock_db_session):
        """Test 400 when user has no trackers."""
        org_create = OrganizationCreate(
            name="Test Organization",
            identifier="test-org",
        )

        with patch.object(
            organizations.crud_organization, "get_by_identifier", return_value=None
        ):
            with patch.object(
                organizations.crud_tracker, "get_for_account", return_value=[]
            ):
                with pytest.raises(HTTPException) as exc_info:
                    call_endpoint(
                        organizations.create_organization,
                        organization=org_create,
                        db=mock_db_session,
                        current_user=mock_user,
                    )

                assert exc_info.value.status_code == 400
                assert "No trackers found" in exc_info.value.detail


class TestListOrganizations:
    """Tests for the list_organizations endpoint."""

    def test_list_organizations_success(
        self, mock_user, mock_tracker, mock_organization, mock_db_session
    ):
        """Test successful organization listing."""
        with patch.object(
            organizations.crud_tracker, "get_for_account", return_value=[mock_tracker]
        ):
            with patch.object(
                organizations.crud_organization,
                "get_for_trackers",
                return_value=([mock_organization], 1),
            ):
                result = call_endpoint(
                    organizations.list_organizations,
                    limit=100,
                    offset=0,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert "items" in result
                assert len(result["items"]) == 1
                assert result["total"] == 1

    def test_list_organizations_no_trackers(self, mock_user, mock_db_session):
        """Test listing when user has no trackers."""
        with patch.object(
            organizations.crud_tracker, "get_for_account", return_value=[]
        ):
            result = call_endpoint(
                organizations.list_organizations,
                limit=100,
                offset=0,
                db=mock_db_session,
                current_user=mock_user,
            )

            assert result["items"] == []
            assert result["total"] == 0

    def test_list_organizations_pagination(
        self, mock_user, mock_tracker, mock_organization, mock_db_session
    ):
        """Test organization listing with pagination."""
        with patch.object(
            organizations.crud_tracker, "get_for_account", return_value=[mock_tracker]
        ):
            with patch.object(
                organizations.crud_organization,
                "get_for_trackers",
                return_value=([mock_organization], 5),
            ) as mock_get:
                call_endpoint(
                    organizations.list_organizations,
                    limit=10,
                    offset=20,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                # Verify pagination params were passed
                mock_get.assert_called_once()
                call_kwargs = mock_get.call_args[1]
                assert call_kwargs["skip"] == 20
                assert call_kwargs["limit"] == 10


class TestGetOrganization:
    """Tests for the get_organization endpoint."""

    def test_get_organization_success(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test successful organization retrieval."""
        with patch.object(
            organizations.crud_organization, "get", return_value=mock_organization
        ):
            result = call_endpoint(
                organizations.get_organization,
                organization_id=mock_organization.id,
                db=mock_db_session,
                current_user=mock_user,
            )

            assert result == mock_organization

    def test_get_organization_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        with patch.object(organizations.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    organizations.get_organization,
                    organization_id=str(uuid.uuid4()),
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found"


class TestGetOrganizationByIdentifier:
    """Tests for the get_organization_by_identifier endpoint."""

    def test_get_organization_by_identifier_success(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test successful organization retrieval by identifier."""
        with patch.object(
            organizations.crud_organization,
            "get_by_identifier",
            return_value=mock_organization,
        ):
            result = call_endpoint(
                organizations.get_organization_by_identifier,
                identifier="test-org",
                db=mock_db_session,
                current_user=mock_user,
            )

            assert result == mock_organization

    def test_get_organization_by_identifier_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found by identifier."""
        with patch.object(
            organizations.crud_organization, "get_by_identifier", return_value=None
        ):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    organizations.get_organization_by_identifier,
                    identifier="non-existent",
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
            assert exc_info.value.detail == "Organization not found"


class TestUpdateOrganization:
    """Tests for the update_organization endpoint."""

    def test_update_organization_success(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test successful organization update."""
        org_update = OrganizationUpdate(
            name="Updated Organization",
            description="Updated description",
        )

        updated_org = MagicMock()
        updated_org.id = mock_organization.id
        updated_org.name = "Updated Organization"
        updated_org.identifier = mock_organization.identifier
        updated_org.description = "Updated description"

        with patch.object(
            organizations.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                organizations.crud_organization, "update", return_value=updated_org
            ):
                result = call_endpoint(
                    organizations.update_organization,
                    organization_id=mock_organization.id,
                    organization_update=org_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result.name == "Updated Organization"

    def test_update_organization_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        org_update = OrganizationUpdate(name="Updated Organization")

        with patch.object(organizations.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    organizations.update_organization,
                    organization_id=str(uuid.uuid4()),
                    organization_update=org_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404

    def test_update_organization_partial(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test partial organization update."""
        org_update = OrganizationUpdate(description="Only description updated")

        with patch.object(
            organizations.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(
                organizations.crud_organization,
                "update",
                return_value=mock_organization,
            ) as mock_update:
                call_endpoint(
                    organizations.update_organization,
                    organization_id=mock_organization.id,
                    organization_update=org_update,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                # Verify update was called with only the description
                call_kwargs = mock_update.call_args[1]
                assert "description" in call_kwargs["obj_in"]


class TestDeleteOrganization:
    """Tests for the delete_organization endpoint."""

    def test_delete_organization_success(
        self, mock_user, mock_organization, mock_db_session
    ):
        """Test successful organization deletion."""
        with patch.object(
            organizations.crud_organization, "get", return_value=mock_organization
        ):
            with patch.object(organizations.crud_organization, "delete") as mock_delete:
                result = call_endpoint(
                    organizations.delete_organization,
                    organization_id=mock_organization.id,
                    db=mock_db_session,
                    current_user=mock_user,
                )

                assert result is None
                mock_delete.assert_called_once_with(
                    mock_db_session, id=mock_organization.id
                )

    def test_delete_organization_not_found(self, mock_user, mock_db_session):
        """Test 404 when organization is not found."""
        with patch.object(organizations.crud_organization, "get", return_value=None):
            with pytest.raises(HTTPException) as exc_info:
                call_endpoint(
                    organizations.delete_organization,
                    organization_id=str(uuid.uuid4()),
                    db=mock_db_session,
                    current_user=mock_user,
                )

            assert exc_info.value.status_code == 404
