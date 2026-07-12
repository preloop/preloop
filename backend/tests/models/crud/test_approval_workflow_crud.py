"""Tests for approval_workflow CRUD operations."""

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.approval_workflow import CRUDApprovalWorkflow


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_approval_workflow():
    """Fixture for a CRUDApprovalWorkflow instance."""
    return CRUDApprovalWorkflow()


def test_get(crud_approval_workflow, mock_db_session):
    """Test retrieving an approval workflow by ID."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_workflow

    # Act
    result = crud_approval_workflow.get(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result.id == workflow_id
    assert result.account_id == account_id


def test_get_by_name(crud_approval_workflow, mock_db_session):
    """Test retrieving an approval workflow by name and account."""
    # Arrange
    account_id = str(uuid4())
    policy_name = "test-policy"
    mock_workflow = MagicMock()
    mock_workflow.name = policy_name
    mock_workflow.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_workflow

    # Act
    result = crud_approval_workflow.get_by_name(
        mock_db_session, account_id=account_id, name=policy_name
    )

    # Assert
    assert result.name == policy_name
    assert result.account_id == account_id


def test_get_multi_by_account(crud_approval_workflow, mock_db_session):
    """Test retrieving approval workflows for a specific account."""
    # Arrange
    account_id = str(uuid4())
    mock_workflow1 = MagicMock()
    mock_workflow1.account_id = account_id
    mock_workflow2 = MagicMock()
    mock_workflow2.account_id = account_id
    mock_policies = [mock_workflow1, mock_workflow2]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_policies

    # Act
    result = crud_approval_workflow.get_multi_by_account(
        mock_db_session, account_id=account_id, skip=0, limit=100
    )

    # Assert
    assert len(result) == 2
    assert all(policy.account_id == account_id for policy in result)


def test_remove(crud_approval_workflow, mock_db_session):
    """Test removing an approval workflow by ID."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id
    mock_workflow.is_default = False

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = mock_workflow
    mock_db_session.delete = MagicMock()
    mock_db_session.flush = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_approval_workflow.remove(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result.id == workflow_id
    mock_db_session.delete.assert_called_once_with(mock_workflow)
    mock_db_session.flush.assert_called_once()
    mock_db_session.commit.assert_called_once()


def test_remove_not_found(crud_approval_workflow, mock_db_session):
    """Test removing a non-existent approval workflow."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.first.return_value = None
    mock_db_session.delete = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_approval_workflow.remove(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result is None
    mock_db_session.delete.assert_not_called()
    mock_db_session.commit.assert_not_called()


def test_get_default(crud_approval_workflow, mock_db_session):
    """Test retrieving the default approval workflow for an account."""
    # Arrange
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.is_default = True
    mock_workflow.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_workflow

    # Act
    result = crud_approval_workflow.get_default(mock_db_session, account_id=account_id)

    # Assert
    assert result is not None
    assert result.is_default is True


def test_get_default_not_found(crud_approval_workflow, mock_db_session):
    """Test retrieving default policy when none exists."""
    # Arrange
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None

    # Act
    result = crud_approval_workflow.get_default(mock_db_session, account_id=account_id)

    # Assert
    assert result is None


def test_create_first_policy_becomes_default(crud_approval_workflow, mock_db_session):
    """Test that first policy for an account becomes default."""
    # Arrange
    account_id = str(uuid4())
    policy_data = {"name": "First Policy", "approval_type": "standard"}

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    # No existing policies
    mock_query.count.return_value = 0

    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()

    # Act
    result = crud_approval_workflow.create(
        mock_db_session, obj_in=policy_data, account_id=account_id
    )

    # Assert
    assert result is not None
    mock_db_session.add.assert_called_once()
    mock_db_session.commit.assert_called_once()


def test_create_with_explicit_default(crud_approval_workflow, mock_db_session):
    """Test creating policy with explicit is_default=True."""
    # Arrange
    account_id = str(uuid4())
    policy_data = {
        "name": "New Default",
        "approval_type": "standard",
        "is_default": True,
    }

    # One existing policy
    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.count.return_value = 1
    mock_query.first.return_value = None  # No current default to unmark

    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()
    mock_db_session.flush = MagicMock()

    # Act
    result = crud_approval_workflow.create(
        mock_db_session, obj_in=policy_data, account_id=account_id
    )

    # Assert
    assert result is not None
    mock_db_session.add.assert_called_once()


def test_create_without_default_when_policies_exist(
    crud_approval_workflow, mock_db_session
):
    """Test creating non-default policy when others exist."""
    # Arrange
    account_id = str(uuid4())
    policy_data = {"name": "Second Policy", "approval_type": "standard"}

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    # Existing policies
    mock_query.count.return_value = 1

    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()

    # Act
    result = crud_approval_workflow.create(
        mock_db_session, obj_in=policy_data, account_id=account_id
    )

    # Assert
    assert result is not None


def test_update_setting_as_default(crud_approval_workflow, mock_db_session):
    """Test updating policy to become default."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id
    mock_workflow.is_default = False

    update_data = {"is_default": True}

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None  # No existing default

    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()
    mock_db_session.flush = MagicMock()

    # Act
    result = crud_approval_workflow.update(
        mock_db_session, db_obj=mock_workflow, obj_in=update_data
    )

    # Assert
    mock_db_session.add.assert_called()
    mock_db_session.commit.assert_called_once()


def test_update_not_changing_default(crud_approval_workflow, mock_db_session):
    """Test updating policy without changing default status."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id
    mock_workflow.is_default = True

    update_data = {"name": "Updated Name"}

    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()
    mock_db_session.refresh = MagicMock()

    # Act
    result = crud_approval_workflow.update(
        mock_db_session, db_obj=mock_workflow, obj_in=update_data
    )

    # Assert
    assert result.name == "Updated Name"
    mock_db_session.commit.assert_called_once()


def test_remove_default_promotes_another(crud_approval_workflow, mock_db_session):
    """Test that removing default policy promotes another policy."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id
    mock_workflow.is_default = True

    replacement_policy = MagicMock()
    replacement_policy.is_default = False

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    # First call returns policy to delete, second call returns replacement
    mock_query.first.side_effect = [mock_workflow, replacement_policy]

    mock_db_session.delete = MagicMock()
    mock_db_session.flush = MagicMock()
    mock_db_session.add = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_approval_workflow.remove(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result == mock_workflow
    mock_db_session.delete.assert_called_once_with(mock_workflow)
    mock_db_session.commit.assert_called_once()
    # Replacement should be marked as default
    assert replacement_policy.is_default is True


def test_remove_default_no_replacement(crud_approval_workflow, mock_db_session):
    """Test removing default policy when no other policies exist."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())
    mock_workflow = MagicMock()
    mock_workflow.id = workflow_id
    mock_workflow.account_id = account_id
    mock_workflow.is_default = True

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    # First call returns policy to delete, second call returns None (no replacement)
    mock_query.first.side_effect = [mock_workflow, None]

    mock_db_session.delete = MagicMock()
    mock_db_session.flush = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_approval_workflow.remove(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result == mock_workflow
    mock_db_session.delete.assert_called_once_with(mock_workflow)
    mock_db_session.commit.assert_called_once()


def test_get_by_name_not_found(crud_approval_workflow, mock_db_session):
    """Test retrieving policy by name that doesn't exist."""
    # Arrange
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None

    # Act
    result = crud_approval_workflow.get_by_name(
        mock_db_session, account_id=account_id, name="Non-existent"
    )

    # Assert
    assert result is None


def test_get_not_found(crud_approval_workflow, mock_db_session):
    """Test retrieving policy by ID that doesn't exist."""
    # Arrange
    workflow_id = uuid4()
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None

    # Act
    result = crud_approval_workflow.get(
        mock_db_session, id=workflow_id, account_id=account_id
    )

    # Assert
    assert result is None


def test_get_multi_by_account_empty(crud_approval_workflow, mock_db_session):
    """Test retrieving policies when none exist."""
    # Arrange
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = []

    # Act
    result = crud_approval_workflow.get_multi_by_account(
        mock_db_session, account_id=account_id
    )

    # Assert
    assert result == []


def test_get_multi_by_account_with_pagination(crud_approval_workflow, mock_db_session):
    """Test retrieving policies with pagination."""
    # Arrange
    account_id = str(uuid4())
    mock_policies = [MagicMock() for _ in range(5)]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_policies

    # Act
    result = crud_approval_workflow.get_multi_by_account(
        mock_db_session, account_id=account_id, skip=10, limit=5
    )

    # Assert
    assert len(result) == 5
    mock_query.offset.assert_called()
    mock_query.limit.assert_called()


def test_find_accounts_missing_workflows_flags_and_clears(
    crud_approval_workflow, db_session, test_user
):
    """Accounts without any approval workflow must be flagged by the startup
    seeding pass, and drop off the list once a workflow exists."""
    missing = crud_approval_workflow.find_accounts_missing_workflows(db_session)
    assert test_user.account_id in missing

    crud_approval_workflow.create(
        db_session,
        obj_in={"name": "Default Approval Workflow", "approval_type": "standard"},
        account_id=str(test_user.account_id),
    )

    missing = crud_approval_workflow.find_accounts_missing_workflows(db_session)
    assert test_user.account_id not in missing


def test_find_startup_repair_targets_combines_broken_and_missing(
    crud_approval_workflow, db_session, test_user
):
    """The startup scan must return both legacy defaults and accounts with no
    workflows in a single pass."""
    from preloop.models.crud import crud_approval_workflow as crud

    # test_user's account starts with no workflows -> missing.
    broken, missing = crud.find_startup_repair_targets(db_session)
    assert test_user.account_id in missing
    assert test_user.account_id not in broken

    # Seed a legacy-shaped default (manual + no approvers) -> broken, not missing.
    crud.create(
        db_session,
        obj_in={
            "name": "Default Approval Workflow",
            "approval_type": "manual",
            "is_default": True,
            "approver_user_ids": [],
            "approver_team_ids": [],
        },
        account_id=str(test_user.account_id),
    )
    broken, missing = crud.find_startup_repair_targets(db_session)
    assert test_user.account_id in broken
    assert test_user.account_id not in missing
