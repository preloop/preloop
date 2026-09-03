"""Single source of truth for resolving account model-price overrides.

Every code path that estimates model cost (gateway recording, budget
preflight, execution metrics, tool usage stats, repricing) must resolve
overrides through :func:`resolve_pricing_override` so they cannot disagree
about which override applies to a request.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, TypedDict, Union

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_model_price_override
from preloop.models.models.ai_model import AIModel
from preloop.models.models.model_price_override import ModelPriceOverride

logger = logging.getLogger(__name__)


class PricingOverrideDict(TypedDict, total=False):
    """Normalized pricing fields returned by :func:`resolve_pricing_override`."""

    id: str
    currency: str
    input_price_per_1k: float
    output_price_per_1k: float
    cache_read_input_price_per_1k: float
    cache_creation_input_price_per_1k: float
    price_per_1k: float
    request_price: float
    discount_percent: float
    prepaid_token_balance: float
    prepaid_credit_balance_usd: float
    original_currency: str
    fx_rate_to_usd: float
    original_prices: Dict[str, float]


def alias_candidates(
    ai_model: AIModel, requested_alias: Optional[str] = None
) -> List[str]:
    """Return the alias precedence used to match overrides, most specific first.

    Canonical order: the model's configured gateway alias, then the alias the
    client requested, then the raw model identifier.

    Args:
        ai_model: The resolved AI model row.
        requested_alias: Model name as the client sent it, when available.

    Returns:
        Stripped, de-duplicated alias candidates in precedence order.
    """
    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    raw_gateway_config = meta_data.get("gateway")
    gateway_config: Dict[str, Any] = (
        raw_gateway_config if isinstance(raw_gateway_config, dict) else {}
    )
    candidates: List[str] = []
    for alias in (
        gateway_config.get("model_alias"),
        requested_alias,
        ai_model.model_identifier,
    ):
        if isinstance(alias, str):
            normalized = alias.strip()
            if normalized and normalized not in candidates:
                candidates.append(normalized)
    return candidates


def _overrides_table_available(db: Session) -> bool:
    """Report whether the overrides table exists, never raising."""
    try:
        bind = db.get_bind()
        if bind is not None and not inspect(bind).has_table("model_price_overrides"):
            return False
    except SQLAlchemyError:
        logger.debug("Pricing override table check failed", exc_info=True)
        return False
    return True


def resolve_active_override_row(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_model: AIModel,
    requested_alias: Optional[str] = None,
) -> Optional[ModelPriceOverride]:
    """Return the override row that prices one model request, if any.

    The first alias (see :func:`alias_candidates`) with an active override
    wins; model-specific rows beat account-wide wildcards and effective-date
    windows are enforced by the CRUD layer.

    Args:
        db: Database session.
        account_id: Account scope for the lookup.
        ai_model: The resolved AI model row.
        requested_alias: Model name as the client sent it, when available.

    Returns:
        The winning override row, or None when none applies or the table does
        not exist yet.
    """
    if not _overrides_table_available(db):
        return None

    for alias in alias_candidates(ai_model, requested_alias):
        try:
            override = crud_model_price_override.get_active_for_model(
                db,
                account_id=account_id,
                ai_model_id=ai_model.id,
                model_alias=alias,
                provider_name=ai_model.provider_name,
            )
        except SQLAlchemyError:
            # Transient lookup failure for one alias must not skip remaining
            # candidates (gateway alias → requested → model identifier).
            logger.debug(
                "Pricing override lookup failed for alias %s",
                alias,
                exc_info=True,
            )
            continue
        if override is not None:
            return override
    return None


def resolve_pricing_override(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_model: AIModel,
    requested_alias: Optional[str] = None,
) -> Optional[PricingOverrideDict]:
    """Resolve the active pricing override for one model request.

    Args:
        db: Database session.
        account_id: Account scope for the lookup.
        ai_model: The resolved AI model row.
        requested_alias: Model name as the client sent it, when available.

    Returns:
        The normalized pricing dict (USD-converted, includes ``id``), or None
        when no override applies or the table does not exist yet.
    """
    override = resolve_active_override_row(
        db,
        account_id=account_id,
        ai_model=ai_model,
        requested_alias=requested_alias,
    )
    if override is None:
        return None
    pricing = override.to_pricing_dict()
    pricing["id"] = str(override.id)
    return pricing  # type: ignore[return-value]


def _sort_key(override: ModelPriceOverride) -> tuple:
    """Rank one override the way the CRUD lookup's ORDER BY does.

    Mirrors ``ai_model_id IS NULL, effective_from DESC NULLS LAST,
    created_at DESC``: model-specific rows first, then the most recently
    effective, then the most recently created.
    """

    def as_epoch(value: Optional[datetime]) -> float:
        if value is None:
            return 0.0
        moment = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return moment.timestamp()

    return (
        override.ai_model_id is None,
        override.effective_from is None,
        -as_epoch(override.effective_from),
        -as_epoch(override.created_at),
    )


def _matches(
    override: ModelPriceOverride,
    *,
    ai_model: AIModel,
    alias: str,
    observed_at: datetime,
) -> bool:
    """Apply the CRUD lookup's WHERE clause to one already-loaded row.

    Keep in lockstep with ``CRUDModelPriceOverride.get_active_for_model``;
    ``test_bulk_resolver_agrees_with_the_per_model_resolver`` and
    ``test_bulk_resolver_effective_from_and_provider_precedence`` pin parity.
    """
    if not override.is_active:
        return False
    if override.model_alias != alias:
        return False
    if override.ai_model_id is not None and override.ai_model_id != ai_model.id:
        return False
    if override.effective_from is not None:
        effective_from = override.effective_from
        if not effective_from.tzinfo:
            effective_from = effective_from.replace(tzinfo=timezone.utc)
        if effective_from > observed_at:
            return False
    if override.effective_until is not None:
        effective_until = override.effective_until
        if not effective_until.tzinfo:
            effective_until = effective_until.replace(tzinfo=timezone.utc)
        if effective_until <= observed_at:
            return False
    provider_name = ai_model.provider_name
    if provider_name and override.provider_name is not None:
        if override.provider_name not in (provider_name, provider_name.strip().lower()):
            return False
    return True


def resolve_pricing_overrides_bulk(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_models: Sequence[AIModel],
    at: Optional[datetime] = None,
) -> Dict[str, ModelPriceOverride]:
    """Resolve overrides for many models with one query instead of one each.

    Same precedence as :func:`resolve_active_override_row`, applied in memory
    to a single account-scoped read. An account has a handful of overrides at
    most, so loading them costs less than one query per model per alias, which
    is what a page listing every model used to pay.

    Args:
        db: Database session.
        account_id: Account scope for the lookup.
        ai_models: Models to resolve prices for.
        at: Point in time to evaluate effective-date windows at. Defaults to
            now, matching the CRUD lookup.

    Returns:
        Mapping of model id (as a string) to the winning override row. Models
        with no active override are absent.
    """
    if not ai_models or not _overrides_table_available(db):
        return {}

    try:
        overrides = crud_model_price_override.list_for_account(
            db, account_id=account_id, active_only=True
        )
    except SQLAlchemyError:
        logger.debug("Bulk pricing override lookup failed", exc_info=True)
        return {}
    if not overrides:
        return {}

    observed_at = at or datetime.now(timezone.utc)
    resolved: Dict[str, ModelPriceOverride] = {}
    for ai_model in ai_models:
        for alias in alias_candidates(ai_model):
            matching = [
                override
                for override in overrides
                if _matches(
                    override,
                    ai_model=ai_model,
                    alias=alias,
                    observed_at=observed_at,
                )
            ]
            if matching:
                resolved[str(ai_model.id)] = min(matching, key=_sort_key)
                break
    return resolved
