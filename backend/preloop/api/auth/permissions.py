"""Permission enforcement for protected endpoints.

This module provides decorators and helpers for enforcing role-based permissions
across all protected API endpoints.
"""

import functools
import logging
from typing import Callable, List

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from preloop.models.models.user import User

logger = logging.getLogger(__name__)


def has_permission(user: User, permission_name: str, db: Session) -> bool:
    """Check if a user has a specific permission.

    Permissions are aggregated from every role the user holds in their
    account: roles assigned directly and roles granted through team
    membership. Role resolution is shared with
    :func:`preloop.utils.permissions.user_holds_permission`, so both checks
    always agree.

    Args:
        user: The user to check permissions for.
        permission_name: The name of the permission to check (e.g., "create_issue").
        db: Database session for querying roles and permissions.

    Returns:
        True if the user has the permission, False otherwise.

    Note:
        - Users holding the system "owner" role have all permissions
        - Inactive users have no permissions
        - Resolved in a single query
    """
    # Imported here, not at module level: preloop.utils.permissions imports
    # the RBAC plugin at import time, and the plugin may import this module.
    from preloop.utils.permissions import user_holds_permission

    if not user.is_active:
        return False

    return user_holds_permission(db, user, permission_name)


def get_user_permissions(user: User, db: Session) -> List[str]:
    """Get all permissions for a user.

    Permissions are aggregated from every role the user holds in their
    account: roles assigned directly and roles granted through team
    membership. Role resolution is shared with
    :func:`preloop.utils.permissions.user_permission_names`, so this list
    agrees with :func:`has_permission`.

    Args:
        user: The user to get permissions for.
        db: Database session.

    Returns:
        Permission names the user has. Empty when the user is inactive.
        Order is not significant. The system owner role expands to every
        permission name.

    Note:
        Users holding the system "owner" role have all permissions.
        Inactive users have no permissions.
    """
    # Imported here, not at module level: preloop.utils.permissions imports
    # the RBAC plugin at import time, and the plugin may import this module.
    from preloop.utils.permissions import user_permission_names

    if not user.is_active:
        return []

    return user_permission_names(db, user)


def require_permission(permission_name: str):
    """Decorator to require a specific permission for endpoint access.

    This decorator should be applied to FastAPI endpoint functions to enforce
    permission checks. It must be used after the `Depends(get_current_active_user)`
    dependency.

    Args:
        permission_name: The name of the permission required (e.g., "create_issue").

    Returns:
        Decorator function.

    Example:
        @router.post("/issues")
        @require_permission("create_issue")
        async def create_issue(
            issue_data: IssueCreate,
            current_user: User = Depends(get_current_active_user),
            db: Session = Depends(get_db_session),
        ):
            # Implementation here
            pass

    Raises:
        HTTPException: 403 Forbidden if the user doesn't have the required permission.
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Extract current_user and db from kwargs
            current_user = kwargs.get("current_user")
            db = kwargs.get("db")

            if not current_user or not db:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Permission check requires current_user and db dependencies",
                )

            # Check if user has the required permission
            if not has_permission(current_user, permission_name, db):
                logger.warning(
                    f"Permission denied: User {current_user.username} "
                    f"(ID: {current_user.id}) attempted to access endpoint requiring "
                    f"'{permission_name}' permission"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions. Required: {permission_name}",
                )

            # Permission check passed, call the original function
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def require_any_permission(*permission_names: str):
    """Decorator to require any one of multiple permissions.

    This is useful for endpoints that can be accessed by users with different roles.

    Args:
        *permission_names: Variable number of permission names.

    Returns:
        Decorator function.

    Example:
        @require_any_permission("manage_users", "view_users")
        async def list_users(...):
            pass
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            current_user = kwargs.get("current_user")
            db = kwargs.get("db")

            if not current_user or not db:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Permission check requires current_user and db dependencies",
                )

            # Check if user has any of the required permissions
            for perm in permission_names:
                if has_permission(current_user, perm, db):
                    return await func(*args, **kwargs)

            logger.warning(
                f"Permission denied: User {current_user.username} "
                f"attempted to access endpoint requiring one of: {permission_names}"
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required one of: {', '.join(permission_names)}",
            )

        return wrapper

    return decorator


def require_all_permissions(*permission_names: str):
    """Decorator to require all specified permissions.

    This is useful for endpoints that require multiple permissions simultaneously.

    Args:
        *permission_names: Variable number of permission names.

    Returns:
        Decorator function.

    Example:
        @require_all_permissions("manage_flows", "execute_flows")
        async def execute_and_modify_flow(...):
            pass
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            current_user = kwargs.get("current_user")
            db = kwargs.get("db")

            if not current_user or not db:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Permission check requires current_user and db dependencies",
                )

            # Check if user has all required permissions
            missing_perms = []
            for perm in permission_names:
                if not has_permission(current_user, perm, db):
                    missing_perms.append(perm)

            if missing_perms:
                logger.warning(
                    f"Permission denied: User {current_user.username} "
                    f"missing permissions: {missing_perms}"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Insufficient permissions. Missing: {', '.join(missing_perms)}",
                )

            return await func(*args, **kwargs)

        return wrapper

    return decorator
