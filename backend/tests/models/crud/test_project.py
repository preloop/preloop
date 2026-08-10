import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.project import CRUDProject
from preloop.models.models.project import Project


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_project():
    """Fixture for a CRUDProject instance."""
    return CRUDProject(Project)


def test_get_all_active_by_identifier_or_name_globally(crud_project, mock_db_session):
    """Test retrieving all active projects by identifier or name globally."""
    # Arrange
    identifier = "test-project"
    account_id = str(uuid4())
    mock_projects = [Project(id=str(uuid4()), identifier=identifier)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.options.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.all.return_value = mock_projects

    # Act
    result = crud_project.get_all_active_by_identifier_or_name_globally(
        mock_db_session, identifier_or_name=identifier, account_id=account_id
    )

    # Assert
    assert len(result) == 1
    assert result[0].identifier == identifier


def test_get_by_slug_or_identifier(crud_project, mock_db_session):
    """Test retrieving a project by slug or identifier."""
    # Arrange
    slug = "test-project"
    organization_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), slug=slug)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_slug_or_identifier(
        mock_db_session, slug_or_identifier=slug, organization_id=organization_id
    )

    # Assert
    assert result.slug == slug


def test_get_by_name(crud_project, mock_db_session):
    """Test retrieving a project by name."""
    # Arrange
    name = "Test Project"
    organization_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), name=name)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_name(
        mock_db_session, name=name, organization_id=organization_id
    )

    # Assert
    assert result.name == name


def test_get_by_identifier(crud_project, mock_db_session):
    """Test retrieving a project by identifier."""
    # Arrange
    identifier = "test-project"
    account_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), identifier=identifier)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_identifier(
        mock_db_session, identifier=identifier, account_id=account_id
    )

    # Assert
    assert result.identifier == identifier


def test_get_by_identifier_with_organization_id(crud_project, mock_db_session):
    """Test retrieving a project by identifier scoped to an organization.

    Regression: get_by_identifier did not accept organization_id, so
    endpoint call sites passing it (e.g. create_project's duplicate check)
    raised TypeError and returned 500.
    """
    # Arrange
    identifier = "test-project"
    organization_id = str(uuid4())
    mock_project = Project(
        id=str(uuid4()), identifier=identifier, organization_id=organization_id
    )

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_identifier(
        mock_db_session, identifier=identifier, organization_id=organization_id
    )

    # Assert
    assert result.identifier == identifier
    # Both the identifier filter and the organization filter must be applied.
    assert mock_query.filter.call_count == 2


def test_get_for_tracker(crud_project, mock_db_session):
    """Test retrieving projects for a tracker."""
    # Arrange
    tracker_id = str(uuid4())
    mock_projects = [Project(id=str(uuid4())) for _ in range(2)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_projects

    # Act
    result = crud_project.get_for_tracker(mock_db_session, tracker_id=tracker_id)

    # Assert
    assert len(result) == 2


def test_get_for_organization(crud_project, mock_db_session):
    """Test retrieving projects for an organization."""
    # Arrange
    organization_id = str(uuid4())
    account_id = str(uuid4())
    mock_projects = [Project(id=str(uuid4())) for _ in range(3)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_projects

    # Act
    result = crud_project.get_for_organization(
        mock_db_session, organization_id=organization_id, account_id=account_id
    )

    # Assert
    assert len(result) == 3


def test_count_for_organization(crud_project, mock_db_session):
    """Test counting projects for an organization."""
    # Arrange
    organization_id = str(uuid4())
    account_id = str(uuid4())
    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.count.return_value = 5

    # Act
    result = crud_project.count_for_organization(
        mock_db_session, organization_id=organization_id, account_id=account_id
    )

    # Assert
    assert result == 5


def test_get_active(crud_project, mock_db_session):
    """Test retrieving active projects."""
    # Arrange
    account_id = str(uuid4())
    mock_projects = [Project(id=str(uuid4()), is_active=True)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_projects

    # Act
    result = crud_project.get_active(mock_db_session, account_id=account_id)

    # Assert
    assert len(result) == 1
    assert result[0].is_active


def test_deactivate(crud_project, mock_db_session):
    """Test deactivating a project."""
    # Arrange
    project_id = str(uuid4())
    account_id = str(uuid4())
    mock_project = Project(id=project_id, is_active=True)

    crud_project.get = MagicMock(return_value=mock_project)

    # Act
    result = crud_project.deactivate(
        mock_db_session, id=project_id, account_id=account_id
    )

    # Assert
    assert not result.is_active
    mock_db_session.add.assert_called_once_with(mock_project)
    mock_db_session.commit.assert_called_once()
    mock_db_session.refresh.assert_called_once_with(mock_project)


def test_get_by_identifier_or_name_across_orgs(crud_project, mock_db_session):
    """Test retrieving a project by identifier or name across organizations."""
    # Arrange
    identifier = "test-project"
    account_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), identifier=identifier)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_identifier_or_name_across_orgs(
        mock_db_session, identifier_or_name=identifier, account_id=account_id
    )

    # Assert
    assert result.identifier == identifier


def test_get_for_tracker_with_account_id(crud_project, mock_db_session):
    """Test retrieving projects for a tracker with account filter."""
    # Arrange
    tracker_id = str(uuid4())
    account_id = str(uuid4())
    mock_projects = [Project(id=str(uuid4())) for _ in range(2)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_projects

    # Act
    result = crud_project.get_for_tracker(
        mock_db_session, tracker_id=tracker_id, account_id=account_id
    )

    # Assert
    assert len(result) == 2


def test_get_by_identifier_with_account_id(crud_project, mock_db_session):
    """Test retrieving a project by identifier with account filter."""
    # Arrange
    identifier = "test-123"
    account_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), identifier=identifier)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_identifier(
        mock_db_session, identifier=identifier, account_id=account_id
    )

    # Assert
    assert result.identifier == identifier
    assert mock_query.join.called


def test_get_by_slug_or_identifier_with_account_id(crud_project, mock_db_session):
    """Test retrieving a project by slug/identifier with account filter."""
    # Arrange
    slug = "test-slug"
    account_id = str(uuid4())
    mock_project = Project(id=str(uuid4()), slug=slug)

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.join.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = mock_project

    # Act
    result = crud_project.get_by_slug_or_identifier(
        mock_db_session, slug_or_identifier=slug, account_id=account_id
    )

    # Assert
    assert result.slug == slug
    assert mock_query.join.called
