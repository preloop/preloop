"""Connection-scoped SCIM persistence: tokens, admission and resource scope."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_scim_token,
    crud_sso_group_mapping,
    crud_team,
    scim,
)
from preloop.models.crud.enterprise_sso import CrossAccountReferenceError, utc_now


def _connection(
    db: Session, account_id: object, *, enabled: bool = True
) -> models.AccountSsoConnection:
    return crud_account_sso_connection.create_for_account(
        db,
        account_id=account_id,
        protocol="oidc",
        fields={
            "name": "Directory " + uuid4().hex[:6],
            "enabled": enabled,
            "oidc_issuer": "https://idp.example.com",
        },
    )


def _token(
    db: Session, account_id: object, connection_id: object | None
) -> tuple[models.ScimToken, str]:
    secret = "scim_test_" + uuid4().hex
    row = crud_scim_token.create_token(
        db,
        account_id=account_id,
        name="Directory",
        token=secret,
        prefix=secret[:12],
        connection_id=connection_id,
    )
    db.flush()
    return row, secret


def test_token_cannot_bind_another_accounts_connection(
    db_session: Session, test_user: models.User
) -> None:
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    foreign = _connection(db_session, other.id)
    with pytest.raises(CrossAccountReferenceError):
        _token(db_session, test_user.account_id, foreign.id)


def test_authenticate_requires_bound_enabled_unrevoked_token(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    row, secret = _token(db_session, test_user.account_id, connection.id)
    authenticated = scim.authenticate(db_session, token=secret)
    assert authenticated is not None and authenticated[1].id == connection.id
    assert row.rate_count == 1 and row.last_used_at is not None
    _, unbound = _token(db_session, test_user.account_id, None)
    assert scim.authenticate(db_session, token=unbound) is None
    disabled = _connection(db_session, test_user.account_id, enabled=False)
    _, off = _token(db_session, test_user.account_id, disabled.id)
    assert scim.authenticate(db_session, token=off) is None
    crud_scim_token.revoke(db_session, token=row)
    db_session.flush()
    assert scim.authenticate(db_session, token=secret) is None


def test_admission_is_limited_per_minute(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    row, secret = _token(db_session, test_user.account_id, connection.id)
    row.rate_window_at, row.rate_count = utc_now(), 120
    db_session.flush()
    with pytest.raises(scim.ScimRateLimitError):
        scim.authenticate(db_session, token=secret)
    assert scim.authenticate(db_session, token=secret, consume=False) is not None


def test_resources_are_scoped_to_their_connection(
    db_session: Session, test_user: models.User
) -> None:
    first = _connection(db_session, test_user.account_id)
    second = _connection(db_session, test_user.account_id)
    row = scim.create_user(
        db_session,
        connection=first,
        profile={"userName": "new.member@example.com", "active": True},
        email="new.member@example.com",
    )
    member = scim.membership(db_session, connection=first, resource=row)
    assert member.account_id == test_user.account_id
    assert member.user_source == "scim"
    assert (
        scim.get_resource(
            db_session, connection_id=second.id, kind="Users", resource_id=row.id
        )
        is None
    )
    with pytest.raises(scim.ScimScopeError):
        scim.membership(db_session, connection=second, resource=row)
    total, rows = scim.list_resources(
        db_session,
        connection_id=first.id,
        kind="Users",
        name="NEW.MEMBER@example.com",
        start=1,
        count=10,
    )
    assert total == 1 and rows[0].id == row.id
    scim.delete_resource(db_session, resource=row)
    assert (
        scim.get_resource(
            db_session, connection_id=first.id, kind="Users", resource_id=row.id
        )
        is None
    )
    again = scim.create_user(
        db_session,
        connection=first,
        profile={"userName": "new.member@example.com", "active": True},
        email="new.member@example.com",
    )
    assert again.id != row.id and again.user_id == member.id


PROVIDER = "oidc:https://idp.example.com"


def _pending(
    db: Session, connection: models.AccountSsoConnection, email: str
) -> models.User:
    row = scim.create_user(
        db,
        connection=connection,
        profile={"userName": email, "active": True},
        email=email,
    )
    return scim.membership(db, connection=connection, resource=row)


def _resolve(
    db: Session, connection: models.AccountSsoConnection, email: str, **overrides
) -> models.Person | None:
    arguments = dict(
        account_id=connection.account_id,
        connection_id=connection.id,
        email=email,
        source="oidc",
        provider=PROVIDER,
    )
    arguments.update(overrides)
    return scim.resolve_pending_identity(db, **arguments)


def test_pending_identity_activates_only_through_its_bound_connection(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    other = _connection(db_session, test_user.account_id)
    disabled = _connection(db_session, test_user.account_id, enabled=False)
    member = _pending(db_session, connection, "pending@example.com")
    assert _resolve(db_session, other, "pending@example.com") is None
    assert _resolve(db_session, disabled, "pending@example.com") is None
    assert (
        _resolve(
            db_session, connection, "pending@example.com", provider="oidc:https://x"
        )
        is None
    )
    assert (
        _resolve(db_session, connection, "pending@example.com", source="saml") is None
    )
    assert _resolve(db_session, connection, "other@example.com") is None
    person = _resolve(db_session, connection, "pending@example.com")
    assert person is not None and person.id == member.person_id
    assert person.email_verified_at is not None
    # Once verified it is no longer pending.
    assert _resolve(db_session, connection, "pending@example.com") is None


def test_pending_identity_refuses_people_with_credentials_or_siblings(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    member = _pending(db_session, connection, "sibling@example.com")
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Sibling", "is_active": True}
    )
    db_session.add(
        models.User(
            account_id=other.id,
            person_id=member.person_id,
            username="sibling-" + uuid4().hex[:6],
            email="sibling@example.com",
        )
    )
    db_session.flush()
    assert _resolve(db_session, connection, "sibling@example.com") is None
    keyed = _pending(db_session, connection, "keyed@example.com")
    person = db_session.get(models.Person, keyed.person_id)
    person.hashed_password = "not-a-real-hash"
    db_session.flush()
    assert _resolve(db_session, connection, "keyed@example.com") is None


def test_pending_email_change_follows_until_first_login(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    member = _pending(db_session, connection, "before@example.com")
    row = db_session.scalars(
        select(models.ScimResource).where(models.ScimResource.user_id == member.id)
    ).one()
    profile = {"userName": "after@example.com", "active": True}
    scim.update_profile(
        db_session, resource=row, profile=profile, email="after@example.com"
    )
    person = db_session.get(models.Person, member.person_id)
    assert person.email_normalized == "after@example.com"
    assert _resolve(db_session, connection, "after@example.com") is not None
    scim.update_profile(
        db_session, resource=row, profile=profile, email="later@example.com"
    )
    db_session.refresh(person)
    assert person.email_normalized == "after@example.com"


def _mapping(
    db: Session, connection: models.AccountSsoConnection, group: str
) -> models.SsoGroupMapping:
    team = crud_team.create(
        db,
        obj_in={"account_id": connection.account_id, "name": group + uuid4().hex[:4]},
    )
    return crud_sso_group_mapping.create_mapping(
        db,
        account_id=connection.account_id,
        connection_id=connection.id,
        idp_group=group,
        team_id=team.id,
    )


def test_groups_need_one_team_mapping_and_scope_members(
    db_session: Session, test_user: models.User
) -> None:
    connection = _connection(db_session, test_user.account_id)
    other = _connection(db_session, test_user.account_id)
    with pytest.raises(scim.ScimScopeError):
        scim.create_group(
            db_session, connection=connection, profile={"displayName": "x"}
        )
    crud_sso_group_mapping.create_mapping(
        db_session,
        account_id=connection.account_id,
        connection_id=connection.id,
        idp_group="role-only",
        role="member",
    )
    with pytest.raises(scim.ScimScopeError):
        scim.create_group(
            db_session, connection=connection, profile={"displayName": "role-only"}
        )
    _mapping(db_session, connection, "eng")
    group = scim.create_group(
        db_session, connection=connection, profile={"displayName": "eng"}
    )
    first = db_session.scalars(
        select(models.ScimResource).where(
            models.ScimResource.user_id
            == _pending(db_session, connection, "a@example.com").id
        )
    ).one()
    second = db_session.scalars(
        select(models.ScimResource).where(
            models.ScimResource.user_id
            == _pending(db_session, connection, "b@example.com").id
        )
    ).one()
    foreign = db_session.scalars(
        select(models.ScimResource).where(
            models.ScimResource.user_id
            == _pending(db_session, other, "c@example.com").id
        )
    ).one()
    affected = scim.replace_members(db_session, group=group, member_ids={first.id})
    assert [row.id for row in affected] == [first.id]
    assert scim.group_snapshot(db_session, resource=first) == ["eng"]
    with pytest.raises(scim.ScimScopeError):
        scim.replace_members(
            db_session, group=group, member_ids={second.id, foreign.id}
        )
    affected = scim.replace_members(db_session, group=group, member_ids={second.id})
    assert {row.id for row in affected} == {first.id, second.id}
    assert scim.group_snapshot(db_session, resource=first) == []
    assert [row.id for row in scim.members(db_session, group=group)] == [second.id]
    assert scim.replace_members(db_session, group=group, member_ids=set()) != []
    scim.replace_members(db_session, group=group, member_ids={second.id})
    scim.delete_resource(db_session, resource=group)
    assert scim.group_snapshot(db_session, resource=second) == []
