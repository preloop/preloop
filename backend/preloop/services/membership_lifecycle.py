"""Shared console/SCIM offboarding transaction and audit contract.

Both entry points stage their writes in the caller's session. ``commit=False``
(the default) flushes so a SCIM or console caller can finish one transaction;
``commit=True`` commits here. Any failure rolls the session back, so a partial
revocation is never persisted.
"""

from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_audit_log, crud_user
from preloop.models.crud.membership_lifecycle import OffboardingResult, offboard_record
from preloop.services.multiuser import membership_module


def offboard_membership(
    db: Session,
    *,
    account_id: UUID,
    user_id: UUID,
    reason: str,
    actor: models.User | None = None,
    commit: bool = False,
) -> OffboardingResult:
    """Revoke every access path of one membership in one transaction.

    Callers authorize the actor before entry. SCIM passes ``actor=None`` after
    validating its account-scoped token.

    Args:
        db: Session the revocation is staged in.
        account_id: Account that owns the membership.
        user_id: Membership (``User`` row) to offboard.
        reason: Audit reason, 1 to 500 characters.
        actor: Member of ``account_id`` (or a platform superuser) acting, if any.
        commit: Commit here instead of leaving the transaction to the caller.

    Returns:
        Counts of what this call revoked and the preserved machine key ids.

    Raises:
        ValueError: The membership is not in the account, the actor belongs
            to another account, or the reason is empty or too long.
    """
    if actor is not None and actor.account_id != account_id and not actor.is_superuser:
        raise ValueError("Actor does not belong to account")
    if not reason.strip() or len(reason) > 500:
        raise ValueError("Offboarding reason must contain 1 to 500 characters")
    try:
        result = offboard_record(
            db, account_id=account_id, user_id=user_id, reason=reason
        )
        if not result.already_offboarded:
            user = crud_user.get(db, id=user_id, account_id=str(account_id))
            # The account hierarchy overlay only repairs the person's
            # preferred-membership pointers; it moves no credentials, so it
            # is safe after revocation and must skip this inactive row.
            membership = membership_module()
            if membership is not None:
                membership.release_membership(db, user)
            crud_audit_log.log_action(
                db,
                account_id=account_id,
                user_id=actor.id if actor else None,
                action="access.revoked",
                resource_type="user",
                resource_id=str(user_id),
                status="success",
                details={
                    "reason": reason,
                    "counts": result.counts,
                    "preserved_machine_key_ids": [
                        str(key_id) for key_id in result.preserved_machine_key_ids
                    ],
                },
                commit=False,
            )
        if commit:
            db.commit()
        else:
            db.flush()
        return result
    except Exception:
        db.rollback()
        raise


def reactivate_membership(
    db: Session,
    *,
    account_id: UUID,
    user_id: UUID,
    actor: models.User | None = None,
    commit: bool = False,
) -> models.User:
    """Re-enable the membership only; revoked credentials stay revoked.

    Args:
        db: Session the change is staged in.
        account_id: Account that owns the membership.
        user_id: Membership to reactivate.
        actor: Member of ``account_id`` (or a platform superuser) acting, if any.
        commit: Commit here instead of leaving the transaction to the caller.

    Returns:
        The reactivated membership.

    Raises:
        ValueError: The membership, its account or its person is unavailable,
            or the actor belongs to another account.
    """
    from preloop.models.crud.membership_lifecycle import lock_for_reactivation
    from preloop.services.multiuser import enforce_seat_available

    if actor is not None and actor.account_id != account_id and not actor.is_superuser:
        raise ValueError("Actor does not belong to account")
    try:
        user = lock_for_reactivation(db, account_id=account_id, user_id=user_id)
        if not user.is_active:
            enforce_seat_available(db, account_id)
            crud_user.update(db, db_obj=user, obj_in={"is_active": True}, commit=False)
            # The marker means "offboarded and not since reactivated". Leaving
            # it set would turn a later removal into a no-op and leave keys
            # issued after the return active.
            user.offboarded_at = None
            crud_audit_log.log_action(
                db,
                account_id=account_id,
                user_id=actor.id if actor else None,
                action="access.restored",
                resource_type="user",
                resource_id=str(user_id),
                status="success",
                details={"credentials_restored": 0},
                commit=False,
            )
        if commit:
            db.commit()
        else:
            db.flush()
        return user
    except Exception:
        db.rollback()
        raise
