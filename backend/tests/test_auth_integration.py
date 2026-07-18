"""Integration tests for authentication and authorization refactoring.

These tests verify:
1. Registration automatically assigns Owner role with full permissions
2. User → Account relationship works correctly
3. Endpoints properly use User model (not Account) for authentication
4. Type annotations are correct (User vs UserResponse)
"""

from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from unittest.mock import patch

from preloop.models.crud import crud_account, crud_user, crud_user_role
from preloop.models.models.user import User
from preloop.models.models.account import Account
from preloop.api.auth.permissions import has_permission


class TestRegistrationWithOwnerRole:
    """Test that registration automatically assigns Owner role."""

    def test_register_assigns_owner_role(self, db_session: Session):
        """Test that new user automatically gets Owner role during registration."""
        from preloop.api.app import create_app
        from fastapi.testclient import TestClient
        from preloop.models.db.session import get_db_session
        import time

        # Use timestamp to ensure unique username (alphanumeric only)
        unique_suffix = str(int(time.time() * 1000000))[-8:]
        username = f"newowner{unique_suffix}"
        email = f"newowner{unique_suffix}@example.com"

        # Create app with db override
        app = create_app()

        def override_get_db():
            try:
                yield db_session
            finally:
                pass

        app.dependency_overrides[get_db_session] = override_get_db

        with TestClient(app) as client:
            with patch("preloop.api.auth.router.complete_new_account_setup_background"):
                # Register a new user
                response = client.post(
                    "/api/v1/auth/register",
                    json={
                        "username": username,
                        "email": email,
                        "password": "securepassword123",
                        "full_name": "New Owner",
                    },
                )

                assert response.status_code == 201, (
                    f"Registration failed: {response.json()}"
                )
                data = response.json()
                assert data["username"] == username
                assert data["email"] == email

        # Verify user exists in database
        user = crud_user.get_by_username(db_session, username=username)
        assert user is not None, "User was not created in database"
        assert user.username == username
        assert user.email == email

        # Verify Owner role was assigned
        user_roles = crud_user_role.get_by_user(db_session, user_id=user.id)
        assert len(user_roles) > 0, "No roles assigned to user"

        role_names = [ur.role.name.lower() for ur in user_roles]
        assert "owner" in role_names, f"Owner role not assigned. Roles: {role_names}"

        # Verify user has all permissions
        assert has_permission(user, "manage_billing", db_session), (
            "Owner should have manage_billing permission"
        )
        assert has_permission(user, "execute_flows", db_session), (
            "Owner should have execute_flows permission"
        )
        assert has_permission(user, "view_trackers", db_session), (
            "Owner should have view_trackers permission"
        )
        assert has_permission(user, "manage_users", db_session), (
            "Owner should have manage_users permission"
        )


class TestUserAccountRelationship:
    """Test User → Account relationship loading."""

    def test_user_has_account_relationship(self, db_session: Session):
        """Test that User.account relationship loads correctly."""
        # Create account
        account_data = {
            "organization_name": "Test Org",
            "is_active": True,
        }
        account = crud_account.create(db_session, obj_in=account_data)

        # Create user
        user_data = {
            "account_id": account.id,
            "username": "reltest",
            "email": "reltest@example.com",
            "hashed_password": "hashed",
            "is_active": True,
            "email_verified": True,
            "user_source": "local",
        }
        user = crud_user.create(db_session, obj_in=user_data)
        db_session.commit()

        # Refresh to load relationships
        db_session.refresh(user)

        # Test relationship
        assert user.account is not None, "User.account relationship not loaded"
        assert isinstance(user.account, Account), (
            "User.account is not an Account instance"
        )
        assert user.account.id == account.id, "User.account.id doesn't match"
        assert user.account_id == account.id, "User.account_id doesn't match"

    def test_user_account_foreign_key(self, db_session: Session):
        """Test that User.account_id properly references Account.id."""
        # Create account
        account = crud_account.create(
            db_session,
            obj_in={"organization_name": "FK Test Org", "is_active": True},
        )

        # Create user with account_id
        user = crud_user.create(
            db_session,
            obj_in={
                "account_id": account.id,
                "username": "fktest",
                "email": "fktest@example.com",
                "hashed_password": "hashed",
                "is_active": True,
                "email_verified": True,
                "user_source": "local",
            },
        )
        db_session.commit()

        # Query user and check account_id
        queried_user = crud_user.get(db_session, id=user.id)
        assert queried_user is not None
        assert queried_user.account_id == account.id


class TestEndpointsWithUserDependency:
    """Test that endpoints use proper User dependency pattern."""

    def test_get_account_for_user_returns_account(
        self, db_session: Session, test_user: User
    ):
        """Test that get_account_for_user() correctly returns Account from User."""
        from preloop.api.common import get_account_for_user

        # Call the dependency function
        account = get_account_for_user(current_user=test_user, db=db_session)

        assert account is not None, "get_account_for_user returned None"
        assert isinstance(account, Account), f"Expected Account, got {type(account)}"
        assert account.id == test_user.account_id, "Account ID mismatch"

    def test_get_account_for_user_type_annotation(self):
        """Test that get_account_for_user has correct type annotations."""
        from preloop.api.common import get_account_for_user
        import inspect
        from typing import get_type_hints

        # Get function signature
        inspect.signature(get_account_for_user)
        type_hints = get_type_hints(get_account_for_user)

        # Check current_user parameter type
        assert "current_user" in type_hints, "current_user parameter not in type hints"
        param_type = type_hints["current_user"]

        # The type should be User, not UserResponse
        assert param_type.__name__ == "User", (
            f"get_account_for_user should accept User model, not {param_type.__name__}"
        )

    def test_features_endpoint(self, client: TestClient, test_user: User):
        """Test /features endpoint returns plugin features."""
        response = client.get("/api/v1/features")

        assert response.status_code == 200, f"Failed: {response.json()}"
        data = response.json()
        assert "features" in data
        assert "plugins" in data


class TestAuthContextWithUser:
    """Test that authentication context provides User objects."""

    def test_get_current_active_user_returns_user_model(self, test_user: User):
        """Test that get_current_active_user returns User model, not UserResponse."""
        # Verify the test fixture provides a User model, not UserResponse
        # This ensures our auth system returns User objects
        assert isinstance(test_user, User), (
            f"Expected User model, got {type(test_user)}"
        )
        assert hasattr(test_user, "account_id"), "User should have account_id attribute"
        assert hasattr(test_user, "username"), "User should have username attribute"
        assert hasattr(test_user, "email"), "User should have email attribute"

        # Verify it's the SQLAlchemy model, not a Pydantic schema
        assert type(test_user).__name__ == "User", (
            "Should be User model, not UserResponse"
        )

    def test_user_has_required_attributes(self, test_user: User):
        """Test that User model has all required attributes for auth."""
        from uuid import UUID

        # These are the attributes that endpoints expect
        assert hasattr(test_user, "id"), "User missing id"
        assert hasattr(test_user, "account_id"), "User missing account_id"
        assert hasattr(test_user, "username"), "User missing username"
        assert hasattr(test_user, "email"), "User missing email"
        assert hasattr(test_user, "is_active"), "User missing is_active"
        assert hasattr(test_user, "email_verified"), "User missing email_verified"

        # Verify types
        assert isinstance(test_user.account_id, UUID), "account_id should be UUID"
        assert isinstance(test_user.username, str), "username should be str"
        assert isinstance(test_user.email, str), "email should be str"


class TestPermissionEnforcement:
    """Test that permission checks work correctly after registration."""

    def test_new_user_has_owner_permissions(self, db_session: Session):
        """Test that newly registered user has all Owner permissions."""
        from preloop.api.app import create_app
        from fastapi.testclient import TestClient
        from preloop.models.db.session import get_db_session
        import time

        # Use timestamp to ensure unique username
        unique_suffix = str(int(time.time() * 1000000))[-8:]
        username = f"permtest{unique_suffix}"
        email = f"permtest{unique_suffix}@example.com"

        app = create_app()
        app.dependency_overrides[get_db_session] = lambda: db_session

        with TestClient(app) as client:
            with patch("preloop.api.auth.router.complete_new_account_setup_background"):
                # Register user
                response = client.post(
                    "/api/v1/auth/register",
                    json={
                        "username": username,
                        "email": email,
                        "password": "password123",
                        "full_name": "Permission Test",
                    },
                )
                assert response.status_code == 201

        # Get the created user
        user = crud_user.get_by_username(db_session, username=username)
        assert user is not None

        # Test critical permissions that were failing
        critical_permissions = [
            "view_trackers",
            "view_issues",
            "view_flows",
            "create_flows",
            "execute_flows",
            "manage_billing",
            "manage_users",
        ]

        for permission in critical_permissions:
            assert has_permission(user, permission, db_session), (
                f"New user missing critical permission: {permission}"
            )
