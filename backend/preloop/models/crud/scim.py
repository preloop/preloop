"""Connection-scoped SCIM persistence; callers own provisioning transactions."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable
from uuid import UUID, uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud.enterprise_sso import hash_scim_token, utc_now
from preloop.models.crud.person import IdentityConflictError, crud_person
from preloop.models.crud.user import crud_user
from preloop.models.crud import sso_login


class ScimScopeError(ValueError):
    """An object does not exist inside the exact provisioning connection."""


class ScimRateLimitError(ValueError):
    """A token has exhausted its durable one-minute request allowance."""


def lock_token(db: Session, *, token: str) -> models.ScimToken | None:
    """Recheck revocation under a lock, including a previously loaded token."""
    return db.scalar(
        select(models.ScimToken)
        .where(
            models.ScimToken.token_hash == hash_scim_token(token),
            models.ScimToken.revoked_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def authenticate(
    db: Session, *, token: str, consume: bool = True
) -> tuple[models.ScimToken, models.AccountSsoConnection] | None:
    """Lock token→connection→account, enforcing exact tenant and active scope.

    Rate admission is committed separately by the protocol handler, then this
    method is repeated without consumption to retain locks for the operation.
    No positive authentication cache is used.
    """
    row = lock_token(db, token=token)
    if row is None or row.connection_id is None:
        return None
    connection = sso_login.lock_connection(db, connection_id=row.connection_id)
    if (
        connection is None
        or connection.account_id != row.account_id
        or not connection.enabled
    ):
        return None
    account = sso_login.lock_account(db, account_id=row.account_id)
    if account is None or not account.is_active:
        return None
    if consume:
        now = utc_now()
        if row.rate_window_at is None or now - row.rate_window_at >= timedelta(
            minutes=1
        ):
            row.rate_window_at, row.rate_count = now, 0
        if row.rate_count >= 120:
            raise ScimRateLimitError()
        row.rate_count += 1
        row.last_used_at = now
        db.flush()
    return row, connection


def get_resource(
    db: Session,
    *,
    connection_id: UUID,
    kind: str,
    resource_id: UUID,
) -> models.ScimResource | None:
    """A foreign resource id is indistinguishable from a missing resource."""
    query = select(models.ScimResource).where(
        models.ScimResource.connection_id == connection_id,
        models.ScimResource.kind == kind,
        models.ScimResource.id == resource_id,
    )
    return db.scalar(query)


def list_resources(
    db: Session,
    *,
    connection_id: UUID,
    kind: str,
    name: str | None,
    start: int,
    count: int,
) -> tuple[int, list[models.ScimResource]]:
    """Return a bounded deterministic page and the filtered total."""
    conditions = [
        models.ScimResource.connection_id == connection_id,
        models.ScimResource.kind == kind,
    ]
    if name is not None:
        conditions.append(models.ScimResource.name_normalized == name.casefold())
    total = (
        db.scalar(
            select(func.count()).select_from(models.ScimResource).where(*conditions)
        )
        or 0
    )
    rows = list(
        db.scalars(
            select(models.ScimResource)
            .where(*conditions)
            .order_by(models.ScimResource.id)
            .offset(start - 1)
            .limit(count)
        )
    )
    return total, rows


def membership(
    db: Session,
    *,
    connection: models.AccountSsoConnection,
    resource: models.ScimResource,
) -> models.User:
    """Require both resource provenance and physical membership tenant."""
    if resource.connection_id != connection.id or resource.kind != "Users":
        raise ScimScopeError()
    user = db.scalar(
        select(models.User).where(
            models.User.id == resource.user_id,
            models.User.account_id == connection.account_id,
        )
    )
    if user is None:
        raise ScimScopeError()
    return user


def create_user(
    db: Session,
    *,
    connection: models.AccountSsoConnection,
    profile: dict[str, Any],
    email: str,
    admit_new_member: Callable[[], None] | None = None,
) -> models.ScimResource:
    """Enroll only a same-account member or a new credentialless Person.

    A global email match never grants membership. Profile updates after this
    enrollment only touch ScimResource, never the shared canonical identity.

    Args:
        db: Session; the caller owns the transaction.
        connection: Provisioning connection of the authenticated token.
        profile: Validated SCIM User profile.
        email: Normalized enrollment email.
        admit_new_member: Called before a new membership is created, so a
            seat policy can refuse it; linking an existing member skips it.

    Returns:
        The new User resource.

    Raises:
        IdentityConflictError: The email is ambiguous or belongs elsewhere.
    """
    candidates = list(
        db.scalars(
            select(models.User)
            .join(models.Person, models.Person.id == models.User.person_id)
            .where(
                models.User.account_id == connection.account_id,
                models.Person.email_normalized == email,
            )
        )
    )
    if len(candidates) > 1:
        raise IdentityConflictError("identity_enrollment_required")
    if candidates:
        user = candidates[0]
        # Do not turn an unverified, independently created account into an IdP
        # identity. Existing verified members may be explicitly provisioned.
        person = crud_person.get(db, id=user.person_id)
        if _is_pending_scim_member(db, user):
            # A deleted and re-provisioned member who never signed in.
            pass
        elif (
            person is None
            or person.email_verified_at is None
            or person.identity_status != "active"
        ):
            raise IdentityConflictError("identity_enrollment_required")
    else:
        if crud_person.list_by_email(db, email=email):
            raise IdentityConflictError("identity_enrollment_required")
        if admit_new_member is not None:
            admit_new_member()
        user = crud_user.create(
            db,
            obj_in={
                "account_id": connection.account_id,
                "username": f"scim-{uuid4().hex}",
                "email": email,
                "full_name": profile.get("displayName"),
                "is_active": profile["active"],
                "email_verified": False,
                "user_source": "scim",
            },
            commit=False,
        )
    row = models.ScimResource(
        connection_id=connection.id,
        kind="Users",
        user_id=user.id,
        name_normalized=profile["userName"].casefold(),
        external_id=profile.get("externalId") or None,
        profile=dict(profile),
    )
    db.add(row)
    db.flush()
    return row


def mapping_for_name(
    db: Session, *, connection: models.AccountSsoConnection, name: str
) -> models.SsoGroupMapping:
    """Only an existing, unambiguous same-account team mapping is provisionable."""
    rows = list(
        db.scalars(
            select(models.SsoGroupMapping)
            .join(models.Team, models.Team.id == models.SsoGroupMapping.team_id)
            .where(
                models.SsoGroupMapping.connection_id == connection.id,
                models.SsoGroupMapping.idp_group == name,
                models.Team.account_id == connection.account_id,
            )
        )
    )
    if len(rows) != 1:
        raise ScimScopeError()
    return rows[0]


def create_group(
    db: Session, *, connection: models.AccountSsoConnection, profile: dict[str, Any]
) -> models.ScimResource:
    """Create a resource handle for an existing team mapping, never a team."""
    mapping = mapping_for_name(db, connection=connection, name=profile["displayName"])
    row = models.ScimResource(
        connection_id=connection.id,
        kind="Groups",
        mapping_id=mapping.id,
        name_normalized=profile["displayName"].casefold(),
        external_id=profile.get("externalId") or None,
        profile={key: value for key, value in profile.items() if key != "members"},
    )
    db.add(row)
    db.flush()
    return row


def update_profile(
    db: Session,
    *,
    resource: models.ScimResource,
    profile: dict[str, Any],
    email: str | None = None,
) -> None:
    """Write the connection-local profile without mutating Person or siblings.

    Args:
        db: Session; the caller owns the transaction.
        resource: Resource to update.
        profile: Validated SCIM profile.
        email: Normalized provisioning email for a User. While the member is
            still pending (SCIM-created, never signed in, no other
            membership), the canonical email follows it so first login
            through the bound connection finds the member. Afterwards the
            canonical identity is never changed by SCIM.

    Raises:
        IdentityConflictError: The new email belongs to another person.
    """
    if resource.kind == "Users" and email is not None:
        user = db.get(models.User, resource.user_id)
        person = crud_person.get(db, id=user.person_id) if user else None
        if (
            user is not None
            and person is not None
            and person.email_normalized != email
            and _is_pending_scim_member(db, user)
        ):
            if crud_person.list_by_email(db, email=email):
                raise IdentityConflictError("identity_enrollment_required")
            crud_person.update_profile(
                db, membership_id=user.id, changes={"email": email}, commit=False
            )
    resource.profile = {
        key: value for key, value in profile.items() if key != "members"
    }
    resource.name_normalized = profile[
        "userName" if resource.kind == "Users" else "displayName"
    ].casefold()
    resource.external_id = profile.get("externalId") or None
    resource.updated_at = utc_now()
    db.flush()


def members(db: Session, *, group: models.ScimResource) -> list[models.ScimResource]:
    """List only undeleted users in this group's connection."""
    return list(
        db.scalars(
            select(models.ScimResource)
            .join(
                models.ScimGroupMember,
                models.ScimGroupMember.member_id == models.ScimResource.id,
            )
            .where(
                models.ScimGroupMember.group_id == group.id,
                models.ScimResource.connection_id == group.connection_id,
                models.ScimResource.kind == "Users",
            )
            .order_by(models.ScimResource.id)
        )
    )


def replace_members(
    db: Session, *, group: models.ScimResource, member_ids: set[UUID]
) -> list[models.ScimResource]:
    """Validate the complete set before replacing it; return all affected users."""
    desired = (
        list(
            db.scalars(
                select(models.ScimResource).where(
                    models.ScimResource.id.in_(member_ids),
                    models.ScimResource.connection_id == group.connection_id,
                    models.ScimResource.kind == "Users",
                )
            )
        )
        if member_ids
        else []
    )
    if len(desired) != len(member_ids):
        raise ScimScopeError()
    affected = {row.id: row for row in members(db, group=group)}
    affected.update({row.id: row for row in desired})
    db.execute(
        delete(models.ScimGroupMember).where(
            models.ScimGroupMember.group_id == group.id
        )
    )
    db.add_all(
        [models.ScimGroupMember(group_id=group.id, member_id=row.id) for row in desired]
    )
    db.flush()
    return sorted(affected.values(), key=lambda row: str(row.user_id))


def group_snapshot(db: Session, *, resource: models.ScimResource) -> list[str]:
    """Return every currently configured group claim for this user/source."""
    return list(
        db.scalars(
            select(models.SsoGroupMapping.idp_group)
            .join(
                models.ScimResource,
                models.ScimResource.mapping_id == models.SsoGroupMapping.id,
            )
            .join(
                models.ScimGroupMember,
                models.ScimGroupMember.group_id == models.ScimResource.id,
            )
            .where(
                models.ScimGroupMember.member_id == resource.id,
                models.ScimResource.connection_id == resource.connection_id,
                models.SsoGroupMapping.connection_id == resource.connection_id,
            )
        )
    )


def delete_resource(db: Session, *, resource: models.ScimResource) -> None:
    """Delete a resource and its desired group memberships.

    The membership itself is kept (offboarded by the caller), so the same
    userName or externalId can be provisioned again and links back to it.
    """
    db.execute(
        delete(models.ScimGroupMember).where(
            (models.ScimGroupMember.member_id == resource.id)
            | (models.ScimGroupMember.group_id == resource.id)
        )
    )
    db.delete(resource)
    db.flush()


def _is_pending_scim_member(db: Session, user: models.User) -> bool:
    """True for a SCIM-created membership whose person never signed in."""
    person = crud_person.get(db, id=user.person_id)
    return (
        person is not None
        and user.user_source == "scim"
        and person.email_verified_at is None
        and person.identity_status == "active"
        and len(crud_person.list_memberships(db, person_id=person.id)) == 1
        and not crud_person.list_password_hashes(db, person_id=person.id)
        and not crud_person.has_external_identity(db, person_id=person.id)
    )


def resolve_pending_identity(
    db: Session,
    *,
    account_id: UUID,
    connection_id: UUID,
    email: str,
    source: str,
    provider: str,
) -> models.Person | None:
    """Activate a pending SCIM identity only after proof from its exact IdP.

    The caller has already verified the assertion and email. A SCIM token alone
    never reaches this path. Existing unrelated unverified people and people
    with sibling memberships remain ineligible for implicit enrollment.
    """
    connection = db.scalar(
        select(models.AccountSsoConnection).where(
            models.AccountSsoConnection.id == connection_id,
            models.AccountSsoConnection.account_id == account_id,
            models.AccountSsoConnection.enabled.is_(True),
        )
    )
    if connection is None or connection.protocol != source:
        return None
    issuer = (
        connection.oidc_issuer if source == "oidc" else connection.saml_idp_entity_id
    )
    if provider != f"{source}:{issuer}":
        return None
    candidates = list(
        db.scalars(
            select(models.Person)
            .join(models.User, models.User.person_id == models.Person.id)
            .join(models.ScimResource, models.ScimResource.user_id == models.User.id)
            .where(
                models.ScimResource.connection_id == connection_id,
                models.ScimResource.kind == "Users",
                models.User.account_id == account_id,
                models.User.user_source == "scim",
                models.User.is_active.is_(True),
                models.Person.email_normalized == email,
                models.Person.email_verified_at.is_(None),
            )
        )
    )
    if len(candidates) != 1:
        return None
    person = db.scalar(
        select(models.Person)
        .where(models.Person.id == candidates[0].id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        person is None
        or person.identity_status != "active"
        or person.email_normalized != email
    ):
        return None
    if (
        len(crud_person.list_memberships(db, person_id=person.id)) != 1
        or crud_person.list_password_hashes(db, person_id=person.id)
        or crud_person.has_external_identity(db, person_id=person.id)
    ):
        return None
    crud_person.verify_email(
        db,
        person_id=person.id,
        expected_email=email,
        expected_generation=person.auth_generation,
        commit=False,
    )
    return person
