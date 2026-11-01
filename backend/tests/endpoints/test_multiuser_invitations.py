"""Tests for invitation endpoints."""

from datetime import datetime, timezone, timedelta
from unittest.mock import patch
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_user, crud_user_invitation, crud_account
from preloop.models.models.user import User


def test_get_invitation_public_info_invalid_token(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting public info with invalid token."""
    response = client.get("/api/v1/invitations/public/invalid-token")

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert "Invalid" in data["error_message"]


def test_get_invitation_public_info_expired(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting public info for expired invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "expired@example.com",
        "invited_by": test_user.id,
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get(f"/api/v1/invitations/public/{invitation.token}")

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert "expired" in data["error_message"]


def test_get_invitation_public_info_accepted(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting public info for already accepted invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "accepted@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    crud_user_invitation.accept(
        db_session, invitation_id=invitation.id, user_id=test_user.id
    )
    db_session.commit()

    response = client.get(f"/api/v1/invitations/public/{invitation.token}")

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert "already been accepted" in data["error_message"]


def test_get_invitation_public_info_cancelled(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting public info for cancelled invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "cancelled@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    crud_user_invitation.cancel(db_session, invitation_id=invitation.id)
    db_session.commit()

    response = client.get(f"/api/v1/invitations/public/{invitation.token}")

    assert response.status_code == 200
    data = response.json()
    assert data["is_valid"] is False
    assert "cancelled" in data["error_message"]


def test_get_invitation_public_info_valid(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting public info for valid invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "valid@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get(f"/api/v1/invitations/public/{invitation.token}")

    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "valid@example.com"
    assert data["is_valid"] is True
    assert data["organization_name"] == "Test Organization"
    assert data["error_message"] is None


def test_accept_invitation_invalid_token(
    client: TestClient, test_user: User, db_session: Session
):
    """Test accepting invitation with invalid token."""
    response = client.post(
        "/api/v1/invitations/accept",
        json={
            "token": "invalid-token",
            "username": "newuser",
            "password": "password123",
            "full_name": "New User",
        },
    )

    assert response.status_code == 404
    assert "Invalid" in response.json()["detail"]


def test_list_invitations_empty(
    client: TestClient, test_user: User, db_session: Session
):
    """Test listing invitations when none exist."""
    response = client.get("/api/v1/invitations")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 0
    assert len(data["invitations"]) == 0
    assert data["skip"] == 0
    assert data["limit"] == 100


def test_list_invitations_with_data(
    client: TestClient, test_user: User, db_session: Session
):
    """Test listing invitations when some exist."""
    # Create invitations directly in database
    for i in range(2):
        invitation_dict = {
            "account_id": test_user.account_id,
            "email": f"user{i}@example.com",
            "invited_by": test_user.id,
        }
        crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get("/api/v1/invitations")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert len(data["invitations"]) == 2


def test_list_invitations_pagination(
    client: TestClient, test_user: User, db_session: Session
):
    """Test invitation list pagination."""
    # Create invitations
    for i in range(5):
        invitation_dict = {
            "account_id": test_user.account_id,
            "email": f"page{i}@example.com",
            "invited_by": test_user.id,
        }
        crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get("/api/v1/invitations?skip=1&limit=3")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert len(data["invitations"]) == 3
    assert data["skip"] == 1
    assert data["limit"] == 3


def test_get_invitation_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting a specific invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "specific@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get(f"/api/v1/invitations/{invitation.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(invitation.id)
    assert data["email"] == "specific@example.com"


def test_get_invitation_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting non-existent invitation."""
    fake_id = str(uuid4())
    response = client.get(f"/api/v1/invitations/{fake_id}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_invitation_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test accessing invitation from different account fails."""
    from preloop.models.crud import crud_account

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

    invitation_dict = {
        "account_id": other_account.id,
        "email": "invited@example.com",
        "invited_by": other_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.get(f"/api/v1/invitations/{invitation.id}")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_cancel_invitation_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test cancelling a pending invitation."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "cancel@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.delete(f"/api/v1/invitations/{invitation.id}")

    assert response.status_code == 204

    # Verify invitation is cancelled
    get_response = client.get(f"/api/v1/invitations/{invitation.id}")
    assert get_response.json()["status"] == "cancelled"


def test_cancel_invitation_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test cancelling non-existent invitation."""
    fake_id = str(uuid4())
    response = client.delete(f"/api/v1/invitations/{fake_id}")

    assert response.status_code == 404


def test_cancel_accepted_invitation(
    client: TestClient, test_user: User, db_session: Session
):
    """Test cancelling an already accepted invitation fails."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "accepted@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    crud_user_invitation.accept(
        db_session, invitation_id=invitation.id, user_id=test_user.id
    )
    db_session.commit()

    response = client.delete(f"/api/v1/invitations/{invitation.id}")

    assert response.status_code == 400
    assert "accepted" in response.json()["detail"]


def test_resend_invitation_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test resending non-existent invitation."""
    fake_id = str(uuid4())
    response = client.post(f"/api/v1/invitations/{fake_id}/resend")

    assert response.status_code == 404


def test_resend_invitation_not_pending(
    client: TestClient, test_user: User, db_session: Session
):
    """Test resending non-pending invitation fails."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "cancelled@example.com",
        "invited_by": test_user.id,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    crud_user_invitation.cancel(db_session, invitation_id=invitation.id)
    db_session.commit()

    response = client.post(f"/api/v1/invitations/{invitation.id}/resend")

    assert response.status_code == 400
    assert "status" in response.json()["detail"]


def test_resend_invitation_expired(
    client: TestClient, test_user: User, db_session: Session
):
    """Test resending expired invitation fails."""
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "expired@example.com",
        "invited_by": test_user.id,
        "expires_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    response = client.post(f"/api/v1/invitations/{invitation.id}/resend")

    assert response.status_code == 400
    assert "expired" in response.json()["detail"]


# ============================================================================
# Security Tests for Cross-Account Team/Role Assignment Prevention
# ============================================================================


@patch("preloop.api.endpoints.invitations.send_invitation_email")
def test_create_invitation_with_cross_account_team_fails(
    mock_send_email, client: TestClient, test_user: User, db_session: Session
):
    """Test that creating invitation with team from different account fails.

    Security: CVE-2025-XXXX - Prevent cross-account team assignment vulnerability
    """
    from preloop.models.crud import crud_account, crud_team

    # Create another account
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org"},
    )
    db_session.commit()

    # Create a team in the other account
    other_team = crud_team.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "name": "Other Team",
            "description": "Team from different account",
        },
    )
    db_session.commit()

    # Try to create invitation with the other account's team
    invitation_data = {
        "email": "victim@example.com",
        "team_ids": [str(other_team.id)],
    }

    response = client.post("/api/v1/invitations", json=invitation_data)

    # Should fail with 403 Forbidden
    assert response.status_code == 403
    assert "does not belong to your account" in response.json()["detail"]

    # Email should not be sent for failed invitation
    mock_send_email.assert_not_called()


@patch("preloop.api.endpoints.invitations.send_invitation_email")
def test_create_invitation_with_cross_account_role_fails(
    mock_send_email, client: TestClient, test_user: User, db_session: Session
):
    """Test that creating invitation with role from different account fails.

    Security: CVE-2025-XXXX - Prevent cross-account role assignment vulnerability
    """
    from preloop.models.crud import crud_account, crud_role

    # Create another account
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org"},
    )
    db_session.commit()

    # Create a custom role in the other account
    other_role = crud_role.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "name": "Other Custom Role",
            "description": "Custom role from different account",
            "is_system_role": False,
        },
    )
    db_session.commit()

    # Try to create invitation with the other account's role
    invitation_data = {
        "email": "victim@example.com",
        "role_ids": [str(other_role.id)],
    }

    response = client.post("/api/v1/invitations", json=invitation_data)

    # Core grants no roles, so any role request is refused before a lookup.
    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "upgrade_required"

    # Email should not be sent for failed invitation
    mock_send_email.assert_not_called()


def test_accept_invitation_with_tampered_team_ids(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that accepting invitation with tampered team IDs doesn't add user to wrong teams.

    Security: CVE-2025-XXXX - Prevent exploitation via database manipulation
    This tests the defense-in-depth validation during acceptance.
    """
    from preloop.models.crud import crud_account, crud_team, crud_user_invitation

    # Create another account
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org"},
    )
    db_session.commit()

    # Create a team in the other account
    other_team = crud_team.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "name": "Other Team",
            "description": "Team from different account",
        },
    )
    db_session.commit()

    # Create invitation with valid team from test_user's account
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "victim@example.com",
        "invited_by": test_user.id,
        "team_ids": None,  # Initially no teams
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    # Simulate attacker tampering with database to inject other_team.id
    # (This simulates a scenario where validation was bypassed in create)
    invitation.team_ids = str(other_team.id)
    db_session.commit()

    # Accept the invitation
    accept_data = {
        "token": invitation.token,
        "username": "victim_user",
        "password": "SecureP@ssw0rd123",
        "full_name": "Victim User",
    }

    response = client.post("/api/v1/invitations/accept", json=accept_data)

    # Acceptance should succeed (user created) - returns 201 Created
    assert response.status_code == 201

    # But user should NOT be added to other_team
    from preloop.models.crud import crud_user

    created_user = crud_user.get_by_username(db_session, username="victim_user")
    assert created_user is not None

    # Check that user is NOT a member of other_team
    from preloop.models.crud import crud_team

    user_teams = crud_team.get_user_teams(db_session, user_id=created_user.id)
    team_ids = [team.id for team in user_teams]
    assert other_team.id not in team_ids


def test_accept_invitation_with_tampered_role_ids(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that accepting invitation with tampered role IDs doesn't assign wrong roles.

    Security: CVE-2025-XXXX - Prevent exploitation via database manipulation
    """
    from preloop.models.crud import crud_account, crud_role, crud_user_invitation

    # Create another account
    other_account = crud_account.create(
        db_session,
        obj_in={"organization_name": "Other Org"},
    )
    db_session.commit()

    # Create a custom role in the other account (e.g., admin role)
    other_role = crud_role.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "name": "Other Admin",
            "description": "Admin role from different account",
            "is_system_role": False,
        },
    )
    db_session.commit()

    # Create invitation with no roles
    invitation_dict = {
        "account_id": test_user.account_id,
        "email": "victim2@example.com",
        "invited_by": test_user.id,
        "role_ids": None,
    }
    invitation = crud_user_invitation.create(db_session, obj_in=invitation_dict)
    db_session.commit()

    # Simulate attacker tampering with database
    invitation.role_ids = str(other_role.id)
    db_session.commit()

    # Accept the invitation
    accept_data = {
        "token": invitation.token,
        "username": "victim_user2",
        "password": "SecureP@ssw0rd123",
        "full_name": "Victim User 2",
    }

    response = client.post("/api/v1/invitations/accept", json=accept_data)

    # Acceptance should succeed (user created) - returns 201 Created
    assert response.status_code == 201

    # But user should NOT have other_role assigned
    from preloop.models.crud import crud_user, crud_user_role

    created_user = crud_user.get_by_username(db_session, username="victim_user2")
    assert created_user is not None

    # Check that user doesn't have the other_role
    user_roles = crud_user_role.get_by_user(db_session, user_id=created_user.id)
    role_ids = [ur.role_id for ur in user_roles]
    assert other_role.id not in role_ids


@patch("preloop.api.endpoints.invitations.send_invitation_email")
def test_create_invitation_with_own_account_team_succeeds(
    mock_send_email, client: TestClient, test_user: User, db_session: Session
):
    """Test that creating invitation with team from own account succeeds.

    This verifies that the fix doesn't break legitimate use cases.
    """
    from preloop.models.crud import crud_team

    # Create a team in test_user's account
    own_team = crud_team.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "My Team",
            "description": "Team from my account",
        },
    )
    db_session.commit()

    # Create invitation with own team
    invitation_data = {
        "email": "legitimate@example.com",
        "team_ids": [str(own_team.id)],
    }

    response = client.post("/api/v1/invitations", json=invitation_data)

    # Should succeed - returns 201 Created
    assert response.status_code == 201
    data = response.json()
    assert data["email"] == "legitimate@example.com"

    # Email should be sent for successful invitation
    mock_send_email.assert_called_once()


@patch("preloop.api.endpoints.invitations.send_invitation_email")
def test_create_invitation_with_system_role_requires_upgrade(
    mock_send_email, client: TestClient, test_user: User, db_session: Session
):
    """Even seeded system roles are an access control overlay feature."""
    from preloop.models.crud import crud_role

    system_role = crud_role.get_by_name(db_session, name="admin")
    assert system_role is not None

    response = client.post(
        "/api/v1/invitations",
        json={
            "email": "system_role_user@example.com",
            "role_ids": [str(system_role.id)],
        },
    )

    assert response.status_code == 402
    assert response.json()["detail"]["code"] == "upgrade_required"
    mock_send_email.assert_not_called()


def _rbac_enabled_client(db_session: Session, user: User) -> TestClient:
    """Build a TestClient with RBAC enforcement enabled for ``user``.

    Clears both the ``DISABLE_RBAC`` env var and ``settings.disable_rbac``,
    then resets the plugin manager so proprietary routes (invitations, etc.)
    are registered on the fresh app.
    """
    import os

    from preloop.api.app import create_app
    from preloop.api.auth import get_current_active_user
    from preloop.config import settings
    from preloop.models.db.session import get_db_session as get_db
    from preloop.plugins.base import reset_plugin_manager

    old_disable_rbac = os.environ.get("DISABLE_RBAC")
    old_settings_flag = settings.disable_rbac
    if "DISABLE_RBAC" in os.environ:
        del os.environ["DISABLE_RBAC"]
    settings.disable_rbac = False
    reset_plugin_manager()

    def override_get_current_active_user():
        return user

    def override_get_db():
        yield db_session

    app = create_app()
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_active_user] = override_get_current_active_user
    client = TestClient(app)
    client._old_disable_rbac = old_disable_rbac  # type: ignore[attr-defined]
    client._old_settings_disable_rbac = old_settings_flag  # type: ignore[attr-defined]
    return client


def _restore_rbac_env(client: TestClient) -> None:
    import os

    from preloop.config import settings
    from preloop.plugins.base import reset_plugin_manager

    old_disable_rbac = getattr(client, "_old_disable_rbac", None)
    if old_disable_rbac is not None:
        os.environ["DISABLE_RBAC"] = old_disable_rbac
    elif "DISABLE_RBAC" in os.environ:
        del os.environ["DISABLE_RBAC"]

    old_settings_flag = getattr(client, "_old_settings_disable_rbac", True)
    settings.disable_rbac = old_settings_flag
    reset_plugin_manager()


@patch("preloop.api.endpoints.invitations.send_invitation_email")
def test_viewer_can_create_invitations_in_core(
    mock_send_email, db_session: Session, test_viewer_user: User
):
    """Core applies no access control between members of one account."""
    client = _rbac_enabled_client(db_session, test_viewer_user)
    try:
        response = client.post(
            "/api/v1/invitations",
            json={"email": "allowed@example.com"},
        )
        assert response.status_code == 201, response.text
    finally:
        _restore_rbac_env(client)


def test_roleless_member_can_list_invitations_in_core(db_session: Session):
    """A user with no roles still lists the invitations of their account."""
    account = crud_account.create(
        db_session,
        obj_in={"organization_name": "No Roles Org", "is_active": True},
    )
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "noroles@example.com",
            "username": "noroles",
            "full_name": "No Roles",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "testpassword",
            "user_source": "local",
        },
    )
    db_session.flush()

    client = _rbac_enabled_client(db_session, user)
    try:
        response = client.get("/api/v1/invitations")
        assert response.status_code == 200, response.text
    finally:
        _restore_rbac_env(client)


def test_viewer_can_cancel_invitation_in_core(
    db_session: Session, test_user: User, test_viewer_user: User
):
    """Any member of the account can cancel its invitations."""
    invitation = crud_user_invitation.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "email": "cancel-allowed@example.com",
            "invited_by": test_user.id,
        },
    )
    db_session.flush()

    test_viewer_user.account_id = test_user.account_id
    db_session.flush()

    client = _rbac_enabled_client(db_session, test_viewer_user)
    try:
        response = client.delete(f"/api/v1/invitations/{invitation.id}")
        assert response.status_code == 204, response.text
    finally:
        _restore_rbac_env(client)


def test_create_invitation_rejects_more_ids_than_the_column_holds(
    client: TestClient, test_user: User, db_session: Session
):
    """Too many team or role ids is a 422 before any database write."""
    too_many = [str(uuid4()) for _ in range(14)]
    for field in ("team_ids", "role_ids"):
        response = client.post(
            "/api/v1/invitations",
            json={"email": "new.person@example.com", field: too_many},
        )
        assert response.status_code == 422, field
