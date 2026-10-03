"""Tests for project resolver."""

from unittest.mock import MagicMock, patch

import pytest

from preloop.services.prompt_resolvers.base import ResolverContext
from preloop.services.prompt_resolvers.project import ProjectResolver


class TestProjectResolver:
    """Test ProjectResolver class."""

    def test_prefix(self):
        """Test that prefix returns correct value."""
        resolver = ProjectResolver()
        assert resolver.prefix == "project"

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_name_with_project_id(self, mock_crud_project):
        """Test resolving project name with project_id."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.name = "Test Project"
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("name", context)

        assert result == "Test Project"
        mock_crud_project.get.assert_called_once_with(
            mock_db, id="proj-123", account_id="acct-1"
        )

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_description_with_project_identifier(self, mock_crud_project):
        """Test resolving project description with project_identifier."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_identifier": "TP"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.description = "Test description"
        mock_crud_project.get_by_identifier.return_value = mock_project

        result = await resolver.resolve("description", context)

        assert result == "Test description"
        mock_crud_project.get_by_identifier.assert_called_once_with(
            mock_db, identifier="TP", account_id="acct-1"
        )

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_description_empty(self, mock_crud_project):
        """Test resolving project description when None returns empty string."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.description = None
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("description", context)

        assert result == ""

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_identifier(self, mock_crud_project):
        """Test resolving project identifier."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.identifier = "TP"
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("identifier", context)

        assert result == "TP"

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_organization(self, mock_crud_project):
        """Test resolving project organization."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.organization = "Test Org"
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("organization", context)

        assert result == "Test Org"

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_id(self, mock_crud_project):
        """Test resolving project id."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_project.id = "proj-123"
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("id", context)

        assert result == "proj-123"

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_no_project_found(self, mock_crud_project):
        """Test resolving when project not found."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_crud_project.get.return_value = None

        result = await resolver.resolve("name", context)

        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_no_project_info(self):
        """Test resolving when no project info in trigger event."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        result = await resolver.resolve("name", context)

        assert result is None

    @pytest.mark.asyncio
    async def test_resolve_no_trigger_event_data(self):
        """Test resolving when no trigger event data."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data=None,
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        result = await resolver.resolve("name", context)

        assert result is None

    @pytest.mark.asyncio
    @patch("preloop.services.prompt_resolvers.project.crud_project")
    async def test_resolve_unknown_field(self, mock_crud_project):
        """Test resolving unknown field."""
        resolver = ProjectResolver()

        mock_db = MagicMock()
        context = ResolverContext(
            db=mock_db,
            trigger_event_data={"payload": {"project_id": "proj-123"}},
            flow_id="flow-1",
            execution_id="exec-1",
            account_id="acct-1",
        )

        mock_project = MagicMock()
        mock_crud_project.get.return_value = mock_project

        result = await resolver.resolve("unknown_field", context)

        assert result is None
