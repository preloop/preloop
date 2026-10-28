"""Core account-only SSO identity and administrator proof persistence."""

from datetime import timedelta

import pytest

from preloop.models.crud import (
    crud_account,
    crud_account_sso_connection,
    crud_person,
    crud_user,
)
from preloop.models.crud import sso_login
from preloop.models.crud.enterprise_sso import utc_now
from preloop.models.crud.person import IdentityConflictError

PROVIDER = "oidc:https://idp.example.test"


def test_email_only_link_requires_destination_membership(db_session, test_user):
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    with pytest.raises(IdentityConflictError, match="identity_enrollment_required"):
        sso_login.resolve_identity(
            db_session,
            account_id=other.id,
            provider=PROVIDER,
            subject="subject",
            email=test_user.email,
            email_verified=True,
        )
    assert (
        crud_person.get_by_provider(
            db_session, source="oidc", provider=PROVIDER, subject="subject"
        )
        is None
    )
    person, member = sso_login.resolve_identity(
        db_session,
        account_id=test_user.account_id,
        provider=PROVIDER,
        subject="subject",
        email=test_user.email,
        email_verified=True,
    )
    assert person.id == test_user.person_id and member.id == test_user.id


def test_provider_namespace_is_part_of_identity(db_session, test_user):
    crud_person.link_provider(
        db_session,
        person_id=test_user.person_id,
        source="oidc",
        provider=PROVIDER,
        subject="subject",
    )
    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other", "is_active": True}
    )
    person, member = sso_login.resolve_identity(
        db_session,
        account_id=other.id,
        provider=PROVIDER,
        subject="subject",
        email=test_user.email,
        email_verified=True,
    )
    assert person.id == test_user.person_id and member is None
    for provider, subject in (
        ("oidc:https://different.example", "subject"),
        (PROVIDER, "different-subject"),
    ):
        with pytest.raises(IdentityConflictError, match="identity_enrollment_required"):
            sso_login.resolve_identity(
                db_session,
                account_id=other.id,
                provider=provider,
                subject=subject,
                email=test_user.email,
                email_verified=True,
            )


def test_proof_scope_revision_and_expiration(db_session, test_user):
    connection = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=test_user.account_id,
        protocol="oidc",
        fields={"name": "Work"},
    )
    now = utc_now()
    sso_login.claim_test_proof(
        db_session,
        connection_id=connection.id,
        admin_id=test_user.id,
        revision="revision",
        flow_id="one",
        expires_at=now + timedelta(hours=24),
    )
    args = dict(
        connection_id=connection.id,
        account_id=test_user.account_id,
        admin_id=test_user.id,
        revision="revision",
        now=now,
    )
    assert sso_login.has_test_proof(db_session, **args)
    assert not sso_login.has_test_proof(db_session, **{**args, "revision": "changed"})
    assert not sso_login.has_test_proof(
        db_session, **{**args, "account_id": test_user.id}
    )
    assert not sso_login.has_test_proof(
        db_session, **{**args, "admin_id": test_user.account_id}
    )
    assert not sso_login.has_test_proof(
        db_session, **{**args, "now": now + timedelta(hours=24)}
    )


def test_empty_config_patch_invalidates_revision_for_secret_rotation(
    db_session, test_user
):
    connection = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=test_user.account_id,
        protocol="oidc",
        fields={"name": "Work"},
    )
    old = connection.updated_at
    crud_account_sso_connection.apply_update(
        db_session, connection=connection, fields={}
    )
    assert connection.updated_at != old


def test_membership_creation_does_not_grant_roles_or_change_other_account(
    db_session, test_user
):
    destination = crud_account.create(
        db_session, obj_in={"organization_name": "Destination", "is_active": True}
    )
    person = crud_person.get(db_session, id=test_user.person_id)
    member = sso_login.create_membership(
        db_session,
        account_id=destination.id,
        person=person,
        email=test_user.email,
        full_name=None,
    )
    assert member.person_id == test_user.person_id
    assert member.account_id == destination.id
    assert test_user.account_id != destination.id
    assert crud_user.get(db_session, id=test_user.id).is_active


def test_email_link_rechecks_canonical_address_after_lock(
    db_session, test_user, monkeypatch
):
    original = crud_person.get

    def changed_after_lookup(db, *, id):
        crud_person.update_profile(
            db,
            membership_id=test_user.id,
            changes={"email": "changed@example.test"},
            commit=False,
        )
        return original(db, id=id)

    monkeypatch.setattr(crud_person, "get", changed_after_lookup)
    with pytest.raises(IdentityConflictError, match="identity_enrollment_required"):
        sso_login.resolve_identity(
            db_session,
            account_id=test_user.account_id,
            provider=PROVIDER,
            subject="subject",
            email="test@example.com",
            email_verified=True,
        )
