"""Open-source cost analytics endpoints."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import Future
from datetime import date, datetime, timedelta
from typing import Literal, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
)
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import get_current_active_user
from preloop.api.common import get_account_or_404
from preloop.api.heavy_routes import heavy_route
from preloop.models import models
from preloop.models.crud import crud_api_usage
from preloop.models.db.session import get_db_session
from preloop.schemas.cost_analytics import (
    CostAnalyticsSummaryResponse,
    CostHealthResponse,
    CostTimeseriesResponse,
    ImportedUsageByConversation,
    ImportedUsageByModel,
    ImportedUsageSummary,
    LedgerBackfillBucket,
    LedgerBackfillResidual,
    LedgerBackfillResponse,
    LedgerBackfillUnmatched,
    PriceCatalogInfo,
    RepriceRequest,
    RepriceResponse,
    SubscriptionUsageSummary,
    UnpricedModelUsage,
)
from preloop.schemas.gateway_usage import UsageBreakdown
from preloop.services.analytics_history import restrict_history_window
from preloop.services.gateway_accounting_check import run_accounting_checks
from preloop.services.ledger_backfill import (
    apply_ledger_backfill,
    load_unpriced_rows,
    parse_explorer_csv,
    plan_ledger_allocation,
)
from preloop.services.model_gateway_usage import (
    ModelGatewayUsageService,
    normalize_usage_period,
)
from preloop.services.usage_repricing import reprice_gateway_usage
from preloop.utils.permissions import ensure_permission_in_oss, require_permission
from preloop.api.loop_safety import run_db_off_loop

router = APIRouter(prefix="/cost", tags=["Cost Analytics"])

#: Hard cap for one synchronous reprice request. Keyset pagination keeps
#: memory flat, but a bound keeps one HTTP request from scanning unbounded
#: history — split longer backfills into consecutive calls.
REPRICE_MAX_WINDOW_DAYS = 92

#: Explore exports are one row per (day, model); even a year is tiny.
MAX_LEDGER_CSV_BYTES = 2 * 1024 * 1024

#: How many unpriced models the summary names for the banner. Capped so a
#: long tail of one-off aliases cannot bloat every cost-summary response;
#: the biggest token offenders sort first.
UNPRICED_MODELS_LIMIT = 10

#: Longest window one spend-chart request may cover. A year of day buckets is
#: 366 rows; the query runs on ix_api_usage_account_action_ts either way.
TIMESERIES_MAX_WINDOW_DAYS = 366

#: Hour buckets are capped tighter: 7 days is 168 rows.
TIMESERIES_MAX_HOURLY_WINDOW_DAYS = 7


def _price_catalog_info() -> Optional[PriceCatalogInfo]:
    """Return vendored price-catalog provenance for the staleness indicator."""
    try:
        from preloop.services.model_price_catalog import catalog_metadata

        meta = catalog_metadata()
        return PriceCatalogInfo(**meta) if meta else None
    except Exception:  # noqa: BLE001 - purely informational
        return None


@router.get("/timeseries", response_model=CostTimeseriesResponse)
@heavy_route
@require_permission("view_cost")
def get_cost_timeseries(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    granularity: Literal["hour", "day", "week", "month"] = Query(
        "day", description="Bucket size; buckets start on UTC boundaries."
    ),
    runtime_principal_id: Optional[str] = Query(None),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CostTimeseriesResponse:
    """Return gateway spend and requests per time bucket for the spend chart.

    Bounded to ``TIMESERIES_MAX_WINDOW_DAYS`` so one request never scans
    unbounded history.
    """
    account = get_account_or_404(db, current_user)
    start, end = normalize_usage_period(start_date, end_date)
    if end <= start:
        raise HTTPException(status_code=422, detail="end_date must be after start_date")
    if end - start > timedelta(days=TIMESERIES_MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=422,
            detail=f"Window exceeds {TIMESERIES_MAX_WINDOW_DAYS} days",
        )
    if granularity == "hour" and end - start > timedelta(
        days=TIMESERIES_MAX_HOURLY_WINDOW_DAYS
    ):
        raise HTTPException(
            status_code=422,
            detail=(f"Hourly window exceeds {TIMESERIES_MAX_HOURLY_WINDOW_DAYS} days"),
        )
    rows = crud_api_usage.get_gateway_usage_timeseries(
        db,
        account_id=str(account.id),
        start_date=start,
        end_date=end,
        runtime_principal_id=runtime_principal_id,
        granularity=granularity,
    )
    return CostTimeseriesResponse(
        period_start=start,
        period_end=end,
        granularity=granularity,
        buckets=rows,
    )


@router.get("/summary", response_model=CostAnalyticsSummaryResponse)
@heavy_route
@require_permission("view_cost")
def get_cost_summary(
    start_date: Optional[datetime] = Query(None),
    end_date: Optional[datetime] = Query(None),
    runtime_principal_id: Optional[str] = Query(None),
    exclude_retries: bool = Query(
        False,
        description=(
            "Exclude rows marked as retries of an identical earlier request. "
            "Retries consume real provider tokens, so they count by default."
        ),
    ),
    include_breakdown: bool = Query(
        True,
        description="Include breakdowns; false returns totals and pricing context only.",
    ),
    breakdown: Optional[list[UsageBreakdown]] = Query(
        None,
        description="Optional breakdown selection. Repeat for multiple sections; omitted returns all. Ignored when include_breakdown=false.",
    ),
    session_limit: Optional[int] = Query(
        None,
        ge=1,
        le=200,
        description=(
            "Page the session breakdown: every model row of this many "
            "sessions, most recent first. Omitted keeps the historical 250 "
            "most recent rows. The first page also returns total_sessions."
        ),
    ),
    session_offset: int = Query(
        0, ge=0, description="Sessions to skip when session_limit is set."
    ),
    compare_start_date: Optional[datetime] = Query(
        None,
        description=(
            "Start of a comparison window. With compare_end_date, the "
            "response carries that window's totals in `previous`, so a page "
            "comparing two ranges needs one request instead of two."
        ),
    ),
    compare_end_date: Optional[datetime] = Query(
        None, description="End of the comparison window; see compare_start_date."
    ),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CostAnalyticsSummaryResponse:
    """Return the OSS cost overview using gateway usage and pricing metadata."""
    if (compare_start_date is None) != (compare_end_date is None):
        raise HTTPException(
            status_code=422,
            detail="compare_start_date and compare_end_date go together.",
        )
    account = get_account_or_404(db, current_user)
    summary = _cached_cost_summary(
        db,
        account=account,
        start_date=start_date,
        end_date=end_date,
        runtime_principal_id=runtime_principal_id,
        exclude_retries=exclude_retries,
        include_breakdown=include_breakdown,
        breakdown=breakdown,
        session_limit=session_limit,
        session_offset=session_offset,
    )
    if compare_start_date is not None and compare_end_date is not None:
        summary.previous = _cached_cost_summary(
            db,
            account=account,
            start_date=compare_start_date,
            end_date=compare_end_date,
            runtime_principal_id=runtime_principal_id,
            exclude_retries=exclude_retries,
            include_breakdown=False,
            breakdown=None,
        )
    return summary


SUMMARY_CACHE_TTL_SECONDS = 60.0
_SUMMARY_CACHE_MAX_ENTRIES = 128
_SUMMARY_CACHE_LOCK = threading.Lock()
_SUMMARY_CACHE: dict[
    tuple[object, ...], tuple[float, Future[CostAnalyticsSummaryResponse]]
] = {}


def _cached_cost_summary(
    db: Session,
    *,
    account: models.Account,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    runtime_principal_id: Optional[str],
    exclude_retries: bool,
    include_breakdown: bool,
    breakdown: Optional[list[UsageBreakdown]],
    session_limit: Optional[int] = None,
    session_offset: int = 0,
) -> CostAnalyticsSummaryResponse:
    """Share exact-window aggregates for a minute, including concurrent reads.

    Authorization and current history policy run before every cache lookup.
    Return a copy so attaching a comparison cannot modify another response.
    Failed reads are never cached; the bounded cache is local to each worker.
    """
    start, end = normalize_usage_period(start_date, end_date)
    restricted_start, restricted_end = restrict_history_window(
        db, account=account, start_date=start, end_date=end
    )
    # Normalization supplies a lower bound even for an omitted start date.
    assert restricted_start is not None and restricted_end is not None
    start, end = restricted_start, restricted_end
    key = (
        str(account.id),
        json.dumps(account.meta_data or {}, sort_keys=True),
        start,
        end,
        runtime_principal_id,
        exclude_retries,
        include_breakdown,
        tuple(sorted(set(breakdown))) if breakdown is not None else None,
        session_limit,
        session_offset,
    )
    now = time.monotonic()
    with _SUMMARY_CACHE_LOCK:
        cached = _SUMMARY_CACHE.get(key)
        if cached is not None and (not cached[1].done() or cached[0] > now):
            future = cached[1]
            owner = False
        else:
            future = Future()
            owner = True
            # Keep in-flight entries so readers can share them. If every slot
            # is busy, compute without caching rather than grow without bound.
            if len(_SUMMARY_CACHE) >= _SUMMARY_CACHE_MAX_ENTRIES:
                for old_key, (_, pending) in list(_SUMMARY_CACHE.items()):
                    if pending.done():
                        del _SUMMARY_CACHE[old_key]
                        break
            if len(_SUMMARY_CACHE) < _SUMMARY_CACHE_MAX_ENTRIES:
                _SUMMARY_CACHE[key] = (now + SUMMARY_CACHE_TTL_SECONDS, future)
    if not owner:
        return future.result().model_copy(deep=True)
    try:
        result = _build_cost_summary(
            db,
            account=account,
            start_date=start,
            end_date=end,
            runtime_principal_id=runtime_principal_id,
            exclude_retries=exclude_retries,
            include_breakdown=include_breakdown,
            breakdown=breakdown,
            session_limit=session_limit,
            session_offset=session_offset,
        )
    except Exception as exc:
        with _SUMMARY_CACHE_LOCK:
            if _SUMMARY_CACHE.get(key, (0, None))[1] is future:
                del _SUMMARY_CACHE[key]
        future.set_exception(exc)
        raise
    with _SUMMARY_CACHE_LOCK:
        if _SUMMARY_CACHE.get(key, (0, None))[1] is future:
            _SUMMARY_CACHE[key] = (
                time.monotonic() + SUMMARY_CACHE_TTL_SECONDS,
                future,
            )
    future.set_result(result)
    return result.model_copy(deep=True)


def _build_cost_summary(
    db: Session,
    *,
    account: models.Account,
    start_date: Optional[datetime],
    end_date: Optional[datetime],
    runtime_principal_id: Optional[str],
    exclude_retries: bool,
    include_breakdown: bool,
    breakdown: Optional[list[UsageBreakdown]],
    session_limit: Optional[int] = None,
    session_offset: int = 0,
) -> CostAnalyticsSummaryResponse:
    """One window of the cost overview (totals, and breakdowns if asked)."""
    selected = set(breakdown) if breakdown is not None else None
    include_imported = include_breakdown and (
        selected is None or "imported" in selected
    )
    summary = ModelGatewayUsageService(db).get_account_summary(
        account=account,
        start_date=start_date,
        end_date=end_date,
        runtime_principal_id=runtime_principal_id,
        include_breakdown=include_breakdown,
        breakdowns=selected,
        exclude_retries=exclude_retries,
        session_limit=session_limit,
        session_offset=session_offset,
    )
    # Imported (observed) spend is reported as a SEPARATE block, using the
    # summary's normalized window: it is never added into estimated_cost or
    # the budget figures (issue #123 — imported and gateway-metered spend
    # must not silently mix).
    imported_totals = crud_api_usage.get_imported_usage_summary(
        db,
        account_id=str(account.id),
        start_date=summary.period_start,
        end_date=summary.period_end,
        runtime_principal_id=runtime_principal_id,
    )
    imported_usage = None
    if imported_totals["event_count"]:
        imported_usage = ImportedUsageSummary(
            event_count=imported_totals["event_count"],
            total_tokens=imported_totals["total_tokens"],
            imported_cost=imported_totals["imported_cost"],
            usage_by_model=[
                ImportedUsageByModel(**row)
                for row in crud_api_usage.get_imported_usage_by_model(
                    db,
                    account_id=str(account.id),
                    start_date=summary.period_start,
                    end_date=summary.period_end,
                    runtime_principal_id=runtime_principal_id,
                )
            ]
            if include_imported
            else [],
            # Per-conversation rollup for the console: subagent workers
            # (parent_conversation_id) nest under their parent thread, and
            # estimated vs reconciled amounts stay separate fields — the UI
            # must never add them together (design-partner honesty rail).
            usage_by_conversation=[
                ImportedUsageByConversation(**row)
                for row in crud_api_usage.get_imported_usage_by_conversation(
                    db,
                    account_id=str(account.id),
                    start_date=summary.period_start,
                    end_date=summary.period_end,
                    runtime_principal_id=runtime_principal_id,
                )
            ]
            if include_imported
            else [],
        )
    # Subscription workload is a SEPARATE block too: its marginal spend is
    # $0 and its API-equivalent figure is an estimate, never a bill (#1401).
    subscription_totals = crud_api_usage.get_subscription_usage_summary(
        db,
        account_id=str(account.id),
        start=summary.period_start,
        end=summary.period_end,
        runtime_principal_id=runtime_principal_id,
        exclude_retries=exclude_retries,
    )
    subscription_usage = None
    if subscription_totals["request_count"]:
        requests = subscription_totals["request_count"]
        covered = subscription_totals["coverage_rows"]
        subscription_usage = SubscriptionUsageSummary(
            request_count=requests,
            prompt_tokens=subscription_totals["prompt_tokens"],
            completion_tokens=subscription_totals["completion_tokens"],
            total_tokens=subscription_totals["total_tokens"],
            api_equivalent_cost=subscription_totals["api_equivalent_cost"],
            covered_requests=covered,
            coverage=covered / requests,
        )
    return CostAnalyticsSummaryResponse(
        period_start=summary.period_start,
        period_end=summary.period_end,
        total_requests=summary.total_requests,
        successful_requests=summary.successful_requests,
        failed_requests=summary.failed_requests,
        token_usage=summary.token_usage,
        estimated_cost=summary.estimated_cost,
        unpriced_requests=summary.unpriced_requests,
        unpriced_tokens=summary.unpriced_tokens,
        unpriced_models=[
            UnpricedModelUsage(**row)
            for row in crud_api_usage.get_unpriced_model_breakdown(
                db,
                account_id=str(account.id),
                start_date=summary.period_start,
                end_date=summary.period_end,
                runtime_principal_id=runtime_principal_id,
                exclude_retries=exclude_retries,
                limit=UNPRICED_MODELS_LIMIT,
            )
        ]
        if summary.unpriced_requests
        else [],
        price_catalog=_price_catalog_info(),
        budget=summary.budget,
        requests_by_day=summary.requests_by_day,
        usage_by_model=summary.usage_by_model,
        usage_by_flow=summary.usage_by_flow,
        usage_by_session=summary.usage_by_session,
        total_sessions=summary.total_sessions,
        usage_by_tool=summary.usage_by_tool,
        imported_usage=imported_usage,
        subscription_usage=subscription_usage,
    )


@router.get("/health", response_model=CostHealthResponse)
@require_permission("view_cost")
def get_cost_health(
    hours: int = Query(
        24,
        ge=1,
        le=168,
        description="Lookback window in hours (max 168 = 7 days).",
    ),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> CostHealthResponse:
    """Run the gateway accounting self-check over a lookback window.

    Verifies end-to-end that gateway traffic is recorded, streaming usage is
    captured, requests are priced, usage is provider-reported, and audit
    events are being written — so silent accounting breakage cannot go
    unnoticed.
    """
    account = get_account_or_404(db, current_user)
    account_id = str(account.id)
    key = (account_id, hours)
    now = time.monotonic()
    with _HEALTH_CACHE_LOCK:
        cached = _HEALTH_CACHE.get(key)
        generation = _HEALTH_GENERATION.get(account_id, 0)
    if cached is not None and cached[0] > now:
        return cached[1]
    result = CostHealthResponse(
        **run_accounting_checks(db, account_id=account_id, window_hours=hours)
    )
    with _HEALTH_CACHE_LOCK:
        # A reprice that finished while this ran made the result stale:
        # serve it once, but do not keep it for the next minute.
        if _HEALTH_GENERATION.get(account_id, 0) == generation:
            if len(_HEALTH_CACHE) >= _HEALTH_CACHE_MAX_ENTRIES:
                _HEALTH_CACHE.clear()
            _HEALTH_CACHE[key] = (now + HEALTH_CACHE_TTL_SECONDS, result)
    return result


# The accounting self-check reads a day (up to a week) of gateway and audit
# rows. It changes slowly and the Spend page asks it on every load, so each
# worker keeps an answer per (account, window) for a minute (#1583). A
# reprice drops the account's entries; nothing else the check reads is
# changed by a user action that expects to see it at once.
HEALTH_CACHE_TTL_SECONDS = 60.0
_HEALTH_CACHE_MAX_ENTRIES = 1024
_HEALTH_CACHE: dict[tuple[str, int], tuple[float, CostHealthResponse]] = {}
# Bumped by every reprice of the account, so a check that started before it
# is not stored after it.
_HEALTH_GENERATION: dict[str, int] = {}
_HEALTH_CACHE_LOCK = threading.Lock()


def _forget_cost_health(account_id: str) -> None:
    """Drop summary and health entries for an account's plain string id.

    Removing in-flight summaries also prevents their old results from being
    stored after an OSS reprice or CSV reconciliation.
    """
    with _SUMMARY_CACHE_LOCK:
        for key in [key for key in _SUMMARY_CACHE if key[0] == account_id]:
            del _SUMMARY_CACHE[key]
    with _HEALTH_CACHE_LOCK:
        _HEALTH_GENERATION[account_id] = _HEALTH_GENERATION.get(account_id, 0) + 1
        for key in [key for key in _HEALTH_CACHE if key[0] == account_id]:
            del _HEALTH_CACHE[key]


@router.post("/reprice", response_model=RepriceResponse)
@require_permission("manage_budgets")
def reprice_account_gateway_usage(
    reprice_in: RepriceRequest,
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> RepriceResponse:
    """Re-price historical gateway usage synchronously and report real counts.

    Unlike the billing plugin's ``/billing/cost/reprice`` (which dispatches
    windows over 7 days to a background worker and acknowledges with empty
    counters), this endpoint always scans in-request — keyset-paginated, so
    an operator backfilling weeks of history gets actual examined/updated
    numbers instead of an ambiguous async ack. Budget-spend buckets are never
    rewritten; provider-side actuals (``provider``/``reconciled``/
    ``imported``) and ``subscription`` rows are never touched.
    """
    ensure_permission_in_oss(db, current_user, "manage_budgets")
    get_account_or_404(db, current_user)
    window = reprice_in.end_date - reprice_in.start_date
    if window > timedelta(days=REPRICE_MAX_WINDOW_DAYS):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Window exceeds {REPRICE_MAX_WINDOW_DAYS} days; split the "
                "backfill into smaller ranges"
            ),
        )
    # Authorization reads are complete. Release their transaction before the
    # catalog preflight performs provider I/O on detached credentials.
    repricing_account_id = current_user.account_id
    db.rollback()
    result = reprice_gateway_usage(
        db,
        account_id=repricing_account_id,
        start=reprice_in.start_date,
        end=reprice_in.end_date,
        only_unpriced=reprice_in.only_unpriced,
        dry_run=reprice_in.dry_run,
    )
    if not reprice_in.dry_run:
        _forget_cost_health(str(repricing_account_id))
    return RepriceResponse(
        submitted_async=False,
        rows_examined=result.rows_examined,
        rows_updated=result.rows_updated,
        rows_skipped=result.rows_skipped,
        cost_before=result.cost_before,
        cost_after=result.cost_after,
        dry_run=result.dry_run,
        provider_lookup=result.provider_lookup,
    )


@router.post("/ledger-backfill/csv", response_model=LedgerBackfillResponse)
@require_permission("manage_budgets")
async def backfill_from_provider_ledger_csv(
    file: UploadFile = File(
        ...,
        description=(
            "OpenRouter Activity -> Explore daily export "
            "(columns: date__day, model, total_usage)"
        ),
    ),
    provider: str = Form(default="openrouter"),
    start: Optional[date] = Form(
        default=None, description="First UTC day (inclusive); default: CSV min."
    ),
    end: Optional[date] = Form(
        default=None, description="Last UTC day (inclusive); default: CSV max."
    ),
    apply: bool = Form(
        default=False,
        description="Persist the allocation. Default is a dry run.",
    ),
    db: Session = Depends(get_db_session),
    current_user: models.User = Depends(get_current_active_user),
) -> LedgerBackfillResponse:
    """Reconcile unpriced gateway rows against a provider daily-usage export.

    Distributes each (day x model) ledger total across this account's
    still-unpriced gateway rows for that day — pro-rata by tokens, equal
    split when the bucket recorded no tokens — and tags them
    ``cost_source='reconciled'`` so provider actuals never mix with catalog
    estimates. Only rows without a resolved cost are ever touched, which
    also makes re-running the same export idempotent. The export's "Other"
    bucket is reported as an unallocatable residual, never distributed.

    Defaults to a dry run that returns the full allocation plan; pass
    ``apply=true`` to persist it.
    """
    await run_db_off_loop(
        lambda: ensure_permission_in_oss(db, current_user, "manage_budgets")
    )
    get_account_or_404(db, current_user)
    raw = await file.read(MAX_LEDGER_CSV_BYTES + 1)
    if len(raw) > MAX_LEDGER_CSV_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"CSV exceeds the {MAX_LEDGER_CSV_BYTES // (1024 * 1024)} MiB limit"
            ),
        )
    try:
        ledger = parse_explorer_csv(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    ledger_days = [entry.day for entry in ledger.entries] + [
        day for day, _ in ledger.other_by_day
    ]
    if not ledger_days:
        raise HTTPException(
            status_code=422, detail="The export contains no usable ledger rows"
        )
    start = start or min(ledger_days)
    end = end or max(ledger_days)
    if end < start:
        raise HTTPException(status_code=422, detail="end must not be before start")

    entries = [entry for entry in ledger.entries if start <= entry.day <= end]
    other_residual = sum(usd for day, usd in ledger.other_by_day if start <= day <= end)
    rows = load_unpriced_rows(
        db,
        account_id=str(current_user.account_id),
        provider_name=provider.strip().lower(),
        start=start,
        end=end,
    )
    plan = plan_ledger_allocation(entries, rows)
    rows_updated = apply_ledger_backfill(db, plan) if apply else None
    if rows_updated:
        _forget_cost_health(str(current_user.account_id))

    return LedgerBackfillResponse(
        dry_run=not apply,
        provider=provider.strip().lower(),
        start=start,
        end=end,
        ledger_entries=len(entries),
        eligible_rows=len(rows),
        rows_to_reconcile=len(plan.allocations),
        total_allocated=round(
            sum(allocation.allocated_cost for allocation in plan.allocations), 12
        ),
        rows_updated=rows_updated,
        other_residual_usd=round(other_residual, 12),
        buckets=[
            LedgerBackfillBucket(
                day=bucket.day,
                family=bucket.family,
                ledger_total=bucket.ledger_total,
                row_count=bucket.row_count,
                allocated_total=bucket.allocated_total,
                ledger_models=list(bucket.ledger_models),
            )
            for bucket in plan.buckets
        ],
        unallocated_ledger=[
            LedgerBackfillResidual(day=day, family=family, amount_usd=usd)
            for day, family, usd in plan.unallocated_ledger
        ],
        unmatched_rows=[
            LedgerBackfillUnmatched(day=day, family=family, row_count=count)
            for day, family, count in plan.unmatched_rows
        ],
        skipped_csv_rows=ledger.skipped,
    )
