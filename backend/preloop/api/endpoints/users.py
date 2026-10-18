"""User management endpoints."""

import logging
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from preloop.api.auth.identity import require_session_profile_edit
from pydantic import BaseModel
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.schemas.user import (
    AdminUserCreate,
    UserListResponse,
    UserPasswordUpdate,
    AdminUserResponse,
    UserSummary,
    AdminUserUpdate,
)
from preloop.models import models
from preloop.models.crud import crud_account, crud_person, crud_user, crud_user_role
from preloop.models.crud.identity_directory import (
    IdentityPresentation,
    identity_directory,
)
from preloop.models.crud.person import IdentityConflictError, IdentityUnavailableError

from preloop.services.multiuser import email_exists
from preloop.models.db.session import get_db_session
from preloop.utils.permissions import require_permission

from preloop.services.multiuser import membership_module
from preloop.services.multiuser import enforce_seat_available

User = models.User
Role = models.Role


logger = logging.getLogger(__name__)

router = APIRouter()

# System role that grants unrestricted access (billing + account closure). Only
# an existing owner may grant it ; otherwise `assign_roles`/`invite_users`
# holders (e.g. admins) could self-promote to owner.
_OWNER_ROLE_NAME = "owner"


def caller_has_owner_role(db: Session, user: User) -> bool:
    """Return True if ``user`` directly holds the system ``owner`` role."""
    from preloop.models.crud import crud_role

    for user_role in crud_user_role.get_by_user(db, user_id=user.id):
        role = crud_role.get(db, id=user_role.role_id)
        if (
            role
            and role.name == _OWNER_ROLE_NAME
            and role.account_id is None
            and role.is_system_role
        ):
            return True
    return False


def _ensure_can_remove_user(db: Session, current_user: User, user: User) -> None:
    """Require ownership before removing a primary user or another owner."""
    if current_user.is_superuser:
        return
    account = crud_account.get(db, id=user.account_id)
    protected = (
        account is not None and account.primary_user_id == user.id
    ) or caller_has_owner_role(db, user)
    if protected and not caller_has_owner_role(db, current_user):
        raise HTTPException(
            status_code=403,
            detail="Only an account owner can remove an owner or primary user; transfer ownership first",
        )


def _ensure_can_grant_role(db: Session, current_user: User, role: Role) -> None:
    """Block privilege escalation when granting a role.

    Currently guards the ``owner`` role: a caller may only grant it if they are
    themselves an owner. Raises HTTP 403 otherwise.
    """
    from preloop.services.multiuser import ensure_role_available

    ensure_role_available(db, current_user, role)
    if (
        role.name == _OWNER_ROLE_NAME
        and role.account_id is None
        and not caller_has_owner_role(db, current_user)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only an account owner can grant the owner role.",
        )


def _build_admin_user_response(
    db: Session,
    user: User,
    *,
    profile: IdentityPresentation | None = None,
    role_context: tuple[list[dict[str, Any]], list[dict[str, Any]]] | None = None,
) -> AdminUserResponse:
    """Build an admin user response without exposing ORM relationship objects."""
    profile = profile or identity_directory.profile_for_membership(
        db, membership_id=user.id
    )
    roles, inherited_roles = (
        role_context
        if role_context is not None
        else identity_directory.role_context(db, membership_id=user.id)
    )

    return AdminUserResponse(
        id=user.id,
        account_id=user.account_id,
        person_id=profile.person_id,
        username=profile.username,
        email=profile.email,
        email_verified=profile.email_verified,
        full_name=profile.full_name,
        avatar_url=profile.avatar_url,
        avatar_source=profile.avatar_source,
        identity_status=profile.identity_status,
        is_active=user.is_active,
        user_source=user.user_source,
        oauth_provider=user.oauth_provider,
        last_login=user.last_login,
        created_at=user.created_at,
        updated_at=user.updated_at,
        roles=roles,
        inherited_roles=inherited_roles,
    )


def _build_member_page(db: Session, users: list[User]) -> list[AdminUserResponse]:
    """Keep account pages bounded regardless of their member count."""
    profiles, roles = identity_directory.projections_for_memberships(
        db, membership_ids=[user.id for user in users]
    )
    return [
        _build_admin_user_response(
            db, user, profile=profiles[user.id], role_context=roles[user.id]
        )
        for user in users
    ]


@router.get("/users/me", response_model=AdminUserResponse)
def get_current_user_info(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get current user's information.

    Returns:
        UserResponse: Current user's profile data
    """
    # Re-fetch the user object to ensure it is fully bound to the current
    # session before navigating its lazy-loaded ORM relationships.
    current_user = crud_user.get(db=db, id=current_user.id)

    return _build_admin_user_response(db, current_user)


@router.patch("/users/me", response_model=AdminUserResponse)
def update_current_user(
    update_data: AdminUserUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Update current user's profile.

    Args:
        update_data: User update data
        current_user: Current authenticated user
        db: Database session

    Returns:
        UserResponse: Updated user data
    """
    changes = update_data.model_dump(exclude_unset=True)
    if "is_active" in changes:
        raise HTTPException(
            status_code=403, detail="Profile edits cannot change membership status"
        )
    _update_shared_profile(db, current_user, changes)
    return _build_admin_user_response(db, current_user)


def _update_shared_profile(
    db: Session, user: User, changes: dict[str, Any], *, commit: bool = True
) -> None:
    """Apply own identity edits through the canonical conflict contract."""
    require_session_profile_edit(user, changes)
    if any(changes.get(key) is None for key in ("username", "email") if key in changes):
        raise HTTPException(status_code=422, detail="Username and email cannot be null")
    try:
        crud_person.update_profile(
            db, membership_id=user.id, changes=changes, commit=commit
        )
    except (IdentityConflictError, IdentityUnavailableError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/users/me/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    password_data: UserPasswordUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Response:
    """Change current user's password.

    Args:
        password_data: Current and new password
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If the identity is inactive or the password is incorrect.
    """
    from preloop.api.auth.identity import require_active_identity
    from preloop.api.auth.jwt import get_password_hash, verify_password
    from preloop.models.crud import crud_person

    holder = require_active_identity(db, current_user, lock=True)
    if not any(
        verify_password(password_data.current_password, password_hash)
        for password_hash in crud_person.list_password_hashes(db, person_id=holder.id)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Current password is incorrect",
        )
    crud_person.set_password(
        db,
        membership_id=current_user.id,
        hashed_password=get_password_hash(password_data.new_password),
    )

    logger.info(f"Password changed for user {current_user.username}")

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/users", response_model=AdminUserResponse, status_code=status.HTTP_201_CREATED
)
@require_permission("manage_users")
def create_user(
    user_data: AdminUserCreate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Create a new user in the current account.

    Args:
        user_data: User creation data
        current_user: Current authenticated user
        db: Database session

    Returns:
        UserResponse: Created user data

    Raises:
        HTTPException: If username or email already exists, or the account has
            no seat left on its plan (402).
    """
    # Seat cap. No-ops without the billing plugin.
    enforce_seat_available(db, current_user.account_id)

    # Check if username already exists
    existing_user = crud_user.get_by_username(db=db, username=user_data.username)
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    # Check if email already exists in this account. Scoped to the account
    # so the same address in another account is not a collision, and so a
    # shared address does not raise.
    if email_exists(db=db, email=user_data.email, account_id=current_user.account_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already exists in this account",
        )

    # Create user with password hash
    from preloop.api.auth.jwt import get_password_hash

    user_dict = user_data.model_dump()
    user_dict["account_id"] = current_user.account_id
    user_dict["hashed_password"] = get_password_hash(user_data.password)
    user_dict["is_active"] = True
    user_dict["email_verified"] = False
    user_dict["user_source"] = "local"
    # Somebody added to an existing account never chose a plan and is not the
    # person who gets to. Stamping the choice as made here is what keeps the
    # first-login plan choice off a colleague's first console visit, and what
    # stops their console asking the billing plugin about it on every page
    # load for the rest of the account's life.
    user_dict["plan_choice_made_at"] = datetime.now(timezone.utc)

    # Remove password from dict as we already hashed it
    del user_dict["password"]

    user = crud_user.create(db=db, obj_in=user_dict)
    db.commit()
    db.refresh(user)

    from preloop.services.multiuser import assign_default_member_role

    assign_default_member_role(db, user, granted_by=current_user.id)

    logger.info(f"User {user.username} created by {current_user.username}")

    # Update Stripe subscription quantity to reflect new user count
    try:
        from preloop.services.multiuser import sync_subscription_quantity

        sync_subscription_quantity(db, str(current_user.account_id))
    except Exception as e:
        logger.warning(f"Failed to update Stripe subscription quantity: {e}")
        # Don't fail user creation if billing update fails

    return _build_admin_user_response(db, user)


@router.get("/users", response_model=UserListResponse)
@require_permission("view_users")
def list_users(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """List all users in the current user's account.

    Args:
        current_user: Current authenticated user
        db: Database session
        skip: Number of records to skip
        limit: Maximum number of records to return

    Returns:
        UserListResponse: Paginated list of users
    """
    users, total = identity_directory.account_members(
        db, account_id=current_user.account_id, skip=skip, limit=limit
    )
    return {
        "users": _build_member_page(db, users),
        "total": total,
        "skip": skip,
        "limit": limit,
    }


@router.get("/users/{user_id}", response_model=AdminUserResponse)
@require_permission("view_users")
def get_user(
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get a specific user by ID.

    Args:
        user_id: User ID to retrieve
        current_user: Current authenticated user
        db: Database session

    Returns:
        UserResponse: User data

    Raises:
        HTTPException: If user not found or not in same account
    """
    user = crud_user.get(db=db, id=user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Verify user is in the same account
    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access user from different account",
        )

    return _build_admin_user_response(db, user)


@router.patch("/users/{user_id}", response_model=AdminUserResponse)
@require_permission("manage_users")
def update_user(
    user_id: UUID,
    update_data: AdminUserUpdate,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Update a user's profile (admin only).

    Args:
        user_id: User ID to update
        update_data: User update data
        current_user: Current authenticated user
        db: Database session

    Returns:
        UserResponse: Updated user data

    Raises:
        HTTPException: If user not found or not in same account
    """

    user = crud_user.get(db=db, id=user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Verify user is in the same account
    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot update user from different account",
        )

    # Reactivating a deactivated user claims a seat just like creating one,
    # so it goes through the same gate. Edits that do not flip is_active to
    # True (including re-saving an already-active user) are not seat events.
    update_fields = update_data.model_dump(exclude_unset=True)
    if "is_active" in update_fields and update_fields["is_active"] is None:
        raise HTTPException(status_code=422, detail="Membership status cannot be null")
    identity_fields = {
        key: value for key, value in update_fields.items() if key != "is_active"
    }
    if identity_fields:
        if user.id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="Account administrators cannot edit another person's global identity; manage their membership instead",
            )
        _update_shared_profile(db, user, identity_fields, commit=False)
        update_fields = {
            key: value for key, value in update_fields.items() if key == "is_active"
        }
    if update_fields.get("is_active") is True and not user.is_active:
        enforce_seat_available(db, current_user.account_id)
    # Switching a membership off is a removal: a primary row hands the
    # person's credentials to another membership first, as deactivate does.
    if update_fields.get("is_active") is False:
        if user.id == current_user.id:
            raise HTTPException(400, "Cannot deactivate your own account")
        _ensure_can_remove_user(db, current_user, user)
        if user.is_active:
            _release_membership(db, user)

    updated_user = crud_user.update(db=db, db_obj=user, obj_in=update_fields)
    db.commit()
    db.refresh(updated_user)

    return _build_admin_user_response(db, updated_user)


def _release_membership(db: Session, user: User) -> None:
    """Let the account hierarchy plugin move a leaving primary's credentials."""
    membership = membership_module()
    if membership is not None:
        membership.release_membership(db, user)


@router.post("/users/{user_id}/deactivate", status_code=status.HTTP_204_NO_CONTENT)
@require_permission("manage_users")
def deactivate_user(
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Response:
    """Deactivate a user (admin only).

    Args:
        user_id: User ID to deactivate
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If user not found, not in same account, or trying to deactivate self
    """
    if user_id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot deactivate your own account",
        )

    user = crud_user.get(db=db, id=user_id)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Verify user is in the same account
    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot deactivate user from different account",
        )

    _ensure_can_remove_user(db, current_user, user)
    _release_membership(db, user)
    crud_user.deactivate(db=db, user_id=user_id)
    db.commit()

    logger.info(f"User {user_id} deactivated by {current_user.username}")

    # Update Stripe subscription quantity to reflect reduced user count
    try:
        from preloop.services.multiuser import sync_subscription_quantity

        sync_subscription_quantity(db, str(current_user.account_id))
    except Exception as e:
        logger.warning(f"Failed to update Stripe subscription quantity: {e}")
        # Don't fail user deactivation if billing update fails

    return Response(status_code=status.HTTP_204_NO_CONTENT)


class BulkDeleteUsersRequest(BaseModel):
    """Request body for bulk user deletion."""

    user_ids: List[UUID]
    hard_delete: bool = False


class BulkDeleteUserResult(BaseModel):
    """Outcome of one user in a bulk deletion request."""

    user_id: UUID
    status: str  # "deleted" | "deactivated" | "account_purged" | "skipped" | "error"
    reason: Optional[str] = None


class BulkDeleteUsersResponse(BaseModel):
    """Per-user results of a bulk deletion request."""

    results: List[BulkDeleteUserResult]


def _get_billing_service(db: Session) -> Any:
    """Return the optional billing service registered by the commercial overlay."""
    from preloop.services.multiuser import get_billing_service

    return get_billing_service(db)


def _purge_account(db: Session, account_id: str, deleted_by: str) -> None:
    """Hard-delete an account, cleaning up Stripe when billing is available.

    With the billing plugin installed this cancels active Stripe
    subscriptions and deletes the Stripe customer before purging; without it
    the account is purged from the database only.
    """
    billing_service = _get_billing_service(db)
    if billing_service is not None:
        billing_service.delete_account(
            account_id=account_id,
            cancel_stripe=True,
            hard_delete=True,
            deleted_by=deleted_by,
        )
    else:
        crud_account.purge(db, account_id=account_id)


@router.post("/users/bulk-delete", response_model=BulkDeleteUsersResponse)
@require_permission("manage_users")
def bulk_delete_users(
    request: BulkDeleteUsersRequest,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Delete or deactivate several users at once.

    With ``hard_delete=false`` each user is deactivated (soft delete). With
    ``hard_delete=true`` each user is permanently deleted, including their
    SSO identity records (``oauth_*``/``external_id`` fields, provider
    tokens, identity links).

    Primary users are handled specially:
    - Primary user of a multi-user account: skipped (transfer ownership or
      delete the whole account instead).
    - Primary user of a solo account (the SSO-signup shape): the entire
      account is purged, including cancelling Stripe subscriptions and
      deleting the Stripe customer when the billing plugin is installed.

    Targets must belong to the caller's account unless the caller is a
    superuser. The caller can never delete themselves.

    Returns a per-user results array; the endpoint always returns 200 with
    individual ``skipped``/``error`` statuses rather than failing the batch.
    """
    deleted_by = current_user.email or current_user.username
    results: List[BulkDeleteUserResult] = []
    touched_account_ids: set[str] = set()

    seen: set[UUID] = set()
    for user_id in request.user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)

        if user_id == current_user.id:
            results.append(
                BulkDeleteUserResult(
                    user_id=user_id,
                    status="skipped",
                    reason="Cannot delete your own user",
                )
            )
            continue

        user = crud_user.get(db=db, id=user_id)
        if not user:
            results.append(
                BulkDeleteUserResult(
                    user_id=user_id, status="skipped", reason="User not found"
                )
            )
            continue

        if user.account_id != current_user.account_id and not current_user.is_superuser:
            results.append(
                BulkDeleteUserResult(
                    user_id=user_id,
                    status="skipped",
                    reason="User belongs to a different account",
                )
            )
            continue

        account_id = str(user.account_id)
        try:
            try:
                _ensure_can_remove_user(db, current_user, user)
            except HTTPException as exc:
                results.append(
                    BulkDeleteUserResult(
                        user_id=user_id, status="skipped", reason=str(exc.detail)
                    )
                )
                continue
            if not request.hard_delete:
                _release_membership(db, user)
                crud_user.deactivate(db=db, user_id=user.id)
                touched_account_ids.add(account_id)
                results.append(
                    BulkDeleteUserResult(user_id=user_id, status="deactivated")
                )
                logger.info(
                    f"User {user_id} deactivated (bulk) by {current_user.username}"
                )
                continue

            account = crud_account.get(db=db, id=account_id)
            is_primary = account is not None and account.primary_user_id == user.id
            if is_primary:
                member_count = crud_user.count_by_account(db, account_id=account_id)
                if member_count > 1:
                    results.append(
                        BulkDeleteUserResult(
                            user_id=user_id,
                            status="skipped",
                            reason=(
                                "Primary user of a multi-user account ; "
                                "transfer ownership or delete the account "
                                "instead"
                            ),
                        )
                    )
                    continue
                _purge_account(db, account_id, deleted_by)
                results.append(
                    BulkDeleteUserResult(user_id=user_id, status="account_purged")
                )
                logger.info(
                    f"Account {account_id} purged (bulk, solo primary user "
                    f"{user_id}) by {current_user.username}"
                )
                continue

            _release_membership(db, user)
            crud_user.hard_delete(db, user_id=user.id)
            touched_account_ids.add(account_id)
            results.append(BulkDeleteUserResult(user_id=user_id, status="deleted"))
            logger.info(
                f"User {user_id} hard-deleted (bulk) by {current_user.username}"
            )
        except Exception as e:
            logger.error(f"Bulk delete failed for user {user_id}: {e}", exc_info=True)
            db.rollback()
            results.append(
                BulkDeleteUserResult(user_id=user_id, status="error", reason=str(e))
            )

    # Best-effort Stripe seat-quantity update for accounts that lost members
    # (purged accounts are gone ; their Stripe customer was deleted above).
    billing_service = _get_billing_service(db)
    if billing_service is not None:
        for account_id in touched_account_ids:
            try:
                billing_service.update_subscription_quantity(account_id)
            except Exception as e:
                logger.warning(
                    f"Failed to update Stripe subscription quantity for "
                    f"account {account_id}: {e}"
                )

    return BulkDeleteUsersResponse(results=results)


@router.get("/users/search/{username}", response_model=List[UserSummary])
@require_permission("view_users")
def search_users_by_username(
    username: str,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
    limit: int = 10,
) -> Any:
    """Search users by username within the current account.

    Args:
        username: Username search string
        current_user: Current authenticated user
        db: Database session
        limit: Maximum number of results

    Returns:
        List[UserSummary]: List of matching users
    """
    users, _ = identity_directory.account_members(
        db,
        account_id=current_user.account_id,
        search=username,
        active_only=True,
        limit=limit,
    )
    return [
        UserSummary.model_validate(response.model_dump())
        for response in _build_member_page(db, users)
    ]


@router.get("/users/{user_id}/roles", response_model=List[dict])
@require_permission("view_users")
def get_user_roles(
    user_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Get roles assigned to a user.

    Args:
        user_id: User ID
        current_user: Current authenticated user
        db: Database session

    Returns:
        List[dict]: List of roles assigned to the user

    Raises:
        HTTPException: If user not found or not in same account
    """
    # Verify user exists and is in the same account
    user = crud_user.get(db=db, id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot access user from different account",
        )

    # Get user roles
    from preloop.models.crud import crud_role

    user_roles = crud_user_role.get_by_user(db=db, user_id=user_id)

    roles = []
    for user_role in user_roles:
        role = crud_role.get(db=db, id=user_role.role_id)
        if role:
            roles.append(
                {
                    "id": str(role.id),
                    "name": role.name,
                    "description": role.description,
                    "granted_at": user_role.granted_at.isoformat()
                    if user_role.granted_at
                    else None,
                }
            )

    return roles


class UserRoleAssignment(BaseModel):
    """A role identifier validated before querying account roles."""

    role_id: UUID | None = None


@router.post("/users/{user_id}/roles", status_code=status.HTTP_201_CREATED)
@require_permission("assign_roles")
def assign_user_role(
    user_id: UUID,
    role_data: UserRoleAssignment,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> Any:
    """Assign a role to a user.

    Args:
        user_id: User ID
        role_data: Dict with role_id
        current_user: Current authenticated user
        db: Database session

    Returns:
        dict: Success message

    Raises:
        HTTPException: If user not found, role not found, or not in same account
    """
    # Verify user exists and is in the same account
    user = crud_user.get(db=db, id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify user from different account",
        )

    # Verify role exists
    from preloop.models.crud import crud_role

    role_id = role_data.role_id
    if not role_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="role_id is required",
        )

    # Security: the role must belong to the caller's account or be a system
    # role. Without this scope check, a user with `assign_roles` could bind an
    # arbitrary other-account role ; or the global `owner` role ; to a user and
    # escalate privileges (see also invitations.py).
    role = crud_role.get(db=db, id=role_id, account_id=current_user.account_id)
    if not role:
        system_role = crud_role.get(db=db, id=role_id)
        if not system_role or system_role.account_id is not None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Role not found",
            )
        role = system_role

    _ensure_can_grant_role(db=db, current_user=current_user, role=role)

    # Membership-bound assignments remain account resources.
    existing = next(
        (
            assignment
            for assignment in crud_user_role.get_by_user(db, user_id=user_id)
            if str(assignment.role_id) == str(role_id)
        ),
        None,
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User already has this role",
        )

    # Assign role using the CRUD method
    crud_user_role.assign_role(
        db=db, user_id=user_id, role_id=role_id, granted_by=current_user.id
    )

    logger.info(f"Role {role_id} assigned to user {user_id} by {current_user.username}")

    return {"message": "Role assigned successfully"}


@router.delete(
    "/users/{user_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT
)
@require_permission("assign_roles")
def remove_user_role(
    user_id: UUID,
    role_id: UUID,
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
) -> None:
    """Remove a role from a user.

    Args:
        user_id: User ID
        role_id: Role ID to remove
        current_user: Current authenticated user
        db: Database session

    Raises:
        HTTPException: If user not found, role not found, or not in same account
    """
    # Verify user exists and is in the same account
    user = crud_user.get(db=db, id=user_id)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.account_id != current_user.account_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify user from different account",
        )

    from preloop.models.crud import crud_role

    role = crud_role.get(db, id=role_id)
    if role is not None and role.account_id is None and role.name == _OWNER_ROLE_NAME:
        _ensure_can_grant_role(db, current_user, role)

    # Remove role using CRUD method
    success = crud_user_role.remove_role(db=db, user_id=user_id, role_id=role_id)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User does not have this role",
        )

    logger.info(
        f"Role {role_id} removed from user {user_id} by {current_user.username}"
    )
