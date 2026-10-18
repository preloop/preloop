"""Tenant-local verifier record access; no collector ingestion API."""

from datetime import datetime
from uuid import UUID, uuid4
import re

from sqlalchemy.dialects.postgresql import insert

from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models


class CRUDObservationVerification:
    """Read and invalidate verifier records without interpreting observations."""

    def record(
        self,
        db: Session,
        *,
        account_id: UUID,
        source_ref: UUID,
        subject_key: str,
        policy_revision: str,
        evidence_hash: str,
        verified_at: datetime,
        expires_at: datetime,
    ) -> models.ObservationVerification:
        """Store a trusted server verifier success; callers own authorization/commit.

        This method has no collector or public API. Re-verification replaces a
        scoped result while the audit retains every previous success/invalidation.
        Older verifier results cannot replace a newer one.
        """
        for value in (subject_key, policy_revision, evidence_hash):
            if re.fullmatch(r"[0-9a-f]{64}", value) is None:
                raise ValueError("Verification identities must be SHA-256 digests")
        if (
            verified_at.tzinfo is None
            or expires_at.tzinfo is None
            or expires_at <= verified_at
        ):
            raise ValueError("Verification needs an ordered timezone-aware window")
        model = models.ObservationVerification
        values = dict(
            id=uuid4(),
            account_id=account_id,
            source_ref=source_ref,
            subject_key=subject_key,
            policy_revision=policy_revision,
            evidence_hash=evidence_hash,
            verified_at=verified_at,
            expires_at=expires_at,
        )
        statement = (
            insert(model)
            .values(**values)
            .on_conflict_do_update(
                constraint="uq_observation_verification_scope",
                set_={
                    "policy_revision": policy_revision,
                    "evidence_hash": evidence_hash,
                    "verified_at": verified_at,
                    "expires_at": expires_at,
                    "invalidated_at": None,
                    "invalidation_reason": None,
                },
                where=model.verified_at < verified_at,
            )
            .returning(model.id)
        )
        changed = db.scalar(statement) is not None
        row = db.scalar(
            select(model)
            .where(
                model.account_id == account_id,
                model.source_ref == source_ref,
                model.subject_key == subject_key,
            )
            .execution_options(populate_existing=True)
        )
        assert row is not None
        if changed:
            from preloop.models.crud import crud_audit_log

            crud_audit_log.log_action(
                db,
                account_id=account_id,
                action="configuration_change",
                resource_type="observation_verification",
                resource_id=str(row.id),
                status="success",
                details={
                    "reason": "verification_recorded",
                    "source_ref": str(source_ref),
                    "subject_key": subject_key,
                    "policy_revision": policy_revision,
                    "evidence_hash": evidence_hash,
                    "verified_at": verified_at.isoformat(),
                    "expires_at": expires_at.isoformat(),
                },
                commit=False,
            )
        db.flush()
        return row

    def list_for_account(
        self, db: Session, *, account_id: UUID, limit: int = 5001
    ) -> list[models.ObservationVerification]:
        """Read a bounded set under the owning tenant only."""
        return list(
            db.scalars(
                select(models.ObservationVerification)
                .where(models.ObservationVerification.account_id == account_id)
                .order_by(models.ObservationVerification.id)
                .limit(max(0, min(limit, 5001)))
            )
        )

    def invalidate(
        self, db: Session, *, account_id: UUID, id: UUID, now: datetime, reason: str
    ) -> bool:
        """Invalidate a source-specific result once, retaining the original evidence."""
        row = db.scalar(
            select(models.ObservationVerification)
            .where(
                models.ObservationVerification.account_id == account_id,
                models.ObservationVerification.id == id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.invalidated_at is not None or now < row.verified_at:
            return False
        row.invalidated_at = now
        row.invalidation_reason = reason
        from preloop.models.crud import crud_audit_log

        crud_audit_log.log_action(
            db,
            account_id=account_id,
            action="configuration_change",
            resource_type="observation_verification",
            resource_id=str(row.id),
            status="success",
            details={
                "reason": reason,
                "source_ref": str(row.source_ref),
                "subject_key": row.subject_key,
                "policy_revision": row.policy_revision,
                "evidence_hash": row.evidence_hash,
                "invalidated_at": now.isoformat(),
            },
            commit=False,
        )
        db.flush()
        return True


crud_observation_verification = CRUDObservationVerification()
