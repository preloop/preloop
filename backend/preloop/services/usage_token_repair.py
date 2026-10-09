"""Repair normalized cache/reasoning token columns from retained usage payloads.

Before #1401 the gateway did not read the OpenAI Responses usage shape
(``input_tokens_details.cached_tokens``,
``output_tokens_details.reasoning_tokens``), so every Responses row was
recorded with NULL ``cache_read_tokens`` / ``reasoning_tokens`` even though
the raw provider usage was retained in ``meta_data.usage_details``. This
repair re-runs the fixed extractor over those retained payloads.

Properties:

- Dry run by default: nothing is written unless ``apply`` is True.
- Bounded: account, time window and a row limit.
- Idempotent: only NULL columns are filled, never overwritten, so a re-run
  finds nothing left to do.
- Cost is untouched unless ``reprice`` is requested. Pricing reads the raw
  ``usage_details``, not the normalized columns, so filling the columns does
  not change a cost by itself. ``reprice`` re-prices only the repaired rows
  through :func:`preloop.services.usage_repricing.reprice_single_row`, which
  keeps its protections: subscription rows and provider-side actuals are
  never rewritten and budget spend is never changed.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Optional, Union

from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage
from preloop.services.usage_repricing import reprice_single_row
from preloop.services.usage_token_details import extract_token_details

logger = logging.getLogger(__name__)

TOKEN_DETAIL_COLUMNS = (
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)


@dataclass
class TokenDetailRepairResult:
    """Outcome of a repair run."""

    dry_run: bool = True
    rows_examined: int = 0
    rows_repairable: int = 0
    rows_updated: int = 0
    rows_repriced: int = 0
    columns_filled: Dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(TOKEN_DETAIL_COLUMNS, 0)
    )
    limit_reached: bool = False


def repair_token_details(
    db: Session,
    *,
    account_id: Optional[Union[uuid.UUID, str]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    limit: int = 1000,
    apply: bool = False,
    reprice: bool = False,
    batch_size: int = 500,
) -> TokenDetailRepairResult:
    """Recompute NULL token-detail columns from retained Responses usage.

    Args:
        db: Database session.
        account_id: Optional account scope.
        since: Optional window start (inclusive).
        until: Optional window end (exclusive).
        limit: Maximum candidate rows examined.
        apply: Persist the repair. False (default) only reports.
        reprice: With ``apply``, re-price each repaired row afterwards.
        batch_size: Rows fetched per page.

    Returns:
        Counts of examined, repairable, updated and repriced rows.

    Raises:
        ValueError: When ``reprice`` is requested without ``apply``.
    """
    if reprice and not apply:
        raise ValueError("reprice requires apply")
    result = TokenDetailRepairResult(dry_run=not apply)
    after_id: Optional[uuid.UUID] = None
    remaining = max(int(limit), 0)
    repaired_ids: list[uuid.UUID] = []
    while remaining > 0:
        batch = crud_api_usage.list_token_detail_repair_candidates(
            db,
            account_id=account_id,
            since=since,
            until=until,
            limit=min(batch_size, remaining),
            after_id=after_id,
        )
        if not batch:
            break
        for row in batch:
            result.rows_examined += 1
            meta = row.meta_data if isinstance(row.meta_data, dict) else {}
            details = extract_token_details(meta.get("usage_details"))
            fillable = {
                column: details[column]
                for column in TOKEN_DETAIL_COLUMNS
                if details[column] is not None and getattr(row, column) is None
            }
            if not fillable:
                continue
            result.rows_repairable += 1
            for column in fillable:
                result.columns_filled[column] += 1
            if apply:
                written = crud_api_usage.fill_token_detail_columns(
                    db, api_usage_id=row.id, values=fillable
                )
                if written:
                    result.rows_updated += 1
                    repaired_ids.append(row.id)
        remaining -= len(batch)
        after_id = batch[-1].id
        if apply:
            db.commit()
    result.limit_reached = remaining <= 0 and result.rows_examined > 0
    if reprice:
        for api_usage_id in repaired_ids:
            if reprice_single_row(
                db, api_usage_id=api_usage_id, repriced_by="token_detail_repair"
            ):
                result.rows_repriced += 1
    logger.info(
        "Token detail repair (%s): examined=%d repairable=%d updated=%d repriced=%d",
        "apply" if apply else "dry-run",
        result.rows_examined,
        result.rows_repairable,
        result.rows_updated,
        result.rows_repriced,
    )
    return result
