"""Canonical identity authentication with independent account memberships."""

import uuid
from datetime import UTC, datetime, timedelta

from types import SimpleNamespace

import jwt
import pytest
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy.orm import Session
from starlette.requests import Request

from preloop.api.auth import email_verification
from preloop.api.auth.identity import login_membership, require_active_identity
from preloop.api.auth.jwt import (
    create_access_token,
    decode_token,
    get_password_hash,
    reject_revoked_token,
    verify_password,
)
from preloop.api.auth.router import (
    _authenticate_user_sync,
    _person_for_token,
    OnboardingRequest,
    complete_onboarding,
    forgot_password,
    resend_verification,
    reset_password,
    verify_email,
)
from preloop.api.auth.webauthn_router import (
    AuthenticationVerifyRequest,
    RegistrationVerifyRequest,
    _issue_challenge_token,
    authentication_verify,
    delete_credential,
    list_credentials,
    registration_verify,
)
from preloop.config import settings
from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_person,
    crud_user,
    crud_webauthn_credential,
)
from preloop.schemas.auth import (
    EmailVerificationRequest,
    EmailVerificationResendRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
)
from preloop.utils.tokens import (
    ALGORITHM,
    SECRET_KEY,
    TokenError,
    create_email_verification_token,
    create_password_reset_token,
    create_onboarding_claim_token,
    hash_onboarding_claim_token,
    verify_user_token,
)
from preloop.utils.email import EmailError

PASSWORD = "initial-password-example"
NEW_PASSWORD = "recovered-password-example"


@pytest.mark.asyncio
@pytest.mark.parametrize("purpose", ["verification", "recovery"])
async def test_mail_failure_does_not_abort_another_identity(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, purpose: str
) -> None:
    """One SMTP outage cannot drop the next canonical identity's bound message."""
    first, _child = memberships(db_session)
    second, _other_child = memberships(db_session)
    crud_person.update_profile(
        db_session, membership_id=second.id, changes={"email": first.email}
    )
    attempted: list[str] = []

    def sender(*, user_email: str, token: str, username: str) -> None:
        attempted.append(username)
        if len(attempted) == 1:
            raise EmailError("synthetic transient SMTP failure")

    tasks = BackgroundTasks()
    if purpose == "verification":
        monkeypatch.setattr("preloop.api.auth.router.send_verification_email", sender)
        resend_verification(
            EmailVerificationResendRequest(email=first.email),
            tasks,
            request(),
            db_session,
        )
    else:
        monkeypatch.setattr("preloop.api.auth.router.send_password_reset_email", sender)
        await forgot_password(
            PasswordResetRequest(email=first.email), tasks, request(), db_session
        )
    await tasks()
    assert set(attempted) == {first.username, second.username}


def request() -> Request:
    """An anonymous mail request with a synthetic client address."""
    return Request({"type": "http", "headers": [], "client": ("127.0.0.8", 1234)})


def memberships(
    db: Session, *, verified: bool = False
) -> tuple[models.User, models.User]:
    """Create one explicit person link and a credential-free child membership."""
    suffix = uuid.uuid4().hex
    parent = crud_account.create(
        db, obj_in={"organization_name": "Parent", "is_active": True}
    )
    source = crud_user.create(
        db,
        obj_in={
            "account_id": parent.id,
            "username": f"chosen-{suffix}",
            "email": f"person-{suffix}@example.com",
            "email_verified": verified,
            "is_active": True,
            "hashed_password": get_password_hash(PASSWORD),
            "user_source": "local",
        },
    )
    child = crud_account.create(
        db, obj_in={"organization_name": "Child", "is_active": True}
    )
    grant = models.AccountAccessGrant(
        parent_account_id=parent.id,
        subject_type="user",
        subject_id=source.id,
        access_level="read",
        target_mode="all",
        created_by=source.id,
    )
    db.add(grant)
    db.flush()
    inherited = crud_user.create(
        db,
        obj_in={
            "account_id": child.id,
            "person_id": source.person_id,
            "username": f"generated-membership-{suffix}",
            "email": source.email,
            "email_verified": False,
            "is_active": True,
            "hashed_password": None,
            "user_source": "local",
            "membership_kind": "inherited",
            "access_grant_id": grant.id,
        },
    )
    return source, inherited


@pytest.fixture(autouse=True)
def clean_limits() -> None:
    email_verification.reset_resend_rate_limit()


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_denies", [False, True])
async def test_one_provisional_person_one_mail_one_verification(
    db_session: Session, monkeypatch: pytest.MonkeyPatch, policy_denies: bool
) -> None:
    source, child = memberships(db_session)
    monkeypatch.setattr(settings, "require_email_verification", True)
    tasks = BackgroundTasks()
    resend_verification(
        EmailVerificationResendRequest(email=source.email), tasks, request(), db_session
    )
    assert len(tasks.tasks) == 1
    mail = tasks.tasks[0].kwargs
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    assert mail["username"] == person.username == source.username
    assert verify_user_token(mail["token"], "email_verification").person_id == person.id
    if policy_denies:
        from unittest.mock import Mock
        from preloop.services import authentication_policy

        gate = Mock()
        gate.authorize.side_effect = HTTPException(403, "SSO required")
        monkeypatch.setattr(
            authentication_policy,
            "get_plugin_manager",
            lambda: Mock(get_service=Mock(return_value=gate)),
        )
        with pytest.raises(HTTPException, match="SSO required"):
            await verify_email(
                EmailVerificationRequest(token=mail["token"]), db_session
            )
        assert gate.authorize.call_args.kwargs["claims"]["amr"] == ["email_link"]
        return
    result = await verify_email(
        EmailVerificationRequest(token=mail["token"]), db_session
    )
    assert result["access_token"]
    assert decode_token(result["access_token"]).claims["amr"] == ["email_link"]
    assert decode_token(result["refresh_token"]).claims["amr"] == ["email_link"]
    for member in (source, child):
        require_active_identity(db_session, member)
        email_verification.enforce_verified_email(member, db_session)
    assert _authenticate_user_sync(source.username, PASSWORD, db_session) is not None
    assert _authenticate_user_sync(child.username, PASSWORD, db_session) is None
    with pytest.raises(HTTPException) as replay:
        await verify_email(EmailVerificationRequest(token=mail["token"]), db_session)
    assert replay.value.status_code == 400


@pytest.mark.asyncio
async def test_recovery_replaces_one_owner_revokes_every_membership(
    db_session: Session,
) -> None:
    source, child = memberships(db_session, verified=True)
    old_tokens = [
        decode_token(
            create_access_token(
                data={"sub": str(row.id), "scopes": []},
                auth_generation=row.auth_generation,
            )
        )
        for row in (source, child)
    ]
    tasks = BackgroundTasks()
    await forgot_password(
        PasswordResetRequest(email=source.email), tasks, request(), db_session
    )
    assert len(tasks.tasks) == 1
    token = tasks.tasks[0].kwargs["token"]
    await reset_password(
        PasswordResetConfirmRequest(token=token, new_password=NEW_PASSWORD), db_session
    )
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    assert verify_password(NEW_PASSWORD, person.hashed_password)
    assert source.hashed_password is None and child.hashed_password is None
    for row, old in zip((source, child), old_tokens, strict=True):
        with pytest.raises(HTTPException):
            reject_revoked_token(db_session, row, old)
    assert _authenticate_user_sync(source.username, NEW_PASSWORD, db_session)
    assert _authenticate_user_sync(source.username, PASSWORD, db_session) is None
    with pytest.raises(HTTPException):
        await reset_password(
            PasswordResetConfirmRequest(token=token, new_password=PASSWORD), db_session
        )


def test_equal_provisional_addresses_never_deduplicate_or_merge(
    db_session: Session,
) -> None:
    source, _ = memberships(db_session)
    other, _ = memberships(db_session)
    other = crud_user.update(db_session, db_obj=other, obj_in={"email": source.email})
    crud_person.update_profile(
        db_session, membership_id=other.id, changes={"email": source.email}
    )
    tasks = BackgroundTasks()
    resend_verification(
        EmailVerificationResendRequest(email=source.email), tasks, request(), db_session
    )
    assert len(tasks.tasks) == 2
    ids = {
        verify_user_token(task.kwargs["token"], "email_verification").person_id
        for task in tasks.tasks
    }
    assert ids == {source.person_id, other.person_id}


@pytest.mark.parametrize("purpose", ["email_verification", "password_reset"])
def test_tokens_reject_changed_email_wrong_person_and_expiry(
    db_session: Session, purpose: str
) -> None:
    source, _ = memberships(db_session)
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    mint = (
        create_email_verification_token
        if purpose == "email_verification"
        else create_password_reset_token
    )
    token = mint(
        person.email, person_id=person.id, auth_generation=person.auth_generation
    )
    crud_person.update_profile(
        db_session,
        membership_id=source.id,
        changes={"email": f"changed-{uuid.uuid4().hex}@example.com"},
    )
    with pytest.raises(TokenError):
        _person_for_token(db_session, token, purpose)
    expired = jwt.encode(
        {
            "pid": str(person.id),
            "sub": person.email,
            "type": purpose,
            "gen": person.auth_generation,
            "exp": datetime.now(UTC) - timedelta(seconds=1),
        },
        SECRET_KEY,
        algorithm=ALGORITHM,
    )
    with pytest.raises(TokenError):
        _person_for_token(db_session, expired, purpose)
    mismatched = mint(
        person.email,
        person_id=uuid.uuid4(),
        user_id=source.id,
        auth_generation=person.auth_generation,
    )
    with pytest.raises(TokenError):
        _person_for_token(db_session, mismatched, purpose)


def test_legacy_row_link_is_same_person_only_before_security_change(
    db_session: Session,
) -> None:
    source, child = memberships(db_session)
    token = create_email_verification_token(source.email, user_id=child.id)
    assert (
        _person_for_token(db_session, token, "email_verification").id
        == source.person_id
    )
    crud_person.revoke_sessions(db_session, person_id=source.person_id)
    with pytest.raises(TokenError):
        _person_for_token(db_session, token, "email_verification")


def test_person_suspension_and_membership_revocation_are_independent(
    db_session: Session,
) -> None:
    source, child = memberships(db_session, verified=True)
    crud_user.update(db_session, db_obj=child, obj_in={"is_active": False})
    require_active_identity(db_session, source)
    with pytest.raises(HTTPException):
        require_active_identity(db_session, child)
    person = crud_person.set_status(
        db_session, person_id=source.person_id, status="suspended"
    )
    assert login_membership(db_session, person) is None
    with pytest.raises(HTTPException):
        require_active_identity(db_session, source)
    assert _authenticate_user_sync(source.username, PASSWORD, db_session) is None


def test_passkey_owner_survives_registration_membership_removal(
    db_session: Session,
) -> None:
    source, child = memberships(db_session, verified=True)
    cred = crud_webauthn_credential.create(
        db_session,
        obj_in={
            "person_id": source.person_id,
            "user_id": child.id,
            "credential_id": uuid.uuid4().hex,
            "public_key": "cHVibGlj",
            "sign_count": 0,
            "name": "Shared passkey",
            "is_active": True,
        },
    )
    assert [item.id for item in list_credentials(source, db_session)] == [cred.id]
    crud_user.hard_delete(db_session, user_id=child.id)
    db_session.expire_all()
    assert [item.id for item in list_credentials(source, db_session)] == [cred.id]
    other, _ = memberships(db_session, verified=True)
    with pytest.raises(HTTPException):
        delete_credential(cred.id, other, db_session)
    delete_credential(cred.id, source, db_session)
    assert list_credentials(source, db_session) == []


@pytest.mark.asyncio
async def test_reset_survives_last_membership_removal(db_session: Session) -> None:
    source, child = memberships(db_session, verified=True)
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    token = create_password_reset_token(
        person.email, person_id=person.id, auth_generation=person.auth_generation
    )
    crud_user.hard_delete(db_session, user_id=child.id)
    crud_user.hard_delete(db_session, user_id=source.id)
    await reset_password(
        PasswordResetConfirmRequest(token=token, new_password=NEW_PASSWORD), db_session
    )
    person = crud_person.get(db_session, id=person.id)
    assert verify_password(NEW_PASSWORD, person.hashed_password)
    assert login_membership(db_session, person) is None


def test_passkey_signin_survives_origin_membership_removal(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, child = memberships(db_session, verified=True)
    cred = crud_webauthn_credential.create(
        db_session,
        obj_in={
            "person_id": source.person_id,
            "user_id": child.id,
            "credential_id": "Y3JlZGVudGlhbA",
            "public_key": "cHVibGlj",
            "sign_count": 0,
            "name": "Retained passkey",
            "is_active": True,
        },
    )
    crud_user.hard_delete(db_session, user_id=child.id)
    monkeypatch.setattr(
        "preloop.api.auth.webauthn_router.verify_authentication_response",
        lambda **kwargs: SimpleNamespace(new_sign_count=1),
    )
    req = Request(
        {
            "type": "http",
            "headers": [],
            "client": ("testclient", 1234),
            "scheme": "https",
            "server": ("example.com", 443),
            "path": "/auth",
        }
    )
    result = authentication_verify(
        AuthenticationVerifyRequest(
            credential={"id": cred.credential_id, "rawId": cred.credential_id},
            challenge_token=_issue_challenge_token(b"challenge", "authenticate"),
        ),
        req,
        db_session,
    )
    assert decode_token(result["access_token"]).sub == str(source.id)
    crud_person.set_status(db_session, person_id=source.person_id, status="suspended")
    with pytest.raises(HTTPException):
        authentication_verify(
            AuthenticationVerifyRequest(
                credential={"id": cred.credential_id, "rawId": cred.credential_id},
                challenge_token=_issue_challenge_token(b"second", "authenticate"),
            ),
            req,
            db_session,
        )


def test_login_selection_prefers_history_then_primary_over_iteration_order(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal creation timestamps cannot change the default landing membership."""
    source, child = memberships(db_session, verified=True)
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    monkeypatch.setattr(
        crud_person, "list_memberships", lambda db, *, person_id: [child, source]
    )
    assert login_membership(db_session, person).id == source.id
    person.last_active_user_id = child.id
    assert login_membership(db_session, person).id == child.id
    child.is_active = False
    assert login_membership(db_session, person).id == source.id


def test_password_reset_invalidates_outstanding_passkey_registration(
    db_session: Session,
) -> None:
    """A ceremony started before recovery cannot install a credential afterward."""
    source, child = memberships(db_session, verified=True)
    person = crud_person.resolve_for_membership(db_session, membership_id=source.id)
    challenge = _issue_challenge_token(
        b"synthetic-registration",
        "register",
        str(person.id),
        auth_generation=person.auth_generation,
    )
    crud_person.set_password(
        db_session,
        person_id=person.id,
        hashed_password=get_password_hash(NEW_PASSWORD),
    )
    with pytest.raises(HTTPException) as denial:
        registration_verify(
            RegistrationVerifyRequest(challenge_token=challenge, credential={}),
            request(),
            child,
            db_session,
        )
    assert denial.value.status_code == 400
    assert (
        crud_webauthn_credential.list_for_person(db_session, person_id=person.id) == []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("policy_denies", [False, True])
async def test_onboarding_claim_consumption_and_canonical_password_are_atomic(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    policy_denies: bool,
) -> None:
    """A valid claim sets one canonical password and cannot be replayed."""
    source, child = memberships(db_session)
    person = crud_person.set_password(
        db_session, person_id=source.person_id, hashed_password="NEEDS_RESET"
    )
    token = create_onboarding_claim_token(
        email=person.email,
        account_id=str(source.account_id),
        checkout_session_id="synthetic-checkout",
    )
    crud_user.update(
        db_session,
        db_obj=source,
        obj_in={"onboarding_claim_hash": hash_onboarding_claim_token(token)},
    )
    body = OnboardingRequest(
        email=person.email,
        username=f"claimed-{uuid.uuid4().hex}",
        password=NEW_PASSWORD,
        full_name="Claimed Person",
        claim_token=token,
    )
    before = (
        source.onboarding_claim_hash,
        person.hashed_password,
        person.username,
        person.email_verified_at,
        person.auth_generation,
    )
    if policy_denies:
        from unittest.mock import Mock
        from preloop.services import authentication_policy

        gate = Mock()
        gate.authorize.side_effect = HTTPException(403, "SSO required")
        monkeypatch.setattr(
            authentication_policy,
            "get_plugin_manager",
            lambda: Mock(get_service=Mock(return_value=gate)),
        )
        with pytest.raises(HTTPException, match="SSO required"):
            await complete_onboarding(body, db_session)
        assert gate.authorize.call_args.kwargs["claims"]["amr"] == ["password"]
        db_session.refresh(source)
        db_session.refresh(person)
        assert (
            source.onboarding_claim_hash,
            person.hashed_password,
            person.username,
            person.email_verified_at,
            person.auth_generation,
        ) == before
        gate.authorize.side_effect = None
    result = await complete_onboarding(body, db_session)
    assert result["access_token"] and result["refresh_token"]
    assert decode_token(result["access_token"]).claims["amr"] == ["password"]
    assert decode_token(result["refresh_token"]).claims["amr"] == ["password"]
    db_session.refresh(source)
    assert source.onboarding_claim_hash is None
    person = crud_person.credential_owner(db_session, membership_id=child.id)
    assert person.username == body.username and person.full_name == "Claimed Person"
    assert person.email_verified_at is not None
    assert verify_password(NEW_PASSWORD, person.hashed_password)
    assert child.hashed_password is None
    with pytest.raises(HTTPException) as replay:
        await complete_onboarding(body, db_session)
    assert replay.value.status_code == 400
