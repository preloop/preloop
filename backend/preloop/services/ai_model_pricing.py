"""Read one model's effective price, and fetch a provider's published price.

Two questions the console could not answer before this module: what is this
model priced at right now and where did that number come from, and can the
provider tell us what it should be. Both are read-only; nothing here writes
a price. Saving one stays with the override endpoints, so a fetched number
is always confirmed by a person before it starts changing what spend means.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from sqlalchemy.orm import Session

from preloop.models.models.ai_model import AIModel
from preloop.models.models.model_price_override import ModelPriceOverride
from preloop.schemas.ai_model_pricing import (
    AIModelPrice,
    AIModelPriceQuote,
    AIModelPricingResponse,
)
from preloop.services import model_price_catalog
from preloop.services.model_pricing import (
    _get_configured_pricing,
    _iter_litellm_model_candidates,
)
from preloop.services.pricing_overrides import (
    resolve_active_override_row,
    resolve_pricing_overrides_bulk,
)

logger = logging.getLogger(__name__)

#: Providers that publish machine-readable prices Preloop can read back.
#: OpenRouter's ``GET /api/v1/models`` carries a ``pricing`` block per model;
#: no other supported provider serves prices from its API, so the console
#: offers the fetch to these and disables it, by name, for the rest.
PRICE_FETCH_PROVIDERS = {"openrouter"}

#: Shown when a provider does not publish prices.
PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "gemini": "Gemini",
    "bedrock": "Bedrock",
    "azure": "Azure OpenAI",
    "openrouter": "OpenRouter",
    "deepseek": "DeepSeek",
    "mistral": "Mistral",
    "groq": "Groq",
    "together": "Together",
    "ollama": "Ollama",
    "openai-compatible": "this endpoint",
    "custom": "this endpoint",
}

_PER_1K_TO_PER_1M = 1000.0
_PER_TOKEN_TO_PER_1M = 1_000_000.0


class PriceFetchUnsupportedError(ValueError):
    """The provider does not publish prices Preloop can read."""


class PriceFetchUnavailableError(RuntimeError):
    """The provider publishes prices but this model is not among them."""


def provider_label(provider_name: Optional[str]) -> str:
    """Return a display name for a provider, falling back to the raw value."""
    provider = (provider_name or "").strip().lower()
    if not provider:
        return "this provider"
    return PROVIDER_LABELS.get(provider, provider_name or "this provider")


def provider_supports_price_fetch(ai_model: AIModel) -> bool:
    """Report whether the model's provider publishes readable prices.

    An OpenAI-compatible model pointed at OpenRouter's base URL counts: the
    endpoint is what answers, not the label somebody typed.
    """
    provider = (ai_model.provider_name or "").strip().lower()
    if provider in PRICE_FETCH_PROVIDERS:
        return True
    endpoint = (getattr(ai_model, "api_endpoint", None) or "").strip().lower()
    return "openrouter.ai" in endpoint


def _model_alias(ai_model: AIModel) -> Optional[str]:
    """Return the gateway alias clients use for this model, if configured."""
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    gateway = meta_data.get("gateway")
    if isinstance(gateway, dict):
        alias = gateway.get("model_alias")
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
    return ai_model.model_identifier


def _flat_price(value: Any) -> Optional[float]:
    """Read a per-request price as it is stored, in dollars, or read nothing.

    Everything else on the card is scaled to a per-million quote; a request
    price is already a flat dollar amount, so it only needs the same guard.
    Both dicts this module converts are account-supplied JSON (the pricing
    stored on the model, an override row) or a third-party price map, so a
    value that is not a number is a page the operator cannot open, not an
    exception worth raising: it reads as unpriced, exactly like a missing one.
    """
    if value is None:
        return None
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def _price_from_configured(pricing: Dict[str, Any]) -> AIModelPrice:
    """Convert a stored per-1k pricing dict into per-million display prices."""

    def scaled(key: str) -> Optional[float]:
        value = pricing.get(key)
        if value is None:
            return None
        try:
            return round(float(value) * _PER_1K_TO_PER_1M, 6)
        except (TypeError, ValueError):
            return None

    return AIModelPrice(
        input_per_1m=scaled("input_price_per_1k"),
        output_per_1m=scaled("output_price_per_1k"),
        cached_input_per_1m=scaled("cache_read_input_price_per_1k"),
        blended_per_1m=scaled("price_per_1k"),
        request_price=_flat_price(pricing.get("request_price")),
    )


def _price_from_catalog_entry(entry: Dict[str, Any]) -> AIModelPrice:
    """Convert a litellm price entry (USD per token) into per-million prices."""

    def scaled(key: str) -> Optional[float]:
        value = entry.get(key)
        if value is None:
            return None
        try:
            return round(float(value) * _PER_TOKEN_TO_PER_1M, 6)
        except (TypeError, ValueError):
            return None

    return AIModelPrice(
        input_per_1m=scaled("input_cost_per_token"),
        output_per_1m=scaled("output_cost_per_token"),
        cached_input_per_1m=scaled("cache_read_input_token_cost"),
        request_price=_flat_price(entry.get("input_cost_per_request")),
    )


def _catalog_entry(ai_model: AIModel) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Return the price list entry that would price this model, if any.

    Reads litellm's in-process map only: this is a page load, so it must not
    reach the network. A model the catalog cannot price reads as "none" and
    the gateway's own background lookup fills the map later.
    """
    try:
        import litellm

        model_cost = getattr(litellm, "model_cost", None) or {}
    except Exception:  # noqa: BLE001 - litellm import is best-effort here
        logger.debug("litellm price map unavailable", exc_info=True)
        return None

    for candidate in _iter_litellm_model_candidates(ai_model):
        entry = model_cost.get(candidate)
        if isinstance(entry, dict) and (
            entry.get("input_cost_per_token") is not None
            or entry.get("output_cost_per_token") is not None
        ):
            return candidate, entry
    return None


def _has_any_price(price: AIModelPrice) -> bool:
    return any(
        value is not None
        for value in (
            price.input_per_1m,
            price.output_per_1m,
            price.blended_per_1m,
            price.request_price,
        )
    )


def _pricing_response(
    ai_model: AIModel,
    override_row: Optional[ModelPriceOverride],
) -> AIModelPricingResponse:
    """Build the pricing answer for one model from an already-resolved override.

    Everything after the override lookup is in-process (model config, then
    litellm's price map), so this function never touches the database. That is
    what lets the batch endpoint price a whole page from one override query.
    """
    alias = _model_alias(ai_model)
    base = {
        "ai_model_id": str(ai_model.id),
        "model_alias": alias,
        "provider_name": ai_model.provider_name,
        "fetch_supported": provider_supports_price_fetch(ai_model),
        "fetch_provider_label": provider_label(ai_model.provider_name),
    }

    if override_row is not None:
        override = dict(override_row.to_pricing_dict())
        price = _price_from_configured(override)
        if _has_any_price(price):
            return AIModelPricingResponse(
                **base,
                source="override",
                price=price,
                currency=str(override.get("currency") or "USD"),
                override_id=str(override_row.id),
                effective_from=override_row.effective_from,
                effective_until=override_row.effective_until,
            )

    configured = _get_configured_pricing(ai_model)
    if configured:
        price = _price_from_configured(configured)
        if _has_any_price(price):
            return AIModelPricingResponse(
                **base,
                source="model_config",
                price=price,
                currency=str(configured.get("currency") or "USD"),
            )

    catalog = _catalog_entry(ai_model)
    if catalog:
        catalog_key, entry = catalog
        return AIModelPricingResponse(
            **base,
            source="catalog",
            price=_price_from_catalog_entry(entry),
            catalog_key=catalog_key,
        )

    return AIModelPricingResponse(**base, source="none")


def get_effective_pricing(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_model: AIModel,
) -> AIModelPricingResponse:
    """Return the price this account is charged for one model, and its source.

    The resolution order is the gateway's own (override, then pricing
    configured on the model, then the catalog), so the card cannot claim a
    price the cost estimator would not use.
    """
    return _pricing_response(
        ai_model,
        resolve_active_override_row(
            db,
            account_id=account_id,
            ai_model=ai_model,
            requested_alias=_model_alias(ai_model),
        ),
    )


def get_effective_pricing_bulk(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_models: Sequence[AIModel],
) -> Dict[str, AIModelPricingResponse]:
    """Price many models with one override query, not one lookup per model.

    Same answers as calling :func:`get_effective_pricing` per model; the
    difference is that the console's Models page no longer costs one request
    and one pooled connection per row.

    Args:
        db: Database session.
        account_id: Account scope for override resolution.
        ai_models: Models to price.

    Returns:
        Mapping of model id (as a string) to its pricing answer.
    """
    overrides = resolve_pricing_overrides_bulk(
        db, account_id=account_id, ai_models=ai_models
    )
    return {
        str(ai_model.id): _pricing_response(ai_model, overrides.get(str(ai_model.id)))
        for ai_model in ai_models
    }


def _openrouter_candidates(ai_model: AIModel) -> List[str]:
    """Candidate OpenRouter keys for one model, most specific first."""
    candidates = [
        candidate
        for candidate in _iter_litellm_model_candidates(ai_model)
        if candidate.startswith("openrouter/")
    ]
    identifier = (ai_model.model_identifier or "").strip()
    if identifier:
        for candidate in (
            f"openrouter/{identifier}",
            f"openrouter/{identifier.split('/', 1)[-1]}",
        ):
            if candidate not in candidates:
                candidates.append(candidate)
    return candidates


def fetch_provider_pricing(ai_model: AIModel) -> AIModelPriceQuote:
    """Read this model's published price from its provider. Never saves it.

    Raises:
        PriceFetchUnsupportedError: the provider publishes no readable prices.
        PriceFetchUnavailableError: the provider answered but not about this model,
            or its price list could not be reached.
    """
    if not provider_supports_price_fetch(ai_model):
        raise PriceFetchUnsupportedError(
            f"{provider_label(ai_model.provider_name)} does not publish prices"
        )

    price_map = model_price_catalog.fetch_openrouter_price_map()
    if not price_map:
        raise PriceFetchUnavailableError("OpenRouter's price list could not be reached")

    for candidate in _openrouter_candidates(ai_model):
        entry = price_map.get(candidate)
        if isinstance(entry, dict):
            return AIModelPriceQuote(
                ai_model_id=str(ai_model.id),
                provider_name=ai_model.provider_name,
                source_url=model_price_catalog.OPENROUTER_MODELS_URL,
                model_key=candidate.split("/", 1)[1],
                price=_price_from_catalog_entry(entry),
                fetched_at=datetime.now(timezone.utc),
            )

    raise PriceFetchUnavailableError(
        "OpenRouter does not list a price for this model identifier"
    )
