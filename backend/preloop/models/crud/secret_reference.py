"""CRUD operations for SecretReference."""

from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.secret_reference import UPSTREAM_AUTH_REJECTION_KEY
from .base import CRUDBase

#: A burst of rejected requests rewrites the secret row at most this often.
UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL = timedelta(seconds=60)


class CRUDSecretReference(CRUDBase[models.SecretReference]):
    """CRUD class for SecretReference operations."""

    def get_for_update(
        self,
        db: Session,
        *,
        secret_id: UUID,
        account_id: Optional[UUID],
        nowait: bool = False,
    ) -> Optional[models.SecretReference]:
        """Read an account's current credential while excluding refresh writers."""
        return (
            db.query(self.model)
            .filter(self.model.id == secret_id, self.model.account_id == account_id)
            .populate_existing()
            .with_for_update(nowait=nowait)
            .one_or_none()
        )

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

    def _lock_for_ai_model(
        self, db: Session, *, ai_model_id: UUID
    ) -> Optional[models.SecretReference]:
        """Lock the credential secret a model currently points at."""
        return (
            db.query(self.model)
            .join(
                models.AIModel,
                models.AIModel.credentials_secret_id == self.model.id,
            )
            .filter(models.AIModel.id == ai_model_id)
            .populate_existing()
            .with_for_update(of=self.model)
            .one_or_none()
        )

    def record_upstream_auth_rejection(
        self,
        db: Session,
        *,
        ai_model_id: UUID,
        provider: str,
        status_code: int,
        message: str,
        now: datetime,
        commit: bool = True,
    ) -> bool:
        """Record that the provider rejected a model's stored API key.

        The first rejection keeps its time in ``first_rejected_at`` until the
        key is accepted, replaced or rotated. Later rejections inside
        ``UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL`` are not written, so a
        retry burst costs one row update.

        Args:
            db: Database session.
            ai_model_id: Model whose current credential was rejected.
            provider: Provider name that answered.
            status_code: Upstream HTTP status.
            message: Provider message, already trimmed by the caller.
            now: Time of the rejection (timezone-aware).
            commit: Commit after writing; False leaves it to the caller.

        Returns:
            True when the row was written.
        """
        secret = self._lock_for_ai_model(db, ai_model_id=ai_model_id)
        if secret is None:
            if commit:
                db.rollback()
            return False
        previous = secret.upstream_auth_rejection
        if previous is not None:
            try:
                last = datetime.fromisoformat(str(previous["last_rejected_at"]))
            except ValueError:
                last = None
            if (
                last is not None
                and last.tzinfo is not None
                and now - last < UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL
            ):
                if commit:
                    db.rollback()
                return False
        stamp = now.isoformat()
        secret.meta_data = {
            **(secret.meta_data if isinstance(secret.meta_data, dict) else {}),
            UPSTREAM_AUTH_REJECTION_KEY: {
                "provider": provider,
                "status_code": status_code,
                "message": message,
                "first_rejected_at": (previous or {}).get("first_rejected_at") or stamp,
                "last_rejected_at": stamp,
            },
        }
        db.add(secret)
        if commit:
            db.commit()
        else:
            db.flush()
        return True

    def clear_upstream_auth_rejection(
        self,
        db: Session,
        *,
        ai_model_id: UUID,
        now: datetime,
        commit: bool = True,
    ) -> bool:
        """Forget a recorded rejection once the provider accepts the key again.

        Args:
            db: Database session.
            ai_model_id: Model whose current credential was accepted.
            now: Time of the accepted request, stored as ``last_verified_at``.
            commit: Commit after writing; False leaves it to the caller.

        Returns:
            True when a rejection was removed.
        """
        secret = self._lock_for_ai_model(db, ai_model_id=ai_model_id)
        if secret is None or secret.upstream_auth_rejection is None:
            if commit:
                db.rollback()
            return False
        secret.meta_data = {
            key: value
            for key, value in (secret.meta_data or {}).items()
            if key != UPSTREAM_AUTH_REJECTION_KEY
        }
        secret.last_verified_at = now
        db.add(secret)
        if commit:
            db.commit()
        else:
            db.flush()
        return True


crud_secret_reference = CRUDSecretReference(models.SecretReference)
