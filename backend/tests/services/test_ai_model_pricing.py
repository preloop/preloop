"""Tests for reading a model's effective price and fetching a published one."""

from datetime import datetime, timedelta, timezone

import pytest

from preloop.models.crud import crud_ai_model
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.services import ai_model_pricing, model_price_catalog


def _model(db_session, account_id, **overrides):
    payload = {
        "name": "Pricing Model",
        "provider_name": "openai",
        "model_identifier": "gpt-4o-mini",
    }
    payload.update(overrides)
    return crud_ai_model.create_with_account(
        db=db_session, obj_in=payload, account_id=account_id
    )


def test_effective_pricing_reads_the_catalog_in_per_million(db_session, test_user):
    """A model nobody overrode is priced by the catalog, quoted per million."""
    model = _model(db_session, test_user.account_id)

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "catalog"
    assert pricing.catalog_key is not None
    # litellm quotes per token; anything else means the conversion is wrong.
    assert 0 < (pricing.price.input_per_1m or 0) < 100
    assert (pricing.price.output_per_1m or 0) > 0
    # OpenAI publishes no machine-readable price list, so no fetch is offered.
    assert pricing.fetch_supported is False
    assert pricing.fetch_provider_label == "OpenAI"


def test_effective_pricing_reports_a_model_nothing_can_price(db_session, test_user):
    """No override, no configured price, no catalog entry: say so plainly."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="custom",
        model_identifier="no-such-model-anywhere-9000",
        api_endpoint="https://models.internal.example.com/v1",
    )

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "none"
    assert pricing.price.input_per_1m is None
    assert pricing.fetch_supported is False


def test_effective_pricing_prefers_the_account_override(db_session, test_user):
    """An override is a human decision and wins over every list price."""
    model = _model(db_session, test_user.account_id)
    effective_from = datetime.now(timezone.utc) - timedelta(days=2)
    db_session.add(
        ModelPriceOverride(
            account_id=test_user.account_id,
            ai_model_id=model.id,
            model_alias="gpt-4o-mini",
            provider_name="openai",
            currency="USD",
            input_price_per_1k=0.002,
            output_price_per_1k=0.008,
            cache_read_input_price_per_1k=0.0005,
            request_price=0.01,
            effective_from=effective_from,
            is_active=True,
        )
    )
    db_session.commit()

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "override"
    assert pricing.price.input_per_1m == 2.0
    assert pricing.price.output_per_1m == 8.0
    assert pricing.price.cached_input_per_1m == 0.5
    assert pricing.price.request_price == 0.01
    assert pricing.override_id is not None
    assert pricing.effective_from is not None


def test_effective_pricing_uses_pricing_configured_on_the_model(db_session, test_user):
    """Pricing stored on the model itself reads as model_config, not catalog."""
    model = _model(
        db_session,
        test_user.account_id,
        model_identifier="gpt-4o-mini",
        meta_data={
            "pricing": {"input_price_per_1k": 0.001, "output_price_per_1k": 0.003}
        },
    )

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "model_config"
    assert pricing.price.input_per_1m == 1.0
    assert pricing.price.output_per_1m == 3.0


def test_openrouter_model_offers_a_price_fetch(db_session, test_user, monkeypatch):
    """OpenRouter publishes prices, so the model page can offer to read them."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="openrouter",
        model_identifier="anthropic/claude-sonnet-4",
    )

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )
    assert pricing.fetch_supported is True
    assert pricing.fetch_provider_label == "OpenRouter"

    monkeypatch.setattr(
        model_price_catalog,
        "fetch_openrouter_price_map",
        lambda: {
            "openrouter/anthropic/claude-sonnet-4": {
                "litellm_provider": "openrouter",
                "input_cost_per_token": 0.000003,
                "output_cost_per_token": 0.000015,
                "cache_read_input_token_cost": 0.0000003,
            }
        },
    )

    quote = ai_model_pricing.fetch_provider_pricing(model)

    assert quote.model_key == "anthropic/claude-sonnet-4"
    assert quote.price.input_per_1m == 3.0
    assert quote.price.output_per_1m == 15.0
    assert quote.price.cached_input_per_1m == 0.3
    assert quote.source_url == model_price_catalog.OPENROUTER_MODELS_URL
    # A fetch is a read: nothing about the account's prices may change.
    assert (
        db_session.query(ModelPriceOverride)
        .filter(ModelPriceOverride.account_id == test_user.account_id)
        .count()
        == 0
    )


def test_price_fetch_follows_an_openai_compatible_endpoint(db_session, test_user):
    """What answers the endpoint decides, not the label somebody typed."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="openai-compatible",
        model_identifier="anthropic/claude-sonnet-4",
        api_endpoint="https://openrouter.ai/api/v1",
    )

    assert ai_model_pricing.provider_supports_price_fetch(model) is True


def test_price_fetch_refuses_a_provider_that_publishes_nothing(db_session, test_user):
    """Anthropic has no price API, so the fetch says so instead of guessing."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="anthropic",
        model_identifier="claude-sonnet-4",
    )

    with pytest.raises(ai_model_pricing.PriceFetchUnsupportedError) as excinfo:
        ai_model_pricing.fetch_provider_pricing(model)
    assert "Anthropic" in str(excinfo.value)


def test_price_fetch_reports_a_model_the_provider_does_not_list(
    db_session, test_user, monkeypatch
):
    """An unreachable or silent price list is not a price of zero."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="openrouter",
        model_identifier="vendor/model-nobody-lists",
    )
    monkeypatch.setattr(
        model_price_catalog, "fetch_openrouter_price_map", lambda: {"openrouter/x": {}}
    )

    with pytest.raises(ai_model_pricing.PriceFetchUnavailableError):
        ai_model_pricing.fetch_provider_pricing(model)

    monkeypatch.setattr(model_price_catalog, "fetch_openrouter_price_map", lambda: None)
    with pytest.raises(ai_model_pricing.PriceFetchUnavailableError):
        ai_model_pricing.fetch_provider_pricing(model)


def test_a_malformed_request_price_reads_as_unpriced(db_session, test_user):
    """Account-supplied JSON must not turn the Pricing card into a 500.

    ``meta_data["pricing"]`` is whatever was stored on the model, so a
    request price of "n/a" is a shape this code has to survive: the rest of
    the price still shows and the request line reads as no price at all.
    """
    model = _model(
        db_session,
        test_user.account_id,
        meta_data={
            "pricing": {
                "input_price_per_1k": 0.001,
                "output_price_per_1k": 0.003,
                "request_price": "n/a",
            }
        },
    )

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "model_config"
    assert pricing.price.input_per_1m == 1.0
    assert pricing.price.output_per_1m == 3.0
    assert pricing.price.request_price is None


def test_a_price_that_is_only_a_malformed_request_price_is_no_price(
    db_session, test_user
):
    """Nothing readable in the configured dict falls through to the catalog."""
    model = _model(
        db_session,
        test_user.account_id,
        provider_name="custom",
        model_identifier="no-such-model-anywhere-9000",
        api_endpoint="https://models.internal.example.com/v1",
        meta_data={"pricing": {"request_price": ["not", "a", "number"]}},
    )

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "none"
    assert pricing.price.request_price is None


def test_a_malformed_catalog_request_price_reads_as_unpriced(
    db_session, test_user, monkeypatch
):
    """The third-party price map gets the same guard the stored dict gets."""
    entry = {
        "input_cost_per_token": 0.000001,
        "output_cost_per_token": 0.000003,
        "input_cost_per_request": "free",
    }
    monkeypatch.setattr(
        ai_model_pricing,
        "_catalog_entry",
        lambda ai_model: ("vendor/model", entry),
    )
    model = _model(db_session, test_user.account_id)

    pricing = ai_model_pricing.get_effective_pricing(
        db_session, account_id=test_user.account_id, ai_model=model
    )

    assert pricing.source == "catalog"
    assert pricing.price.input_per_1m == 1.0
    assert pricing.price.request_price is None
