"""Role management endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.loop_safety import run_db_off_loop
from preloop.models.crud import crud_role
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.utils.permissions import require_permission

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/roles")
@require_permission("view_users")
async def list_roles(
    current_user: Annotated[User, Depends(get_current_active_user)],
    db: Session = Depends(get_db_session),
):
    """List all available roles.

    Args:
        current_user: Current authenticated user
        db: Database session

    Returns:
        dict: List of roles with their permissions
    """

    def _query() -> dict:
        # Keep custom role metadata within its owning account.
        from preloop.services.multiuser import advanced_roles_enabled

        roles = crud_role.get_available_roles(
            db, account_id=current_user.account_id, advanced=advanced_roles_enabled()
        )

        result = []
        for role in roles:
            role_dict = {
                "id": str(role.id),
                "name": role.name,
                "description": role.description,
                "permissions": [
                    {
                        "id": str(rp.permission.id),
                        "name": rp.permission.name,
                        "description": rp.permission.description,
                    }
                    for rp in role.role_permissions
                ],
            }
            result.append(role_dict)

        return {"roles": result, "total": len(result)}

    return await run_db_off_loop(_query)
