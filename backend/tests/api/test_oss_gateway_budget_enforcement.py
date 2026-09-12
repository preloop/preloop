"""Basic BYOK budgets apply to OSS gateway requests before provider dispatch."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.api.app import create_app
from preloop.config import settings
from preloop.api.deps import get_budget_enforcer
from preloop.api.endpoints.openai_gateway import get_model_gateway_auth_context
from preloop.models import models
from preloop.models.crud import crud_ai_model
from preloop.models.crud.budget import crud_budget_policy
from preloop.models.db.session import get_db_session
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.plugins.base import PluginManager
from preloop.services.model_gateway_budget_enforcer import ModelGatewayBudgetEnforcer


@pytest.mark.parametrize(
    "limit, pricing, policy_alias, expected_status",
    [
        (0.00001, True, None, 403),
        (0.0, True, None, 403),
        (100.0, True, None, 200),
        (100.0, False, None, 403),
        (100.0, False, "another-model", 200),
        (None, False, None, 200),
    ],
)
def test_dedicated_gateway_applies_real_budget_before_dispatch(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    limit: float | None,
    pricing: bool,
    policy_alias: str | None,
    expected_status: int,
) -> None:
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", lambda: PluginManager())
    monkeypatch.setattr("preloop.plugins.base._plugin_manager", None)
    crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Priced synthetic BYOK",
            "provider_name": "openai",
            "model_identifier": "synthetic-priced-model",
            "api_key": "unused-synthetic-key",
            "meta_data": {
                "gateway": {"enabled": True, "model_alias": "priced-test"},
                "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1}
                if pricing
                else {},
            },
        },
    )
    crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "subject_type": "account",
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": limit,
            "model_alias": policy_alias,
            "soft_limit_usd": 1.0 if limit is None else None,
        },
    )
    app = create_app()
    assert get_budget_enforcer not in app.dependency_overrides
    assert isinstance(get_budget_enforcer(), ModelGatewayBudgetEnforcer)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_model_gateway_auth_context] = (
        lambda: ModelGatewayAuthContext(
            token="synthetic-authenticated-user", user=test_user
        )
    )
    original = ModelGatewayBudgetEnforcer.enforce_or_raise
    with (
        patch.object(
            ModelGatewayBudgetEnforcer,
            "enforce_or_raise",
            autospec=True,
            side_effect=original,
        ) as enforce,
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value={
                "id": "synthetic-response",
                "object": "chat.completion",
                "model": "synthetic-priced-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        ) as provider,
        TestClient(app) as client,
    ):
        response = client.post(
            "/openai/v1/chat/completions",
            json={
                "model": "priced-test",
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
            },
        )
    assert response.status_code == expected_status, response.text
    if expected_status == 403 and not pricing:
        assert "budget_pricing_unavailable" in response.text
    enforce.assert_called_once()
    assert provider.call_count == (1 if expected_status == 200 else 0)


def test_subscription_zero_cost_is_distinct_from_unknown_pricing(
    db_session: Session, test_user: models.User
) -> None:
    from preloop.services.model_gateway_budget import ModelGatewayBudgetService
    from preloop.services.secret_service import OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE

    model = models.AIModel(
        provider_name="openai",
        model_identifier="synthetic-subscription-model",
        credentials_secret=models.SecretReference(
            secret_kind="ai_model_credentials",
            meta_data={"credential_type": OPENAI_CODEX_OAUTH_CREDENTIAL_TYPE},
        ),
    )
    auth = ModelGatewayAuthContext(token="synthetic", user=test_user)
    service = ModelGatewayBudgetService(db_session, auth)
    assert service._estimate_request_cost(model, {"max_tokens": 10}) == 0.0
    with (
        patch.object(
            service.__class__, "_pricing_override_for_request", return_value=None
        ),
        patch.object(crud_budget_policy, "get_policies_for_subject") as lookup,
    ):
        get_budget_enforcer().enforce_or_raise(
            db_session, auth, model, {"max_tokens": 10}
        )
    lookup.assert_not_called()
