"""Tests for the vector types module."""

from unittest.mock import MagicMock

import numpy as np

from preloop.models.db.vector_types import (
    VectorType,
    check_pgvector_extension,
    cosine_distance,
    euclidean_distance,
)


def test_cosine_distance():
    """Test cosine distance calculation."""
    v1 = [1.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0]
    v3 = [1.0, 1.0, 0.0]

    # Orthogonal vectors have distance of 1
    assert cosine_distance(v1, v2) == 1.0

    # Same vector has distance of 0
    assert cosine_distance(v1, v1) == 0.0

    # 45-degree angle has distance of 1-cos(45°) = 1-0.7071 ≈ 0.2929
    distance = cosine_distance(v1, v3)
    assert 0.29 < distance < 0.30


def test_euclidean_distance():
    """Test euclidean distance calculation."""
    v1 = [1.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0]
    v3 = [1.0, 1.0, 0.0]

    # Distance between orthogonal unit vectors should be sqrt(2)
    assert abs(euclidean_distance(v1, v2) - np.sqrt(2)) < 1e-10

    # Distance between same vector should be 0
    assert euclidean_distance(v1, v1) == 0.0

    # Distance between v1 and v3 should be 1.0
    assert abs(euclidean_distance(v1, v3) - 1.0) < 1e-10


def test_vector_type_initialization():
    """Test VectorType initialization."""
    vector_type = VectorType(dimensions=512)
    assert vector_type.dimensions == 512


def test_process_result_value_postgresql():
    """Test process_result_value with PostgreSQL."""
    vector_type = VectorType(dimensions=512)
    dialect = MagicMock()
    dialect.name = "postgresql"

    # Test with numpy array (as pgvector would return)
    test_array = np.array([0.1, 0.2, 0.3])
    result = vector_type.process_result_value(test_array, dialect)
    assert isinstance(result, list)
    assert result == test_array.tolist()


def test_check_pgvector_extension():
    """Test check_pgvector_extension function."""
    mock_engine = MagicMock()
    mock_connection = mock_engine.connect.return_value.__enter__.return_value
    mock_connection.execute.return_value.scalar.return_value = True

    assert check_pgvector_extension(mock_engine) is True
    mock_connection.execute.assert_called_once()


# install_pgvector_extension is covered by integration tests elsewhere.


def test_python_type():
    """Test python_type property."""
    vector_type = VectorType(dimensions=512)
    assert vector_type.python_type is list
