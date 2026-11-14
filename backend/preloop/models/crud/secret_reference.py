"""CRUD operations for SecretReference."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.models.secret_reference import (
    UPSTREAM_AUTH_REJECTION_KEY,
    UPSTREAM_BILLING_REJECTION_KEY,
)
from .base import CRUDBase

#: A burst of rejected requests rewrites the secret row at most this often.
UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True)
class CredentialRejectionOutcome:
    """A serialized verdict, including whether a new episode started."""

    written: bool = False
    first_rejection: bool = False
    rejection: Optional[dict[str, Any]] = None


class CRUDSecretReference(CRUDBase[models.SecretReference]):
    """CRUD class for SecretReference operations."""

    def validate_model_reuse(
        self, db: Session, *, secret: models.SecretReference, account_id: Any
    ) -> None:
        """Prove ownership without adopting unowned or mixed-account secrets.

        Explicit ownership must match. A legacy secret without an owner needs
        at least one model reference, and every reference must name this account.
        Global model references are not evidence of account ownership.

        Raises:
            ValueError: Ownership is foreign, ambiguous, or cannot be proved.
        """
        if account_id is None:
            raise ValueError("Referenced credential secret requires an owning account")
        owner = secret.account_id
        if owner is not None and str(owner) != str(account_id):
            raise ValueError(
                "Referenced credential secret belongs to a different account"
            )
        references = (
            db.query(models.AIModel.account_id)
            .filter(models.AIModel.credentials_secret_id == secret.id)
            .distinct()
            .all()
        )
        if owner is None and not references:
            raise ValueError(
                "Referenced credential secret has no owning account and no model "
                "references proving ownership"
            )
        if any(
            model_account is None or str(model_account) != str(account_id)
            for (model_account,) in references
        ):
            raise ValueError(
                "Referenced credential secret has model references belonging to a "
                "different account or a global model"
            )

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
        self,
        db: Session,
        *,
        ai_model_id: UUID,
        secret_id: Optional[UUID],
        version: Optional[int],
    ) -> Optional[models.SecretReference]:
        """Lock the credential secret a request used, if it is still current.

        ``secret_id`` and ``version`` name the stored material the request
        resolved. When the model has since been pointed at another secret, or
        the material was rotated, the request's verdict is about a key that is
        no longer stored and ``None`` is returned. Callers that pass ``None``
        for both accept whatever the model points at now.
        """
        secret = (
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
        if secret is None:
            return None
        if secret_id is not None and secret.id != secret_id:
            return None
        if version is not None and (secret.version or 1) != version:
            return None
        return secret

    def record_upstream_auth_rejection(
        self,
        db: Session,
        *,
        ai_model_id: UUID,
        secret_id: Optional[UUID] = None,
        version: Optional[int] = None,
        provider: str,
        status_code: int,
        message: str,
        now: datetime,
        commit: bool = True,
    ) -> CredentialRejectionOutcome:
        """Record that the provider rejected a model's stored API key.

        The first rejection keeps its time in ``first_rejected_at`` until the
        key is accepted, replaced or rotated. Later rejections inside
        ``UPSTREAM_AUTH_REJECTION_MIN_WRITE_INTERVAL`` are not written, so a
        retry burst costs one row update.

        Args:
            db: Database session.
            ai_model_id: Model whose credential was rejected.
            secret_id: Secret row the rejected request resolved, if known.
            version: Rotation counter of that material, if known. A verdict
                for material that is no longer stored is ignored.
            provider: Provider name that answered.
            status_code: Upstream HTTP status.
            message: Provider message, already trimmed by the caller.
            now: Time of the rejection (timezone-aware).
            commit: Commit after writing; False leaves it to the caller.

        Returns:
            Whether a row was written and a new rejection episode started.
        """
        secret = self._lock_for_ai_model(
            db, ai_model_id=ai_model_id, secret_id=secret_id, version=version
        )
        if secret is None:
            if commit:
                db.rollback()
            return CredentialRejectionOutcome()
        key = (
            UPSTREAM_BILLING_REJECTION_KEY
            if status_code == 402
            else UPSTREAM_AUTH_REJECTION_KEY
        )
        meta = secret.meta_data if isinstance(secret.meta_data, dict) else {}
        candidate = meta.get(key)
        previous = (
            candidate
            if isinstance(candidate, dict) and candidate.get("last_rejected_at")
            else None
        )
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
                return CredentialRejectionOutcome()
        stamp = now.isoformat()
        secret.meta_data = {
            **{
                name: value
                for name, value in meta.items()
                if name
                not in {UPSTREAM_AUTH_REJECTION_KEY, UPSTREAM_BILLING_REJECTION_KEY}
            },
            key: {
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
        return CredentialRejectionOutcome(
            written=True,
            first_rejection=previous is None,
            rejection=dict(secret.meta_data[key]),
        )

    def clear_upstream_auth_rejection(
        self,
        db: Session,
        *,
        ai_model_id: UUID,
        secret_id: Optional[UUID] = None,
        version: Optional[int] = None,
        now: datetime,
        commit: bool = True,
    ) -> bool:
        """Forget a recorded rejection once the provider accepts the key again.

        Args:
            db: Database session.
            ai_model_id: Model whose credential was accepted.
            secret_id: Secret row the accepted request resolved, if known.
            version: Rotation counter of that material, if known. An accepted
                request with material that is no longer stored clears nothing.
            now: Time of the accepted request, stored as ``last_verified_at``.
            commit: Commit after writing; False leaves it to the caller.

        Returns:
            True when a rejection was removed.
        """
        secret = self._lock_for_ai_model(
            db, ai_model_id=ai_model_id, secret_id=secret_id, version=version
        )
        if secret is None or secret.upstream_credential_rejection is None:
            if commit:
                db.rollback()
            return False
        secret.meta_data = {
            key: value
            for key, value in (secret.meta_data or {}).items()
            if key not in {UPSTREAM_AUTH_REJECTION_KEY, UPSTREAM_BILLING_REJECTION_KEY}
        }
        secret.last_verified_at = now
        db.add(secret)
        if commit:
            db.commit()
        else:
            db.flush()
        return True


crud_secret_reference = CRUDSecretReference(models.SecretReference)
