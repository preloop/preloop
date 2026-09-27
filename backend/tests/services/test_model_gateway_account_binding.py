"""Gateway attribution follows the API key's account, not the user's (#984)."""

import re
from pathlib import Path

from preloop.models.crud import crud_account, crud_ai_model, crud_api_key
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import (
    ModelGatewayAuthContext,
    build_runtime_key_auth_context,
)
from preloop.services.openai_gateway import OpenAIGatewayService


def _other_account(db_session):
    return crud_account.create(
        db_session,
        obj_in={"organization_name": "Key Organization", "is_active": True},
    )


def _key_in(db_session, test_user, account_id):
    return crud_api_key.create_runtime_key(
        db_session,
        name="Gateway key",
        account_id=account_id,
        user_id=test_user.id,
    )


def test_key_authenticated_context_carries_the_keys_account(db_session, test_user):
    """With a key present, the context account is the key's account."""
    other = _other_account(db_session)
    api_key, token = _key_in(db_session, test_user, other.id)

    context = ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)

    assert context.account_id == other.id
    # The detached snapshot handed to later request phases keeps the binding.
    assert str(context.snapshot().account_id) == str(other.id)


def test_jwt_context_carries_the_users_account(test_user):
    """A user session with no key falls back to the user's account."""
    context = ModelGatewayAuthContext(token="jwt", user=test_user)

    assert context.account_id == test_user.account_id
    assert str(context.snapshot().account_id) == str(test_user.account_id)


def test_runtime_key_context_rejects_a_cross_account_key(db_session, test_user):
    """The non-request context builder fails closed on the same mismatch."""
    other = _other_account(db_session)
    api_key, token = _key_in(db_session, test_user, other.id)

    assert (
        build_runtime_key_auth_context(
            db_session, token=token, api_key_id=str(api_key.id)
        )
        is None
    )


def test_usage_row_for_key_request_carries_the_keys_account(db_session, test_user):
    """Usage recorded for a key-authenticated call is billed to the key's account."""
    other = _other_account(db_session)
    api_key, token = _key_in(db_session, test_user, other.id)
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {"enabled": True, "model_alias": "openai/gpt-5"},
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
        },
        account_id=other.id,
    )
    context = ModelGatewayAuthContext(token=token, user=test_user, api_key=api_key)
    service = OpenAIGatewayService(db_session, context)

    service._record_gateway_request(
        endpoint="/openai/v1/chat/completions",
        method="POST",
        status_code=200,
        duration=0.1,
        ai_model=ai_model,
        requested_model="openai/gpt-5",
        response_payload={
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        },
        upstream_response=None,
        endpoint_kind="chat_completions",
        request_payload={"model": "openai/gpt-5", "messages": []},
    )

    rows = (
        db_session.query(ApiUsage)
        .filter(
            ApiUsage.api_key_id == api_key.id,
            ApiUsage.action_type == "model_gateway",
        )
        .all()
    )
    assert len(rows) == 1
    assert rows[0].account_id == other.id
    assert rows[0].user_id == test_user.id


def test_gateway_modules_read_the_account_from_the_auth_context():
    """No gateway module reaches past the context to the user's account."""
    package = Path(__file__).resolve().parents[2] / "preloop"
    pattern = re.compile(r"auth_context\.user\.account_id")
    offenders = []
    for name in (
        "services/openai_gateway.py",
        "services/model_gateway_auth.py",
        "services/model_gateway_budget.py",
        "services/model_gateway_budget_enforcer.py",
        "services/model_content_policy.py",
        "api/endpoints/security_screen.py",
    ):
        for lineno, line in enumerate(
            (package / name).read_text().splitlines(), start=1
        ):
            if pattern.search(line):
                offenders.append(f"{name}:{lineno}")
    assert offenders == []
