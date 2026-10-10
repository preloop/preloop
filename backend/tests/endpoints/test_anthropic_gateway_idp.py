"""Customer IdP tokens on the Anthropic gateway and the admin API (#1414).

Locally generated keys and a stub JWKS; no network. Regression tests for API
keys and Preloop JWTs come first: they pin today's behaviour.
"""

from __future__ import annotations

import time
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from preloop.models.crud import crud_account, crud_api_key, crud_user
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.gateway_identity_provider import GatewayIdentityProvider
from preloop.models.models.gateway_subject import GatewaySubject
from preloop.services import gateway_idp_auth as idp
from preloop.services.gateway_idp_auth import JwksCache
from preloop.services.gateway_upstream_identity import TRUSTED_UPSTREAM_SCOPE
from tests.endpoints.test_anthropic_gateway_trusted_upstream import (
    _LITELLM_MESSAGE,
    ANTHROPIC_VERSION,
    _body,
    _model,
)
from tests.services.idp_test_keys import (
    AUDIENCE,
    EC_KEY,
    ISSUER,
    StubIdp,
    claims,
    sign,
    unsigned,
)

API = "/api/v1/account/gateway-identity-providers"


@pytest.fixture(autouse=True)
def account_admin(db_session, test_user):
    """The test user is the account's primary user, so an account admin."""
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.commit()


@pytest.fixture
def stub_idp(monkeypatch):
    stub = StubIdp()
    monkeypatch.setattr(idp, "default_jwks_cache", JwksCache(fetcher=stub))
    return stub


def _binding_key(db_session, user, *, context=None, name="IdP binding"):
    return crud_api_key.create_runtime_key(
        db_session,
        name=name,
        account_id=user.account_id,
        user_id=user.id,
        scopes=[TRUSTED_UPSTREAM_SCOPE],
        context_data=context or {},
    )


def _create_provider(client, api_key, **extra):
    payload = {
        "name": "Corp IdP",
        "issuer": ISSUER,
        "audiences": [AUDIENCE],
        "api_key_id": str(api_key.id),
        **extra,
    }
    return client.post(API, json=payload)


def _setup(client, db_session, user, *, context=None, **extra):
    _model(db_session, user.account_id)
    api_key, token = _binding_key(db_session, user, context=context)
    response = _create_provider(client, api_key, **extra)
    assert response.status_code == 201, response.text
    return api_key, token, response.json()


def _call(client, *, bearer=None, x_api_key=None, path="/anthropic/v1/messages"):
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    if x_api_key is not None:
        headers["x-api-key"] = x_api_key
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_MESSAGE,
    ):
        return client.post(path, headers=headers, json=_body())


def _last_usage(db_session, api_key):
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.api_key_id == api_key.id)
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


def _assert_idp_401(response, reason):
    assert response.status_code == 401, response.text
    assert response.headers["www-authenticate"].startswith(
        'Bearer error="invalid_token"'
    )
    body = response.json()
    assert body["type"] == "error"
    assert body["error"]["type"] == "authentication_error"
    assert reason in body["error"]["message"]


# --- Regression: existing credentials unchanged -----------------------------


def test_api_key_as_x_api_key_is_unchanged(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    _model(db_session, test_user.account_id, alias="anthropic/other", name="other")
    api_key, token = _binding_key(db_session, test_user, name="plain")
    api_key.scopes = []
    db_session.commit()

    response = _call(client, x_api_key=token)

    assert response.status_code == 200
    meta = _last_usage(db_session, api_key).meta_data
    assert meta["gateway_source"] == "direct"
    assert "auth_method" not in meta and "idp_provider_id" not in meta
    assert stub_idp.calls == []


def test_api_key_as_bearer_is_unchanged(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    api_key, token = _binding_key(db_session, test_user, name="plain")
    api_key.scopes = []
    db_session.commit()

    response = _call(client, bearer=token)

    assert response.status_code == 200
    assert "auth_method" not in _last_usage(db_session, api_key).meta_data
    assert stub_idp.calls == []


def test_preloop_jwt_bearer_is_unchanged(client, db_session, test_user, stub_idp):
    from preloop.api.auth.jwt import create_access_token

    _setup(client, db_session, test_user)
    token = create_access_token(
        data={"sub": str(test_user.id)}, expires_delta=timedelta(minutes=5)
    )
    assert idp.looks_like_jwt(token)

    response = _call(client, bearer=token)

    assert response.status_code == 200, response.text
    assert stub_idp.calls == []
    assert "www-authenticate" not in response.headers


def test_invalid_credential_error_is_unchanged(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    response = _call(client, bearer="not-a-real-key")
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid authentication credentials"
    assert "www-authenticate" not in response.headers


def test_jwt_from_unconfigured_issuer_falls_through(
    client, db_session, test_user, stub_idp
):
    _setup(client, db_session, test_user)
    token = sign(claims(iss="https://other-idp.example"))
    response = _call(client, bearer=token)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid authentication credentials"
    assert stub_idp.calls == []


# --- Accepted IdP tokens ---------------------------------------------------


def test_valid_idp_token_resolves_subject_and_attributes_usage(
    client, db_session, test_user, stub_idp
):
    api_key, _, provider = _setup(client, db_session, test_user)
    token = sign(claims(email=test_user.email))

    response = _call(client, bearer=token)

    assert response.status_code == 200, response.text
    subject = db_session.query(GatewaySubject).one()
    assert subject.api_key_id == api_key.id
    assert subject.external_subject == "user-123"
    assert subject.linked_user_id == test_user.id
    meta = _last_usage(db_session, api_key).meta_data
    assert meta["gateway_source"] == "direct"
    assert meta["auth_method"] == "idp"
    assert meta["idp_provider_id"] == provider["id"]
    assert meta["gateway_subject_id"] == str(subject.id)


def test_ec_signed_idp_token_is_accepted(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    response = _call(client, bearer=sign(key=EC_KEY, alg="ES256", kid="ec-1"))
    assert response.status_code == 200


def test_first_seen_audit_written_once(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    token = sign()
    assert _call(client, bearer=token).status_code == 200
    assert _call(client, bearer=token).status_code == 200

    rows = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == idp.AUDIT_SUBJECT_FIRST_SEEN)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].details["email"] == "dev@corp.example"
    assert token not in str(rows[0].details)


def test_idp_token_never_creates_a_user(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    before = db_session.query(crud_user.model).count()
    assert _call(client, bearer=sign(claims(email="new@corp.example"))).status_code
    assert db_session.query(crud_user.model).count() == before
    assert db_session.query(GatewaySubject).one().linked_user_id is None


def test_per_subject_budget_from_binding_key_applies(
    client, db_session, test_user, stub_idp
):
    _setup(
        client,
        db_session,
        test_user,
        context={
            "per_subject_budget": {"period": "monthly", "hard_limit_usd": 0.000001}
        },
    )

    response = _call(client, bearer=sign())

    # Same rendering as the trusted identity path today (#1447 not merged).
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "billing_error"
    assert "dev@corp.example" in response.json()["error"]["message"]


# --- Rejected IdP tokens ---------------------------------------------------


@pytest.mark.parametrize(
    ("token_factory", "reason"),
    [
        (
            lambda: sign(
                claims(iat=int(time.time()) - 7200, exp=int(time.time()) - 600)
            ),
            "expired",
        ),
        (lambda: sign(claims(nbf=int(time.time()) + 900)), "not_yet_valid"),
        (lambda: sign(claims(aud="other-app")), "bad_audience"),
        (lambda: sign(claims(iss=ISSUER + "/")), None),
        (lambda: unsigned(), "bad_algorithm"),
        (lambda: sign(claims(email="x@evil.example")), "domain_not_allowed"),
        (lambda: sign(claims(email_verified=False)), "email_not_verified"),
        (lambda: sign(claims(sub=None)), "bad_subject"),
        (lambda: sign(kid="unknown"), "unknown_kid"),
    ],
)
def test_rejected_tokens_get_401_with_www_authenticate(
    client, db_session, test_user, stub_idp, token_factory, reason
):
    _setup(client, db_session, test_user, allowed_email_domains=["corp.example"])
    response = _call(client, bearer=token_factory())
    if reason is None:
        # A different iss string matches no provider: falls through as before.
        assert response.status_code == 401
        assert "www-authenticate" not in response.headers
    else:
        _assert_idp_401(response, reason)
    assert db_session.query(GatewaySubject).count() == 0


def test_oversize_jwt_gets_401(client, db_session, test_user, stub_idp):
    _setup(client, db_session, test_user)
    token = sign(claims(pad="x" * (idp.MAX_TOKEN_BYTES + 1)))
    _assert_idp_401(_call(client, bearer=token), "token_too_large")
    assert stub_idp.calls == []


def test_jwks_unavailable_fails_closed_with_401(
    client, db_session, test_user, stub_idp
):
    _setup(client, db_session, test_user)
    stub_idp.fail = True
    _assert_idp_401(_call(client, bearer=sign()), "issuer_unavailable")


def test_idp_token_in_x_api_key_is_not_treated_as_idp(
    client, db_session, test_user, stub_idp
):
    _setup(client, db_session, test_user)
    response = _call(client, x_api_key=sign())
    assert response.status_code == 401
    assert "www-authenticate" not in response.headers
    assert stub_idp.calls == []


@pytest.mark.parametrize(
    ("path", "headers_extra"),
    [("/openai/v1/chat/completions", {}), ("/gemini/v1beta/models", {})],
)
def test_idp_token_rejected_on_openai_and_gemini(
    client, db_session, test_user, stub_idp, path, headers_extra
):
    _setup(client, db_session, test_user)
    headers = {"authorization": f"Bearer {sign()}"}
    if path.startswith("/gemini"):
        response = client.get(path, headers=headers)
    else:
        response = client.post(
            path,
            headers=headers,
            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert response.status_code == 401
    assert stub_idp.calls == []


def test_disabled_provider_stops_access_on_next_request(
    client, db_session, test_user, stub_idp
):
    _, _, provider = _setup(client, db_session, test_user)
    token = sign()
    assert _call(client, bearer=token).status_code == 200
    assert (
        client.patch(f"{API}/{provider['id']}", json={"enabled": False}).status_code
        == 200
    )
    response = _call(client, bearer=token)
    assert response.status_code == 401
    assert response.json()["error"]["message"] == "Invalid authentication credentials"


def test_deactivated_linked_member_is_refused(client, db_session, test_user, stub_idp):
    from preloop.models.models.user import User

    _setup(client, db_session, test_user)
    _model(db_session, test_user.account_id, alias="anthropic/x", name="x")
    other = User(
        account_id=test_user.account_id,
        email="gone@corp.example",
        username="gone",
        is_active=False,
        email_verified=True,
        hashed_password="x",
        user_source="local",
    )
    db_session.add(other)
    db_session.commit()
    response = _call(client, bearer=sign(claims(email="gone@corp.example")))
    _assert_idp_401(response, "member_inactive")


def test_inactive_binding_key_fails_closed(client, db_session, test_user, stub_idp):
    api_key, _, _ = _setup(client, db_session, test_user)
    api_key.is_active = False
    db_session.commit()
    _assert_idp_401(_call(client, bearer=sign()), "binding_key_invalid")


def test_token_is_never_logged(client, db_session, test_user, stub_idp, caplog):
    _setup(client, db_session, test_user)
    token = sign(claims(aud="other-app"))
    with caplog.at_level("DEBUG"):
        _call(client, bearer=token)
    assert token not in caplog.text
    assert token.split(".")[2] not in caplog.text


# --- Cross-account isolation ----------------------------------------------


def _second_account_admin(db_session):
    from preloop.models.crud import crud_role, crud_user_role

    account = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    user = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": "admin@other.example",
            "username": f"other-{uuid.uuid4().hex[:6]}",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    owner = crud_role.get_by_name(db_session, name="owner")
    if owner:
        crud_user_role.create(
            db_session, obj_in={"user_id": user.id, "role_id": owner.id}
        )
    account.primary_user_id = user.id
    db_session.commit()
    return user


def _as(app, user):
    from preloop.api.auth import get_current_active_user

    app.dependency_overrides[get_current_active_user] = lambda: user


def test_two_accounts_same_issuer_resolve_by_audience(
    app, client, db_session, test_user, stub_idp
):
    key_a, _, _ = _setup(client, db_session, test_user)
    other = _second_account_admin(db_session)
    _as(app, other)
    _model(db_session, other.account_id)
    key_b, _ = _binding_key(db_session, other)
    assert (
        _create_provider(client, key_b, audiences=["desktop-client-b"]).status_code
        == 201
    )

    assert _call(client, bearer=sign(claims(aud="desktop-client-b"))).status_code == 200
    assert _call(client, bearer=sign(claims(aud=AUDIENCE))).status_code == 200

    subjects = {s.api_key_id: s for s in db_session.query(GatewaySubject).all()}
    assert set(subjects) == {key_a.id, key_b.id}
    assert subjects[key_b.id].account_id == other.account_id
    assert subjects[key_a.id].account_id == test_user.account_id


def test_same_issuer_and_audience_twice_is_rejected_across_accounts(
    app, client, db_session, test_user, stub_idp
):
    _setup(client, db_session, test_user)
    other = _second_account_admin(db_session)
    _as(app, other)
    key_b, _ = _binding_key(db_session, other)
    response = _create_provider(client, key_b)
    assert response.status_code == 409
    assert str(test_user.account_id) not in response.text


# --- Admin API -----------------------------------------------------------


def test_admin_crud_round_trip_with_audit(client, db_session, test_user):
    api_key, _, created = _setup(client, db_session, test_user)
    assert created["audiences"] == [AUDIENCE]
    assert sorted(created["allowed_algorithms"]) == ["ES256", "RS256"]
    assert created["clock_skew_seconds"] == 60
    assert created["allow_private_network_issuer_effective"] is False

    listed = client.get(API).json()
    assert [p["id"] for p in listed] == [created["id"]]

    updated = client.patch(
        f"{API}/{created['id']}",
        json={"audiences": [AUDIENCE, "second"], "name": "Renamed"},
    )
    assert updated.status_code == 200
    assert updated.json()["audiences"] == [AUDIENCE, "second"]

    assert client.delete(f"{API}/{created['id']}").status_code == 204
    assert db_session.query(GatewayIdentityProvider).count() == 0

    actions = [
        row.action
        for row in db_session.query(AuditLog)
        .filter(AuditLog.resource_type == "gateway_identity_provider")
        .order_by(AuditLog.timestamp)
        .all()
    ]
    assert sorted(actions) == sorted(
        [
            "gateway_identity_provider_created",
            "gateway_identity_provider_updated",
            "gateway_identity_provider_deleted",
        ]
    )


@pytest.mark.parametrize(
    "bad",
    [
        {"issuer": "http://idp.corp.example"},
        {"audiences": []},
        {"allowed_algorithms": ["HS256"]},
        {"allowed_algorithms": ["none"]},
        {"clock_skew_seconds": 301},
    ],
)
def test_create_validation(client, db_session, test_user, bad):
    api_key, _ = _binding_key(db_session, test_user)
    assert _create_provider(client, api_key, **bad).status_code == 422


def test_binding_key_must_belong_to_account(app, client, db_session, test_user):
    other = _second_account_admin(db_session)
    foreign_key, _ = _binding_key(db_session, other)
    assert _create_provider(client, foreign_key).status_code == 422


def test_binding_key_serves_one_provider(client, db_session, test_user):
    api_key, _, _ = _setup(client, db_session, test_user)
    response = _create_provider(
        client, api_key, issuer="https://idp2.corp.example", audiences=["x"]
    )
    assert response.status_code == 409


def test_non_admin_cannot_manage_providers(
    app, client, db_session, test_user, test_viewer_user
):
    api_key, _ = _binding_key(db_session, test_user)
    _as(app, test_viewer_user)
    assert client.get(API).status_code == 403
    assert _create_provider(client, api_key).status_code == 403


def test_provider_of_another_account_is_404(app, client, db_session, test_user):
    _, _, created = _setup(client, db_session, test_user)
    _as(app, _second_account_admin(db_session))
    assert client.patch(f"{API}/{created['id']}", json={"name": "x"}).status_code == 404
    assert client.delete(f"{API}/{created['id']}").status_code == 404


def test_test_endpoint_reports_key_ids(client, db_session, test_user, monkeypatch):
    _, _, created = _setup(client, db_session, test_user)
    real = idp.probe_issuer

    async def stubbed(issuer, **kwargs):
        return await real(issuer, fetcher=StubIdp(), **kwargs)

    monkeypatch.setattr(idp, "probe_issuer", stubbed)
    response = client.post(f"{API}/{created['id']}/test")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert {k["kid"] for k in body["keys"]} == {"rsa-1", "ec-1"}
