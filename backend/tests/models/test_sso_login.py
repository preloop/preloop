"""Core account-only SSO identity and administrator proof persistence."""

from datetime import timedelta

import pytest

from preloop.models import models

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
    batch = dict(
        account_id=test_user.account_id,
        admin_id=test_user.id,
        revisions={connection.id: "revision"},
        now=now,
    )
    assert sso_login.valid_test_proof_connections(db_session, **batch) == {
        connection.id
    }
    for overrides in (
        {"account_id": test_user.id},
        {"admin_id": test_user.account_id},
        {"revisions": {connection.id: "changed"}},
        {"now": now + timedelta(hours=24)},
    ):
        assert (
            sso_login.valid_test_proof_connections(db_session, **{**batch, **overrides})
            == set()
        )
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


def test_empty_config_patch_advances_connection_revision(db_session, test_user):
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


def test_unverified_member_cannot_preclaim_verified_idp_email(db_session, test_user):
    crud_person.update_profile(
        db_session,
        membership_id=test_user.id,
        changes={"email": "victim@example.test"},
        commit=False,
    )
    with pytest.raises(IdentityConflictError, match="identity_enrollment_required"):
        sso_login.resolve_identity(
            db_session,
            account_id=test_user.account_id,
            provider=PROVIDER,
            subject="victim-subject",
            email="victim@example.test",
            email_verified=True,
        )
    assert (
        crud_person.get_by_provider(
            db_session, source="oidc", provider=PROVIDER, subject="victim-subject"
        )
        is None
    )


def test_canonical_normalization_accepts_whitespace_and_case(db_session, test_user):
    person, member = sso_login.resolve_identity(
        db_session,
        account_id=test_user.account_id,
        provider=PROVIDER,
        subject="subject",
        email=" \tTEST@EXAMPLE.COM\r\n",
        email_verified=True,
    )
    assert person.id == test_user.person_id and member.id == test_user.id


def test_repaired_identity_cannot_restore_retired_provider_link(db_engine, monkeypatch):
    """An actual second transaction repairs after lookup, before login's lock."""
    from uuid import uuid4
    from sqlalchemy import delete
    from sqlalchemy.orm import Session
    from preloop.models import models

    suffix = uuid4().hex
    with Session(db_engine) as setup:
        account = crud_account.create(
            setup,
            obj_in={
                "organization_name": "Repair ordering " + suffix,
                "is_active": True,
            },
        )
        user = crud_user.create(
            setup,
            obj_in={
                "account_id": account.id,
                "username": "repair-" + suffix,
                "email": suffix + "@example.test",
                "email_verified": True,
                "is_active": True,
            },
        )
        crud_person.link_provider(
            setup,
            person_id=user.person_id,
            source="oidc",
            provider=PROVIDER,
            subject=suffix,
        )
        account_id, person_id, email = account.id, user.person_id, user.email
        person = crud_person.get(setup, id=person_id)
        person.identity_status = "quarantined"
        setup.commit()
    original = crud_person.get_by_provider
    try:
        with Session(db_engine) as login:

            def repair_between_lookup_and_lock(db, **kwargs):
                candidate = original(db, **kwargs)
                if db is login and candidate is not None:
                    with Session(db_engine) as repair:
                        crud_person.repair_conflict(
                            repair,
                            person_id=person_id,
                            username="repaired-" + suffix,
                            email="repaired-" + suffix + "@example.test",
                            hashed_password="synthetic-retired-password",
                            ownership_reference="synthetic-admin-review",
                            discard_credentials=True,
                        )
                        assert (
                            original(
                                repair, source="oidc", provider=PROVIDER, subject=suffix
                            )
                            is None
                        )
                return candidate

            monkeypatch.setattr(
                crud_person, "get_by_provider", repair_between_lookup_and_lock
            )
            sso_login.lock_subject(login, provider=PROVIDER, subject=suffix)
            with pytest.raises(
                IdentityConflictError, match="identity_enrollment_required"
            ):
                sso_login.resolve_identity(
                    login,
                    account_id=account_id,
                    provider=PROVIDER,
                    subject=suffix,
                    email=email,
                    email_verified=True,
                )
            login.rollback()
            assert (
                original(login, source="oidc", provider=PROVIDER, subject=suffix)
                is None
            )
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.execute(delete(models.Person).where(models.Person.id == person_id))
            cleanup.commit()


def test_secret_revision_scope_rotation_status_and_proof(db_session, test_user):
    from uuid import uuid4
    from preloop.models.crud.secret_reference import crud_secret_reference

    reference = crud_secret_reference.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "name": "Synthetic OIDC credential",
            "backend_type": "local_encrypted",
            "secret_kind": "sso_oidc_client_secret",
            "encrypted_value": "synthetic-unresolved-ciphertext",
            "status": "active",
        },
    )
    args = dict(account_id=test_user.account_id, secret_id=reference.id)
    original = sso_login.secret_revision(db_session, **args)
    assert original and original.endswith(":active")
    assert sso_login.secret_revisions(
        db_session, account_id=test_user.account_id, secret_ids={reference.id, uuid4()}
    ) == {reference.id: original}
    assert (
        sso_login.secret_revisions(
            db_session, account_id=uuid4(), secret_ids={reference.id}
        )
        == {}
    )
    assert (
        sso_login.secret_revision(db_session, **{**args, "account_id": uuid4()}) is None
    )
    assert (
        sso_login.secret_revision(db_session, **{**args, "secret_id": uuid4()}) is None
    )
    assert (
        sso_login.secret_revision(
            db_session, account_id=test_user.account_id, secret_id=None
        )
        is None
    )
    connection = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=test_user.account_id,
        protocol="oidc",
        fields={"name": "Secret proof"},
    )
    now = utc_now()
    proof = dict(
        connection_id=connection.id,
        account_id=test_user.account_id,
        admin_id=test_user.id,
        now=now,
    )
    sso_login.claim_test_proof(
        db_session,
        connection_id=connection.id,
        admin_id=test_user.id,
        revision=original,
        flow_id="proof",
        expires_at=now + timedelta(hours=24),
    )
    assert sso_login.has_test_proof(db_session, **proof, revision=original)
    crud_secret_reference.update(
        db_session,
        db_obj=reference,
        obj_in={"updated_at": reference.updated_at + timedelta(seconds=1)},
    )
    rotated = sso_login.secret_revision(db_session, **args)
    assert rotated != original
    assert not sso_login.has_test_proof(db_session, **proof, revision=rotated)
    # Force the unchanged timestamp into the UPDATE rather than allowing the
    # model's automatic onupdate timestamp to obscure the status-only case.
    from sqlalchemy.orm.attributes import flag_modified

    fixed_timestamp = reference.updated_at
    flag_modified(reference, "updated_at")
    crud_secret_reference.update(
        db_session,
        db_obj=reference,
        obj_in={"updated_at": fixed_timestamp, "status": "revoked"},
    )
    assert reference.updated_at == fixed_timestamp
    revoked = sso_login.secret_revision(db_session, **args)
    assert revoked != rotated and revoked.endswith(":revoked")
    assert not sso_login.has_test_proof(db_session, **proof, revision=revoked)


def test_email_verification_must_survive_person_lock(
    db_session, test_user, monkeypatch
):
    original = crud_person.get

    def unverify_after_lookup(db, *, id):
        person = original(db, id=id)
        person.email_verified_at = None
        db.flush()
        return person

    monkeypatch.setattr(crud_person, "get", unverify_after_lookup)
    with pytest.raises(IdentityConflictError, match="identity_enrollment_required"):
        sso_login.resolve_identity(
            db_session,
            account_id=test_user.account_id,
            provider=PROVIDER,
            subject="subject",
            email=test_user.email,
            email_verified=True,
        )


def test_saml_provider_namespace_and_membership_source(db_session, test_user):
    provider = "saml:urn:example:idp"
    crud_person.link_provider(
        db_session,
        person_id=test_user.person_id,
        source="saml",
        provider=provider,
        subject="employee",
    )
    person, member = sso_login.resolve_identity(
        db_session,
        account_id=test_user.account_id,
        provider=provider,
        subject="employee",
        email=test_user.email,
        email_verified=False,
        source="saml",
    )
    assert person.id == test_user.person_id and member.id == test_user.id
    destination = crud_account.create(
        db_session, obj_in={"organization_name": "SAML destination", "is_active": True}
    )
    created = sso_login.create_membership(
        db_session,
        account_id=destination.id,
        person=person,
        email=test_user.email,
        full_name=None,
        source="saml",
    )
    db_session.expire(created)
    assert created.user_source == models.UserSource.SAML
    assert created.account_id == destination.id and created.person_id == person.id
    with pytest.raises(IdentityConflictError, match="verified_email_required"):
        sso_login.resolve_identity(
            db_session,
            account_id=test_user.account_id,
            provider=provider,
            subject="employee",
            email=test_user.email,
            email_verified=False,
            source="oidc",
        )


def test_pending_saml_request_is_scoped_expiring_and_once(db_session, test_user):
    from uuid import uuid4
    from preloop.models.crud import crud_sso_replay_guard as guards

    connection = crud_account_sso_connection.create_for_account(
        db_session,
        account_id=test_user.account_id,
        protocol="saml",
        fields={"name": "SAML request"},
    )
    now = utc_now()
    guard = "pending:saml:browser:request"
    assert guards.claim(
        db_session,
        connection_id=connection.id,
        guard_id=guard,
        expires_at=now + timedelta(minutes=10),
    )
    assert not guards.consume_pending(db_session, connection_id=uuid4(), guard_id=guard)
    assert not guards.consume_pending(
        db_session,
        connection_id=connection.id,
        guard_id=guard,
        now=now + timedelta(minutes=11),
    )
    assert guards.consume_pending(
        db_session, connection_id=connection.id, guard_id=guard
    )
    assert not guards.consume_pending(
        db_session, connection_id=connection.id, guard_id=guard
    )
    with pytest.raises(ValueError):
        guards.consume_pending(
            db_session, connection_id=connection.id, guard_id="assertion:saml:used"
        )


def test_simultaneous_saml_request_consume_has_exactly_one_winner(db_engine):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import delete
    from sqlalchemy.orm import Session
    from preloop.models import models
    from preloop.models.crud import crud_sso_replay_guard as guards

    with Session(db_engine) as setup:
        account = crud_account.create(
            setup,
            obj_in={"organization_name": "SAML concurrency fixture", "is_active": True},
        )
        account_id = account.id
        connection = crud_account_sso_connection.create_for_account(
            setup,
            account_id=account_id,
            protocol="saml",
            fields={"name": "Concurrent requests"},
        )
        connection_id = connection.id
        guard = "pending:saml:browser:concurrent"
        guards.claim(
            setup,
            connection_id=connection_id,
            guard_id=guard,
            expires_at=utc_now() + timedelta(minutes=10),
        )
        setup.commit()
    barrier = Barrier(2)

    def consume():
        with Session(db_engine) as session:
            barrier.wait(timeout=10)
            result = guards.consume_pending(
                session, connection_id=connection_id, guard_id=guard
            )
            session.commit()
            return result

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(consume) for _ in range(2)]
            assert sorted(future.result(timeout=15) for future in futures) == [
                False,
                True,
            ]
    finally:
        with Session(db_engine) as cleanup:
            cleanup.execute(
                delete(models.Account).where(models.Account.id == account_id)
            )
            cleanup.commit()
