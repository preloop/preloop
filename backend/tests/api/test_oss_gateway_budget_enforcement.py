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


@pytest.mark.parametrize("policy_scope", ["account", "model_id", "model_alias"])
@pytest.mark.parametrize("explicit_alias", [True, False])
@pytest.mark.parametrize(
    "limit, pricing, policy_alias, expected_status",
    [
        (0.00001, True, None, 429),
        (0.0, True, None, 429),
        (100.0, True, None, 200),
        (100.0, False, None, 429),
        (100.0, False, "another-model", 200),
        (None, False, None, 200),
        (0.0, True, "", 429),
        (0.0, True, "old-model-alias", 200),
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
    explicit_alias: bool,
    policy_scope: str,
) -> None:
    # An ID-scoped policy binds to this model whatever alias it stores.
    policy_binds_this_model = policy_scope == "model_id" or policy_alias not in {
        "another-model",
        "old-model-alias",
    }
    if pricing:
        expect_warning = False
        if policy_scope == "model_id" and policy_alias in {
            "another-model",
            "old-model-alias",
        }:
            expected_status = 429
    else:
        # Founder ruling 2026-09-18: a model with no known price is never
        # blocked by a budget it cannot be measured against. The request runs
        # and the caller is told the limit did not apply.
        expected_status = 200
        expect_warning = limit is not None and policy_binds_this_model
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", PluginManager)
    monkeypatch.setattr("preloop.plugins.base._plugin_manager", None)
    ai_model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Priced synthetic BYOK",
            "provider_name": "openai",
            "model_identifier": "synthetic-priced-model",
            "api_key": "unused-synthetic-key",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    **({"model_alias": "priced-test"} if explicit_alias else {}),
                },
                "pricing": {"input_price_per_1k": 1, "output_price_per_1k": 1}
                if pricing
                else {},
            },
        },
    )
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    requested_alias = resolve_ai_model_runtime(ai_model).model_gateway_model_alias
    crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "subject_type": "account" if policy_scope == "account" else "ai_model",
            "subject_id": ai_model.id if policy_scope == "model_id" else None,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": limit,
            "model_alias": policy_alias
            if policy_alias is not None
            else (
                requested_alias
                if (not explicit_alias or policy_scope == "model_alias")
                else None
            ),
            "soft_limit_usd": 1.0 if limit is None else None,
        },
    )
    if policy_scope == "model_id" and limit == 100 and pricing:
        from datetime import datetime, timezone
        from preloop.models.crud.budget import crud_budget_spend, get_period_start

        # Spend on other models must not exhaust an ID-only model policy.
        crud_budget_spend.upsert_spend(
            db_session,
            account_id=test_user.account_id,
            subject_type="account",
            subject_id=None,
            model_alias=None,
            period=models.BudgetPeriod.monthly,
            period_start=get_period_start(
                datetime.now(timezone.utc), models.BudgetPeriod.monthly
            ),
            spend_increment_usd=1000.0,
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
                "model": requested_alias,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
            },
        )
    assert response.status_code == expected_status, response.text
    warning = response.headers.get("X-Preloop-Warning", "")
    if expect_warning:
        assert "budget_pricing_unavailable" in warning, warning
        assert requested_alias in warning, warning
    else:
        assert "budget_pricing_unavailable" not in warning, warning
    enforce.assert_called_once()
    assert provider.call_count == (1 if expected_status == 200 else 0)


def test_unpriced_hard_limit_serves_the_request_and_pages_an_admin(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The gemini-3.8-flash production case: warn and serve, never refuse.

    An account with a monthly hard limit of $100 and no spend was refused
    outright because the requested model had not reached the price catalog.
    The request must now succeed, carry the warning, record zero spend with
    ``pricing_available=false``, and page an admin so the catalog hole gets
    closed.
    """
    from preloop.services import unpriced_model_alert

    unpriced_model_alert.reset_alert_state_for_tests()
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", PluginManager)
    monkeypatch.setattr("preloop.plugins.base._plugin_manager", None)
    ai_model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Uncatalogued hosted model",
            "provider_name": "openai",
            "model_identifier": "google/gemini-3.8-flash",
            "api_key": "unused-synthetic-key",
            "meta_data": {"gateway": {"enabled": True}, "pricing": {}},
        },
    )
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    requested_alias = resolve_ai_model_runtime(ai_model).model_gateway_model_alias
    crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "subject_type": "account",
            "subject_id": None,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": 100.0,
            "soft_limit_usd": 80.0,
        },
    )
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_model_gateway_auth_context] = (
        lambda: ModelGatewayAuthContext(
            token="synthetic-authenticated-user", user=test_user
        )
    )
    with (
        patch("preloop.services.unpriced_model_alert.notify_admins") as notify_admins,
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value={
                "id": "synthetic-response",
                "object": "chat.completion",
                "model": "google/gemini-3.8-flash",
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
                "model": requested_alias,
                "messages": [{"role": "user", "content": "hello"}],
                "max_tokens": 10,
            },
        )
        second = client.post(
            "/openai/v1/chat/completions",
            json={
                "model": requested_alias,
                "messages": [{"role": "user", "content": "hello again"}],
                "max_tokens": 10,
            },
        )

    assert response.status_code == 200, response.text
    assert second.status_code == 200, second.text
    assert provider.call_count == 2
    warning = response.headers["X-Preloop-Warning"]
    assert "budget_pricing_unavailable" in warning
    assert requested_alias in warning
    assert "counted as $0.00 spend" in warning
    # One page per model per cooldown, not one per refused request.
    assert notify_admins.call_count == 1

    gateway_rows = (
        db_session.query(models.ApiUsage)
        .filter(models.ApiUsage.ai_model_id == ai_model.id)
        .all()
    )
    assert gateway_rows, "the served request must still be recorded"
    for row in gateway_rows:
        assert float(row.estimated_cost or 0.0) == 0.0
        assert row.meta_data["budget"]["pricing_available"] is False


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
        patch.object(crud_budget_policy, "get_gateway_policies") as lookup,
    ):
        get_budget_enforcer().enforce_or_raise(
            db_session, auth, model, {"max_tokens": 10}
        )
    lookup.assert_not_called()


def test_legacy_model_policy_lookup_is_account_scoped(
    db_session: Session, test_user: models.User
) -> None:
    import uuid
    from preloop.models.crud import crud_account

    model_id = uuid.uuid4()
    foreign = crud_account.create(
        db_session, obj_in={"organization_name": "Other account"}
    )
    own = crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "subject_type": "ai_model",
            "subject_id": model_id,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": 10,
        },
    )
    own_id = own.id
    crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": foreign.id,
            "subject_type": "ai_model",
            "subject_id": model_id,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": 0,
        },
    )
    found = crud_budget_policy.get_gateway_policies(
        db_session,
        account_id=test_user.account_id,
        ai_model_id=model_id,
        model_alias="synthetic-model",
    )
    assert [policy.id for policy in found] == [own_id]


@pytest.mark.parametrize(
    "route, body",
    [
        (
            "/openai/v1/chat/completions",
            {"messages": [{"role": "user", "content": "hello"}]},
        ),
        ("/openai/v1/responses", {"input": "hello"}),
    ],
)
def test_unpriced_hard_limit_warning_reaches_streaming_callers(
    db_session: Session,
    test_user: models.User,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
    body: dict,
) -> None:
    """A ``stream: true`` caller gets the same header as a non-streaming one.

    Most chat traffic streams. The service resolves the model and runs budget
    preflight before it hands back the SSE body generator, so the warning is
    known while the headers can still be set; this pins that the endpoint
    puts it there instead of dropping it (issue #810).
    """
    from preloop.services import unpriced_model_alert

    unpriced_model_alert.reset_alert_state_for_tests()
    monkeypatch.setenv("PRELOOP_SERVICE_ROLE", "gateway")
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.setattr(settings, "disable_rbac", True)
    monkeypatch.setattr("preloop.plugins.get_plugin_manager", PluginManager)
    monkeypatch.setattr("preloop.plugins.base._plugin_manager", None)
    ai_model = crud_ai_model.create_with_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "name": "Uncatalogued hosted model",
            "provider_name": "openai",
            "model_identifier": "google/gemini-3.8-flash",
            "api_key": "unused-synthetic-key",
            "meta_data": {
                # The mock below is litellm.completion, so keep /responses on
                # the transcode path rather than a native passthrough POST.
                "gateway": {"enabled": True, "responses_api": "transcode"},
                "pricing": {},
            },
        },
    )
    from preloop.services.model_runtime_resolver import resolve_ai_model_runtime

    requested_alias = resolve_ai_model_runtime(ai_model).model_gateway_model_alias
    crud_budget_policy.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "subject_type": "account",
            "subject_id": None,
            "period": models.BudgetPeriod.monthly,
            "hard_limit_usd": 100.0,
            "soft_limit_usd": 80.0,
        },
    )
    app = create_app()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_model_gateway_auth_context] = (
        lambda: ModelGatewayAuthContext(
            token="synthetic-authenticated-user", user=test_user
        )
    )
    chunks = [
        {
            "id": "chatcmpl_stream",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {"content": "ok"}}],
        },
        {
            "id": "chatcmpl_stream",
            "created": 1710000000,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    with (
        patch("preloop.services.unpriced_model_alert.notify_admins"),
        patch(
            "preloop.services.openai_gateway.litellm.completion",
            return_value=iter(chunks),
        ) as provider,
        TestClient(app) as client,
    ):
        response = client.post(
            route,
            json={"model": requested_alias, "stream": True, **body},
        )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in response.text
    assert provider.call_count == 1
    warning = response.headers["X-Preloop-Warning"]
    assert "budget_pricing_unavailable" in warning
    assert requested_alias in warning
    assert "counted as $0.00 spend" in warning
