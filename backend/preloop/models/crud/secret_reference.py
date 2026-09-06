"""CRUD operations for SecretReference."""

from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from .base import CRUDBase


class CRUDSecretReference(CRUDBase[models.SecretReference]):
    """CRUD class for SecretReference operations."""

    def inspect_for_refresh(
        self,
        db: Session,
        *,
        secret_id: UUID,
        inspect: Callable[
            [Optional[models.SecretReference]], tuple[dict[str, Any], bool]
        ],
    ) -> dict[str, Any]:
        """Read current secret state under an owned, releasable row lock.

        The inspector returns a payload and whether refresh still needs the
        lock. Rolling back only this savepoint releases an unnecessary lock
        without committing or discarding the caller's surrounding transaction.
        A required refresh retains exclusion until the caller persists rotation.
        """
        with db.begin_nested() as savepoint:
            secret = (
                db.query(self.model)
                .filter(self.model.id == secret_id)
                .populate_existing()
                .with_for_update()
                .one_or_none()
            )
            payload, needs_refresh = inspect(secret)
            if not needs_refresh:
                savepoint.rollback()
            return payload

    def get_for_account(
        self, db: Session, *, secret_id: str, account_id: str
    ) -> Optional[models.SecretReference]:
        """Get a secret reference scoped to an account."""
        return (
            db.query(self.model)
            .filter(self.model.id == secret_id, self.model.account_id == account_id)
            .first()
        )


crud_secret_reference = CRUDSecretReference(models.SecretReference)
