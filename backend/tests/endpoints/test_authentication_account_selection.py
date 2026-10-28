"""Fresh password and passkey account selection with real membership isolation."""

from types import SimpleNamespace
from uuid import uuid4
import pytest
from preloop.models.crud import crud_account, crud_user


@pytest.mark.parametrize("transport", ["json", "form"])
def test_password_login_targets_held_account_after_using_another(
    db_session, test_user, transport
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth import router as auth_module
    from preloop.api.auth.jwt import get_password_hash, decode_token
    from preloop.models.crud import crud_person
    from preloop.models.db.session import get_db_session

    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Last used account", "is_active": True}
    )
    other = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "person_id": test_user.person_id,
            "username": f"other-{uuid4().hex}",
            "email": test_user.email,
            "is_active": True,
        },
    )
    person = crud_person.resolve_for_membership(db_session, membership_id=test_user.id)
    person.last_active_user_id = other.id
    crud_person.set_password(
        db_session,
        person_id=person.id,
        hashed_password=get_password_hash("synthetic-recovery-password"),
    )
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as browser:
        payload = {
            "username": test_user.username,
            "password": "synthetic-recovery-password",
            "account_id": str(test_user.account_id),
        }
        response = (
            browser.post("/auth/token/json", json=payload)
            if transport == "json"
            else browser.post("/auth/token", data=payload)
        )
    assert response.status_code == 200, response.text
    token = decode_token(response.json()["access_token"])
    assert token.sub == str(test_user.id)
    assert token.claims["auth_account_id"] == str(test_user.account_id)


@pytest.mark.parametrize("target_kind", ["unknown", "other_person", "inactive"])
def test_targeted_password_rejects_unheld_or_inactive_accounts_identically(
    db_session, test_user, target_kind
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth import router as auth_module
    from preloop.api.auth.jwt import get_password_hash
    from preloop.models.crud import crud_person
    from preloop.models.db.session import get_db_session

    crud_person.set_password(
        db_session,
        person_id=test_user.person_id,
        hashed_password=get_password_hash("synthetic-recovery-password"),
    )
    target_id = uuid4()
    if target_kind != "unknown":
        account = crud_account.create(
            db_session,
            obj_in={
                "organization_name": "Unavailable",
                "is_active": target_kind != "inactive",
            },
        )
        fields = {
            "account_id": account.id,
            "username": f"target-{uuid4().hex}",
            "email": "otherperson@example.test",
            "is_active": True,
        }
        if target_kind == "inactive":
            fields.update(person_id=test_user.person_id, email=test_user.email)
        crud_user.create(db_session, obj_in=fields)
        target_id = account.id
    app = FastAPI()
    app.include_router(auth_module.router, prefix="/auth")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as browser:
        response = browser.post(
            "/auth/token/json",
            json={
                "username": test_user.username,
                "password": "synthetic-recovery-password",
                "account_id": str(target_id),
            },
        )
    assert response.status_code == 401, response.text
    assert response.json() == {"detail": "Invalid account selection"}


def test_passkey_recovery_binds_target_and_selects_only_held_membership(
    db_session, test_user, monkeypatch
):
    import base64
    from unittest.mock import Mock
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from preloop.api.auth import webauthn_router as api
    from preloop.api.auth.jwt import decode_token
    from preloop.models.crud import crud_person
    from preloop.models.db.session import get_db_session

    other_account = crud_account.create(
        db_session, obj_in={"organization_name": "Last used", "is_active": True}
    )
    other = crud_user.create(
        db_session,
        obj_in={
            "account_id": other_account.id,
            "person_id": test_user.person_id,
            "username": f"other-{uuid4().hex}",
            "email": test_user.email,
            "is_active": True,
        },
    )
    person = crud_person.resolve_for_membership(db_session, membership_id=test_user.id)
    person.last_active_user_id = other.id
    db_session.commit()
    credential = SimpleNamespace(
        id=uuid4(),
        user_id=other.id,
        person_id=person.id,
        public_key=base64.urlsafe_b64encode(b"synthetic-public-key").decode(),
        sign_count=1,
    )
    monkeypatch.setattr(
        api.crud_webauthn_credential,
        "get_by_credential_id",
        lambda *a, **kw: credential,
    )
    monkeypatch.setattr(api.crud_webauthn_credential, "touch", lambda *a, **kw: None)
    verify = Mock(return_value=SimpleNamespace(new_sign_count=2))
    monkeypatch.setattr(api, "verify_authentication_response", verify)
    app = FastAPI()
    app.include_router(api.router, prefix="/auth/webauthn")
    app.dependency_overrides[get_db_session] = lambda: db_session
    with TestClient(app) as browser:
        challenge = browser.post(
            "/auth/webauthn/authenticate/options",
            json={"account_id": str(test_user.account_id)},
        ).json()["challenge_token"]
        response = browser.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": {"id": "synthetic"},
                "challenge_token": challenge,
                "account_id": str(other_account.id),
            },
        )
        assert response.status_code == 400
        verify.assert_not_called()
        removed = browser.post(
            "/auth/webauthn/authenticate/verify",
            json={"credential": {"id": "synthetic"}, "challenge_token": challenge},
        )
        assert removed.status_code == 400
        no_target = browser.post("/auth/webauthn/authenticate/options").json()[
            "challenge_token"
        ]
        added = browser.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": {"id": "synthetic"},
                "challenge_token": no_target,
                "account_id": str(test_user.account_id),
            },
        )
        assert added.status_code == 400
        verify.assert_not_called()
        response = browser.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": {"id": "synthetic"},
                "challenge_token": challenge,
                "account_id": str(test_user.account_id),
            },
        )
        assert response.status_code == 200, response.text
        token = decode_token(response.json()["access_token"])
        assert token.sub == str(test_user.id)
        unknown = str(uuid4())
        challenge = browser.post(
            "/auth/webauthn/authenticate/options", json={"account_id": unknown}
        ).json()["challenge_token"]
        response = browser.post(
            "/auth/webauthn/authenticate/verify",
            json={
                "credential": {"id": "synthetic"},
                "challenge_token": challenge,
                "account_id": unknown,
            },
        )
        assert response.status_code == 401
        assert response.json() == {"detail": "Invalid account selection"}
