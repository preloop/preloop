"""One denial status contract across every gateway router (#1447).

Per router (OpenAI, Anthropic, Gemini): a budget denial is ``429`` with the
provider-shaped body, an integer ``retry-after`` and ``x-should-retry:
false``; a model allowlist denial stays ``403``; a rate limit is ``429``
without ``x-should-retry: false``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from unittest.mock import patch

import litellm
import pytest

from preloop.api.endpoints.anthropic_gateway import get_anthropic_gateway_auth_context
from preloop.api.endpoints.gemini_gateway import get_gemini_gateway_auth_context
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models.crud import crud_ai_model
from preloop.models.models.api_usage import ApiUsage
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import BudgetCheckResult
from preloop.services.openai_gateway import OpenAIGatewayService

COMPLETION = "preloop.services.openai_gateway.litellm.completion"
BUDGET_MESSAGE = "Model gateway budget exceeded: account monthly limit reached"


def _result(reason: str, *, reset_in: int | None = 600) -> BudgetCheckResult:
    return BudgetCheckResult(
        account_limit_usd=0.00001,
        account_soft_limit_usd=None,
        account_current_spend_usd=0.0,
        account_estimated_total_usd=1.0,
        flow_limit_usd=None,
        flow_soft_limit_usd=None,
        flow_current_spend_usd=0.0,
        flow_estimated_total_usd=None,
        estimated_request_cost_usd=1.0,
        trial_hosted_model_limit_usd=None,
        trial_hosted_model_current_spend_usd=None,
        trial_hosted_model_estimated_total_usd=None,
        hard_limit_exceeded=True,
        soft_limit_exceeded=False,
        enforcement_reason=reason,
        pricing_available=True,
        reset_at=(
            datetime.now(timezone.utc) + timedelta(seconds=reset_in)
            if reset_in is not None
            else None
        ),
        requested_model="contract-model",
        allowed_models=("other-model",)
        if reason == "subject_model_not_allowed"
        else None,
    )


def _model(db_session, account_id, *, provider: str, alias: str) -> None:
    crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": f"Contract {alias}",
            "provider_name": provider,
            "model_identifier": alias.split("/")[-1],
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                    "responses_api": "transcode",
                },
                "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
            },
            "is_default": True,
        },
        account_id=account_id,
    )


def _openai(app, client, db_session, user, *, stream: bool = False):
    _model(db_session, user.account_id, provider="openai", alias="openai/contract")
    app.dependency_overrides[get_model_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=user)
    )
    body: dict[str, Any] = {
        "model": "openai/contract",
        "messages": [{"role": "user", "content": "hi"}],
    }
    if stream:
        body["stream"] = True
    return client.post("/openai/v1/chat/completions", json=body)


def _anthropic(app, client, db_session, user, *, stream: bool = False):
    _model(
        db_session, user.account_id, provider="anthropic", alias="anthropic/contract"
    )
    app.dependency_overrides[get_anthropic_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="runtime-token", user=user)
    )
    body: dict[str, Any] = {
        "model": "anthropic/contract",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }
    if stream:
        body["stream"] = True
    return client.post(
        "/anthropic/v1/messages",
        headers={"x-api-key": "ignored", "anthropic-version": "2023-06-01"},
        json=body,
    )


def _gemini(app, client, db_session, user, *, stream: bool = False):
    _model(db_session, user.account_id, provider="openai", alias="gemini-contract")
    app.dependency_overrides[get_gemini_gateway_auth_context] = lambda: (
        ModelGatewayAuthContext(token="gateway-token", user=user)
    )
    action = "streamGenerateContent" if stream else "generateContent"
    return client.post(
        f"/gemini/v1beta/models/gemini-contract:{action}",
        headers={"x-goog-api-key": "ignored"},
        json={"contents": [{"role": "user", "parts": [{"text": "hi"}]}]},
    )


ROUTERS: dict[str, Callable[..., Any]] = {
    "openai": _openai,
    "anthropic": _anthropic,
    "gemini": _gemini,
}


def _error_kind(router: str, body: dict[str, Any]) -> str:
    error = body["error"]
    return error["status"] if router == "gemini" else error["type"]


@pytest.mark.parametrize(
    ("router", "expected_kind"),
    [
        ("openai", "insufficient_quota"),
        ("anthropic", "billing_error"),
        ("gemini", "RESOURCE_EXHAUSTED"),
    ],
)
def test_budget_denial_is_429_on_every_router(
    app, client, db_session, test_user, router, expected_kind
):
    with (
        patch(COMPLETION) as upstream,
        patch.object(
            OpenAIGatewayService,
            "_check_budget",
            return_value=_result("account_budget_exceeded"),
        ),
    ):
        response = ROUTERS[router](app, client, db_session, test_user)

    assert response.status_code == 429, response.text
    body = response.json()
    assert _error_kind(router, body) == expected_kind
    assert body["error"]["message"].startswith("Model gateway budget exceeded")
    if router == "openai":
        assert body["error"]["code"] == "insufficient_quota"
        assert body["error"]["preloop_code"] == "budget_limit_exceeded"
    retry_after = int(response.headers["retry-after"])
    assert 1 <= retry_after <= 600
    assert response.headers["x-should-retry"] == "false"
    upstream.assert_not_called()
    rows = (
        db_session.query(ApiUsage)
        .filter(ApiUsage.account_id == test_user.account_id)
        .all()
    )
    assert [row.status_code for row in rows] == [429]
    # Recorded as Preloop's budget, never as an upstream rate limit.
    assert rows[0].error_class == "budget_exceeded"
    assert not (rows[0].meta_data or {}).get("rate_limit")


def test_budget_429_keeps_budget_audit_vocabulary():
    """Usage and audit rows keep ``budget_limit_exceeded`` on the 429."""
    assert (
        OpenAIGatewayService._audit_error_type(429, BUDGET_MESSAGE)
        == "budget_limit_exceeded"
    )
    assert OpenAIGatewayService._audit_outcome(429, BUDGET_MESSAGE) == "budget_denied"
    # A plain rate limit is not a budget denial.
    assert OpenAIGatewayService._audit_outcome(429, "slow down") == "failed"


def test_budget_denial_without_reset_defaults_retry_after(
    app, client, db_session, test_user
):
    with (
        patch(COMPLETION),
        patch.object(
            OpenAIGatewayService,
            "_check_budget",
            return_value=_result("flow_budget_exceeded", reset_in=None),
        ),
    ):
        response = _openai(app, client, db_session, test_user)

    assert response.status_code == 429
    assert response.headers["retry-after"] == "3600"


@pytest.mark.parametrize(
    ("router", "expected_kind"),
    [
        ("openai", "permission_error"),
        ("anthropic", "permission_error"),
        ("gemini", "PERMISSION_DENIED"),
    ],
)
def test_allowlist_denial_stays_403(
    app, client, db_session, test_user, router, expected_kind
):
    with (
        patch(COMPLETION) as upstream,
        patch.object(
            OpenAIGatewayService,
            "_check_budget",
            return_value=_result("subject_model_not_allowed"),
        ),
    ):
        response = ROUTERS[router](app, client, db_session, test_user)

    assert response.status_code == 403, response.text
    assert _error_kind(router, response.json()) == expected_kind
    assert "x-should-retry" not in response.headers
    upstream.assert_not_called()


@pytest.mark.parametrize(
    ("router", "expected_kind"),
    [
        ("openai", "rate_limit_error"),
        ("anthropic", "rate_limit_error"),
        ("gemini", "RESOURCE_EXHAUSTED"),
    ],
)
def test_rate_limit_is_429_without_should_retry_false(
    app, client, db_session, test_user, router, expected_kind
):
    error = litellm.RateLimitError(
        message="Rate limit reached, please slow down",
        llm_provider="openai",
        model="contract",
    )
    with (
        patch(COMPLETION, side_effect=error),
        patch.object(OpenAIGatewayService, "_check_budget", return_value=None),
    ):
        response = ROUTERS[router](app, client, db_session, test_user)

    assert response.status_code == 429, response.text
    assert _error_kind(router, response.json()) == expected_kind
    assert response.headers.get("x-should-retry") != "false"


@pytest.mark.parametrize("router", ["openai", "anthropic"])
def test_streaming_budget_denial_is_json_429_before_stream_opens(
    app, client, db_session, test_user, router
):
    with (
        patch(COMPLETION) as upstream,
        patch.object(
            OpenAIGatewayService,
            "_check_budget",
            return_value=_result("account_budget_exceeded"),
        ),
    ):
        response = ROUTERS[router](app, client, db_session, test_user, stream=True)

    assert response.status_code == 429, response.text
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-should-retry"] == "false"
    assert response.json()["error"]["message"] == BUDGET_MESSAGE
    upstream.assert_not_called()
