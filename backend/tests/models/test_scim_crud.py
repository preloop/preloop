"""Connection-scoped SCIM persistence: tokens, admission and resource scope."""

from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_scim_token,
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
