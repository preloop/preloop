"""Tests for the shared pricing-override resolver."""

from preloop.models.crud import crud_ai_model, crud_model_price_override
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_budget import ModelGatewayBudgetService
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.pricing_overrides import resolve_pricing_override


def _create_model(db_session, test_user, alias="openai/gpt-5"):
    return crud_ai_model.create_with_account(
        db=db_session,
        obj_in={
            "name": "Gateway Model",
            "provider_name": "openai",
            "model_identifier": "gpt-5",
            "api_key": "provider-secret",
            "meta_data": {
                "gateway": {
                    "enabled": True,
                    "model_alias": alias,
                    "provider_adapter": "preloop",
                }
            },
        },
        account_id=test_user.account_id,
    )


def test_resolver_prefers_gateway_alias(db_session, test_user):
    """The model's configured gateway alias wins over the requested alias."""
    ai_model = _create_model(db_session, test_user)
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "openai/gpt-5", "input_price_per_1k": 1.0},
    )
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "client-alias", "input_price_per_1k": 9.0},
    )

    pricing = resolve_pricing_override(
        db_session,
        account_id=test_user.account_id,
        ai_model=ai_model,
        requested_alias="client-alias",
    )
    assert pricing is not None
    assert pricing["input_price_per_1k"] == 1.0


def test_resolver_falls_back_to_requested_alias(db_session, test_user):
    """Without a gateway-alias override, the requested alias resolves."""
    ai_model = _create_model(db_session, test_user)
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "client-alias", "input_price_per_1k": 9.0},
    )

    pricing = resolve_pricing_override(
        db_session,
        account_id=test_user.account_id,
        ai_model=ai_model,
        requested_alias="client-alias",
    )
    assert pricing is not None
    assert pricing["input_price_per_1k"] == 9.0
    assert "id" in pricing


def test_gateway_and_budget_paths_resolve_identical_override(db_session, test_user):
    """Parity: the recording path and budget preflight see the same override."""
    ai_model = _create_model(db_session, test_user)
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "openai/gpt-5", "input_price_per_1k": 2.5},
    )
    auth_context = ModelGatewayAuthContext(token="runtime-token", user=test_user)

    gateway_pricing = OpenAIGatewayService(
        db_session, auth_context
    )._pricing_override_for_request(ai_model=ai_model, model_alias="openai/gpt-5")
    budget_pricing = ModelGatewayBudgetService(
        db_session, auth_context
    )._pricing_override_for_request(ai_model, {"model": "openai/gpt-5"})

    assert gateway_pricing is not None and budget_pricing is not None
    assert gateway_pricing["id"] == budget_pricing["id"]
    assert (
        gateway_pricing["input_price_per_1k"]
        == budget_pricing["input_price_per_1k"]
        == 2.5
    )


def test_resolver_returns_none_without_override(db_session, test_user):
    """No override configured resolves to None (list price applies)."""
    ai_model = _create_model(db_session, test_user)
    assert (
        resolve_pricing_override(
            db_session, account_id=test_user.account_id, ai_model=ai_model
        )
        is None
    )


def test_bulk_resolver_agrees_with_the_per_model_resolver(db_session, test_user):
    """The batch path must price a fleet exactly as the single path does.

    The Models page resolves every model's price from one query. That is only
    safe while the in-memory matcher and the SQL lookup pick the same row, so
    this asserts they agree across the cases that differ: a model-specific
    override, an account-wide wildcard, an expired window, and no override at
    all.
    """
    from datetime import datetime, timedelta, timezone

    from preloop.services.pricing_overrides import (
        resolve_active_override_row,
        resolve_pricing_overrides_bulk,
    )

    specific = _create_model(db_session, test_user, alias="openai/specific")
    wildcard = _create_model(db_session, test_user, alias="openai/wildcard")
    expired = _create_model(db_session, test_user, alias="openai/expired")
    unpriced = _create_model(db_session, test_user, alias="openai/unpriced")

    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "model_alias": "openai/specific",
            "ai_model_id": specific.id,
            "input_price_per_1k": 1.0,
        },
    )
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "openai/specific", "input_price_per_1k": 2.0},
    )
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={"model_alias": "openai/wildcard", "input_price_per_1k": 3.0},
    )
    crud_model_price_override.create_for_account(
        db_session,
        account_id=test_user.account_id,
        obj_in={
            "model_alias": "openai/expired",
            "input_price_per_1k": 4.0,
            "effective_until": datetime.now(timezone.utc) - timedelta(days=1),
        },
    )
    db_session.flush()

    models = [specific, wildcard, expired, unpriced]
    bulk = resolve_pricing_overrides_bulk(
        db_session, account_id=test_user.account_id, ai_models=models
    )

    for ai_model in models:
        single = resolve_active_override_row(
            db_session, account_id=test_user.account_id, ai_model=ai_model
        )
        batched = bulk.get(str(ai_model.id))
        assert (single is None) == (batched is None), ai_model.name
        if single is not None and batched is not None:
            assert str(single.id) == str(batched.id), ai_model.name

    # And the values themselves: model-specific beats the account wildcard.
    assert bulk[str(specific.id)].input_price_per_1k == 1.0
    assert bulk[str(wildcard.id)].input_price_per_1k == 3.0
    assert str(expired.id) not in bulk
    assert str(unpriced.id) not in bulk
