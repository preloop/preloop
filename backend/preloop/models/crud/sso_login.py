"""Atomic, account-scoped persistence for enterprise identity authentication.

These helpers do not choose roles or authorize SSO. The caller verifies the
provider assertion and tenant policy before passing proven identity claims.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.person import IdentityConflictError, crud_person
from preloop.models.crud.user import crud_user


def lock_subject(db: Session, *, provider: str, subject: str) -> None:
    """Serialize first sign-ins for a provider subject until the caller commits."""
    key = int.from_bytes(
        hashlib.sha256(f"oidc\0{provider}\0{subject}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


def resolve_identity(
    db: Session,
    *,
    account_id: uuid.UUID,
    provider: str,
    subject: str,
    email: str,
    email_verified: bool,
) -> tuple[models.Person | None, models.User | None]:
    """Link by provider proof or verified email of an existing tenant member.

    Email-only matches outside this account never establish identity. An
    ambiguous address fails closed, without touching another account.
    """
    person = crud_person.get_by_provider(
        db, source="oidc", provider=provider, subject=subject
    )
    linked_subject = person is not None
    if person is None:
        if not email_verified:
            raise IdentityConflictError("verified_email_required")
        candidates = list(
            db.scalars(
                select(models.User)
                .join(models.Person, models.Person.id == models.User.person_id)
                .where(
                    models.User.account_id == account_id,
                    models.Person.email_normalized == email.lower(),
                )
            )
        )
        if len(candidates) > 1:
            raise IdentityConflictError("identity_enrollment_required")
        if candidates:
            person = crud_person.get(db, id=candidates[0].person_id)
        elif crud_person.list_by_email(db, email=email):
            # Global verified-email uniqueness cannot safely represent a new
            # verified person at an address owned elsewhere. Do not auto-link.
            raise IdentityConflictError("identity_enrollment_required")
    if person is None:
        return None, None
    # Match canonical session/revocation lock order: person, then membership.
    person = db.scalar(
        select(models.Person)
        .where(models.Person.id == person.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if person is None or person.identity_status != "active":
        raise IdentityConflictError("identity_inactive")
    if not linked_subject and person.email_normalized != email.lower():
        raise IdentityConflictError("identity_enrollment_required")
    memberships = list(
        db.scalars(
            select(models.User)
            .where(
                models.User.account_id == account_id, models.User.person_id == person.id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    if len(memberships) > 1:
        raise IdentityConflictError("identity_enrollment_required")
    member = memberships[0] if memberships else None
    if member is not None and not member.is_active:
        raise IdentityConflictError("membership_inactive")
    return person, member


def create_membership(
    db: Session,
    *,
    account_id: uuid.UUID,
    person: models.Person | None,
    email: str,
    full_name: str | None,
) -> models.User:
    """Create only a destination membership; caller handles admission and roles."""
    fields = {
        "account_id": account_id,
        "username": f"sso-{uuid.uuid4().hex}",
        "email": email,
        "full_name": full_name,
        "is_active": True,
        "email_verified": True,
        "user_source": "oidc",
    }
    if person is not None:
        fields.update(
            person_id=person.id,
            email=person.email,
            full_name=person.full_name,
            email_verified=person.email_verified_at is not None,
        )
    return crud_user.create(db, obj_in=fields, commit=False)


def claim_test_proof(
    db: Session,
    *,
    connection_id: uuid.UUID,
    admin_id: uuid.UUID,
    revision: str,
    flow_id: str,
    expires_at: datetime,
) -> None:
    """Record successful current-admin authentication in the replay ledger."""
    from preloop.models.crud import crud_sso_replay_guard

    crud_sso_replay_guard.claim(
        db,
        connection_id=connection_id,
        guard_id=f"test:{admin_id}:{revision}:{flow_id}",
        expires_at=expires_at,
    )


def has_test_proof(
    db: Session,
    *,
    connection_id: uuid.UUID,
    account_id: uuid.UUID,
    admin_id: uuid.UUID,
    revision: str,
    now: datetime,
) -> bool:
    """True only for an unexpired proof of this admin and exact configuration."""
    prefix = f"test:{admin_id}:{revision}:"
    return (
        db.scalar(
            select(models.SsoReplayGuard.id)
            .join(
                models.AccountSsoConnection,
                models.AccountSsoConnection.id == models.SsoReplayGuard.connection_id,
            )
            .where(
                models.SsoReplayGuard.connection_id == connection_id,
                models.AccountSsoConnection.account_id == account_id,
                models.SsoReplayGuard.guard_id.startswith(prefix, autoescape=True),
                models.SsoReplayGuard.expires_at > now,
            )
            .limit(1)
        )
        is not None
    )


def lock_connection(
    db: Session, *, connection_id: uuid.UUID
) -> models.AccountSsoConnection | None:
    """Re-read and lock the connection until the caller finishes authentication."""
    return db.scalar(
        select(models.AccountSsoConnection)
        .where(models.AccountSsoConnection.id == connection_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def secret_revision(
    db: Session, *, account_id: uuid.UUID, secret_id: uuid.UUID | None
) -> str | None:
    """Read current non-secret revision even if an ORM row was cached earlier."""
    if secret_id is None:
        return None
    row = db.execute(
        select(models.SecretReference.updated_at, models.SecretReference.status).where(
            models.SecretReference.id == secret_id,
            models.SecretReference.account_id == account_id,
        )
    ).first()
    return f"{row.updated_at}:{row.status}" if row else None


def lock_account(db: Session, *, account_id: uuid.UUID) -> models.Account | None:
    """Refresh account activity under a lock through final session issuance."""
    return db.scalar(
        select(models.Account)
        .where(models.Account.id == account_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
