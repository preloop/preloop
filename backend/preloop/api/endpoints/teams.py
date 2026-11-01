"""Team management endpoints."""

import logging
from typing import Any, Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.schemas.team import (
    TeamCreate,
    TeamDetailResponse,
    TeamListResponse,
    TeamMemberAdd,
    TeamMemberResponse,
    TeamMemberUpdate,
    TeamResponse,
    TeamUpdate,
)
from preloop.models.crud.identity_directory import identity_directory
from preloop.models.crud import crud_team
from preloop.models.db.session import get_db_session
from preloop.models import models

from preloop.utils.permissions import require_permission
from preloop.services import multiuser


Team = models.Team
TeamMembership = models.TeamMembership
User = models.User

logger = logging.getLogger(__name__)

router = APIRouter()


def _team_member_responses(
    db: Session, team_id: UUID, members: list[User]
) -> list[TeamMemberResponse]:
    """Project canonical profiles once for the whole team member list."""
    profiles, _ = identity_directory.projections_for_memberships(
        db, membership_ids=[user.id for user in members]
    )
    memberships = crud_team.get_memberships_for_users(
        db, team_id=team_id, user_ids=[user.id for user in members]
    )
    responses: list[TeamMemberResponse] = []
    for user in members:
        membership = memberships.get(user.id)
        if membership is None:
            continue
        profile = profiles.get(user.id)
        if profile is None:
            profile = identity_directory.profile_for_membership(
                db, membership_id=user.id
            )
        responses.append(
            TeamMemberResponse(
                id=membership.id,
                team_id=membership.team_id,
                user_id=membership.user_id,
                role=membership.role,
                added_at=membership.added_at,
                added_by=membership.added_by,
                person_id=profile.person_id,
                identity_status=profile.identity_status,
                username=profile.username,
                email=profile.email,
                full_name=profile.full_name,
            )
        )
    return responses


def _reject_taken_team_name(
    db: Session, *, name: str, account_id: Any, team_id: UUID | None = None
) -> None:
    """Raise 409 when another team in the account already uses ``name``.

    Args:
        db: Database session.
        name: Requested team name.
        account_id: Account the team belongs to.
        team_id: Team being renamed, which may keep its own name.

    Raises:
        HTTPException: 409 if the name is taken by a different team.
    """
    existing = crud_team.get_by_name(db, name=name, account_id=account_id)
    if existing is not None and existing.id != team_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A team with this name already exists",
        )


@router.post("/teams", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
@require_permission("create_teams")
def create_team(
    team_data: TeamCreate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Create a new team in the current account.

    Args:
        team_data: Team creation data
        current_user: Current authenticated user
        db: Database session

    Returns:
        TeamResponse: Created team data
    """
    team_dict = team_data.model_dump()
    team_dict["account_id"] = current_user.account_id
    _reject_taken_team_name(
        db, name=team_dict["name"], account_id=current_user.account_id
    )

    team = crud_team.create(db=db, obj_in=team_dict)
    db.commit()
    db.refresh(team)

    logger.info(f"Team '{team.name}' created by user {current_user.username}")

    return TeamResponse.model_validate(team)


@router.get("/teams", response_model=TeamListResponse)
@require_permission("view_teams")
def list_teams(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List all teams in the current account.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        TeamListResponse: Paginated list of teams
    """
    teams = crud_team.get_by_account(
        db, account_id=current_user.account_id, skip=skip, limit=limit
    )
    total = crud_team.count_by_account(db, account_id=current_user.account_id)

    # Get roles for each team
    from preloop.models.crud import crud_team_role

    roles_by_team = (
        crud_team_role.get_roles_for_teams(
            db, account_id=current_user.account_id, team_ids=[team.id for team in teams]
        )
        if multiuser.access_control() is not None
        else {}
    )
    team_responses = []
    for team in teams:
        # Build team dict manually to avoid validation issues with roles relationship
        team_dict = {
            "id": team.id,
            "account_id": team.account_id,
            "name": team.name,
            "description": team.description,
            "created_at": team.created_at,
            "updated_at": team.updated_at,
        }

        # Get team's roles
        team_roles = roles_by_team.get(team.id, [])
        team_dict["roles"] = [
            {
                "id": str(role.id),
                "name": role.name,
                "description": role.description,
            }
            for role in team_roles
        ]

        team_responses.append(team_dict)

    return {
        "teams": team_responses,
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/teams/{team_id}", response_model=TeamDetailResponse)
@require_permission("view_teams")
def get_team(
    team_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get a specific team by ID with its members.

    Args:
        team_id: Team ID to retrieve
        current_user: Current authenticated user
        db: Database session

    Returns:
        TeamDetailResponse: Team data with members

    Raises:
        HTTPException: If team not found or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access team from different account",
        )

    members = crud_team.get_members(db=db, team_id=team_id)
    member_responses = _team_member_responses(db, team_id, members)

    return TeamDetailResponse(
        id=team.id,
        account_id=team.account_id,
        name=team.name,
        description=team.description,
        created_at=team.created_at,
        updated_at=team.updated_at,
        members=member_responses,
    )


@router.patch("/teams/{team_id}", response_model=TeamResponse)
@require_permission("edit_teams")
def update_team(
    team_id: UUID,
    team_data: TeamUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Update a team's information.

    Args:
        team_id: Team ID to update
        team_data: Team update data
        current_user: Current authenticated user
        db: Database session

    Returns:
        TeamResponse: Updated team data

    Raises:
        HTTPException: If team not found or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot update team from different account",
        )

    changes = team_data.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        _reject_taken_team_name(
            db,
            name=changes["name"],
            account_id=current_user.account_id,
            team_id=team.id,
        )
    updated_team = crud_team.update(db=db, db_obj=team, obj_in=changes)
    db.commit()
    db.refresh(updated_team)

    return TeamResponse.model_validate(updated_team)


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("delete_teams")
def delete_team(
    team_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Response:
    """Delete a team.

    Args:
        team_id: Team ID to delete
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If team not found or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete team from different account",
        )

    crud_team.delete(db=db, id=team_id)
    db.commit()

    logger.info(f"Team {team_id} deleted by user {current_user.username}")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/teams/{team_id}/members", status_code=status.HTTP_201_CREATED)
@require_permission("manage_teams")
def add_team_member(
    team_id: UUID,
    member_data: TeamMemberAdd,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Add a member to a team.

    Args:
        team_id: Team ID
        member_data: Member data (user_id and optional role)
        current_user: Current authenticated user
        db: Database session

    Returns:
        TeamMemberResponse: Added member data

    Raises:
        HTTPException: If team not found, user not found, or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify team from different account",
        )

    # Verify user exists and is in the same account
    from preloop.models.crud import crud_user

    user = crud_user.get(db=db, id=member_data.user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot add user from different account to team",
        )

    if crud_team.get_membership(db, team_id=team_id, user_id=member_data.user_id):
        raise HTTPException(409, "User is already a member of this team")
    try:
        membership = crud_team.add_member(
            db=db,
            team_id=team_id,
            user_id=member_data.user_id,
            role=member_data.role,
            added_by=current_user.id,
        )
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        if (
            getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            == "uq_team_user"
        ):
            raise HTTPException(409, "User is already a member of this team") from exc
        raise

    logger.info(
        f"User {member_data.user_id} added to team {team_id} by {current_user.username}"
    )

    profile = identity_directory.profile_for_membership(db, membership_id=user.id)
    return TeamMemberResponse(
        id=membership.id,
        team_id=membership.team_id,
        user_id=membership.user_id,
        role=membership.role,
        added_at=membership.added_at,
        added_by=membership.added_by,
        person_id=profile.person_id,
        identity_status=profile.identity_status,
        username=profile.username,
        email=profile.email,
        full_name=profile.full_name,
    )


@router.delete(
    "/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
@require_permission("manage_teams")
def remove_team_member(
    team_id: UUID,
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Response:
    """Remove a member from a team.

    Args:
        team_id: Team ID
        user_id: User ID to remove
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If team not found or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify team from different account",
        )

    success = crud_team.remove_member(db=db, team_id=team_id, user_id=user_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this team",
        )

    db.commit()

    logger.info(
        f"User {user_id} removed from team {team_id} by {current_user.username}"
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/teams/{team_id}/members", response_model=list[TeamMemberResponse])
@require_permission("view_teams")
def get_team_members(
    team_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get all members of a team.

    Args:
        team_id: Team ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        list[TeamMemberResponse]: List of team members

    Raises:
        HTTPException: If team not found or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access team from different account",
        )

    members = crud_team.get_members(db=db, team_id=team_id)
    return _team_member_responses(db, team_id, members)


@router.patch("/teams/{team_id}/members/{user_id}")
@require_permission("manage_teams")
def update_team_member(
    team_id: UUID,
    user_id: UUID,
    member_data: TeamMemberUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Update a team member's role.

    Args:
        team_id: Team ID
        user_id: User ID to update
        member_data: Member update data (role)
        current_user: Current authenticated user
        db: Database session

    Returns:
        TeamMemberResponse: Updated member data

    Raises:
        HTTPException: If team not found, member not found, or not in same account
    """
    team = crud_team.get(db=db, id=team_id)

    if not team:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Team not found",
        )

    # Verify team is in the same account
    if team.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify team from different account",
        )

    # Get the membership
    membership = crud_team.get_membership(db, team_id=team_id, user_id=user_id)

    if not membership:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User is not a member of this team",
        )

    # Update the role
    membership = crud_team.update_member(
        db, membership=membership, role=member_data.role
    )

    # Get user info
    from preloop.models.crud import crud_user

    user = crud_user.get(db=db, id=user_id)

    logger.info(
        f"User {user_id} role updated in team {team_id} by {current_user.username}"
    )

    profile = (
        identity_directory.profile_for_membership(db, membership_id=user.id)
        if user
        else None
    )
    return TeamMemberResponse(
        id=membership.id,
        team_id=membership.team_id,
        user_id=membership.user_id,
        role=membership.role,
        added_at=membership.added_at,
        added_by=membership.added_by,
        person_id=profile.person_id if profile else None,
        identity_status=profile.identity_status if profile else "unavailable",
        username=profile.username if profile else None,
        email=profile.email if profile else "",
        full_name=profile.full_name if profile else None,
    )
