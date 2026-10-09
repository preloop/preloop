"""Tests for mcp_server CRUD operations."""

import pytest
from unittest.mock import MagicMock
from uuid import uuid4

from sqlalchemy.orm import Session

from preloop.models.crud.mcp_server import CRUDMCPServer


@pytest.fixture
def mock_db_session():
    """Fixture for a mock database session."""
    session = MagicMock(spec=Session)
    return session


@pytest.fixture
def crud_mcp_server():
    """Fixture for a CRUDMCPServer instance."""
    return CRUDMCPServer()


def test_get(crud_mcp_server, mock_db_session):
    """Test retrieving an MCP server by ID."""
    # Arrange
    server_id = uuid4()
    mock_server = MagicMock()
    mock_server.id = server_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_server

    # Act
    result = crud_mcp_server.get(mock_db_session, id=server_id)

    # Assert
    assert result.id == server_id
    mock_db_session.query.assert_called_once()


def test_get_with_account_id(crud_mcp_server, mock_db_session):
    """Test retrieving an MCP server by ID with account filter."""
    # Arrange
    server_id = uuid4()
    account_id = str(uuid4())
    mock_server = MagicMock()
    mock_server.id = server_id
    mock_server.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_server

    # Act
    result = crud_mcp_server.get(mock_db_session, id=server_id, account_id=account_id)

    # Assert
    assert result.id == server_id
    assert result.account_id == account_id


def test_get_by_name(crud_mcp_server, mock_db_session):
    """Test retrieving an MCP server by name and account."""
    # Arrange
    account_id = str(uuid4())
    name = "test-server"
    mock_server = MagicMock()
    mock_server.name = name
    mock_server.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_server

    # Act
    result = crud_mcp_server.get_by_name(
        mock_db_session, account_id=account_id, name=name
    )

    # Assert
    assert result.name == name
    assert result.account_id == account_id


def test_get_multi_by_account(crud_mcp_server, mock_db_session):
    """Test retrieving MCP servers for a specific account."""
    # Arrange
    account_id = str(uuid4())
    mock_server1 = MagicMock()
    mock_server1.account_id = account_id
    mock_server2 = MagicMock()
    mock_server2.account_id = account_id
    mock_servers = [mock_server1, mock_server2]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.offset.return_value = mock_query
    mock_query.limit.return_value = mock_query
    mock_query.all.return_value = mock_servers

    # Act
    result = crud_mcp_server.get_multi_by_account(
        mock_db_session, account_id=account_id, skip=0, limit=100
    )

    # Assert
    assert len(result) == 2
    assert all(server.account_id == account_id for server in result)


def test_get_active_by_account(crud_mcp_server, mock_db_session):
    """Test retrieving active MCP servers for a specific account."""
    # Arrange
    account_id = str(uuid4())
    mock_server1 = MagicMock()
    mock_server1.account_id = account_id
    mock_server1.status = "active"
    mock_server2 = MagicMock()
    mock_server2.account_id = account_id
    mock_server2.status = "active"
    mock_servers = [mock_server1, mock_server2]

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    # First-wins order (#1135): created_at, then id.
    mock_query.order_by.return_value = mock_query
    mock_query.all.return_value = mock_servers

    # Act
    result = crud_mcp_server.get_active_by_account(
        mock_db_session, account_id=account_id
    )

    # Assert
    assert len(result) == 2
    assert all(server.status == "active" for server in result)


def test_remove(crud_mcp_server, mock_db_session):
    """Test removing an MCP server by ID."""
    # Arrange
    server_id = uuid4()
    account_id = str(uuid4())
    mock_server = MagicMock()
    mock_server.id = server_id
    mock_server.account_id = account_id

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = mock_server
    mock_db_session.delete = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_mcp_server.remove(
        mock_db_session, id=server_id, account_id=account_id
    )

    # Assert
    assert result.id == server_id
    mock_db_session.delete.assert_called_once_with(mock_server)
    mock_db_session.commit.assert_called_once()


def test_remove_not_found(crud_mcp_server, mock_db_session):
    """Test removing a non-existent MCP server."""
    # Arrange
    server_id = uuid4()
    account_id = str(uuid4())

    mock_query = MagicMock()
    mock_db_session.query.return_value = mock_query
    mock_query.filter.return_value = mock_query
    mock_query.first.return_value = None
    mock_db_session.delete = MagicMock()
    mock_db_session.commit = MagicMock()

    # Act
    result = crud_mcp_server.remove(
        mock_db_session, id=server_id, account_id=account_id
    )

    # Assert
    assert result is None
    mock_db_session.delete.assert_not_called()
    mock_db_session.commit.assert_not_called()
