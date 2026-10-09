"""Atomic synchronous callback completion, with no committed pending state."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models
from .base import CRUDBase


class CallbackReplayConflictError(ValueError):
    """A delivery identifier was reused for different signed bytes."""


class CRUDCallbackReceipt(CRUDBase[models.CallbackReceipt]):
    """Reserve, evaluate and commit once within one bounded transaction.

    A crash rolls back the uncommitted reservation. PostgreSQL unique-index
    arbitration serializes competing inserts across workers. Completed
    receipts and audit writes made by the evaluator commit atomically.
    Evaluators must impose their own hard execution deadline, avoid network
    I/O, and write audit entries through CRUD with commit=False.
    """

    def complete_once(
        self,
        db: Session,
        *,
        account_id: UUID,
        integration_id: UUID,
        delivery_digest: str,
        body_digest: str,
        evaluate: Callable[[UUID], tuple[dict[str, Any], dict[str, Any]]],
        wait_timeout_ms: int = 2500,
        retention_seconds: int = 86400,
    ) -> models.CallbackReceipt:
        """Return the original receipt or commit one evaluation and its audit.

        This method owns transaction completion. Auth and body/header identity
        checks must run before calling it, including on retries. A mismatched
        digest raises CallbackReplayConflictError; database failures propagate.
        """
        if not 100 <= wait_timeout_ms <= 5000:
            raise ValueError("Invalid callback lock deadline")
        if not 600 <= retention_seconds <= 86400:
            raise ValueError("Invalid callback retention")
        if any(
            len(value) != 64 or any(c not in "0123456789abcdef" for c in value)
            for value in (delivery_digest, body_digest)
        ):
            raise ValueError("Callback identifiers must be keyed hex digests")
        try:
            # Covers both unique-index arbitration and row-lock wait. No
            # unbounded committed lease or background pending reservation.
            db.execute(
                select(func.set_config("lock_timeout", f"{wait_timeout_ms}ms", True))
            )
            db.execute(select(func.set_config("statement_timeout", "5000ms", True)))
            owner = (
                db.query(models.SecretReference.id)
                .filter(
                    models.SecretReference.id == integration_id,
                    models.SecretReference.account_id == account_id,
                )
                .first()
            )
            if owner is None:
                raise ValueError("Integration does not belong to account")
            db.execute(
                insert(self.model)
                .values(
                    id=uuid4(),
                    account_id=account_id,
                    integration_id=integration_id,
                    delivery_digest=delivery_digest,
                    body_digest=body_digest,
                    expires_at=datetime.now(timezone.utc)
                    + timedelta(seconds=retention_seconds),
                )
                .on_conflict_do_nothing(constraint="uq_callback_receipt_delivery")
            )
            row = (
                db.query(self.model)
                .filter(
                    self.model.account_id == account_id,
                    self.model.integration_id == integration_id,
                    self.model.delivery_digest == delivery_digest,
                )
                .populate_existing()
                .with_for_update()
                .one()
            )
            if not hmac.compare_digest(row.body_digest, body_digest):
                raise CallbackReplayConflictError("Changed callback replay")
            if row.verdict is None:
                row.verdict, row.evidence = evaluate(row.id)
                db.flush()
            db.commit()
            return row
        except BaseException:
            db.rollback()
            raise

    def health(
        self, db: Session, *, account_id: UUID, integration_id: UUID
    ) -> dict[str, Any]:
        """Return observed receipt counts, without inferring vendor coverage."""
        scope = (
            self.model.account_id == account_id,
            self.model.integration_id == integration_id,
        )
        total, latest = (
            db.query(func.count(self.model.id), func.max(self.model.created_at))
            .filter(*scope)
            .one()
        )
        unsupported = (
            db.query(func.count(self.model.id))
            .filter(*scope, self.model.evidence["reason"].astext == "unsupported_event")
            .scalar()
        )
        return {
            "callback_count": total,
            "unsupported_event_count": unsupported,
            "last_callback_at": latest,
        }

    def prune(self, db: Session, *, now: datetime, limit: int = 1000) -> int:
        """Delete a bounded expired batch; safe to run from multiple workers."""
        if not 1 <= limit <= 1000:
            raise ValueError("Invalid receipt prune batch")
        ids = (
            db.query(self.model.id)
            .filter(self.model.expires_at < now)
            .order_by(self.model.expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .all()
        )
        if not ids:
            return 0
        count = (
            db.query(self.model)
            .filter(self.model.id.in_([item.id for item in ids]))
            .delete(synchronize_session=False)
        )
        db.commit()
        return count


crud_callback_receipt = CRUDCallbackReceipt(models.CallbackReceipt)


class CallbackBindingConflictError(ValueError):
    """An integration key is ambiguous, reused, or from a different epoch."""


class CRUDCallbackKeyBinding(CRUDBase[models.CallbackKeyBinding]):
    """Atomically reserve signing fingerprints alongside encrypted config writes."""

    def bind(
        self,
        db: Session,
        *,
        account_id: UUID,
        integration_id: UUID,
        signing_key_digest: str,
        digest_epoch: str,
        now: datetime,
    ) -> None:
        """Bind an unused key, rejecting reuse and server-key epoch drift.

        Caller finishes the transaction through encrypted-secret CRUD. On any
        failure this method rolls back the transaction, including new config.
        """
        if len(signing_key_digest) != 64 or len(digest_epoch) != 32:
            raise ValueError("Invalid binding fingerprint")
        try:
            db.execute(select(func.set_config("statement_timeout", "5000ms", True)))
            # Serialize registration across epochs as well as tenant accounts.
            db.execute(select(func.pg_advisory_xact_lock(86379511)))
            owner = (
                db.query(models.SecretReference.id)
                .filter(
                    models.SecretReference.id == integration_id,
                    models.SecretReference.account_id == account_id,
                )
                .first()
            )
            if owner is None:
                raise CallbackBindingConflictError("Invalid integration owner")
            if (
                db.query(self.model.id)
                .filter(self.model.digest_epoch != digest_epoch)
                .first()
                is not None
            ):
                raise CallbackBindingConflictError(
                    "Signing digest epoch requires offline maintenance"
                )
            row = (
                db.query(self.model)
                .filter(self.model.signing_key_digest == signing_key_digest)
                .with_for_update()
                .first()
            )
            if row is not None:
                # Never reassign on expiry or revive a retired key implicitly.
                raise CallbackBindingConflictError("Signing key is already bound")
            db.add(
                self.model(
                    account_id=account_id,
                    integration_id=integration_id,
                    signing_key_digest=signing_key_digest,
                    digest_epoch=digest_epoch,
                    expires_at=None,
                )
            )
            db.flush()
        except BaseException:
            db.rollback()
            raise

    def retire(
        self,
        db: Session,
        *,
        account_id: UUID,
        integration_id: UUID,
        signing_key_digest: str,
        expires_at: datetime,
    ) -> None:
        """Bound the previous key overlap without freeing its fingerprint."""
        row = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.integration_id == integration_id,
                self.model.signing_key_digest == signing_key_digest,
            )
            .with_for_update()
            .one()
        )
        row.expires_at = expires_at
        db.flush()

    def assert_usable(
        self,
        db: Session,
        *,
        account_id: UUID,
        integration_id: UUID,
        signing_key_digest: str,
        digest_epoch: str,
        now: datetime,
    ) -> bool:
        """Verify a current uniquely bound key; rotation epoch changes fail closed."""
        row = (
            db.query(self.model)
            .filter(
                self.model.account_id == account_id,
                self.model.integration_id == integration_id,
                self.model.signing_key_digest == signing_key_digest,
                self.model.digest_epoch == digest_epoch,
            )
            .first()
        )
        return row is not None and (row.expires_at is None or row.expires_at > now)


crud_callback_key_binding = CRUDCallbackKeyBinding(models.CallbackKeyBinding)
