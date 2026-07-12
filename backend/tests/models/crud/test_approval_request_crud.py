"""Tests for approval_request CRUD operations."""

from datetime import datetime
import pytest
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.approval_request import CRUDApprovalRequest
from preloop.models.models.approval_request import ApprovalRequest


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_approval_request():
    """Fixture for a CRUDApprovalRequest instance."""
    return CRUDApprovalRequest(ApprovalRequest)


def test_get_by_token(crud_approval_request, mock_db_session):
    """Test retrieving an approval request by token."""
    # Arrange
    token = "test-token-123"
    mock_request = MagicMock()
    mock_request.approval_token = token

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_request

    # Act
    result = crud_approval_request.get_by_token(mock_db_session, token=token)

    # Assert
    assert result.approval_token == token
    mock_db_session.query.assert_called_once()


def test_get_by_id_and_token(crud_approval_request, mock_db_session):
    """Test retrieving an approval request by ID and token."""
    # Arrange
    request_id = str(uuid4())
    token = "test-token-123"
    mock_request = MagicMock()
    mock_request.id = request_id
    mock_request.approval_token = token

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_request

    # Act
    result = crud_approval_request.get_by_id_and_token(
        mock_db_session, request_id=request_id, token=token
    )

    # Assert
    assert result.id == request_id
    assert result.approval_token == token


def test_get_multi_by_execution(crud_approval_request, mock_db_session):
    """Test retrieving approval requests for a specific execution."""
    # Arrange
    execution_id = str(uuid4())
    mock_request1 = MagicMock()
    mock_request1.execution_id = execution_id
    mock_request2 = MagicMock()
    mock_request2.execution_id = execution_id
    mock_requests = [mock_request1, mock_request2]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_requests

    # Act
    result = crud_approval_request.get_multi_by_execution(
        mock_db_session, execution_id=execution_id, skip=0, limit=100
    )

    # Assert
    assert len(result) == 2
    assert all(req.execution_id == execution_id for req in result)


def test_get_multi_by_execution_with_account(crud_approval_request, mock_db_session):
    """Test retrieving approval requests with account filter."""
    # Arrange
    execution_id = str(uuid4())
    account_id = str(uuid4())
    mock_requests = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_requests

    # Act
    result = crud_approval_request.get_multi_by_execution(
        mock_db_session, execution_id=execution_id, account_id=account_id
    )

    # Assert
    assert len(result) == 1


def test_get_multi_by_execution_with_status(crud_approval_request, mock_db_session):
    """Test retrieving approval requests with status filter."""
    # Arrange
    execution_id = str(uuid4())
    status = "pending"
    mock_requests = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_requests

    # Act
    result = crud_approval_request.get_multi_by_execution(
        mock_db_session, execution_id=execution_id, status=status
    )

    # Assert
    assert len(result) == 1


def test_expire_stale_pending_marks_expired(crud_approval_request, mock_db_session):
    """Test stale pending approval requests are marked expired."""
    now = datetime.utcnow()
    account_id = str(uuid4())
    execution_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.update.return_value = 2

    result = crud_approval_request.expire_stale_pending(
        mock_db_session,
        account_id=account_id,
        execution_id=execution_id,
        now=now,
    )

    assert result == 2
    mock_query.update.assert_called_once_with(
        {"status": "expired", "resolved_at": now},
        synchronize_session="fetch",
    )
    mock_db_session.commit.assert_called_once()


def test_get_multi_by_account_pending_does_not_expire_stale_rows(
    crud_approval_request, mock_db_session
):
    """Test pending account lists remain read-only."""
    account_id = str(uuid4())
    mock_requests = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_requests

    with patch.object(
        crud_approval_request,
        "expire_stale_pending",
        return_value=1,
    ) as expire_stale:
        result = crud_approval_request.get_multi_by_account(
            mock_db_session,
            account_id=account_id,
            status="pending",
        )

    assert result == mock_requests
    expire_stale.assert_not_called()


def test_get_multi_by_account_approved_does_not_expire_stale_rows(
    crud_approval_request, mock_db_session
):
    """Test resolved-status account lists do not mutate stale pending rows."""
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = []

    with patch.object(
        crud_approval_request,
        "expire_stale_pending",
        return_value=0,
    ) as expire_stale:
        result = crud_approval_request.get_multi_by_account(
            mock_db_session,
            account_id=account_id,
            status="approved",
        )

    assert result == []
    expire_stale.assert_not_called()
