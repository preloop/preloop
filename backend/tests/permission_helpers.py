"""Explicit permission grants for endpoint test principals."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_permission, crud_role


def grant_permission(
    db: Session,
    user: models.User,
    permission_name: str,
    *,
    role_account_id: UUID | None = None,
) -> None:
    """Assign a custom role granting only the requested permission."""
    permission = crud_permission.get_by_name(db, name=permission_name)
    if permission is None:
        permission = crud_permission.create(
            db,
            obj_in={
                "name": permission_name,
                "description": "Example permission",
                "category": "test",
            },
        )
    role = crud_role.create(
        db,
        obj_in={
            "account_id": role_account_id or user.account_id,
            "name": f"example-{uuid4().hex}",
        },
    )
    db.add(models.RolePermission(role_id=role.id, permission_id=permission.id))
    db.add(models.UserRole(user_id=user.id, role_id=role.id))
    db.flush()
