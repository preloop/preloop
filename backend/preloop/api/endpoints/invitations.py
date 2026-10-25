"""User invitation endpoints."""

import logging
from datetime import datetime, timezone
from typing import Any, Annotated, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user, get_password_hash
from preloop.schemas.invitation import (
    InvitationAccept,
    InvitationCreate,
    InvitationListResponse,
    InvitationPublicInfo,
    InvitationResponse,
)
from preloop.utils.email import send_invitation_email
from preloop.models.crud import (
    crud_account,
    crud_person,
    crud_user,
    crud_user_invitation,
)

from preloop.services.multiuser import email_exists
from preloop.models.db.session import get_db_session
from preloop.models import models

from preloop.utils.permissions import require_permission

from preloop.services.multiuser import membership_module
from preloop.services.multiuser import enforce_seat_available
from preloop.services.multiuser import (
    apply_invitation_roles,
    validate_invitation_roles,
)

User = models.User
UserInvitationStatus = models.UserInvitationStatus

logger = logging.getLogger(__name__)

router = APIRouter()
public_router = APIRouter()  # Public endpoints (no auth required)


@router.post(
    "/invitations",
    response_model=InvitationResponse,
    status_code=status.HTTP_201_CREATED,
)
@require_permission("invite_users")
def create_invitation(
    invitation_data: InvitationCreate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Create a new user invitation.

    Args:
        invitation_data: Invitation data with email, optional role IDs, and optional team IDs
        current_user: Current authenticated user
        db: Database session

    Returns:
        InvitationResponse: Created invitation data

    Raises:
        HTTPException: If user already exists, invitation already pending, or
            the account has no seat left on its plan (402).
    """
    # Seat cap: a pending invitation is a claim on a seat, so this is checked
    # before the row is written (and again on accept, since the cap can move
    # in between). No-ops without the billing plugin.
    enforce_seat_available(
        db, current_user.account_id, invitation_email=invitation_data.email
    )

    # Check if user with this email already exists in the account. Scoped
    # so a shared address in another account can still be invited here.
    if email_exists(
        db=db, email=invitation_data.email, account_id=current_user.account_id
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this email already exists in the account",
        )

    # Check if there's already a pending invitation
    pending_invitation = crud_user_invitation.get_by_email(
        db=db, email=invitation_data.email, account_id=current_user.account_id
    )
    if pending_invitation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A pending invitation already exists for this email",
        )

    # Roles are an access control overlay; core rejects them with 402.
    validated_role_ids = [
        str(role_id)
        for role_id in validate_invitation_roles(
            db, current_user, invitation_data.role_ids
        )
    ]

    # Validate that all team_ids belong to the current account
    # Security: Prevent cross-account team assignment
    validated_team_ids = []
    if invitation_data.team_ids:
        from preloop.models.crud import crud_team

        for team_id in invitation_data.team_ids:
            team = crud_team.get(db=db, id=team_id, account_id=current_user.account_id)
            if not team:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Team {team_id} does not exist or does not belong to your account",
                )
            validated_team_ids.append(str(team_id))

    # Convert role_ids list to comma-separated string
    role_ids_str = ",".join(validated_role_ids) if validated_role_ids else None

    # Convert team_ids list to comma-separated string
    team_ids_str = ",".join(validated_team_ids) if validated_team_ids else None

    # Create invitation
    invitation_dict = {
        "account_id": current_user.account_id,
        "email": invitation_data.email,
        "invited_by": current_user.id,
        "role_ids": role_ids_str,
        "team_ids": team_ids_str,
    }

    invitation = crud_user_invitation.create(db=db, obj_in=invitation_dict)
    db.commit()
    db.refresh(invitation)

    logger.info(
        f"Invitation created for {invitation_data.email} by {current_user.username}"
    )

    # Send invitation email
    account = crud_account.get(db, id=current_user.account_id)
    if account:
        # Get role names (already validated above, but double-check for email)
        role_names = []
        if invitation.role_ids:
            from preloop.models.crud import crud_role

            role_ids = [
                UUID(rid.strip())
                for rid in invitation.role_ids.split(",")
                if rid.strip()
            ]
            for role_id in role_ids:
                # Try account-specific role first, then system role
                role = crud_role.get(
                    db=db, id=role_id, account_id=current_user.account_id
                )
                if not role:
                    # Check for system role
                    role = crud_role.get(db=db, id=role_id)
                    if role and role.account_id is not None:
                        # Skip roles from other accounts (shouldn't happen due to validation above)
                        logger.warning(
                            f"Skipping role {role_id} in invitation email - belongs to different account"
                        )
                        continue
                if role:
                    role_names.append(role.name)

        # Get team names (already validated above, but double-check for email)
        team_names = []
        if invitation.team_ids:
            from preloop.models.crud import crud_team

            team_ids = [
                UUID(tid.strip())
                for tid in invitation.team_ids.split(",")
                if tid.strip()
            ]
            for team_id in team_ids:
                # Validate team belongs to the same account
                team = crud_team.get(
                    db=db, id=team_id, account_id=current_user.account_id
                )
                if not team:
                    # Shouldn't happen due to validation above
                    logger.warning(
                        f"Skipping team {team_id} in invitation email - not found or belongs to different account"
                    )
                    continue
                team_names.append(team.name)

        send_invitation_email(
            user_email=invitation.email,
            token=invitation.token,
            organization_name=account.organization_name or "your organization",
            invited_by=current_user.email or current_user.username,
            role_names=role_names if role_names else None,
            team_names=team_names if team_names else None,
        )
    else:
        logger.warning(
            f"Could not send invitation email: account {current_user.account_id} not found"
        )

    return InvitationResponse.model_validate(invitation)


@router.get("/invitations", response_model=InvitationListResponse)
@require_permission("view_users")
def list_invitations(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
    skip: int = 0,
    limit: int = 100,
    status_filter: Optional[str] = None,
) -> Any:
    """List all invitations in the current account.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip
        limit: Maximum number of records to return
        status_filter: Optional status filter (pending, accepted, expired, cancelled)

    Returns:
        InvitationListResponse: Paginated list of invitations
    """
    invitations = crud_user_invitation.get_by_account(
        db,
        account_id=current_user.account_id,
        status=status_filter,
        skip=skip,
        limit=limit,
    )
    total = crud_user_invitation.count_by_account(
        db, account_id=current_user.account_id, status=status_filter
    )

    return InvitationListResponse(
        invitations=[InvitationResponse.model_validate(inv) for inv in invitations],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get("/invitations/{invitation_id}", response_model=InvitationResponse)
@require_permission("view_users")
def get_invitation(
    invitation_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get a specific invitation by ID.

    Args:
        invitation_id: Invitation ID to retrieve
        current_user: Current authenticated user
        db: Database session

    Returns:
        InvitationResponse: Invitation data

    Raises:
        HTTPException: If invitation not found or not in same account
    """
    invitation = crud_user_invitation.get(db=db, id=invitation_id)

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )

    # Verify invitation is in the same account
    if invitation.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access invitation from different account",
        )

    return InvitationResponse.model_validate(invitation)


@router.delete("/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("invite_users")
def cancel_invitation(
    invitation_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> None:
    """Cancel a pending invitation.

    Args:
        invitation_id: Invitation ID to cancel
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If invitation not found, not in same account, or already accepted
    """
    invitation = crud_user_invitation.get(db=db, id=invitation_id)

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )

    # Verify invitation is in the same account
    if invitation.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot cancel invitation from different account",
        )

    # Check if invitation is already accepted
    if invitation.status == UserInvitationStatus.ACCEPTED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot cancel an accepted invitation",
        )

    # Cancel the invitation
    crud_user_invitation.cancel(db=db, invitation_id=invitation_id)
    db.commit()

    logger.info(f"Invitation {invitation_id} cancelled by {current_user.username}")


@router.post(
    "/invitations/{invitation_id}/resend", status_code=status.HTTP_204_NO_CONTENT
)
@require_permission("invite_users")
def resend_invitation(
    invitation_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> None:
    """Resend an invitation email.

    Args:
        invitation_id: Invitation ID to resend
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If invitation not found, not in same account, or not pending
    """
    invitation = crud_user_invitation.get(db=db, id=invitation_id)

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invitation not found",
        )

    # Verify invitation is in the same account
    if invitation.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot resend invitation from different account",
        )

    # Check if invitation is pending
    if invitation.status != UserInvitationStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot resend invitation with status: {invitation.status}",
        )

    # Check if invitation is expired
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot resend expired invitation. Please create a new one.",
        )

    logger.info(f"Invitation {invitation_id} resent by {current_user.username}")

    # Send invitation email
    account = crud_account.get(db, id=current_user.account_id)
    if account:
        # Get role names (with account validation for defense in depth)
        role_names = []
        if invitation.role_ids:
            from preloop.models.crud import crud_role

            role_ids = [
                UUID(rid.strip())
                for rid in invitation.role_ids.split(",")
                if rid.strip()
            ]
            for role_id in role_ids:
                # Try account-specific role first, then system role
                role = crud_role.get(
                    db=db, id=role_id, account_id=current_user.account_id
                )
                if not role:
                    # Check for system role
                    role = crud_role.get(db=db, id=role_id)
                    if role and role.account_id is not None:
                        # Skip roles from other accounts
                        logger.warning(
                            f"Skipping role {role_id} in invitation {invitation_id} - belongs to different account"
                        )
                        continue
                if role:
                    role_names.append(role.name)

        # Get team names (with account validation for defense in depth)
        team_names = []
        if invitation.team_ids:
            from preloop.models.crud import crud_team

            team_ids = [
                UUID(tid.strip())
                for tid in invitation.team_ids.split(",")
                if tid.strip()
            ]
            for team_id in team_ids:
                # Validate team belongs to the same account
                team = crud_team.get(
                    db=db, id=team_id, account_id=current_user.account_id
                )
                if not team:
                    logger.warning(
                        f"Skipping team {team_id} in invitation {invitation_id} - not found or belongs to different account"
                    )
                    continue
                team_names.append(team.name)

        send_invitation_email(
            user_email=invitation.email,
            token=invitation.token,
            organization_name=account.organization_name or "your organization",
            invited_by=current_user.email or current_user.username,
            role_names=role_names if role_names else None,
            team_names=team_names if team_names else None,
        )
    else:
        logger.warning(
            f"Could not send invitation email: account {current_user.account_id} not found"
        )


# Public endpoint (no authentication required)
@public_router.get("/invitations/public/{token}", response_model=InvitationPublicInfo)
def get_invitation_info(
    token: str,
    db: Session = Depends(get_db_session),
) -> Any:
    """Get public information about an invitation (for the accept page).

    This endpoint is public and doesn't require authentication.

    Args:
        token: Invitation token
        db: Database session

    Returns:
        InvitationPublicInfo: Public invitation information
    """
    invitation = crud_user_invitation.get_by_token(db=db, token=token)

    if not invitation:
        return InvitationPublicInfo(
            email=None,
            organization_name=None,
            expires_at=datetime.now(timezone.utc),
            is_valid=False,
            error_message="Invalid invitation token",
        )

    # Check if invitation is expired
    if invitation.expires_at < datetime.now(timezone.utc):
        return InvitationPublicInfo(
            email=invitation.email,
            organization_name=None,
            expires_at=invitation.expires_at,
            is_valid=False,
            error_message="This invitation has expired",
        )

    # Check if invitation is already accepted
    if invitation.status == UserInvitationStatus.ACCEPTED:
        return InvitationPublicInfo(
            email=invitation.email,
            organization_name=None,
            expires_at=invitation.expires_at,
            is_valid=False,
            error_message="This invitation has already been accepted",
        )

    # Check if invitation is cancelled
    if invitation.status == UserInvitationStatus.CANCELLED:
        return InvitationPublicInfo(
            email=invitation.email,
            organization_name=None,
            expires_at=invitation.expires_at,
            is_valid=False,
            error_message="This invitation has been cancelled",
        )

    # Get account info for organization name
    account = crud_account.get(db=db, id=invitation.account_id)

    # Get role names
    role_names = []
    if invitation.role_ids:
        from preloop.models.crud import crud_role
        from uuid import UUID

        role_ids = [
            UUID(rid.strip()) for rid in invitation.role_ids.split(",") if rid.strip()
        ]
        for role_id in role_ids:
            role = crud_role.get(db=db, id=role_id, account_id=invitation.account_id)
            if not role:
                role = crud_role.get(db=db, id=role_id)
                if role and role.account_id is not None:
                    role = None
            if role:
                role_names.append(role.name)

    # Get team names
    team_names = []
    if invitation.team_ids:
        from preloop.models.crud import crud_team
        from uuid import UUID

        team_ids = [
            UUID(tid.strip()) for tid in invitation.team_ids.split(",") if tid.strip()
        ]
        for team_id in team_ids:
            team = crud_team.get(db=db, id=team_id, account_id=invitation.account_id)
            if team:
                team_names.append(team.name)

    return InvitationPublicInfo(
        email=invitation.email,
        organization_name=account.organization_name if account else None,
        expires_at=invitation.expires_at,
        is_valid=True,
        role_names=role_names,
        team_names=team_names,
    )


# Public endpoint (no authentication required)
@public_router.post("/invitations/accept", status_code=status.HTTP_201_CREATED)
def accept_invitation(
    accept_data: InvitationAccept,
    db: Session = Depends(get_db_session),
) -> Any:
    """Accept an invitation and create a new user account.

    This endpoint is public and doesn't require authentication.

    Args:
        accept_data: Invitation acceptance data (token, username, password, full_name)
        db: Database session

    Returns:
        dict: Success message with user ID

    Raises:
        HTTPException: If invitation invalid, expired, or username already exists
    """
    invitation = crud_user_invitation.get_by_token(db=db, token=accept_data.token)

    if not invitation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid invitation token",
        )

    # Validate invitation
    # Check if invitation is expired
    if invitation.expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This invitation has expired",
        )

    # Check if invitation is not pending
    if invitation.status != UserInvitationStatus.PENDING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This invitation has status: {invitation.status}",
        )

    # Check if username is already taken
    existing_user = crud_user.get_by_username(db=db, username=accept_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    # Seat cap, re-checked at accept time: the cap can be hit between send and
    # accept (plan downgrade, other invites accepted first). This invitation
    # is itself pending and therefore already counted, so the seat it holds is
    # not double-charged against the cap.
    enforce_seat_available(
        db, invitation.account_id, additional_seats=0, include_details=False
    )

    # An address that already belongs to a verified person gets a membership
    # row of that person, without credentials of its own: the person signs in
    # with the credentials they have and switches accounts. The token proves
    # the inbox, as a password reset link would.
    membership = membership_module()
    person = (
        membership.link_invited_person(db, invitation.email, invitation.account_id)
        if membership is not None
        else None
    )

    if person is not None and (
        person.identity_status == "quarantined"
        or not person.username
        or not person.email
    ):
        raise HTTPException(
            status_code=409,
            detail="Identity is unavailable; contact a platform administrator",
        )

    # Create the new user
    user_dict = {
        "account_id": invitation.account_id,
        "username": accept_data.username,
        "email": invitation.email,
        "email_verified": True,  # Email is verified through invitation
        "full_name": accept_data.full_name,
        "hashed_password": (
            None if person is not None else get_password_hash(accept_data.password)
        ),
        "is_active": True,
        "user_source": "local",
        # Accepting an invitation is joining an account somebody else already
        # chose a plan for. Stamping it here keeps the first-login plan choice
        # off a colleague's first console visit, and stops their console
        # asking the billing plugin about it on every page load thereafter.
        "plan_choice_made_at": datetime.now(timezone.utc),
    }

    if person is not None:
        user_dict["person_id"] = person.id

    from preloop.models.crud.billing import billing as crud_billing

    try:
        user = crud_billing.accept_invitation(db, invitation.id, user_dict)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Stored roles are granted by the access control overlay, if any.
    apply_invitation_roles(db, user, invitation)

    # Assign teams if specified (with strict account validation)
    # Security: Prevent cross-account team assignment during invitation acceptance
    if invitation.team_ids:
        from preloop.models.crud import crud_team

        team_ids = [
            UUID(tid.strip()) for tid in invitation.team_ids.split(",") if tid.strip()
        ]
        for team_id in team_ids:
            # Validate team belongs to the invitation's account
            team = crud_team.get(db=db, id=team_id, account_id=invitation.account_id)
            if not team:
                # Team doesn't exist or belongs to a different account - skip it
                logger.error(
                    f"Security: Skipping team {team_id} during invitation acceptance - "
                    f"not found or belongs to different account. Invitation ID: {invitation.id}"
                )
                continue

            crud_team.add_member(
                db=db,
                team_id=team_id,
                user_id=user.id,
                role="member",
                added_by=invitation.invited_by,
            )

    db.commit()
    db.refresh(user)

    profile = crud_person.profile_for_membership(db, membership_id=user.id)
    logger.info(
        f"User {profile.username} created by accepting invitation {invitation.id}"
    )

    # Update Stripe subscription quantity to reflect new user count
    try:
        from preloop.services.multiuser import sync_subscription_quantity

        sync_subscription_quantity(db, str(invitation.account_id))
    except Exception as e:
        logger.warning(f"Failed to update Stripe subscription quantity: {e}")
        # Don't fail user creation if billing update fails

    # Notify admins about new user joining organization
    try:
        from preloop.services.account_setup_service import (
            notify_admins_user_joined_organization,
        )

        # Get account for organization name
        account = crud_account.get(db=db, id=invitation.account_id)
        org_name = account.organization_name if account else None

        # Get inviter info
        inviter = crud_user.get(db=db, id=invitation.invited_by)
        invited_by_str = inviter.email if inviter else str(invitation.invited_by)

        notify_admins_user_joined_organization(
            username=profile.username,
            email=profile.email,
            organization_name=org_name,
            account_id=invitation.account_id,
            invited_by=invited_by_str,
        )
    except Exception as e:
        logger.warning(f"Failed to notify admins about new user: {e}")
        # Don't fail user creation if notification fails

    if person is not None:
        return {
            "message": (
                "You joined the account. Sign in with your existing Preloop "
                "credentials and switch to it."
            ),
            "user_id": str(user.id),
            "username": profile.username,
            "person_id": str(profile.person_id),
            "identity_status": profile.identity_status,
            "linked": True,
        }
    return {
        "message": "Account created successfully",
        "user_id": str(user.id),
        "username": profile.username,
        "person_id": str(profile.person_id),
        "identity_status": profile.identity_status,
    }
