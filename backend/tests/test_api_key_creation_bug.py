"""Simple integration test that would have caught the created_by vs user_id bug.

This test validates that ApiKey objects can be created with the correct field names
and that they work with the actual database schema.
"""

import pytest
import uuid
from datetime import datetime, timezone
from sqlalchemy.orm import Session

from preloop.models.models.api_key import ApiKey
from preloop.models.models.user import User
from preloop.models.models.account import Account


def test_api_key_creation_with_user_id(db_session: Session):
    """Test that ApiKey can be created with user_id field.

    This test would have immediately failed if the code tried to use
    'created_by' instead of 'user_id', catching the bug before deployment.
    """
    suffix = uuid.uuid4().hex[:8]
    # Create a test account
    test_account = Account(
        organization_name=f"Test Organization {suffix}",
        email_verified=True,
        is_active=True,
        is_superuser=False,
    )
    db_session.add(test_account)
    db_session.flush()

    # Create a test user
    test_user = User(
        username=f"testuser_{suffix}",
        email=f"test_user_{suffix}@example.com",
        hashed_password="hashed_password",
        account_id=test_account.id,
        is_active=True,
        email_verified=True,
    )
    db_session.add(test_user)
    db_session.flush()

    # This is the critical test - creating an ApiKey with user_id
    # If the code tries to use 'created_by' this will raise TypeError
    try:
        api_key = ApiKey(
            name="Test API Key",
            key=f"test_key_{uuid.uuid4().hex}",  # Use unique key to avoid duplicates
            account_id=test_user.account_id,
            user_id=test_user.id,  # This is the correct field
            scopes=["read", "write"],
            is_active=True,
        )
        db_session.add(api_key)
        db_session.flush()

        # Verify the key was created with the correct user_id
        assert api_key.id is not None
        assert api_key.user_id == test_user.id
        assert api_key.name == "Test API Key"
        assert hasattr(api_key, "user_id"), "ApiKey must have user_id attribute"

        # Verify that the old field doesn't exist or isn't being used
        # (Some models might still have the old field for migration purposes)
        if hasattr(api_key, "created_by"):
            pytest.fail("ApiKey still has deprecated 'created_by' field")

        # Test the relationship
        assert api_key.creator == test_user
        assert api_key.creator.id == test_user.id

    except TypeError as e:
        if "user_id" in str(e):
            pytest.fail(
                f"ApiKey model does not accept 'user_id' parameter. "
                f"This indicates the created_by vs user_id bug: {e}"
            )
        raise


def test_api_key_creation_fails_with_created_by(db_session: Session):
    """Negative test: verify that using 'created_by' fails.

    This explicitly tests that the old field name is no longer accepted.
    """
    suffix = uuid.uuid4().hex[:8]
    # Create a test account
    test_account = Account(
        organization_name=f"Test Organization 2 {suffix}",
        email_verified=True,
        is_active=True,
        is_superuser=False,
    )
    db_session.add(test_account)
    db_session.flush()

    # Create a test user
    test_user = User(
        username=f"testuser2_{suffix}",
        email=f"test_user2_{suffix}@example.com",
        hashed_password="hashed_password",
        account_id=test_account.id,
        is_active=True,
        email_verified=True,
    )
    db_session.add(test_user)
    db_session.flush()

    # Try to create an ApiKey with the old 'created_by' field
    # This should fail with a TypeError
    with pytest.raises(TypeError) as exc_info:
        ApiKey(
            name="Test API Key",
            key=f"test_key_{uuid.uuid4().hex}",  # Use unique key to avoid duplicates
            account_id=test_user.account_id,
            created_by=test_user.username,  # This should fail!
            scopes=["read", "write"],
            is_active=True,
        )

    # Verify the error message mentions the invalid parameter
    assert "created_by" in str(exc_info.value).lower()


def test_api_key_response_schema_compatibility():
    """Test that response schemas use user_id instead of created_by."""
    from preloop.schemas.auth import ApiKeyResponse

    # Get all fields from the schema
    schema_fields = set(ApiKeyResponse.model_fields.keys())

    # Critical assertions
    assert "user_id" in schema_fields, "ApiKeyResponse must include user_id field"

    assert "created_by" not in schema_fields, (
        "ApiKeyResponse should not include deprecated created_by field"
    )

    # Verify we can create a response with user_id
    test_response = ApiKeyResponse(
        id=uuid.uuid4(),
        name="Test Key",
        key="test_key_123",
        created_at=datetime.now(timezone.utc),
        expires_at=None,
        scopes=[],
        user_id=uuid.uuid4(),  # This is the critical field
        last_used_at=None,
    )

    assert test_response.user_id is not None
