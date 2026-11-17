"""Account-scoped gateway usage export without request or response content."""

import csv
import io
import json
from datetime import datetime
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.services.analytics_history import restrict_history_window
from preloop.services.model_gateway_usage import normalize_usage_period
from preloop.services.upstream_request_summary import public_upstream_summary

COLUMNS = (
    "id",
    "timestamp",
    "model_alias",
    "provider_name",
    "upstream_provider",
    "upstream_request_id",
    "status_code",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "estimated_cost",
    "upstream_parameters",
)
MAX_EXPORT_ROWS = 10000


def gateway_usage_csv(rows: Iterable[dict[str, Any]]) -> str:
    """Render only documented usage fields, protecting spreadsheet text cells."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(COLUMNS)
    for row in rows:
        values = []
        for key in COLUMNS:
            value = row.get(key)
            if key == "upstream_parameters":
                summary = public_upstream_summary(value)
                value = json.dumps(summary, separators=(",", ":")) if summary else ""
            if isinstance(value, str) and value.lstrip().startswith(
                ("=", "+", "-", "@")
            ):
                value = "'" + value
            values.append(value)
        writer.writerow(values)
    return output.getvalue()


def export_gateway_usage(
    db: Session,
    *,
    account: models.Account,
    start_date: datetime | None,
    end_date: datetime | None,
) -> str:
    """Export a bounded account window using the same reporting history policy."""
    start, end = normalize_usage_period(start_date, end_date)
    if start >= end:
        raise HTTPException(status_code=422, detail="start_date must precede end_date")
    visible_start, visible_end = restrict_history_window(
        db,
        account=account,
        start_date=start,
        end_date=end,
    )
    rows = crud_api_usage.export_gateway_rows(
        db,
        account_id=account.id,
        start=visible_start or start,
        end=visible_end or end,
        limit=MAX_EXPORT_ROWS + 1,
    )
    if len(rows) > MAX_EXPORT_ROWS:
        raise HTTPException(
            status_code=413,
            detail="Export exceeds 10,000 requests; choose a shorter date range",
        )
    return gateway_usage_csv(rows)
