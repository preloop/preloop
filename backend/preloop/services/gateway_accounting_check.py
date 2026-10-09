"""End-to-end self-check over the model-gateway accounting chain.

Verifies, per account and lookback window, that gateway traffic is being
recorded with usage (including streaming responses), priced, attributed to a
provider-reported usage source, and audited. The goal is to make silent
accounting breakage (e.g. streaming requests recording 0 tokens) impossible
to miss: operators and the CLI can poll ``GET /api/v1/cost/health`` and alert
on any failing check.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage, crud_audit_log
from preloop.services.usage_token_details import extract_token_details

logger = logging.getLogger(__name__)

# Minimum share of successful streaming rows that must have recorded tokens.
STREAMING_HEALTHY_SHARE = 0.9
# Minimum share of successful token-bearing rows that must carry a cost.
PRICED_HEALTHY_SHARE = 0.9
# usage_source='provider' share thresholds: below WARN the detail flags the
# ratio; only below FAIL does the check actually fail (estimated/partial
# usage is legitimate fallback behavior, just not the expected steady state).
PROVIDER_USAGE_WARN_SHARE = 0.8
PROVIDER_USAGE_FAIL_SHARE = 0.5

NO_TRAFFIC_DETAIL = "no gateway traffic in window"

# Rows compared per run by the raw-vs-normalized token detail check.
TOKEN_DETAIL_SAMPLE_LIMIT = 2000
TOKEN_DETAIL_COLUMNS = (
    "cache_read_tokens",
    "cache_creation_tokens",
    "reasoning_tokens",
)

SUBSCRIPTION_BILLING_UNAVAILABLE = (
    "Subscription billing coverage unavailable: API-equivalent cost is an "
    "estimate, billed subscription dollars are not tracked."
)

# Process-level cache: audit_log presence does not flip without a migration.
_AUDIT_TABLE_EXISTS: Optional[bool] = None


def _check(key: str, status: str, detail: str) -> Dict[str, str]:
    """Build one checklist entry."""
    return {"key": key, "status": status, "detail": detail}


def _audit_table_exists(db: Session) -> bool:
    """Return True when the ``audit_log`` table exists in the database."""
    global _AUDIT_TABLE_EXISTS
    if _AUDIT_TABLE_EXISTS is not None:
        return _AUDIT_TABLE_EXISTS
    try:
        bind = db.get_bind()
        exists = bind is not None and inspect(bind).has_table("audit_log")
    except SQLAlchemyError:
        logger.debug("Audit table existence check failed", exc_info=True)
        return False
    _AUDIT_TABLE_EXISTS = exists
    return exists


def _streaming_check(counters: Dict[str, int]) -> Dict[str, str]:
    """Check that successful streaming responses record token usage."""
    streaming = counters["streaming_rows"]
    if streaming == 0:
        return _check(
            "streaming_usage_recorded",
            "skip",
            "no successful streaming requests in window",
        )
    with_tokens = counters["streaming_rows_with_tokens"]
    share = with_tokens / streaming
    detail = (
        f"{with_tokens}/{streaming} successful streaming requests "
        f"recorded token usage ({share:.0%})"
    )
    if share >= STREAMING_HEALTHY_SHARE:
        return _check("streaming_usage_recorded", "pass", detail)
    return _check(
        "streaming_usage_recorded",
        "fail",
        f"{detail}; streaming usage capture appears broken",
    )


def _costs_priced_check(counters: Dict[str, int]) -> Dict[str, str]:
    """Check that successful token-bearing requests carry a stored cost."""
    priceable = counters["priceable_rows"]
    if priceable == 0:
        return _check(
            "costs_priced",
            "skip",
            "no successful token-bearing requests in window",
        )
    priced = counters["priced_rows"]
    unpriced_source = counters["unpriced_source_rows"]
    share = priced / priceable
    detail = (
        f"{priced}/{priceable} token-bearing requests priced ({share:.0%}); "
        f"{unpriced_source} rows tagged cost_source=unpriced"
    )
    if share >= PRICED_HEALTHY_SHARE:
        return _check("costs_priced", "pass", detail)
    return _check(
        "costs_priced",
        "fail",
        f"{detail}; configure pricing and re-run the reprice endpoint "
        "(POST /billing/cost/reprice) to backfill",
    )


def _usage_source_check(counters: Dict[str, int]) -> Dict[str, str]:
    """Check that provider-reported usage is the norm, not the exception."""
    with_source = counters["usage_source_rows"]
    if with_source == 0:
        return _check(
            "usage_source_health",
            "skip",
            "no successful requests with a recorded usage_source in window",
        )
    provider = counters["provider_usage_rows"]
    share = provider / with_source
    detail = (
        f"{provider}/{with_source} successful requests have provider-reported "
        f"usage ({share:.0%})"
    )
    if share < PROVIDER_USAGE_FAIL_SHARE:
        return _check(
            "usage_source_health",
            "fail",
            f"{detail}; most usage is estimated/partial — provider usage "
            "reporting appears broken",
        )
    if share < PROVIDER_USAGE_WARN_SHARE:
        return _check(
            "usage_source_health",
            "pass",
            f"{detail}; below the expected {PROVIDER_USAGE_WARN_SHARE:.0%} "
            "— estimated/partial usage should be the exception",
        )
    return _check("usage_source_health", "pass", detail)


def _audit_events_check(
    db: Session, *, account_id: str, start: datetime
) -> Dict[str, str]:
    """Check that gateway traffic produces audit events (when audited)."""
    if not _audit_table_exists(db):
        return _check(
            "audit_events_present",
            "skip",
            "audit_log table not present (audit plugin not installed)",
        )
    count = crud_audit_log.count_by_account(db, account_id=account_id, start_date=start)
    if count > 0:
        return _check(
            "audit_events_present",
            "pass",
            f"{count} audit events recorded for the account in window",
        )
    return _check(
        "audit_events_present",
        "fail",
        "gateway traffic seen but no audit events recorded in window",
    )


def _token_details_check(
    db: Session, *, account_id: str, start: datetime
) -> Dict[str, str]:
    """Flag rows whose raw cache/reasoning detail disagrees with the columns.

    A retained ``meta_data.usage_details`` value that the extractor can read
    must match the normalized column. A NULL column next to a readable raw
    value means the count was dropped (e.g. Responses rows recorded before
    #1401), and empty cache columns would wrongly read as "no caching".
    """
    rows = crud_api_usage.list_token_detail_rows(
        db, account_id=account_id, start=start, limit=TOKEN_DETAIL_SAMPLE_LIMIT
    )
    if not rows:
        return _check(
            "token_details_normalized",
            "skip",
            "no requests with provider cache/reasoning detail in window",
        )
    mismatched = 0
    by_column = dict.fromkeys(TOKEN_DETAIL_COLUMNS, 0)
    for row in rows:
        raw = extract_token_details(row.usage_details)
        row_mismatch = False
        for column in TOKEN_DETAIL_COLUMNS:
            if raw[column] is not None and raw[column] != getattr(row, column):
                by_column[column] += 1
                row_mismatch = True
        mismatched += row_mismatch
    sampled = f"{len(rows)} requests with provider cache/reasoning detail"
    if len(rows) >= TOKEN_DETAIL_SAMPLE_LIMIT:
        sampled += " (most recent sample)"
    if not mismatched:
        return _check(
            "token_details_normalized",
            "pass",
            f"{sampled}; normalized cache/reasoning columns match the raw usage",
        )
    columns = ", ".join(f"{name}={count}" for name, count in by_column.items() if count)
    return _check(
        "token_details_normalized",
        "warn",
        f"{mismatched} of {sampled} have normalized cache/reasoning columns "
        f"that are missing or differ from the raw provider usage ({columns}); "
        "run scripts/repair_usage_token_details.py (dry run first) to repair",
    )


def _subscription_coverage_check(
    db: Session, *, account_id: str, start: datetime
) -> Dict[str, str]:
    """Warn whenever subscription rows exist: billed dollars are not tracked.

    A recorded ``api_equivalent_cost`` is a catalog estimate, so its presence
    must not make the subscription spend look accounted for.
    """
    summary = crud_api_usage.get_subscription_usage_summary(
        db,
        account_id=account_id,
        start=start,
        end=datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    requests = summary["request_count"]
    if requests == 0:
        return _check(
            "subscription_billing_coverage",
            "skip",
            "no subscription-covered requests in window",
        )
    covered = summary["coverage_rows"]
    return _check(
        "subscription_billing_coverage",
        "warn",
        f"{SUBSCRIPTION_BILLING_UNAVAILABLE} {requests} subscription requests in "
        f"window; {covered}/{requests} ({covered / requests:.0%}) carry an "
        "API-equivalent estimate.",
    )


def run_accounting_checks(
    db: Session, *, account_id: str, window_hours: int
) -> Dict[str, Any]:
    """Run the gateway accounting self-check for one account.

    Args:
        db: Database session.
        account_id: Account to check.
        window_hours: Lookback window in hours.

    Returns:
        Dict with ``window_hours``, the ordered ``checks`` list, and the
        aggregate ``status`` (fail if any check fails, else warn if any check
        warns, else pass if any check passes, else skip).
    """
    start = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    try:
        counters = crud_api_usage.get_accounting_health_counters(
            db, account_id=account_id, start=start
        )
    except SQLAlchemyError as exc:
        # Health checks must report degraded state, not crash the endpoint.
        logger.warning(
            "Gateway accounting health counter query failed: %s",
            exc,
            exc_info=True,
        )
        return {
            "window_hours": window_hours,
            "checks": [
                _check(
                    "gateway_traffic_seen",
                    "fail",
                    f"accounting counter query failed: {exc.__class__.__name__}",
                )
            ],
            "status": "fail",
        }

    checks: List[Dict[str, str]] = []
    total = counters["total_rows"]
    if total > 0:
        checks.append(
            _check(
                "gateway_traffic_seen",
                "pass",
                f"{total} gateway requests in the last {window_hours}h",
            )
        )
        checks.append(_streaming_check(counters))
        checks.append(_costs_priced_check(counters))
        checks.append(_usage_source_check(counters))
        for key, run in (
            ("token_details_normalized", _token_details_check),
            ("subscription_billing_coverage", _subscription_coverage_check),
        ):
            try:
                checks.append(run(db, account_id=account_id, start=start))
            except SQLAlchemyError as exc:
                logger.warning(
                    "Gateway accounting %s check failed: %s", key, exc, exc_info=True
                )
                checks.append(
                    _check(key, "fail", f"query failed: {exc.__class__.__name__}")
                )
        try:
            checks.append(_audit_events_check(db, account_id=account_id, start=start))
        except SQLAlchemyError as exc:
            logger.warning(
                "Gateway accounting audit check failed: %s",
                exc,
                exc_info=True,
            )
            checks.append(
                _check(
                    "audit_events_present",
                    "fail",
                    f"audit event query failed: {exc.__class__.__name__}",
                )
            )
    else:
        checks.append(_check("gateway_traffic_seen", "skip", NO_TRAFFIC_DETAIL))
        for key in (
            "streaming_usage_recorded",
            "costs_priced",
            "usage_source_health",
            "token_details_normalized",
            "subscription_billing_coverage",
            "audit_events_present",
        ):
            checks.append(_check(key, "skip", NO_TRAFFIC_DETAIL))

    statuses = {check["status"] for check in checks}
    if "fail" in statuses:
        overall = "fail"
    elif "warn" in statuses:
        # A warning is never reported as healthy (#1401).
        overall = "warn"
    elif "pass" in statuses:
        overall = "pass"
    else:
        overall = "skip"

    return {"window_hours": window_hours, "checks": checks, "status": overall}


def _module_state_for_tests() -> dict[str, bool | None]:
    """Expose module cache globals for tests and static analysis."""
    return {"audit_table_exists": _AUDIT_TABLE_EXISTS}
