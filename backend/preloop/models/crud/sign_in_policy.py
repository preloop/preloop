"""Persistence for account sign-in policy; edition providers own decisions.

Mutations flush without committing so settings, revocation and audit are atomic.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_person, crud_user, mfa


def account(
    db: Session, account_id: UUID, *, lock: bool = False
) -> models.Account | None:
    """Read the policy's account, optionally serializing configuration writes."""
    query = select(models.Account).where(models.Account.id == account_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return db.scalars(query).first()


def members(db: Session, account_id: UUID) -> list[models.User]:
    """List memberships including suspended members for complete coverage."""
    return list(
        db.scalars(
            select(models.User)
            .where(models.User.account_id == account_id)
            .order_by(models.User.id)
        )
    )


def roles(db: Session, account_id: UUID) -> dict[UUID, set[str]]:
    """Return direct and inherited roles, constrained to this account."""
    rows = db.execute(
        select(models.UserRole.user_id, models.Role.name)
        .join(models.User, models.User.id == models.UserRole.user_id)
        .join(models.Role, models.Role.id == models.UserRole.role_id)
        .where(
            models.User.account_id == account_id,
            (models.Role.account_id == account_id)
            | (models.Role.account_id.is_(None) & models.Role.is_system_role.is_(True)),
        )
    )
    result: dict[UUID, set[str]] = {}
    for user_id, name in rows:
        result.setdefault(user_id, set()).add(name)
    inherited = db.execute(
        select(models.TeamMembership.user_id, models.Role.name)
        .join(models.User, models.User.id == models.TeamMembership.user_id)
        .join(models.Team, models.Team.id == models.TeamMembership.team_id)
        .join(models.TeamRole, models.TeamRole.team_id == models.Team.id)
        .join(models.Role, models.Role.id == models.TeamRole.role_id)
        .where(
            models.User.account_id == account_id,
            models.Team.account_id == account_id,
            (models.Role.account_id == account_id)
            | (models.Role.account_id.is_(None) & models.Role.is_system_role.is_(True)),
        )
    )
    for user_id, name in inherited:
        result.setdefault(user_id, set()).add(name)
    return result


def factor_coverage(db: Session, account_id: UUID) -> dict[UUID, dict[str, bool]]:
    """Read public factor presence in two batched queries, never secret material."""
    passkeys = set(
        db.scalars(
            select(models.User.id)
            .join(
                models.WebAuthnCredential,
                models.WebAuthnCredential.person_id == models.User.person_id,
            )
            .where(
                models.User.account_id == account_id,
                models.WebAuthnCredential.is_active.is_(True),
            )
        )
    )
    totp = set(
        db.scalars(
            select(models.UserMfaFactor.user_id)
            .join(models.User, models.User.id == models.UserMfaFactor.user_id)
            .where(
                models.User.account_id == account_id,
                models.UserMfaFactor.confirmed_at.is_not(None),
            )
        )
    )
    return {
        user_id: {"passkey": user_id in passkeys, "totp": user_id in totp}
        for user_id in passkeys | totp
    }


def update(
    db: Session,
    row: models.Account,
    *,
    policy: str,
    window_minutes: int,
    revoke_user_ids: list[UUID],
) -> None:
    """Persist a validated policy and revoke the selected non-SSO memberships."""
    row.strong_sign_in_policy = policy
    row.step_up_window_minutes = window_minutes
    for user_id in sorted(revoke_user_ids):
        user = crud_user.lock(db, user_id=user_id)
        if user is not None:
            mfa.end_non_sso_sessions(db, user)
    db.flush()


def reset_member(db: Session, user: models.User) -> None:
    """Clear human factors and end sessions under person then membership locks.

    Passkeys belong to a person, so all their memberships' sessions and local
    passkey-only flags must be cleared when a managed identity is reset.
    """
    crud_person.lock_for_session(db, membership_id=user.id)
    memberships = crud_person.list_memberships(db, person_id=user.person_id)
    for membership in sorted(memberships, key=lambda row: row.id):
        locked = crud_user.lock(db, user_id=membership.id)
        if locked is not None:
            crud_user.set_passkey_only(
                db, user=locked, enabled=False, end_sessions=False, commit=False
            )
            mfa.remove(db, locked.id)
            crud_user.bump_auth_generation(
                db, user_id=locked.id, commit=False, fanout=False
            )
            mfa.end_non_sso_sessions(db, locked)
    db.execute(
        delete(models.WebAuthnCredential).where(
            models.WebAuthnCredential.person_id == user.person_id
        )
    )
    db.flush()


def reset_policy(db: Session, account_id: UUID) -> dict[str, Any]:
    """Disable enforcement for server-side recovery; reject unknown accounts."""
    row = account(db, account_id, lock=True)
    if row is None:
        raise ValueError("Unknown account")
    previous = {
        "policy": row.strong_sign_in_policy,
        "step_up_window_minutes": row.step_up_window_minutes,
    }
    update(db, row, policy="off", window_minutes=0, revoke_user_ids=[])
    return previous
