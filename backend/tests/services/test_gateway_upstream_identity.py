"""Unit tests for trusted upstream identity helpers (#1409)."""

from types import SimpleNamespace
import uuid

import pytest

from preloop.models.crud import crud_api_key, crud_gateway_subject
from preloop.models.models.gateway_subject import GatewaySubject
from preloop.services.gateway_upstream_identity import (
    GatewaySubjectRef,
    TRUSTED_UPSTREAM_SCOPE,
    detect_client,
    fallback_session_id,
    hash_upstream_secret,
    is_trusted_upstream_key,
    per_subject_budget,
    read_identity_headers,
    to_trusted_upstream_error,
    upstream_secret_matches,
)
from preloop.services.model_gateway_errors import ModelGatewayAPIError


def _key(scopes=(), context=None):
    return SimpleNamespace(id=uuid.uuid4(), scopes=list(scopes), context_data=context)


def _subject(email="dev@corp.example"):
    return GatewaySubjectRef(
        id=uuid.uuid4(),
        external_subject="sub-1",
        email=email,
        linked_user_id=None,
        api_key_id=uuid.uuid4(),
    )


def test_trusted_scope_detection():
    assert is_trusted_upstream_key(_key([TRUSTED_UPSTREAM_SCOPE]))
    assert not is_trusted_upstream_key(_key(["mcp:read"]))
    assert not is_trusted_upstream_key(None)


def test_upstream_secret_matching():
    assert upstream_secret_matches(_key(), None)
    key = _key(context={"trusted_upstream_secret_hash": hash_upstream_secret("s3")})
    assert upstream_secret_matches(key, "s3")
    assert not upstream_secret_matches(key, None)
    assert not upstream_secret_matches(key, "nope")


def test_identity_headers_require_sub_and_fall_back_to_litellm_email():
    assert read_identity_headers({"x-claude-gateway-user-email": "a@b.c"}) is None
    identity = read_identity_headers(
        {"x-claude-gateway-user-id": "sub", "x-litellm-end-user-id": "a@b.c"}
    )
    assert identity.external_subject == "sub"
    assert identity.email == "a@b.c"
    no_email = read_identity_headers(
        {"x-claude-gateway-user-id": "sub", "x-litellm-end-user-id": "not-an-email"}
    )
    assert no_email.email is None


def test_detect_client():
    assert detect_client({"x-preloop-client": "claude-desktop"}) == "claude_desktop"
    assert detect_client({"x-claude-code-session-id": "x"}) == "claude_code"
    assert detect_client({"user-agent": "claude-cli/2.0"}) == "claude_code"
    assert detect_client({"user-agent": "curl/8"}) == "unknown"


def test_fallback_session_id_is_per_subject_per_utc_day():
    subject = _subject()
    from datetime import datetime, timezone

    now = datetime(2026, 10, 9, 23, 59, tzinfo=timezone.utc)
    assert fallback_session_id(subject, now) == f"gw:{subject.id}:2026-10-09"


def test_per_subject_budget_parsing():
    assert per_subject_budget(_key(context={})) is None
    assert per_subject_budget(_key(context={"per_subject_budget": {}})) is None
    parsed = per_subject_budget(
        _key(context={"per_subject_budget": {"hard_limit_usd": "5"}})
    )
    assert parsed == {
        "period": "monthly",
        "hard_limit_usd": 5.0,
        "soft_limit_usd": None,
        "model_alias": None,
    }


def test_budget_denial_maps_to_billing_error_429():
    exc = ModelGatewayAPIError(
        provider="anthropic",
        status_code=403,
        message="Model gateway budget exceeded: gateway_subject monthly hard limit",
        code="budget_limit_exceeded",
    )
    exc.budget_reset_seconds = 120
    mapped = to_trusted_upstream_error(exc, _subject())
    assert mapped.status_code == 429
    assert mapped.to_payload() == {
        "type": "error",
        "error": {
            "type": "billing_error",
            "message": (
                "Preloop budget exceeded for dev@corp.example: "
                "gateway_subject monthly hard limit"
            ),
        },
    }
    assert mapped.response_headers() == {
        "retry-after": "120",
        "x-should-retry": "false",
    }


def test_rate_limit_maps_to_rate_limit_error_429_without_should_retry():
    exc = ModelGatewayAPIError(
        provider="anthropic",
        status_code=429,
        message="slow down",
        retry_after_seconds=7,
    )
    mapped = to_trusted_upstream_error(exc, _subject(email=None))
    assert mapped.status_code == 429
    assert mapped.to_payload()["error"]["type"] == "rate_limit_error"
    assert mapped.response_headers() == {"retry-after": "7"}


@pytest.mark.parametrize("status_code", [400, 401, 403, 500])
def test_non_budget_errors_are_unchanged(status_code):
    exc = ModelGatewayAPIError(
        provider="anthropic", status_code=status_code, message="other"
    )
    assert to_trusted_upstream_error(exc, _subject()) is exc


def test_resolve_is_idempotent_and_never_creates_users(db_session, test_user):
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="gw",
        account_id=test_user.account_id,
        user_id=test_user.id,
        scopes=[TRUSTED_UPSTREAM_SCOPE],
    )
    first = crud_gateway_subject.resolve(
        db_session,
        account_id=test_user.account_id,
        api_key_id=api_key.id,
        external_subject="sub-a",
        email="TEST@example.com",
    )
    second = crud_gateway_subject.resolve(
        db_session,
        account_id=test_user.account_id,
        api_key_id=api_key.id,
        external_subject="sub-a",
        email="TEST@example.com",
    )
    assert first.id == second.id
    # Case-insensitive email match links the existing member.
    assert first.linked_user_id == test_user.id
    assert db_session.query(GatewaySubject).count() == 1
