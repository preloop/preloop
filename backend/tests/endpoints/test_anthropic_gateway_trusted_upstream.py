"""Claude Desktop and Claude apps gateway upstream compatibility (#1409).

Covers the trusted upstream key (scope, optional secret, identity headers),
gateway subjects, the 429 contract, ``count_tokens``, ``models``, header
passthrough and usage attribution on the Anthropic gateway.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from preloop.models import models
from preloop.models.crud import (
    crud_account,
    crud_account_halt,
    crud_ai_model,
    crud_api_key,
)
from preloop.models.models.api_usage import ApiUsage
from preloop.models.models.gateway_subject import GatewaySubject
from preloop.models.models.user import User
from preloop.services.gateway_upstream_identity import (
    TRUSTED_UPSTREAM_SCOPE,
    hash_upstream_secret,
)
from preloop.services.kill_switch import invalidate_kill_switch_cache
from preloop.services.subject_governance import set_subject_governance

ALIAS = "anthropic/claude-sonnet-4-5"
SECRET = "upstream-secret-0123456789abcdef"
ANTHROPIC_VERSION = "2023-06-01"

_LITELLM_MESSAGE = {
    "id": "msg_123",
    "created": 1710000000,
    "choices": [
        {
            "message": {"role": "assistant", "content": "Hello"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
}


def _model(db_session, account_id, *, alias=ALIAS, name="Claude Gateway Model"):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": name,
            "provider_name": "anthropic",
            "model_identifier": alias.split("/")[-1],
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 1.0, "output_price_per_1k": 1.0},
            },
        },
        account_id=account_id,
    )


def _key(db_session, user, *, trusted=True, context=None, name="Gateway upstream"):
    api_key, token = crud_api_key.create_runtime_key(
        db_session,
        name=name,
        account_id=user.account_id,
        user_id=user.id,
        scopes=[TRUSTED_UPSTREAM_SCOPE] if trusted else [],
        context_data=context or {},
    )
    return api_key, token


def _headers(token, **extra):
    return {"x-api-key": token, "anthropic-version": ANTHROPIC_VERSION, **extra}


def _identity(sub="idp-sub-1", email="dev@corp.example"):
    headers = {"x-claude-gateway-user-id": sub}
    if email:
        headers["x-claude-gateway-user-email"] = email
        headers["x-litellm-end-user-id"] = email
    return headers


def _body(**extra):
    return {
        "model": ALIAS,
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 16,
        **extra,
    }


def _post(client, token, headers=None, body=None):
    with patch(
        "preloop.services.openai_gateway.litellm.completion",
        return_value=_LITELLM_MESSAGE,
    ):
        return client.post(
            "/anthropic/v1/messages",
            headers=_headers(token, **(headers or {})),
            json=body or _body(),
        )


def _usage(db_session, api_key):
    return (
        db_session.query(ApiUsage)
        .filter(ApiUsage.api_key_id == api_key.id)
        .order_by(ApiUsage.timestamp.desc())
        .first()
    )


# --- Trust model -----------------------------------------------------------


def test_forged_identity_headers_on_normal_key_are_ignored(
    client, db_session, test_user, caplog
):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user, trusted=False)

    with caplog.at_level("DEBUG", logger="preloop.services.gateway_upstream_identity"):
        response = _post(client, token, _identity(email="forged@corp.example"))

    assert response.status_code == 200
    assert db_session.query(GatewaySubject).count() == 0
    usage = _usage(db_session, api_key)
    assert usage.meta_data["gateway_source"] == "direct"
    assert usage.meta_data["gateway_subject_id"] is None
    assert usage.meta_data["gateway_subject_email"] is None
    # Header names are logged at debug, values never are.
    assert "x-claude-gateway-user-id" in caplog.text
    assert "forged@corp.example" not in caplog.text
    assert "idp-sub-1" not in caplog.text


def test_trusted_key_without_secret_honours_identity(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)
    users_before = db_session.query(User).count()

    response = _post(client, token, _identity(email=test_user.email))

    assert response.status_code == 200
    subject = db_session.query(GatewaySubject).one()
    assert subject.external_subject == "idp-sub-1"
    assert subject.api_key_id == api_key.id
    assert subject.account_id == test_user.account_id
    # Email matches a member of the key's account: linked.
    assert subject.linked_user_id == test_user.id
    assert db_session.query(User).count() == users_before
    usage = _usage(db_session, api_key)
    assert usage.meta_data["gateway_source"] == "claude_apps_gateway"
    assert usage.meta_data["gateway_subject_id"] == str(subject.id)
    assert usage.meta_data["gateway_subject_email"] == test_user.email


def test_non_member_email_stays_unlinked_and_creates_no_user(
    client, db_session, test_user, test_viewer_user
):
    _model(db_session, test_user.account_id)
    _key(db_session, test_user)
    token = _key(db_session, test_user, name="second")[1]
    users_before = db_session.query(User).count()

    # viewer@example.com exists, but in another account.
    response = _post(client, token, _identity(sub="s-2", email=test_viewer_user.email))
    assert response.status_code == 200
    response = _post(client, token, _identity(sub="s-3", email="nobody@corp.example"))
    assert response.status_code == 200

    subjects = db_session.query(GatewaySubject).order_by(
        GatewaySubject.external_subject
    )
    assert [s.linked_user_id for s in subjects] == [None, None]
    assert db_session.query(User).count() == users_before


def test_email_change_relinks_subject(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user)
    assert (
        _post(client, token, _identity(email="other@corp.example")).status_code == 200
    )
    subject = db_session.query(GatewaySubject).one()
    assert subject.linked_user_id is None

    assert _post(client, token, _identity(email=test_user.email)).status_code == 200
    db_session.refresh(subject)
    assert subject.email == test_user.email
    assert subject.linked_user_id == test_user.id
    assert db_session.query(GatewaySubject).count() == 1


@pytest.mark.parametrize("presented", [None, "wrong-secret-value-000000"])
def test_trusted_key_with_secret_rejects_missing_or_wrong_secret(
    client, db_session, test_user, presented
):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(
        db_session,
        test_user,
        context={"trusted_upstream_secret_hash": hash_upstream_secret(SECRET)},
    )
    headers = _identity()
    if presented:
        headers["x-preloop-upstream-secret"] = presented

    response = _post(client, token, headers)

    assert response.status_code == 401
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "authentication_error"
    assert db_session.query(GatewaySubject).count() == 0


def test_trusted_key_with_matching_secret_is_accepted(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(
        db_session,
        test_user,
        context={"trusted_upstream_secret_hash": hash_upstream_secret(SECRET)},
    )
    response = _post(
        client, token, {**_identity(), "x-preloop-upstream-secret": SECRET}
    )
    assert response.status_code == 200
    assert db_session.query(GatewaySubject).count() == 1


# --- Budgets and the 429 contract -------------------------------------------


def test_per_subject_budget_exceeded_returns_429_contract(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(
        db_session,
        test_user,
        context={
            "per_subject_budget": {"period": "monthly", "hard_limit_usd": 0.000001}
        },
    )

    response = _post(client, token, _identity(email="dev@corp.example"))

    assert response.status_code == 429
    retry_after = response.headers["retry-after"]
    assert retry_after.isdigit() and int(retry_after) >= 1
    assert response.headers["x-should-retry"] == "false"
    body = response.json()
    assert set(body) == {"type", "error"}
    assert body["type"] == "error"
    assert set(body["error"]) == {"type", "message"}
    assert body["error"]["type"] == "billing_error"
    assert body["error"]["message"].startswith(
        "Preloop budget exceeded for dev@corp.example"
    )


def test_gateway_subject_budget_policy_names_subject_without_email(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)
    assert (
        _post(client, token, _identity(sub="sub-no-email", email=None)).status_code
        == 200
    )
    subject = db_session.query(GatewaySubject).one()
    db_session.add(
        models.BudgetPolicy(
            account_id=test_user.account_id,
            subject_type="gateway_subject",
            subject_id=subject.id,
            period=models.BudgetPeriod.daily,
            hard_limit_usd=0.000001,
        )
    )
    db_session.commit()

    response = _post(client, token, _identity(sub="sub-no-email", email=None))

    assert response.status_code == 429
    assert response.json()["error"]["message"].startswith(
        "Preloop budget exceeded for sub-no-email"
    )
    # Spend from the first call was recorded under the subject's bucket.
    activity = (
        db_session.query(models.BudgetSpendActivity)
        .filter(
            models.BudgetSpendActivity.subject_type == "gateway_subject",
            models.BudgetSpendActivity.subject_id == subject.id,
        )
        .count()
    )
    assert activity > 0


def test_linked_user_budget_applies_to_gateway_subject(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user)
    db_session.add(
        models.BudgetPolicy(
            account_id=test_user.account_id,
            subject_type="user",
            subject_id=test_user.id,
            period=models.BudgetPeriod.monthly,
            hard_limit_usd=0.000001,
        )
    )
    db_session.commit()

    linked = _post(client, token, _identity(email=test_user.email))
    unlinked = _post(client, token, _identity(sub="s-x", email="x@corp.example"))

    assert linked.status_code == 429
    assert linked.json()["error"]["type"] == "billing_error"
    # The key owner's user budget does not apply to an unlinked developer.
    assert unlinked.status_code == 200


def test_same_budget_denial_on_normal_key_is_429(client, db_session, test_user):
    """Since #1447 every budget denial is 429; only the message differs.

    The trusted identity request names the developer; a normal key keeps the
    historical ``Model gateway budget exceeded`` text.
    """
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user, trusted=False)
    db_session.add(
        models.BudgetPolicy(
            account_id=test_user.account_id,
            subject_type="api_key",
            subject_id=api_key.id,
            period=models.BudgetPeriod.monthly,
            hard_limit_usd=0.000001,
        )
    )
    db_session.commit()

    response = _post(client, token, _identity())

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "billing_error"
    assert response.headers["x-should-retry"] == "false"
    assert response.json()["error"]["message"].startswith(
        "Model gateway budget exceeded"
    )


def test_trusted_key_without_identity_budget_denial_is_429(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)
    db_session.add(
        models.BudgetPolicy(
            account_id=test_user.account_id,
            subject_type="api_key",
            subject_id=api_key.id,
            period=models.BudgetPeriod.monthly,
            hard_limit_usd=0.000001,
        )
    )
    db_session.commit()

    response = _post(client, token)

    assert response.status_code == 429
    assert response.json()["error"]["type"] == "billing_error"


def test_subject_allowed_models_is_enforced(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user)
    assert _post(client, token, _identity()).status_code == 200
    subject = db_session.query(GatewaySubject).one()
    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = set_subject_governance(
        account.meta_data,
        subject_type="gateway_subjects",
        subject_id=str(subject.id),
        config={"allowed_models": ["some-other-model"]},
    )
    db_session.commit()

    response = _post(client, token, _identity())

    # 429, not 403: the apps gateway would fail over around a 403.
    assert response.status_code == 429
    assert response.headers["x-should-retry"] == "false"
    assert response.json()["error"]["type"] == "permission_error"

    counted = client.post(
        "/anthropic/v1/messages/count_tokens",
        headers=_headers(token, **_identity()),
        json={"model": ALIAS, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert counted.status_code == 429
    assert counted.json()["error"]["type"] == "permission_error"

    # Same allowlist on a plain request through the key scope stays 403.
    plain_key, plain_token = _key(db_session, test_user, trusted=False, name="plain")
    account.meta_data = set_subject_governance(
        account.meta_data,
        subject_type="api_keys",
        subject_id=str(plain_key.id),
        config={"allowed_models": ["some-other-model"]},
    )
    db_session.commit()
    plain = client.post(
        "/anthropic/v1/messages/count_tokens",
        headers=_headers(plain_token),
        json={"model": ALIAS, "messages": [{"role": "user", "content": "Hi"}]},
    )
    assert plain.status_code == 403


# --- Sessions and attribution -----------------------------------------------


def test_trusted_request_without_session_header_groups_by_subject_day(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)

    assert _post(client, token, _identity()).status_code == 200

    subject = db_session.query(GatewaySubject).one()
    session = (
        db_session.query(models.RuntimeSession)
        .filter(models.RuntimeSession.account_id == test_user.account_id)
        .one()
    )
    assert session.session_source_id.startswith(f"{api_key.id}:gw:{subject.id}:")


def test_trusted_request_keeps_claude_code_session_header(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)

    response = _post(
        client, token, {**_identity(), "x-claude-code-session-id": "cc-session-1"}
    )

    assert response.status_code == 200
    session = (
        db_session.query(models.RuntimeSession)
        .filter(models.RuntimeSession.account_id == test_user.account_id)
        .one()
    )
    assert session.session_source_id == f"{api_key.id}:cc-session-1"
    assert _usage(db_session, api_key).meta_data["client"] == "claude_code"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"X-Preloop-Client": "claude-desktop"}, "claude_desktop"),
        ({"user-agent": "claude-cli/2.1.0 (external, cli)"}, "claude_code"),
        ({}, "unknown"),
    ],
)
def test_client_attribution(client, db_session, test_user, headers, expected):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user, trusted=False)

    assert _post(client, token, headers).status_code == 200

    usage = _usage(db_session, api_key)
    assert usage.meta_data["client"] == expected
    assert usage.meta_data["gateway_source"] == "direct"


# --- count_tokens and models -------------------------------------------------


def test_count_tokens_forwards_upstream_without_usage(client, db_session, test_user):
    _model(db_session, test_user.account_id)
    api_key, token = _key(db_session, test_user)
    upstream = MagicMock()
    upstream.post.return_value = httpx.Response(
        200,
        json={"input_tokens": 42},
        headers={"anthropic-ratelimit-unified-status": "allowed"},
    )
    with patch(
        "preloop.services.openai_gateway._anthropic_passthrough_http_client",
        return_value=upstream,
    ):
        response = client.post(
            "/anthropic/v1/messages/count_tokens",
            headers=_headers(
                token,
                **_identity(),
                **{"anthropic-beta": "token-counting-2099-01-01"},
            ),
            json={"model": ALIAS, "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert response.status_code == 200
    assert response.json() == {"input_tokens": 42}
    assert response.headers["anthropic-ratelimit-unified-status"] == "allowed"
    call = upstream.post.call_args
    assert call.args[0].endswith("/v1/messages/count_tokens")
    assert call.kwargs["headers"]["x-api-key"] == "provider-secret"
    assert call.kwargs["headers"]["anthropic-beta"] == "token-counting-2099-01-01"
    assert call.kwargs["json"]["model"] == "claude-sonnet-4-5"
    assert _usage(db_session, api_key) is None


def test_count_tokens_rejected_while_gateway_halted(client, db_session, test_user):
    """An account halt stops token counting before any upstream forward."""
    _model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user)
    upstream = MagicMock()
    crud_account_halt.set_scopes(
        db_session,
        account_id=test_user.account_id,
        scopes=["gateway"],
        active=True,
        user_id=None,
        reason="runaway agent",
    )
    invalidate_kill_switch_cache(test_user.account_id)
    try:
        with patch(
            "preloop.services.openai_gateway._anthropic_passthrough_http_client",
            return_value=upstream,
        ):
            response = client.post(
                "/anthropic/v1/messages/count_tokens",
                headers=_headers(token, **_identity()),
                json={"model": ALIAS, "messages": [{"role": "user", "content": "Hi"}]},
            )
    finally:
        crud_account_halt.set_scopes(
            db_session,
            account_id=test_user.account_id,
            scopes=["gateway"],
            active=False,
            user_id=None,
        )
        invalidate_kill_switch_cache(test_user.account_id)

    # Trusted identity request: the halt renders as the fail-closed 429.
    assert response.status_code == 429
    assert response.json()["error"]["type"] == "permission_error"
    assert response.headers["x-should-retry"] == "false"
    upstream.post.assert_not_called()


def test_count_tokens_requires_auth(client):
    response = client.post(
        "/anthropic/v1/messages/count_tokens",
        headers={"anthropic-version": ANTHROPIC_VERSION},
        json={"model": ALIAS, "messages": []},
    )
    assert response.status_code == 401


def test_models_lists_anthropic_shape_filtered_by_subject(
    client, db_session, test_user
):
    _model(db_session, test_user.account_id)
    _model(
        db_session,
        test_user.account_id,
        alias="anthropic/claude-haiku-4-5",
        name="Claude Haiku",
    )
    _api_key, token = _key(db_session, test_user)

    response = client.get(
        "/anthropic/v1/models", headers=_headers(token, **_identity())
    )
    assert response.status_code == 200
    body = response.json()
    assert body["has_more"] is False
    ids = {item["id"] for item in body["data"]}
    assert {ALIAS, "anthropic/claude-haiku-4-5"} <= ids
    assert all(item["type"] == "model" for item in body["data"])

    subject = db_session.query(GatewaySubject).one()
    account = crud_account.get(db_session, id=test_user.account_id)
    account.meta_data = set_subject_governance(
        account.meta_data,
        subject_type="gateway_subjects",
        subject_id=str(subject.id),
        config={"allowed_models": ["anthropic/claude-haiku-4-5"]},
    )
    db_session.commit()

    response = client.get(
        "/anthropic/v1/models", headers=_headers(token, **_identity())
    )
    assert [item["id"] for item in response.json()["data"]] == [
        "anthropic/claude-haiku-4-5"
    ]


# --- Passthrough --------------------------------------------------------------


def _oauth_model(db_session, account_id):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Claude Code Subscription",
            "provider_name": "anthropic",
            "model_identifier": "claude-sonnet-4-5",
            "api_key": "unused-placeholder",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": ALIAS,
                    "provider_adapter": "preloop",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _oauth_credentials():
    return SimpleNamespace(
        credential_type="oauth_anthropic_claude_code",
        value="sk-ant-oat01-access-token",
    )


_UPSTREAM_HEADERS = {
    "anthropic-ratelimit-unified-status": "allowed",
    "anthropic-ratelimit-unified-5h-utilization": "0.25",
    "x-should-retry": "true",
}

_SYSTEM = [
    {"type": "text", "text": "first", "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": "second"},
    {"type": "text", "text": "third"},
]


def test_passthrough_forwards_unknown_headers_body_and_response_headers(
    client, db_session, test_user
):
    _oauth_model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user, trusted=False)
    upstream = MagicMock()
    upstream.post.return_value = httpx.Response(
        200,
        json={
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        headers=_UPSTREAM_HEADERS,
    )
    body = _body(system=_SYSTEM, some_future_field={"nested": [1, 2]})
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(
            "preloop.services.openai_gateway._anthropic_passthrough_http_client",
            return_value=upstream,
        ),
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        response = client.post(
            "/anthropic/v1/messages",
            headers=_headers(
                token,
                **{
                    "anthropic-beta": "brand-new-beta-2099-01-01",
                    "anthropic-dangerous-direct-browser-access": "true",
                    "anthropic-future-header": "v1",
                },
            ),
            json=body,
        )

    assert response.status_code == 200
    for name, value in _UPSTREAM_HEADERS.items():
        assert response.headers[name] == value
    sent = upstream.post.call_args.kwargs
    assert "brand-new-beta-2099-01-01" in sent["headers"]["anthropic-beta"].split(",")
    assert sent["headers"]["anthropic-future-header"] == "v1"
    assert sent["headers"]["anthropic-dangerous-direct-browser-access"] == "true"
    assert sent["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert sent["json"]["system"] == _SYSTEM
    assert sent["json"]["some_future_field"] == {"nested": [1, 2]}


def test_passthrough_stream_is_event_stream_with_response_headers(
    client, db_session, test_user
):
    _oauth_model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user, trusted=False)
    chunks = [
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"m","usage":'
        '{"input_tokens":1}}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]
    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers(_UPSTREAM_HEADERS)
    upstream_response.iter_text.return_value = iter(chunks)
    upstream = MagicMock()
    upstream.send.return_value = upstream_response
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(
            "preloop.services.openai_gateway._anthropic_passthrough_http_client",
            return_value=upstream,
        ),
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        response = client.post(
            "/anthropic/v1/messages",
            headers=_headers(token, **{"anthropic-beta": "brand-new-beta"}),
            json=_body(stream=True, system=_SYSTEM),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["anthropic-ratelimit-unified-status"] == "allowed"
    assert response.headers["x-should-retry"] == "true"
    assert response.text == "".join(chunks)
    sent = upstream.build_request.call_args.kwargs
    assert sent["json"]["system"] == _SYSTEM


def test_passthrough_error_body_and_headers_forwarded(client, db_session, test_user):
    _oauth_model(db_session, test_user.account_id)
    _api_key, token = _key(db_session, test_user, trusted=False)
    upstream = MagicMock()
    upstream.post.return_value = httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "bad field"},
        },
        headers={"x-should-retry": "false"},
    )
    with (
        patch("preloop.services.openai_gateway.get_secret_service") as secrets,
        patch(
            "preloop.services.openai_gateway._anthropic_passthrough_http_client",
            return_value=upstream,
        ),
    ):
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            _oauth_credentials()
        )
        response = client.post(
            "/anthropic/v1/messages", headers=_headers(token), json=_body()
        )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert response.json()["error"]["message"] == "bad field"
    assert response.headers["x-should-retry"] == "false"


# --- Trusted key creation -----------------------------------------------------


def test_owner_can_create_trusted_key_with_secret_and_budget(
    client, db_session, test_user
):
    account = crud_account.get(db_session, id=test_user.account_id)
    account.primary_user_id = test_user.id
    db_session.commit()
    response = client.post(
        "/api/v1/auth/api-keys",
        json={
            "name": "apps gateway",
            "scopes": [TRUSTED_UPSTREAM_SCOPE],
            "trusted_upstream_secret": SECRET,
            "per_subject_budget": {"period": "monthly", "hard_limit_usd": 25},
        },
    )
    assert response.status_code == 201, response.text
    key = crud_api_key.get(db_session, id=response.json()["id"])
    assert key.scopes == [TRUSTED_UPSTREAM_SCOPE]
    assert key.context_data["trusted_upstream_secret_hash"] == hash_upstream_secret(
        SECRET
    )
    assert SECRET not in str(key.context_data)
    assert key.context_data["per_subject_budget"] == {
        "period": "monthly",
        "hard_limit_usd": 25.0,
    }


def test_trusted_options_without_scope_are_rejected(client):
    response = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "not trusted", "trusted_upstream_secret": SECRET},
    )
    assert response.status_code == 400


def test_non_admin_cannot_grant_trusted_scope(
    app, client, db_session, test_viewer_user
):
    from preloop.api.auth import get_current_active_user

    app.dependency_overrides[get_current_active_user] = lambda: test_viewer_user
    response = client.post(
        "/api/v1/auth/api-keys",
        json={"name": "sneaky", "scopes": [TRUSTED_UPSTREAM_SCOPE]},
    )
    assert response.status_code == 403
