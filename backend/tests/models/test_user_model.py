"""Tests for User model and CRUD operations."""

from preloop.models.crud import crud_user


def test_create_user(db_session, create_user):
    """Test creating a user."""
    # Create a test user
    user = create_user(username="testuser", email="test@example.com")

    # Check the user was created with correct values
    assert user.id is not None
    assert user.username == "testuser"
    assert user.email == "test@example.com"
    assert user.is_active is True
    assert user.account_id is not None

    # Check user creation timestamp
    assert user.created_at is not None
    assert user.updated_at is not None


def test_get_user_by_email(db_session, create_user, create_account):
    """Test getting a user by email."""
    # Create account and user
    account = create_account()
    user = create_user(email="unique@example.com", account=account)

    # Get user by email
    retrieved = crud_user.get_by_email(
        db_session, email="unique@example.com", account_id=account.id
    )

    # Check retrieval
    assert retrieved is not None
    assert retrieved.id == user.id
    assert retrieved.email == "unique@example.com"


def test_get_user_by_username(db_session, create_user):
    """Test getting a user by username."""
    # Create a test user
    user = create_user(username="uniqueuser")

    # Get user by username
    retrieved = crud_user.get_by_username(db_session, username="uniqueuser")

    # Check retrieval
    assert retrieved is not None
    assert retrieved.id == user.id
    assert retrieved.username == "uniqueuser"


def test_update_user(db_session, create_user):
    """Test updating a user."""
    # Create a test user
    user = create_user()

    # Update user
    updated = crud_user.update(
        db_session,
        db_obj=user,
        obj_in={
            "full_name": "Updated Name",
            "is_active": False,
        },
    )

    # Check updates
    assert updated.full_name == "Updated Name"
    assert updated.is_active is False
    assert updated.username == user.username  # Unchanged
    assert updated.email == user.email  # Unchanged


def test_deactivate_user(db_session, create_user):
    """Test deactivating a user."""
    # Create a test user
    user = create_user()

    assert user.is_active is True

    # Deactivate user
    deactivated = crud_user.deactivate(db_session, user_id=user.id)

    # Check deactivation
    assert deactivated is not None
    assert deactivated.is_active is False


def test_get_users_by_account(db_session, create_account, create_user):
    """Test getting all users in an account."""
    # Create account
    account = create_account()

    # Create multiple users
    user1 = create_user(username="user1", email="user1@example.com", account=account)
    user2 = create_user(username="user2", email="user2@example.com", account=account)

    # Get users by account
    users = crud_user.get_by_account(db_session, account_id=account.id)

    # Check retrieval
    assert len(users) == 2
    assert user1 in users
    assert user2 in users


def test_user_is_local(db_session, create_user):
    """Test is_local_user property."""
    # Create local user
    local_user = create_user(user_source="local")
    assert local_user.is_local_user is True
    assert local_user.is_external_user is False

    # Create OAuth user
    oauth_user = create_user(
        username="oauthuser",
        email="oauth@example.com",
        user_source="oauth",
        oauth_provider="github",
        oauth_id="12345",
    )
    assert oauth_user.is_local_user is False
    assert oauth_user.is_external_user is True


def test_user_account_relationship(db_session, create_account, create_user):
    """Test user-account relationship."""
    # Create account
    account = create_account(organization_name="Test Org")

    # Create user
    user = create_user(account=account)

    # Check relationship
    assert user.account_id == account.id
    assert user.account == account

    # Check reverse relationship
    db_session.refresh(account)
    assert user in account.users


def test_new_user_defaults_auth_generation_to_zero(db_session, create_user):
    """Existing and new users start at generation 0."""
    user = create_user(username="genzero", email="genzero@example.com")
    assert user.auth_generation == 0


def test_bump_auth_generation_increments(db_session, create_user):
    """bump_auth_generation is atomic and returns the new value."""
    user = create_user(username="genbump", email="genbump@example.com")
    first = crud_user.bump_auth_generation(db_session, user_id=user.id)
    second = crud_user.bump_auth_generation(db_session, user_id=user.id)
    assert first == 1
    assert second == 2
    db_session.refresh(user)
    assert user.auth_generation == 2
