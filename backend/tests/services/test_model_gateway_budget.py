"""Tests for model gateway budget enforcement."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from preloop.models.crud import (
    crud_account,
    crud_ai_model,
    crud_api_key,
    crud_api_usage,
)
from preloop.models.crud.plan import plan as crud_plan
from preloop.models.crud.plan import subscription as crud_subscription
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import ModelGatewayBudgetService
from preloop.services.subject_governance import (
    SUBJECT_TYPE_API_KEYS,
    set_subject_governance,
)


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


def _governed_model(db_session, test_user, **overrides):
    """Create a gateway-enabled model whose canonical alias is prefixed."""
    obj_in = {
        "name": "Governed Opus",
        "provider_name": "anthropic",
        "model_identifier": "claude-opus-4-1",
        "meta_data": {
            "gateway": {"enabled": True},
            "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
        },
    }
    obj_in.update(overrides)
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in=obj_in,
        account_id=test_user.account_id,
    )


def _key_scoped_allowlist(db_session, test_user, allowed_models):
    """Attach a per-API-key allowed_models policy and return the key."""
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Governed Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    account = crud_account.get(db_session, id=test_user.account_id)
    crud_account.update(
        db_session,
        db_obj=account,
        obj_in={
            "meta_data": set_subject_governance(
                account.meta_data or {},
                subject_type=SUBJECT_TYPE_API_KEYS,
                subject_id=str(api_key.id),
                config={"allowed_models": list(allowed_models)},
            )
        },
    )
    return api_key


def _governed_service(db_session, test_user, api_key):
    return ModelGatewayBudgetService(
        db_session,
        ModelGatewayAuthContext(
            token="governed-token", user=test_user, api_key=api_key
        ),
    )


@pytest.mark.parametrize("wire_model", ["anthropic/claude-opus-4-1", "claude-opus-4-1"])
def test_allowlist_governs_every_spelling_of_the_same_model(
    db_session, test_user, wire_model
):
    """Both spellings resolve to one model, so one allowlist entry covers both."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(
        db_session, test_user, ["anthropic/claude-opus-4-1"]
    )
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"model": wire_model, "input": "hi"})

    assert result.hard_limit_exceeded is False
    assert result.enforcement_reason is None


@pytest.mark.parametrize("wire_model", ["anthropic/claude-opus-4-1", "claude-opus-4-1"])
def test_allowlist_denies_every_spelling_of_a_disallowed_model(
    db_session, test_user, wire_model
):
    """A model absent from the allowlist is denied however the client spells it."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(db_session, test_user, ["openai/gpt-4o"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"model": wire_model, "input": "hi"})

    assert result.hard_limit_exceeded is True
    assert result.enforcement_reason == "subject_model_not_allowed"


def test_allowlist_accepts_bare_identifier_entry_for_prefixed_request(
    db_session, test_user
):
    """Legacy allowlists holding bare ids still cover prefixed wire spellings."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(db_session, test_user, ["claude-opus-4-1"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        ai_model, {"model": "anthropic/claude-opus-4-1", "input": "hi"}
    )

    assert result.hard_limit_exceeded is False


def test_blank_wire_model_still_enforces_allowlist(db_session, test_user):
    """A payload without a model must not skip the allowlist check."""
    ai_model = _governed_model(db_session, test_user, model_identifier="")
    api_key = _key_scoped_allowlist(db_session, test_user, ["openai/gpt-4o"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"input": "hi"})

    assert result.hard_limit_exceeded is True
    assert result.enforcement_reason == "subject_model_not_allowed"


def test_blank_wire_model_resolves_to_allowed_model(db_session, test_user):
    """Omitting the wire model is fine when the resolved model is allowed."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(
        db_session, test_user, ["anthropic/claude-opus-4-1"]
    )
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"input": "hi"})

    assert result.hard_limit_exceeded is False


def test_account_without_allowlist_is_unaffected(db_session, test_user):
    """No configured allowed_models means no model governance at all."""
    ai_model = _governed_model(db_session, test_user)
    api_key, _ = crud_api_key.create_runtime_key(
        db_session,
        name="Ungoverned Runtime Token",
        account_id=test_user.account_id,
        user_id=test_user.id,
        context_data={},
    )
    service = _governed_service(db_session, test_user, api_key)

    for payload in ({"model": "anthropic/claude-opus-4-1"}, {"model": ""}, {}):
        result = service.preflight_check(ai_model, {**payload, "input": "hi"})
        assert result.hard_limit_exceeded is False
        assert result.enforcement_reason is None


def test_allowlist_accepts_display_name_entry(db_session, test_user):
    """The console stored display names; ``Kimi K3`` must govern its row."""
    ai_model = _governed_model(
        db_session,
        test_user,
        name="Kimi K3",
        provider_name="moonshot",
        model_identifier="kimi-k3",
    )
    api_key = _key_scoped_allowlist(db_session, test_user, ["GLM 5.3 Flash", "Kimi K3"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        ai_model, {"model": "moonshot/kimi-k3", "input": "hi"}
    )

    assert result.hard_limit_exceeded is False
    assert result.enforcement_reason is None


def test_allowlist_display_name_match_is_case_insensitive_and_trimmed(
    db_session, test_user
):
    """Hand-typed names differ in case and whitespace from the stored row."""
    ai_model = _governed_model(db_session, test_user, name="Kimi K3")
    api_key = _key_scoped_allowlist(db_session, test_user, ["  kimi k3 "])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"model": "claude-opus-4-1"})

    assert result.hard_limit_exceeded is False


def test_allowlist_accepts_model_id_entry(db_session, test_user):
    """The deploy wizard persists AIModel ids; those must govern too."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(db_session, test_user, [str(ai_model.id)])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        ai_model, {"model": "anthropic/claude-opus-4-1", "input": "hi"}
    )

    assert result.hard_limit_exceeded is False


def test_allowlist_accepts_configured_gateway_alias_entry(db_session, test_user):
    """An explicit ``meta_data.gateway.model_alias`` is the preferred key."""
    ai_model = _governed_model(
        db_session,
        test_user,
        meta_data={
            "gateway": {"enabled": True, "model_alias": "team/opus"},
            "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
        },
    )
    api_key = _key_scoped_allowlist(db_session, test_user, ["team/opus"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"model": "team/opus", "input": "hi"})

    assert result.hard_limit_exceeded is False


def test_allowlist_unrelated_display_name_still_denies(db_session, test_user):
    """Naming a different model by display name must not open the gate."""
    ai_model = _governed_model(db_session, test_user, name="Governed Opus")
    _governed_model(
        db_session,
        test_user,
        name="Kimi K3",
        provider_name="moonshot",
        model_identifier="kimi-k3",
    )
    api_key = _key_scoped_allowlist(db_session, test_user, ["Kimi K3"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        ai_model, {"model": "anthropic/claude-opus-4-1", "input": "hi"}
    )

    assert result.hard_limit_exceeded is True
    assert result.enforcement_reason == "subject_model_not_allowed"
    assert result.allowed_models == ["Kimi K3"]
    assert result.requested_model == "anthropic/claude-opus-4-1"


def test_allowlist_denial_names_alias_when_request_omits_model(db_session, test_user):
    """Without a wire model the denial quotes the resolved gateway alias."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(db_session, test_user, ["Kimi K3"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(ai_model, {"input": "hi"})

    assert result.hard_limit_exceeded is True
    assert result.requested_model == "anthropic/claude-opus-4-1"


def test_empty_allowlist_allows_every_model(db_session, test_user):
    """An empty list is "unrestricted", not "nothing allowed"."""
    ai_model = _governed_model(db_session, test_user)
    api_key = _key_scoped_allowlist(db_session, test_user, [])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        ai_model, {"model": "anthropic/claude-opus-4-1", "input": "hi"}
    )

    assert result.hard_limit_exceeded is False
    assert result.enforcement_reason is None
    assert result.allowed_models is None


def test_display_name_of_a_sibling_row_does_not_cover_a_different_import(
    db_session, test_user
):
    """``Kimi K3`` names the moonshot row only, not a separate openai-provider import.

    This is the founder's prod shape: the OpenCode onboarding created a second
    row ``moonshotai/kimi-k3`` next to the existing ``Kimi K3``
    (``moonshot/kimi-k3``). Governance keys on rows, so the allowlist must be
    extended (step 4 in the CLI offers that) or the request must pick the
    listed row's alias.
    """
    _governed_model(
        db_session,
        test_user,
        name="Kimi K3",
        provider_name="moonshot",
        model_identifier="kimi-k3",
    )
    opencode_import = _governed_model(
        db_session,
        test_user,
        name="OpenCode moonshotai/kimi-k3",
        provider_name="openai",
        model_identifier="kimi-k3",
        meta_data={
            "gateway": {"enabled": True, "model_alias": "moonshotai/kimi-k3"},
            "pricing": {"input_price_per_1k": 0.01, "output_price_per_1k": 0.02},
        },
    )
    api_key = _key_scoped_allowlist(db_session, test_user, ["GLM 5.3 Flash", "Kimi K3"])
    service = _governed_service(db_session, test_user, api_key)

    result = service.preflight_check(
        opencode_import, {"model": "moonshotai/kimi-k3", "input": "hi"}
    )

    assert result.hard_limit_exceeded is True
    assert result.enforcement_reason == "subject_model_not_allowed"
