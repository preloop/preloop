"""Single source of truth for resolving account model-price overrides.

Every code path that estimates model cost (gateway recording, budget
preflight, execution metrics, tool usage stats, repricing) must resolve
overrides through :func:`resolve_pricing_override` so they cannot disagree
about which override applies to a request.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, Optional, Union

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_model_price_override
from preloop.models.models.ai_model import AIModel

logger = logging.getLogger(__name__)


def resolve_pricing_override(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    ai_model: AIModel,
    requested_alias: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Resolve the active pricing override for one model request.

    Canonical alias precedence: the model's configured gateway alias first,
    then the alias the client requested, then the raw model identifier. The
    first alias with an active override (model-specific rows beat account-wide
    wildcards; effective-date windows enforced by the CRUD layer) wins.

    Args:
        db: Database session.
        account_id: Account scope for the lookup.
        ai_model: The resolved AI model row.
        requested_alias: Model name as the client sent it, when available.

    Returns:
        The normalized pricing dict (USD-converted, includes ``id``), or None
        when no override applies or the table does not exist yet.
    """
    try:
        bind = db.get_bind()
        if bind is not None and not inspect(bind).has_table("model_price_overrides"):
            return None
    except SQLAlchemyError:
        logger.debug("Pricing override table check failed", exc_info=True)
        return None

    meta_data = ai_model.meta_data if isinstance(ai_model.meta_data, dict) else {}
    raw_gateway_config = meta_data.get("gateway")
    gateway_config: Dict[str, Any] = (
        raw_gateway_config if isinstance(raw_gateway_config, dict) else {}
    )
    alias_candidates = []
    for alias in (
        gateway_config.get("model_alias"),
        requested_alias,
        ai_model.model_identifier,
    ):
        if (
            isinstance(alias, str)
            and alias.strip()
            and alias.strip() not in alias_candidates
        ):
            alias_candidates.append(alias.strip())

    for alias in alias_candidates:
        try:
            override = crud_model_price_override.get_active_for_model(
                db,
                account_id=account_id,
                ai_model_id=ai_model.id,
                model_alias=alias,
                provider_name=ai_model.provider_name,
            )
        except SQLAlchemyError:
            logger.debug("Pricing override lookup failed", exc_info=True)
            return None
        if override is not None:
            pricing = override.to_pricing_dict()
            pricing["id"] = str(override.id)
            return pricing
    return None
