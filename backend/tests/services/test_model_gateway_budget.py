"""Tests for model gateway budget enforcement."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.models.crud.plan import plan as crud_plan
from preloop.models.crud.plan import subscription as crud_subscription
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import ModelGatewayBudgetService


def test_enforce_or_raise_reports_trial_hosted_model_limit(db_session, test_user):
    """Trial hosted-model hard caps should return the specific BYOK guidance."""
    now = datetime.now(timezone.utc)
    crud_plan.create(
        db_session,
        obj_in={
            "id": "teams",
            "name": "Teams",
            "price_monthly": 0.0,
            "price_annually": 0.0,
            "is_active": True,
            "features": {},
            "is_custom": False,
        },
    )
    crud_subscription.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "plan_id": "teams",
            "status": "trialing",
            "current_period_start": now - timedelta(days=1),
            "current_period_end": now + timedelta(days=13),
        },
    )
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Hosted Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "meta_data": {
                "hosted": True,
                "pricing": {"price_per_1k": 100.0},
            },
        },
        account_id=test_user.account_id,
    )
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="trial-token", user=test_user),
    )

    with pytest.raises(HTTPException) as exc_info:
        service.enforce_or_raise(ai_model, {"model": "openai/gpt-5", "input": "Hi"})

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == (
        "Preloop trial limit for hosted model reached. Please configure your own "
        "OpenAI/Anthropic API key."
    )


def test_budget_preflight_uses_account_model_price_override(
    db_session, test_user, monkeypatch
):
    """Account-scoped pricing overrides should drive preflight cost estimates."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Negotiated GPT",
            "provider_name": "openai",
            "model_identifier": "gpt-4o",
            "meta_data": {"gateway": {"model_alias": "openai/gpt-4o"}},
        },
        account_id=test_user.account_id,
    )
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="override-token", user=test_user),
    )
    monkeypatch.setattr(
        service,
        "_pricing_override_for_request",
        lambda *_args, **_kwargs: {
            "currency": "USD",
            "input_price_per_1k": 1.0,
            "output_price_per_1k": 2.0,
        },
    )

    result = service.preflight_check(
        ai_model,
        {"model": "openai/gpt-4o", "input": "hello", "max_tokens": 1000},
    )

    assert result.pricing_available is True
    assert result.estimated_request_cost_usd == 2.002


def _hosted_model(db_session, test_user):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Hosted Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "meta_data": {
                "hosted": True,
                "pricing": {"price_per_1k": 100.0},
            },
        },
        account_id=test_user.account_id,
    )


def test_free_account_hosted_model_is_capped(db_session, test_user):
    """No-subscription (card-free) accounts hit the free hosted cap.

    Regression for the open faucet: before T23, only ``trialing``
    subscriptions were capped, so free accounts spent unmetered.
    """
    ai_model = _hosted_model(db_session, test_user)
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="free-token", user=test_user),
    )

    with pytest.raises(HTTPException) as exc_info:
        service.enforce_or_raise(ai_model, {"model": "openai/gpt-5", "input": "Hi"})

    assert exc_info.value.status_code == 403
    assert "free-tier limit" in exc_info.value.detail


def test_free_account_non_hosted_model_not_capped(db_session, test_user):
    """The free cap only applies to built-in hosted models (BYOK is free)."""
    ai_model = crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "BYOK Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "meta_data": {"pricing": {"price_per_1k": 100.0}},
        },
        account_id=test_user.account_id,
    )
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="free-token", user=test_user),
    )

    result = service.enforce_or_raise(
        ai_model, {"model": "openai/gpt-5", "input": "Hi"}
    )
    assert result.hard_limit_exceeded is False


def test_free_cap_skipped_when_enforcement_disabled(db_session, test_user, monkeypatch):
    """Self-hosted EE (paywall off) keeps hosted models uncapped."""
    from preloop.config import settings

    monkeypatch.setattr(settings, "billing_enforce_entitlements", False)
    ai_model = _hosted_model(db_session, test_user)
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="free-token", user=test_user),
    )

    result = service.enforce_or_raise(
        ai_model, {"model": "openai/gpt-5", "input": "Hi"}
    )
    assert result.hard_limit_exceeded is False


def test_active_subscription_not_hit_by_free_cap(db_session, test_user):
    """Paying accounts never enter the free-cap branch."""
    now = datetime.now(timezone.utc)
    crud_plan.create(
        db_session,
        obj_in={
            "id": "teams",
            "name": "Teams",
            "price_monthly": 0.0,
            "price_annually": 0.0,
            "is_active": True,
            "features": {},
            "is_custom": False,
        },
    )
    crud_subscription.create(
        db_session,
        obj_in={
            "account_id": test_user.account_id,
            "plan_id": "teams",
            "status": "active",
            "current_period_start": now - timedelta(days=1),
            "current_period_end": now + timedelta(days=29),
        },
    )
    ai_model = _hosted_model(db_session, test_user)
    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="paid-token", user=test_user),
    )

    result = service.enforce_or_raise(
        ai_model, {"model": "openai/gpt-5", "input": "Hi"}
    )
    assert result.hard_limit_exceeded is False
    assert result.enforcement_reason is None


def test_free_cap_spend_includes_expensive_hosted_model_behind_cheap_traffic(
    db_session, test_user
):
    """Hosted spend must survive high-volume cheap BYOK traffic.

    Regression: the cap summed only the top-N models *by request count*, so
    thousands of cheap BYOK calls pushed a low-volume, high-cost hosted model
    past the row cutoff. Its spend was never summed and the account counted as
    $0 against the hosted hard cap — silent non-enforcement of the paywall.
    """
    hosted_model = _hosted_model(db_session, test_user)
    for index in range(25):
        cheap_model = crud_ai_model.create_with_account(
            db=db_session,
            obj_in={
                "name": f"Cheap BYOK Model {index}",
                "provider_name": "anthropic",
                "model_identifier": f"claude-haiku-{index}",
                "meta_data": {"pricing": {"price_per_1k": 0.001}},
            },
            account_id=test_user.account_id,
        )
        for _ in range(5):
            crud_api_usage.log_gateway_request(
                db_session,
                endpoint="/anthropic/v1/messages",
                method="POST",
                status_code=200,
                duration=0.1,
                user_id=str(test_user.id),
                account_id=str(test_user.account_id),
                ai_model_id=str(cheap_model.id),
                model_alias=f"anthropic/claude-haiku-{index}",
                provider_name="anthropic",
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
                estimated_cost=0.001,
            )

    # Two expensive hosted calls: lowest request count, highest spend.
    for _ in range(2):
        crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/openai/v1/responses",
            method="POST",
            status_code=200,
            duration=0.1,
            user_id=str(test_user.id),
            account_id=str(test_user.account_id),
            ai_model_id=str(hosted_model.id),
            model_alias="openai/gpt-5",
            provider_name="openai",
            prompt_tokens=1000,
            completion_tokens=1000,
            total_tokens=2000,
            estimated_cost=5.0,
        )

    service = ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(token="free-token", user=test_user),
    )
    now = datetime.now(timezone.utc)
    spend = service._get_trial_hosted_model_spend(
        account_id=str(test_user.account_id),
        start=now - timedelta(days=1),
        end=now + timedelta(days=1),
    )

    assert spend == pytest.approx(10.0)
