"""Re-price historical gateway usage rows from stored token counts.

Repricing recomputes ``ApiUsage.estimated_cost`` from each row's persisted
tokens and ``meta_data["usage_details"]`` using the CURRENT price catalog and
account overrides. It exists to:

- fill in rows recorded as unpriced (e.g. before the model appeared in the
  price catalog, or streaming rows recorded with 0 tokens pre-fix),
- apply a newly created/edited price override retroactively on demand.

Budget-spend buckets are deliberately NOT rewritten: spend was charged at
request time and repricing is analytics-only. Rows priced as ``subscription``
are skipped — their $0 cost is correct by construction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Optional, Union

from sqlalchemy.orm import Session

from preloop.models.crud import crud_ai_model, crud_api_usage
from preloop.models.models.ai_model import AIModel
from preloop.services.model_pricing import estimate_ai_model_usage_cost_detailed
from preloop.services.pricing_overrides import resolve_pricing_override

logger = logging.getLogger(__name__)


@dataclass
class RepriceResult:
    """Outcome of a repricing run."""

    rows_examined: int = 0
    rows_updated: int = 0
    rows_skipped: int = 0
    cost_before: float = 0.0
    cost_after: float = 0.0
    dry_run: bool = False


def reprice_single_row(
    db: Session,
    *,
    api_usage_id: Union[uuid.UUID, str],
) -> bool:
    """Re-price one gateway usage row against current prices/overrides.

    Used by the live price lookup to fix the row that triggered it. Follows
    the same rules as the bulk path: subscription rows are left alone and
    budget spend is never rewritten.

    Args:
        db: Database session.
        api_usage_id: Target ``ApiUsage`` row id.

    Returns:
        True when the row was updated with a new cost/source.
    """
    row = crud_api_usage.get(db, id=api_usage_id)
    if row is None or row.cost_source == "subscription" or not row.ai_model_id:
        return False
    ai_model = crud_ai_model.get(db, id=str(row.ai_model_id))
    if ai_model is None or row.account_id is None:
        return False

    pricing_override = resolve_pricing_override(
        db,
        account_id=row.account_id,
        ai_model=ai_model,
        requested_alias=row.model_alias,
    )
    meta = row.meta_data if isinstance(row.meta_data, dict) else {}
    usage_details = meta.get("usage_details")
    estimate = estimate_ai_model_usage_cost_detailed(
        ai_model,
        prompt_tokens=int(row.prompt_tokens or 0),
        completion_tokens=int(row.completion_tokens or 0),
        total_tokens=int(row.total_tokens or 0),
        usage_details=usage_details if isinstance(usage_details, dict) else None,
        pricing_override=pricing_override,
    )
    if estimate.cost == row.estimated_cost and estimate.source == row.cost_source:
        return False

    crud_api_usage.update_cost_fields(
        db,
        api_usage_id=row.id,
        estimated_cost=estimate.cost,
        cost_source=estimate.source,
        meta_data_patch={
            "repriced_at": datetime.now(timezone.utc).isoformat(),
            "previous_estimated_cost": row.estimated_cost,
            "previous_cost_source": row.cost_source,
            "repriced_by": "live_price_lookup",
        },
    )
    return True


def reprice_gateway_usage(
    db: Session,
    *,
    account_id: Union[uuid.UUID, str],
    start: datetime,
    end: datetime,
    only_unpriced: bool = True,
    dry_run: bool = False,
    batch_size: int = 500,
) -> RepriceResult:
    """Re-price gateway usage rows in a time window.

    Args:
        db: Database session.
        account_id: Account whose rows are repriced.
        start: Window start (inclusive).
        end: Window end (exclusive).
        only_unpriced: When True (default), only rows with NULL cost are
            touched; when False every row is recomputed against current
            pricing (retroactive override application).
        dry_run: Compute and report without persisting.
        batch_size: Rows fetched per query page.

    Returns:
        Aggregate counts and the before/after cost totals for examined rows.
    """
    result = RepriceResult(dry_run=dry_run)
    model_cache: Dict[str, Optional[AIModel]] = {}
    override_cache: Dict[str, Optional[dict]] = {}
    repriced_at = datetime.now(timezone.utc).isoformat()

    for row in crud_api_usage.iter_gateway_rows_for_repricing(
        db,
        account_id=account_id,
        start=start,
        end=end,
        only_unpriced=only_unpriced,
        batch_size=batch_size,
    ):
        result.rows_examined += 1
        result.cost_before += float(row.estimated_cost or 0.0)

        if row.cost_source == "subscription":
            result.rows_skipped += 1
            result.cost_after += float(row.estimated_cost or 0.0)
            continue

        model_id = str(row.ai_model_id) if row.ai_model_id else None
        if model_id is None:
            result.rows_skipped += 1
            result.cost_after += float(row.estimated_cost or 0.0)
            continue
        if model_id not in model_cache:
            model_cache[model_id] = crud_ai_model.get(db, id=model_id)
        ai_model = model_cache[model_id]
        if ai_model is None:
            result.rows_skipped += 1
            result.cost_after += float(row.estimated_cost or 0.0)
            continue

        override_key = f"{model_id}:{row.model_alias or ''}"
        if override_key not in override_cache:
            override_cache[override_key] = resolve_pricing_override(
                db,
                account_id=account_id,
                ai_model=ai_model,
                requested_alias=row.model_alias,
            )
        pricing_override = override_cache[override_key]

        meta = row.meta_data if isinstance(row.meta_data, dict) else {}
        usage_details = meta.get("usage_details")
        usage_details = usage_details if isinstance(usage_details, dict) else None

        estimate = estimate_ai_model_usage_cost_detailed(
            ai_model,
            prompt_tokens=int(row.prompt_tokens or 0),
            completion_tokens=int(row.completion_tokens or 0),
            total_tokens=int(row.total_tokens or 0),
            usage_details=usage_details,
            pricing_override=pricing_override,
        )

        unchanged = (
            estimate.cost == row.estimated_cost and estimate.source == row.cost_source
        )
        if unchanged:
            result.cost_after += float(row.estimated_cost or 0.0)
            continue

        result.cost_after += float(estimate.cost or 0.0)
        result.rows_updated += 1
        if dry_run:
            continue

        crud_api_usage.update_cost_fields(
            db,
            api_usage_id=row.id,
            estimated_cost=estimate.cost,
            cost_source=estimate.source,
            meta_data_patch={
                "repriced_at": repriced_at,
                "previous_estimated_cost": row.estimated_cost,
                "previous_cost_source": row.cost_source,
            },
        )

    logger.info(
        "Repriced gateway usage for account %s: examined=%s updated=%s "
        "skipped=%s cost %.6f -> %.6f (dry_run=%s)",
        account_id,
        result.rows_examined,
        result.rows_updated,
        result.rows_skipped,
        result.cost_before,
        result.cost_after,
        dry_run,
    )
    return result
