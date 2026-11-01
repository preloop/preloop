"""Tests for user management endpoints."""

from uuid import uuid4
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import (
    crud_account,
    crud_person,
    crud_role,
    crud_user,
    crud_user_role,
)
from preloop.models.models.user import User
from preloop.schemas.user import AdminUserUpdate


def test_get_current_user_info(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting current user's information."""
    response = client.get("/api/v1/users/me")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(test_user.id)
    assert data["username"] == test_user.username
    assert data["email"] == test_user.email
    assert data["account_id"] == str(test_user.account_id)


def test_update_current_user(client: TestClient, test_user: User, db_session: Session):
    """Test updating current user's profile."""
    response = client.patch(
        "/api/v1/users/me",
        json={
            "full_name": "Updated Name",
            "email": "updated@example.com",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["full_name"] == "Updated Name"
    assert data["email"] == "updated@example.com"

    # Verify database was updated
    db_session.refresh(test_user)
    assert test_user.full_name == "Updated Name"
    assert test_user.email == "updated@example.com"


def test_update_current_user_partial(
    client: TestClient, test_user: User, db_session: Session
):
    """Test partial update of current user's profile."""
    original_email = test_user.email

    response = client.patch(
        "/api/v1/users/me",
        json={"full_name": "New Name Only"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["full_name"] == "New Name Only"
    assert data["email"] == original_email  # Email unchanged


def test_change_password_success(
    client: TestClient, test_user: User, db_session: Session
) -> None:
    """Test changing password successfully."""
    from preloop.api.auth.jwt import get_password_hash, verify_password

    # Set a known password
    crud_person.set_password(
        db_session,
        membership_id=test_user.id,
        hashed_password=get_password_hash("oldpassword123"),
    )

    response = client.post(
        "/api/v1/users/me/change-password",
        json={
            "current_password": "oldpassword123",
            "new_password": "newpassword456",
        },
    )

    assert response.status_code == 204

    # Verify password was changed
    person = crud_person.credential_owner(db_session, membership_id=test_user.id)
    assert verify_password("newpassword456", person.hashed_password)
    assert not verify_password("oldpassword123", person.hashed_password)


def test_change_password_wrong_current(
    client: TestClient, test_user: User, db_session: Session
) -> None:
    """Test changing password with wrong current password."""
    from preloop.api.auth.jwt import get_password_hash

    crud_person.set_password(
        db_session,
        membership_id=test_user.id,
        hashed_password=get_password_hash("correctpassword"),
    )

    response = client.post(
        "/api/v1/users/me/change-password",
        json={
            "current_password": "wrongpassword",
            "new_password": "newpassword456",
        },
    )

    assert response.status_code == 401
    assert "incorrect" in response.json()["detail"].lower()


def test_change_password_external_auth(
    client: TestClient, test_user: User, db_session: Session
) -> None:
    """Test changing password for external auth user fails."""
    from preloop.api.auth.jwt import get_current_active_user

    external_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "username": f"provider-only-{uuid4().hex}",
            "email": "provider-only@example.com",
            "is_active": True,
            "user_source": "oauth",
        },
    )
    client.app.dependency_overrides[get_current_active_user] = lambda: external_user

    response = client.post(
        "/api/v1/users/me/change-password",
        json={
            "current_password": "anything",
            "new_password": "newpassword456",
        },
    )

    assert response.status_code == 401
    assert "incorrect" in response.json()["detail"].lower()


def test_list_users(client: TestClient, test_user: User, db_session: Session):
    """Test listing users in account."""
    # Create additional users
    for i in range(3):
        user_data = {
            "account_id": test_user.account_id,
            "email": f"user{i}@example.com",
            "username": f"user{i}",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        }
        crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get("/api/v1/users")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 4  # test_user + 3 new users
    assert len(data["users"]) == 4
    assert data["skip"] == 0
    assert data["limit"] == 100


def test_list_users_pagination(
    client: TestClient, test_user: User, db_session: Session
):
    """Test user list pagination."""
    # Create additional users
    for i in range(5):
        user_data = {
            "account_id": test_user.account_id,
            "email": f"page{i}@example.com",
            "username": f"page{i}",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        }
        crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get("/api/v1/users?skip=2&limit=3")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 6  # test_user + 5 new users
    assert len(data["users"]) == 3
    assert data["skip"] == 2
    assert data["limit"] == 3


def test_list_users_multi_tenancy(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that users from other accounts are not listed."""
    # Create another account and user
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_user_data = {
        "account_id": other_account.id,
        "email": "other@example.com",
        "username": "otheruser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=other_user_data)
    db_session.flush()

    response = client.get("/api/v1/users")

    assert response.status_code == 200
    data = response.json()
    # Should only see test_user, not other_user
    assert data["total"] == 1
    assert all(
        user["account_id"] == str(test_user.account_id) for user in data["users"]
    )


def test_get_user_success(client: TestClient, test_user: User, db_session: Session):
    """Test getting a specific user."""
    # Create another user in same account
    user_data = {
        "account_id": test_user.account_id,
        "email": "specific@example.com",
        "username": "specificuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    specific_user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get(f"/api/v1/users/{specific_user.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(specific_user.id)
    assert data["username"] == "specificuser"
    assert data["email"] == "specific@example.com"


def test_get_user_not_found(client: TestClient, test_user: User, db_session: Session):
    """Test getting non-existent user."""
    fake_id = str(uuid4())
    response = client.get(f"/api/v1/users/{fake_id}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_user_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting user from different account fails."""
    # Create another account and user
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_user_data = {
        "account_id": other_account.id,
        "email": "other@example.com",
        "username": "otheruser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    other_user = crud_user.create(db_session, obj_in=other_user_data)
    db_session.flush()

    response = client.get(f"/api/v1/users/{other_user.id}")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_tenant_admin_cannot_update_another_person_identity(
    client: TestClient, test_user: User, db_session: Session
):
    """Account membership administration cannot change global profile fields."""
    # Create another user
    user_data = {
        "account_id": test_user.account_id,
        "email": "toupdate@example.com",
        "username": "toupdateuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    target_user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.patch(
        f"/api/v1/users/{target_user.id}",
        json={
            "full_name": "Updated User Name",
            "email": "newemail@example.com",
        },
    )

    assert response.status_code == 403
    assert "global identity" in response.json()["detail"]
    assert (
        crud_person.profile_for_membership(
            db_session, membership_id=target_user.id
        ).email
        == "toupdate@example.com"
    )


def test_update_user_response_hides_legacy_roles(test_user: User, db_session: Session):
    """Stored commercial roles are not exposed as OSS access controls."""
    target_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "roleupdate@example.com",
            "username": "roleupdateuser",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    role = crud_role.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Role Update Tester",
            "description": "Role used to verify update responses",
            "is_system_role": False,
        },
    )
    crud_user_role.create(
        db_session,
        obj_in={"user_id": target_user.id, "role_id": role.id},
    )
    db_session.flush()

    from preloop.api.endpoints.users import update_user

    response = update_user(
        target_user.id,
        AdminUserUpdate(is_active=False),
        test_user,
        db_session,
    )

    data = response.model_dump()
    assert data["is_active"] is False
    assert data["roles"] == []
    assert data["inherited_roles"] == []


def test_core_user_response_does_not_load_legacy_roles(
    test_user: User, db_session: Session
):
    """OSS profiles do not load the stored commercial role assignments."""
    target_user = crud_user.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "batchroles@example.com",
            "username": "batchrolesuser",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    first_role = crud_role.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Batch Role One",
            "description": "First batch-loaded role",
            "is_system_role": False,
        },
    )
    second_role = crud_role.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Batch Role Two",
            "description": "Second batch-loaded role",
            "is_system_role": False,
        },
    )
    crud_user_role.create(
        db_session,
        obj_in={"user_id": target_user.id, "role_id": first_role.id},
    )
    crud_user_role.create(
        db_session,
        obj_in={"user_id": target_user.id, "role_id": second_role.id},
    )
    db_session.flush()

    from preloop.api.endpoints.users import _build_admin_user_response
    from preloop.models.crud.identity_directory import identity_directory

    with patch.object(
        identity_directory,
        "role_context",
        side_effect=AssertionError("core profiles should not load roles"),
    ):
        response = _build_admin_user_response(db_session, target_user)

    assert response.roles == []
    assert response.inherited_roles == []


def test_update_user_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating non-existent user."""
    fake_id = str(uuid4())
    response = client.patch(
        f"/api/v1/users/{fake_id}",
        json={"full_name": "New Name"},
    )

    assert response.status_code == 404


def test_update_user_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating user from different account fails."""
    # Create another account and user
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_user_data = {
        "account_id": other_account.id,
        "email": "other@example.com",
        "username": "otheruser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    other_user = crud_user.create(db_session, obj_in=other_user_data)
    db_session.flush()

    response = client.patch(
        f"/api/v1/users/{other_user.id}",
        json={"full_name": "Hacked Name"},
    )

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_deactivate_user_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test deactivating a user."""
    # Create another user
    user_data = {
        "account_id": test_user.account_id,
        "email": "todeactivate@example.com",
        "username": "todeactivate",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    target_user = crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.post(f"/api/v1/users/{target_user.id}/deactivate")

    assert response.status_code == 204

    # Verify user is deactivated
    db_session.refresh(target_user)
    assert target_user.is_active is False


def test_deactivate_self_fails(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that users cannot deactivate their own account."""
    response = client.post(f"/api/v1/users/{test_user.id}/deactivate")

    assert response.status_code == 400
    assert "your own account" in response.json()["detail"]


def test_deactivate_user_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test deactivating non-existent user."""
    fake_id = str(uuid4())
    response = client.post(f"/api/v1/users/{fake_id}/deactivate")

    assert response.status_code == 404


def test_deactivate_user_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test deactivating user from different account fails."""
    # Create another account and user
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_user_data = {
        "account_id": other_account.id,
        "email": "other@example.com",
        "username": "otheruser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    other_user = crud_user.create(db_session, obj_in=other_user_data)
    db_session.flush()

    response = client.post(f"/api/v1/users/{other_user.id}/deactivate")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_search_users_by_username(
    client: TestClient, test_user: User, db_session: Session
):
    """Test searching users by username."""
    # Create users with different usernames
    usernames = ["alice_smith", "alice_jones", "bob_alice", "charlie"]
    for username in usernames:
        user_data = {
            "account_id": test_user.account_id,
            "email": f"{username}@example.com",
            "username": username,
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        }
        crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get("/api/v1/users/search/alice")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 3  # alice_smith, alice_jones, bob_alice
    assert all("alice" in user["username"].lower() for user in data)


def test_search_users_case_insensitive(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that user search is case-insensitive."""
    user_data = {
        "account_id": test_user.account_id,
        "email": "john@example.com",
        "username": "JohnDoe",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get("/api/v1/users/search/john")

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    assert any(user["username"] == "JohnDoe" for user in data)


def test_search_users_limit(client: TestClient, test_user: User, db_session: Session):
    """Test search users with limit parameter."""
    # Create many users
    for i in range(15):
        user_data = {
            "account_id": test_user.account_id,
            "email": f"search{i}@example.com",
            "username": f"search{i}",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        }
        crud_user.create(db_session, obj_in=user_data)
    db_session.flush()

    response = client.get("/api/v1/users/search/search?limit=5")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 5


def test_search_users_only_active(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that search only returns active users."""
    # Create active and inactive users
    active_user_data = {
        "account_id": test_user.account_id,
        "email": "active@example.com",
        "username": "activeuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=active_user_data)

    inactive_user_data = {
        "account_id": test_user.account_id,
        "email": "inactive@example.com",
        "username": "inactiveuser",
        "is_active": False,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=inactive_user_data)
    db_session.flush()

    response = client.get("/api/v1/users/search/user")

    assert response.status_code == 200
    data = response.json()
    # Should include activeuser and testuser, but not inactiveuser
    usernames = [user["username"] for user in data]
    assert "activeuser" in usernames
    assert "inactiveuser" not in usernames


def test_search_users_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that search doesn't return users from other accounts."""
    # Create another account and user
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_user_data = {
        "account_id": other_account.id,
        "email": "other@example.com",
        "username": "othersearch",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=other_user_data)

    # Create user in test account
    same_account_data = {
        "account_id": test_user.account_id,
        "email": "same@example.com",
        "username": "samesearch",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    crud_user.create(db_session, obj_in=same_account_data)
    db_session.flush()

    response = client.get("/api/v1/users/search/search")

    assert response.status_code == 200
    data = response.json()
    usernames = [user["username"] for user in data]
    assert "samesearch" in usernames
    assert "othersearch" not in usernames
