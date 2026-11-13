"""MFA persistence. Callers own transactions and lock user before factor."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from preloop.models import models


def factor(
    db: Session, user_id: UUID, *, lock: bool = False
) -> models.UserMfaFactor | None:
    """Load the user's TOTP factor, refreshing it after an optional row lock."""
    stmt = select(models.UserMfaFactor).where(
        models.UserMfaFactor.user_id == user_id, models.UserMfaFactor.kind == "totp"
    )
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return db.scalars(stmt).first()


def enabled(db: Session, user_id: UUID) -> bool:
    """Whether the membership has a confirmed factor."""
    obj = factor(db, user_id)
    return obj is not None and obj.confirmed_at is not None


def enroll(db: Session, user_id: UUID, secret_encrypted: str) -> models.UserMfaFactor:
    """Replace an unconfirmed factor; the caller holds the user row lock."""
    obj = factor(db, user_id, lock=True)
    if obj is not None:
        if obj.confirmed_at is not None:
            raise ValueError("mfa_already_enabled")
        db.delete(obj)
        db.flush()
    obj = models.UserMfaFactor(user_id=user_id, secret_encrypted=secret_encrypted)
    db.add(obj)
    db.flush()
    return obj


def confirm(db: Session, obj: models.UserMfaFactor, *, now: datetime) -> None:
    """Confirm an already locked factor after a successful proof."""
    obj.confirmed_at = now
    db.flush()


def use_step(db: Session, obj: models.UserMfaFactor, step: int) -> None:
    """Persist the accepted step while holding the factor row lock."""
    obj.last_used_step = step
    db.flush()


def recovery_codes(db: Session, user_id: UUID) -> list[models.UserMfaRecoveryCode]:
    """Read unused proofs; the factor lock serializes their use."""
    return list(
        db.scalars(
            select(models.UserMfaRecoveryCode).where(
                models.UserMfaRecoveryCode.user_id == user_id,
                models.UserMfaRecoveryCode.used_at.is_(None),
            )
        )
    )


def remaining(db: Session, user_id: UUID) -> int:
    """Count unused proofs without loading their hashes."""
    return int(
        db.scalar(
            select(func.count())
            .select_from(models.UserMfaRecoveryCode)
            .where(
                models.UserMfaRecoveryCode.user_id == user_id,
                models.UserMfaRecoveryCode.used_at.is_(None),
            )
        )
        or 0
    )


def use_recovery_code(
    db: Session, obj: models.UserMfaRecoveryCode, *, now: datetime
) -> None:
    """Consume one proof in the factor's transaction."""
    obj.used_at = now
    db.flush()


def replace_recovery_codes(db: Session, user_id: UUID, hashes: list[str]) -> None:
    """Invalidate the old set and store only hashes of the new set."""
    db.execute(
        delete(models.UserMfaRecoveryCode).where(
            models.UserMfaRecoveryCode.user_id == user_id
        )
    )
    db.add_all(
        models.UserMfaRecoveryCode(user_id=user_id, code_hash=value) for value in hashes
    )
    db.flush()


def remove(db: Session, user_id: UUID) -> None:
    """Remove the factor and recovery proofs under the user row lock."""
    db.execute(
        delete(models.UserMfaRecoveryCode).where(
            models.UserMfaRecoveryCode.user_id == user_id
        )
    )
    db.execute(
        delete(models.UserMfaFactor).where(models.UserMfaFactor.user_id == user_id)
    )


def end_non_sso_sessions(db: Session, user: models.User) -> int:
    """End non-SSO sessions and stage the existing live revocation notification."""
    from preloop.models.crud.auth_revocation import stage_revocation

    user.non_sso_auth_generation = (user.non_sso_auth_generation or 0) + 1
    stage_revocation(db, user_id=user.id, generation=user.non_sso_auth_generation)
    db.flush()
    return user.non_sso_auth_generation
