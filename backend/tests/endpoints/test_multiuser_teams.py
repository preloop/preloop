"""Tests for team management endpoints."""

from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models.crud import crud_user, crud_account, crud_team
from preloop.models.models.user import User
from preloop.models.models.team import TeamMembership


def test_create_team_success(client: TestClient, test_user: User, db_session: Session):
    """Test creating a new team."""
    response = client.post(
        "/api/v1/teams",
        json={
            "name": "Engineering Team",
            "description": "Software engineering team",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Engineering Team"
    assert data["description"] == "Software engineering team"
    assert data["account_id"] == str(test_user.account_id)
    assert "id" in data


def test_create_team_minimal(client: TestClient, test_user: User, db_session: Session):
    """Test creating a team with minimal data."""
    response = client.post(
        "/api/v1/teams",
        json={"name": "Minimal Team"},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["name"] == "Minimal Team"
    assert data["description"] is None


def test_list_teams(client: TestClient, test_user: User, db_session: Session):
    """Test listing teams in account."""
    # Create multiple teams
    for i in range(3):
        team_data = {
            "account_id": test_user.account_id,
            "name": f"Team {i}",
            "description": f"Description {i}",
        }
        crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    response = client.get("/api/v1/teams")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 3
    assert len(data["teams"]) == 3
    assert data["skip"] == 0
    assert data["limit"] == 100


def test_list_teams_pagination(
    client: TestClient, test_user: User, db_session: Session
):
    """Test team list pagination."""
    # Create multiple teams
    for i in range(5):
        team_data = {
            "account_id": test_user.account_id,
            "name": f"Paginated Team {i}",
        }
        crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    response = client.get("/api/v1/teams?skip=1&limit=3")

    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert len(data["teams"]) == 3
    assert data["skip"] == 1
    assert data["limit"] == 3


def test_list_teams_multi_tenancy(
    client: TestClient, test_user: User, db_session: Session
):
    """Test that teams from other accounts are not listed."""
    # Create team in test account
    test_team_data = {
        "account_id": test_user.account_id,
        "name": "Test Account Team",
    }
    crud_team.create(db_session, obj_in=test_team_data)

    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Account Team",
    }
    crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.get("/api/v1/teams")

    assert response.status_code == 200
    data = response.json()
    # Should only see team from test account
    assert data["total"] == 1
    assert data["teams"][0]["name"] == "Test Account Team"


def test_get_team_success(client: TestClient, test_user: User, db_session: Session):
    """Test getting a specific team with its members."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Specific Team",
        "description": "A specific team",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    response = client.get(f"/api/v1/teams/{team.id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == str(team.id)
    assert data["name"] == "Specific Team"
    assert data["description"] == "A specific team"
    assert "members" in data
    assert isinstance(data["members"], list)


def test_get_team_with_members(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting a team with members included."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team With Members",
    }
    team = crud_team.create(db_session, obj_in=team_data)

    # Add members
    crud_team.add_member(
        db_session,
        team_id=team.id,
        user_id=test_user.id,
        role="admin",
        added_by=test_user.id,
    )
    db_session.commit()

    response = client.get(f"/api/v1/teams/{team.id}")

    assert response.status_code == 200
    data = response.json()
    assert len(data["members"]) == 1
    assert data["members"][0]["user_id"] == str(test_user.id)
    assert data["members"][0]["username"] == test_user.username
    assert data["members"][0]["role"] == "admin"


def test_get_team_not_found(client: TestClient, test_user: User, db_session: Session):
    """Test getting non-existent team."""
    fake_id = str(uuid4())
    response = client.get(f"/api/v1/teams/{fake_id}")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_get_team_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test getting team from different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.get(f"/api/v1/teams/{other_team.id}")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_update_team_success(client: TestClient, test_user: User, db_session: Session):
    """Test updating a team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Original Name",
        "description": "Original description",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={
            "name": "Updated Name",
            "description": "Updated description",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "Updated Name"
    assert data["description"] == "Updated description"


def test_update_team_partial(client: TestClient, test_user: User, db_session: Session):
    """Test partial update of a team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Original Name",
        "description": "Original description",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    response = client.patch(
        f"/api/v1/teams/{team.id}",
        json={"name": "New Name Only"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["name"] == "New Name Only"
    assert data["description"] == "Original description"


def test_update_team_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating non-existent team."""
    fake_id = str(uuid4())
    response = client.patch(
        f"/api/v1/teams/{fake_id}",
        json={"name": "New Name"},
    )

    assert response.status_code == 404


def test_update_team_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating team from different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.patch(
        f"/api/v1/teams/{other_team.id}",
        json={"name": "Hacked Name"},
    )

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_delete_team_success(client: TestClient, test_user: User, db_session: Session):
    """Test deleting a team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "To Delete",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    team_id = team.id
    db_session.commit()

    response = client.delete(f"/api/v1/teams/{team_id}")

    assert response.status_code == 204

    # Verify team is deleted - need to refresh session first
    db_session.expire_all()
    deleted_team = crud_team.get(db_session, id=team_id)
    assert deleted_team is None


def test_delete_team_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test deleting non-existent team."""
    fake_id = str(uuid4())
    response = client.delete(f"/api/v1/teams/{fake_id}")

    assert response.status_code == 404


def test_delete_team_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test deleting team from different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.delete(f"/api/v1/teams/{other_team.id}")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_add_team_member_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding a member to a team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Member Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)

    # Create another user to add
    user_data = {
        "account_id": test_user.account_id,
        "email": "member@example.com",
        "username": "memberuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    member_user = crud_user.create(db_session, obj_in=user_data)
    db_session.commit()

    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={
            "user_id": str(member_user.id),
            "role": "member",
        },
    )

    assert response.status_code == 201
    data = response.json()
    assert data["user_id"] == str(member_user.id)
    assert data["team_id"] == str(team.id)
    assert data["role"] == "member"
    assert data["username"] == "memberuser"


def test_add_team_member_no_role(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding a member without specifying role."""
    # Create team and user
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)

    user_data = {
        "account_id": test_user.account_id,
        "email": "norole@example.com",
        "username": "noroleuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    member_user = crud_user.create(db_session, obj_in=user_data)
    db_session.commit()

    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"user_id": str(member_user.id)},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["role"] is None


def test_add_team_member_team_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding member to non-existent team."""
    fake_id = str(uuid4())
    response = client.post(
        f"/api/v1/teams/{fake_id}/members",
        json={"user_id": str(test_user.id)},
    )

    assert response.status_code == 404
    assert "Team not found" in response.json()["detail"]


def test_add_team_member_user_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding non-existent user to team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    fake_user_id = str(uuid4())
    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"user_id": fake_user_id},
    )

    assert response.status_code == 404
    assert "User not found" in response.json()["detail"]


def test_add_team_member_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding user from different account to team fails."""
    # Create team in test account
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)

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
    db_session.commit()

    response = client.post(
        f"/api/v1/teams/{team.id}/members",
        json={"user_id": str(other_user.id)},
    )

    assert response.status_code == 400
    assert "different account" in response.json()["detail"]


def test_add_team_member_to_different_account_team(
    client: TestClient, test_user: User, db_session: Session
):
    """Test adding member to team from different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.post(
        f"/api/v1/teams/{other_team.id}/members",
        json={"user_id": str(test_user.id)},
    )

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_remove_team_member_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test removing a member from a team."""
    # Create team and user
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)

    user_data = {
        "account_id": test_user.account_id,
        "email": "remove@example.com",
        "username": "removeuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    member_user = crud_user.create(db_session, obj_in=user_data)

    # Add member
    crud_team.add_member(
        db_session,
        team_id=team.id,
        user_id=member_user.id,
        added_by=test_user.id,
    )
    db_session.commit()

    response = client.delete(f"/api/v1/teams/{team.id}/members/{member_user.id}")

    assert response.status_code == 204

    # Verify member is removed
    membership = (
        db_session.query(TeamMembership)
        .filter(
            TeamMembership.team_id == team.id,
            TeamMembership.user_id == member_user.id,
        )
        .first()
    )
    assert membership is None


def test_remove_team_member_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test removing non-existent member from team."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    fake_user_id = str(uuid4())
    response = client.delete(f"/api/v1/teams/{team.id}/members/{fake_user_id}")

    assert response.status_code == 404
    assert "not a member" in response.json()["detail"]


def test_remove_team_member_team_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test removing member from non-existent team."""
    fake_team_id = str(uuid4())
    response = client.delete(f"/api/v1/teams/{fake_team_id}/members/{test_user.id}")

    assert response.status_code == 404
    assert "Team not found" in response.json()["detail"]


def test_remove_team_member_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test removing member from team in different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.delete(f"/api/v1/teams/{other_team.id}/members/{test_user.id}")

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]


def test_update_team_member_success(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating a team member's role."""
    # Create team and user
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)

    user_data = {
        "account_id": test_user.account_id,
        "email": "update@example.com",
        "username": "updateuser",
        "is_active": True,
        "email_verified": True,
        "hashed_password": "testpassword",
        "user_source": "local",
    }
    member_user = crud_user.create(db_session, obj_in=user_data)

    # Add member with initial role
    crud_team.add_member(
        db_session,
        team_id=team.id,
        user_id=member_user.id,
        role="member",
        added_by=test_user.id,
    )
    db_session.commit()

    response = client.patch(
        f"/api/v1/teams/{team.id}/members/{member_user.id}",
        json={"role": "admin"},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["role"] == "admin"
    assert data["user_id"] == str(member_user.id)


def test_update_team_member_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating non-existent team member."""
    # Create team
    team_data = {
        "account_id": test_user.account_id,
        "name": "Team",
    }
    team = crud_team.create(db_session, obj_in=team_data)
    db_session.commit()

    fake_user_id = str(uuid4())
    response = client.patch(
        f"/api/v1/teams/{team.id}/members/{fake_user_id}",
        json={"role": "admin"},
    )

    assert response.status_code == 404
    assert "not a member" in response.json()["detail"]


def test_update_team_member_team_not_found(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating member in non-existent team."""
    fake_team_id = str(uuid4())
    response = client.patch(
        f"/api/v1/teams/{fake_team_id}/members/{test_user.id}",
        json={"role": "admin"},
    )

    assert response.status_code == 404
    assert "Team not found" in response.json()["detail"]


def test_update_team_member_different_account(
    client: TestClient, test_user: User, db_session: Session
):
    """Test updating member in team from different account fails."""
    # Create another account and team
    other_account_data = {
        "organization_name": "Other Organization",
        "is_active": True,
    }
    other_account = crud_account.create(db_session, obj_in=other_account_data)

    other_team_data = {
        "account_id": other_account.id,
        "name": "Other Team",
    }
    other_team = crud_team.create(db_session, obj_in=other_team_data)
    db_session.commit()

    response = client.patch(
        f"/api/v1/teams/{other_team.id}/members/{test_user.id}",
        json={"role": "admin"},
    )

    assert response.status_code == 403
    assert "different account" in response.json()["detail"]
