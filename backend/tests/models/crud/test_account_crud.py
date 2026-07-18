import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.account import CRUDAccount
from preloop.models.models.account import Account


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_account():
    """Fixture for a CRUDAccount instance."""
    return CRUDAccount(Account)


def test_create(crud_account, mock_db_session):
    """Test creating an account."""
    # Arrange
    obj_in = {"email": "test@example.com", "username": "testuser"}

    # Act
    with patch("preloop.models.crud.base.CRUDBase.create") as mock_create:
        crud_account.create(mock_db_session, obj_in=obj_in)

        # Assert
        mock_create.assert_called_once()
        created_data = mock_create.call_args[1]["obj_in"]
        assert "created" in created_data
        assert "last_updated" in created_data


def test_update(crud_account, mock_db_session):
    """Test updating an account."""
    # Arrange
    db_obj = Account(id=str(uuid4()))
    obj_in = {"username": "newusername"}

    # Act
    with patch("preloop.models.crud.base.CRUDBase.update") as mock_update:
        crud_account.update(mock_db_session, db_obj=db_obj, obj_in=obj_in)

        # Assert
        mock_update.assert_called_once()
        updated_data = mock_update.call_args[1]["obj_in"]
        assert "last_updated" in updated_data


# NOTE: get_by_email and get_by_username methods moved to User CRUD; Account CRUD
# no longer exposes those lookups, so the former retrieval tests were removed.


def test_get_active(crud_account, mock_db_session):
    """Test retrieving active accounts."""
    # Arrange
    mock_accounts = [Account(id=str(uuid4()), is_active=True)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_accounts

    # Act
    result = crud_account.get_active(mock_db_session)

    # Assert
    assert len(result) == 1
    assert result[0].is_active


# NOTE: get_by_oauth moved to User CRUD; Account CRUD no longer exposes that lookup.


def test_add_to_organization(crud_account, mock_db_session):
    """Test adding an account to an organization."""
    # Arrange
    account_id = str(uuid4())
    organization_id = str(uuid4())

    # Act
    crud_account.add_to_organization(
        mock_db_session, account_id=account_id, organization_id=organization_id
    )

    # Assert
    mock_db_session.execute.assert_called_once()
    mock_db_session.commit.assert_called_once()


def test_remove_from_organization(crud_account, mock_db_session):
    """Test removing an account from an organization."""
    # Arrange
    account_id = str(uuid4())
    organization_id = str(uuid4())

    # Act
    crud_account.remove_from_organization(
        mock_db_session, account_id=account_id, organization_id=organization_id
    )

    # Assert
    mock_db_session.execute.assert_called_once()
    mock_db_session.commit.assert_called_once()


def test_update_organization_role(crud_account, mock_db_session):
    """Test updating an account's role in an organization."""
    # Arrange
    account_id = str(uuid4())
    organization_id = str(uuid4())
    role = "admin"

    # Act
    crud_account.update_organization_role(
        mock_db_session,
        account_id=account_id,
        organization_id=organization_id,
        role=role,
    )

    # Assert
    mock_db_session.execute.assert_called_once()
    mock_db_session.commit.assert_called_once()


def test_get_organizations(crud_account, mock_db_session):
    """Test getting all organizations and roles for an account."""
    # Arrange
    account_id = str(uuid4())
    mock_account = Account(id=account_id)
    mock_account.organizations = [
        MagicMock(organization_id="org1", role="admin"),
        MagicMock(organization_id="org2", role="member"),
    ]

    crud_account.get = MagicMock(return_value=mock_account)

    # Act
    result = crud_account.get_organizations(mock_db_session, account_id=account_id)

    # Assert
    assert result == {"org1": "admin", "org2": "member"}


def test_get_organizations_not_found(crud_account, mock_db_session):
    """Test getting organizations when account doesn't exist."""
    # Arrange
    account_id = str(uuid4())
    crud_account.get = MagicMock(return_value=None)

    # Act
    result = crud_account.get_organizations(mock_db_session, account_id=account_id)

    # Assert
    assert result == {}


def test_update_with_pydantic_model(crud_account, mock_db_session):
    """Test updating an account with a Pydantic model."""
    # Arrange
    db_obj = Account(id=str(uuid4()))

    # Create a mock Pydantic model with model_dump method
    mock_pydantic_obj = MagicMock()
    mock_pydantic_obj.model_dump.return_value = {"username": "newusername"}

    # Act
    with patch("preloop.models.crud.base.CRUDBase.update") as mock_update:
        crud_account.update(mock_db_session, db_obj=db_obj, obj_in=mock_pydantic_obj)

        # Assert
        mock_pydantic_obj.model_dump.assert_called_once_with(exclude_unset=True)
        mock_update.assert_called_once()
        updated_data = mock_update.call_args[1]["obj_in"]
        assert "last_updated" in updated_data
