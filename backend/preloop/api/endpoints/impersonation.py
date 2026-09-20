"""
User impersonation endpoints for admin users.

Allows superusers to impersonate other users for debugging and support purposes.
All impersonation actions are logged in the audit log.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timezone, timedelta
from typing import Dict

from preloop.models.db.session import get_db_session
from preloop.models.models import User
from preloop.api.auth.jwt import get_current_active_user, create_access_token
from preloop.models.crud import crud_audit_log

router = APIRouter(prefix="/admin/impersonate", tags=["admin", "impersonation"])


@router.post("/{user_id}")
async def impersonate_user(
    user_id: str,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, str]:
    """
    Generate an impersonation token for the specified user.

    Only superusers can impersonate other users.
    The token is valid for 8 hours and includes impersonation metadata.

    Args:
        user_id: ID of the user to impersonate
        db: Database session
        current_user: The superuser performing the impersonation

    Returns:
        Dict with access_token and user info

    Raises:
        HTTPException: If user is not a superuser or target user not found
    """
    # Check if current user is a superuser
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=403, detail="Only superusers can impersonate other users"
        )

    # Get target user
    target_user = db.query(User).filter(User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Don't allow impersonating inactive users
    if not target_user.is_active:
        raise HTTPException(status_code=400, detail="Cannot impersonate inactive user")

    # Create impersonation token (8 hour expiry)
    access_token_expires = timedelta(hours=8)
    from preloop.api.auth.jwt import user_auth_generation

    access_token = create_access_token(
        data={
            "sub": str(target_user.id),
            "account_id": str(target_user.account_id),
            "impersonated_by": str(current_user.id),
            "impersonation_started_at": datetime.now(timezone.utc).isoformat(),
        },
        expires_delta=access_token_expires,
        auth_generation=user_auth_generation(target_user),
    )

    # Log the impersonation in audit log
    crud_audit_log.log_action(
        db=db,
        user_id=current_user.id,
        account_id=current_user.account_id,
        action="user.impersonate",
        resource_type="user",
        resource_id=str(target_user.id),
        status="success",
        details={
            "impersonated_user_id": str(target_user.id),
            "impersonated_username": target_user.username,
            "impersonated_email": target_user.email,
            "impersonator_user_id": str(current_user.id),
            "impersonator_username": current_user.username,
        },
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "user": {
            "id": str(target_user.id),
            "username": target_user.username,
            "email": target_user.email,
            "account_id": str(target_user.account_id),
        },
        "impersonated_by": {
            "id": str(current_user.id),
            "username": current_user.username,
        },
        "expires_in_hours": 8,
    }


@router.post("/stop")
async def stop_impersonation(
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> Dict[str, str]:
    """
    Stop impersonating and return to original user.

    This endpoint should be called with the impersonation token.
    It will log the end of impersonation.

    Returns:
        Message confirming impersonation has stopped
    """
    # Log the end of impersonation
    crud_audit_log.log_action(
        db=db,
        user_id=current_user.id,
        account_id=current_user.account_id,
        action="user.impersonate.stop",
        resource_type="user",
        resource_id=str(current_user.id),
        status="success",
        details={
            "impersonated_user_id": str(current_user.id),
            "impersonated_username": current_user.username,
        },
    )

    return {
        "message": "Impersonation stopped. Please use your original token to continue.",
        "user_id": str(current_user.id),
    }
