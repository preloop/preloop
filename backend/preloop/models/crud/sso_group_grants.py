"""Transactional SSO/SCIM assignment provenance and tenant-safe validation.

Callers supply a complete desired snapshot per connection, member and source.
These helpers flush but never commit; login and SCIM own their transactions.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from preloop.models import models

from .permission import crud_role, crud_team_role


class GrantValidationError(ValueError):
    """An assignment would cross tenants or grant owner authority."""


@dataclass(frozen=True)
class GrantDelta:
    """Physical grants changed by one reconciliation (manual rows excluded)."""

    roles_added: tuple[UUID, ...] = ()
    roles_removed: tuple[UUID, ...] = ()
    teams_added: tuple[UUID, ...] = ()
    teams_removed: tuple[UUID, ...] = ()


def permission_names(db: Session, role: models.Role) -> frozenset[str]:
    """Return active permission names used for role comparison."""
    return frozenset(
        permission.name
        for permission in crud_role.get_permissions(db, role_id=role.id)
        if permission.is_active
    )


def validate_role(db: Session, *, account_id: UUID, role: models.Role) -> None:
    """Reject foreign/unowned roles and owner-only privileges.

    Billing management and account closure are documented owner-only system
    permissions. Catalog differences also protect future owner-only additions.
    """
    if (
        role.name.strip().lower() == "owner"
        or role.account_id not in (account_id, None)
        or (role.account_id is None and not role.is_system_role)
    ):
        raise GrantValidationError("unsafe_role")
    forbidden = {"manage_billing", "close_account"}
    owner = crud_role.get_by_name(db, name="owner")
    admin = crud_role.get_by_name(db, name="admin")
    if owner and admin and owner.is_system_role and admin.is_system_role:
        forbidden.update(permission_names(db, owner) - permission_names(db, admin))
    if forbidden & permission_names(db, role):
        raise GrantValidationError("owner_permissions")


def resolve_role(db: Session, *, account_id: UUID, name: str) -> models.Role:
    """Resolve a configured role; reserved member always means system viewer."""
    if name == "member":
        role = crud_role.get_by_name(db, name="viewer")
    else:
        role = crud_role.get_by_name(db, name=name, account_id=str(account_id))
        role = role or crud_role.get_by_name(db, name=name)
    if role is None:
        raise GrantValidationError("role_unavailable")
    validate_role(db, account_id=account_id, role=role)
    return role


def validate_team(db: Session, *, account_id: UUID, team_id: UUID) -> models.Team:
    """Validate team ownership and every inherited role before granting it."""
    team = db.get(models.Team, team_id)
    if team is None or team.account_id != account_id:
        raise GrantValidationError("team_unavailable")
    for role in crud_team_role.get_team_roles(db, team_id=team.id):
        validate_role(db, account_id=account_id, role=role)
    return team


def _remove_claims(
    db: Session, claims: list[models.SsoProvisioningGrant]
) -> GrantDelta:
    targets: set[tuple[str, UUID]] = set()
    for claim in claims:
        if claim.user_role_id is not None:
            targets.add(("role", claim.user_role_id))
        elif claim.team_membership_id is not None:
            targets.add(("team", claim.team_membership_id))
        db.delete(claim)
    db.flush()
    removed: dict[str, list[UUID]] = {"role": [], "team": []}
    for kind, target_id in sorted(targets):
        role_target = kind == "role"
        model = models.UserRole if role_target else models.TeamMembership
        column = (
            models.SsoProvisioningGrant.user_role_id
            if role_target
            else models.SsoProvisioningGrant.team_membership_id
        )
        # Refresh under a physical-row lock: a concurrent successful manual
        # team edit must be visible before deciding whether this row is managed.
        row = db.scalar(
            select(model)
            .where(model.id == target_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None or row.manually_granted:
            continue
        if db.execute(
            select(models.SsoProvisioningGrant.id).where(column == target_id)
        ).first():
            continue
        removed[kind].append(
            row.role_id if isinstance(row, models.UserRole) else row.team_id
        )
        db.delete(row)
    db.flush()
    return GrantDelta(
        roles_removed=tuple(sorted(removed["role"])),
        teams_removed=tuple(sorted(removed["team"])),
    )


def reconcile_grants(
    db: Session,
    *,
    account_id: UUID,
    connection_id: UUID,
    membership_id: UUID,
    source: str,
    role_ids: set[UUID] | frozenset[UUID] = frozenset(),
    team_ids: set[UUID] | frozenset[UUID] = frozenset(),
) -> GrantDelta:
    """Reconcile one complete source snapshot without touching manual grants.

    Lock order is connection then membership, matching normal SSO login. The
    member lock serializes different source/connection claims on shared rows.
    Validate all targets before writing any provenance or assignments.
    """
    connection = db.execute(
        select(models.AccountSsoConnection)
        .where(models.AccountSsoConnection.id == connection_id)
        .with_for_update()
    ).scalar_one_or_none()
    member = db.execute(
        select(models.User).where(models.User.id == membership_id).with_for_update()
    ).scalar_one_or_none()
    if (
        source not in {"oidc", "saml", "scim"}
        or connection is None
        or connection.account_id != account_id
        or member is None
        or member.account_id != account_id
    ):
        raise GrantValidationError("invalid_scope")
    for role_id in role_ids:
        role = db.get(models.Role, role_id)
        if role is None:
            raise GrantValidationError("role_unavailable")
        validate_role(db, account_id=account_id, role=role)
    for team_id in team_ids:
        validate_team(db, account_id=account_id, team_id=team_id)
    claims = list(
        db.scalars(
            select(models.SsoProvisioningGrant).where(
                models.SsoProvisioningGrant.connection_id == connection_id,
                models.SsoProvisioningGrant.user_id == membership_id,
                models.SsoProvisioningGrant.source == source,
            )
        )
    )
    stale = []
    existing: set[tuple[str, UUID]] = set()
    row: models.UserRole | models.TeamMembership | None
    for claim in claims:
        if claim.user_role_id:
            row = db.get(models.UserRole, claim.user_role_id)
            key = ("role", row.role_id) if row else None
            wanted = row is not None and row.role_id in role_ids
        else:
            row = db.get(models.TeamMembership, claim.team_membership_id)
            key = ("team", row.team_id) if row else None
            wanted = row is not None and row.team_id in team_ids
        if wanted and key is not None:
            existing.add(key)
        else:
            stale.append(claim)
    added: dict[str, list[UUID]] = {"role": [], "team": []}
    for kind, ids, model, column, fk in (
        ("role", role_ids, models.UserRole, "role_id", "user_role_id"),
        ("team", team_ids, models.TeamMembership, "team_id", "team_membership_id"),
    ):
        for target_id in sorted(ids):
            if (kind, target_id) in existing:
                continue
            created_id = db.execute(
                insert(model)
                .values(
                    user_id=membership_id,
                    **{column: target_id},
                    manually_granted=False,
                )
                .on_conflict_do_nothing()
                .returning(model.id)
            ).scalar_one_or_none()
            if created_id:
                added[kind].append(target_id)
            row_id = created_id or db.scalar(
                select(model.id).where(
                    model.user_id == membership_id,
                    getattr(model, column) == target_id,
                )
            )
            db.add(
                models.SsoProvisioningGrant(
                    connection_id=connection_id,
                    user_id=membership_id,
                    source=source,
                    **{fk: row_id},
                )
            )
    db.flush()
    removed = _remove_claims(db, stale)
    return GrantDelta(
        roles_added=tuple(added["role"]),
        teams_added=tuple(added["team"]),
        roles_removed=removed.roles_removed,
        teams_removed=removed.teams_removed,
    )


def revoke_connection_grants(
    db: Session, *, connection_id: UUID, account_id: UUID
) -> GrantDelta:
    """Remove a connection's claims before deletion, keeping other/manual claims."""
    connection = db.execute(
        select(models.AccountSsoConnection)
        .where(
            models.AccountSsoConnection.id == connection_id,
            models.AccountSsoConnection.account_id == account_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if connection is None:
        raise GrantValidationError("connection_unavailable")
    claims = list(
        db.scalars(
            select(models.SsoProvisioningGrant).where(
                models.SsoProvisioningGrant.connection_id == connection_id,
            )
        )
    )
    member_ids = sorted({claim.user_id for claim in claims})
    if member_ids:
        list(
            db.scalars(
                select(models.User)
                .where(models.User.id.in_(member_ids))
                .order_by(models.User.id)
                .with_for_update()
            )
        )
    return _remove_claims(db, claims)
