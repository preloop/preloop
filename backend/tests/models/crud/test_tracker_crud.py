"""Tests for tracker CRUD operations."""

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.tracker import CRUDTracker
from preloop.models.models.tracker import Tracker


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_tracker():
    """Fixture for a CRUDTracker instance."""
    return CRUDTracker(Tracker)


def test_get_for_account(crud_tracker, mock_db_session):
    """Test retrieving trackers for an account."""
    # Arrange
    account_id = str(uuid4())
    mock_trackers = [MagicMock(), MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.options.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_trackers

    # Act
    result = crud_tracker.get_for_account(mock_db_session, account_id=account_id)

    # Assert
    assert len(result) == 2


def test_get_active_without_account_id(crud_tracker, mock_db_session):
    """Test retrieving active trackers without account filter."""
    # Arrange
    mock_trackers = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_trackers

    # Act
    result = crud_tracker.get_active(mock_db_session)

    # Assert
    assert len(result) == 1


def test_get_active_with_account_id(crud_tracker, mock_db_session):
    """Test retrieving active trackers with account filter."""
    # Arrange
    account_id = str(uuid4())
    mock_trackers = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_trackers

    # Act
    result = crud_tracker.get_active(mock_db_session, account_id=account_id)

    # Assert
    assert len(result) == 1


def test_get_by_type_without_account_id(crud_tracker, mock_db_session):
    """Test retrieving trackers by type without account filter."""
    # Arrange
    from preloop.models.models.tracker import TrackerType

    mock_trackers = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_trackers

    # Act
    result = crud_tracker.get_by_type(mock_db_session, tracker_type=TrackerType.GITHUB)

    # Assert
    assert len(result) == 1


def test_get_by_type_with_account_id(crud_tracker, mock_db_session):
    """Test retrieving trackers by type with account filter."""
    # Arrange
    from preloop.models.models.tracker import TrackerType

    account_id = str(uuid4())
    mock_trackers = [MagicMock()]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_trackers

    # Act
    result = crud_tracker.get_by_type(
        mock_db_session, tracker_type=TrackerType.GITHUB, account_id=account_id
    )

    # Assert
    assert len(result) == 1


def test_get_by_id(crud_tracker, mock_db_session):
    """Test retrieving a tracker by ID."""
    # Arrange
    tracker_id = str(uuid4())
    mock_tracker = MagicMock()

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_tracker

    # Act
    result = crud_tracker.get_by_id(mock_db_session, id=tracker_id)

    # Assert
    assert result == mock_tracker
