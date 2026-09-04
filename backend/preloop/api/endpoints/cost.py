"""Open-source cost analytics endpoints."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

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
from preloop.models.crud import crud_api_usage
from preloop.models.db.session import get_db_session
from preloop.models.models.user import User
from preloop.schemas.cost_analytics import (
    CostAnalyticsSummaryResponse,
    CostHealthResponse,
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
    UnpricedModelUsage,
)
from preloop.services.gateway_accounting_check import run_accounting_checks
from preloop.services.ledger_backfill import (
    apply_ledger_backfill,
    load_unpriced_rows,
    parse_explorer_csv,
    plan_ledger_allocation,
)
from preloop.services.model_gateway_usage import ModelGatewayUsageService
from preloop.services.usage_repricing import reprice_gateway_usage
from preloop.utils.permissions import require_permission

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


def _price_catalog_info() -> Optional[PriceCatalogInfo]:
    """Return vendored price-catalog provenance for the staleness indicator."""
    try:
        from preloop.services.model_price_catalog import catalog_metadata

        meta = catalog_metadata()
        return PriceCatalogInfo(**meta) if meta else None
    except Exception:  # noqa: BLE001 - purely informational
        return None


@router.get("/summary", response_model=CostAnalyticsSummaryResponse)
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
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
) -> CostAnalyticsSummaryResponse:
    """Return the OSS cost overview using gateway usage and pricing metadata."""
    account = get_account_or_404(db, current_user)
    summary = ModelGatewayUsageService(db).get_account_summary(
        account=account,
        start_date=start_date,
        end_date=end_date,
        runtime_principal_id=runtime_principal_id,
        include_breakdown=True,
        exclude_retries=exclude_retries,
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
            ],
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
            ],
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
                limit=UNPRICED_MODELS_LIMIT,
            )
        ],
        price_catalog=_price_catalog_info(),
        budget=summary.budget,
        requests_by_day=summary.requests_by_day,
        usage_by_model=summary.usage_by_model,
        usage_by_flow=summary.usage_by_flow,
        usage_by_session=summary.usage_by_session,
        usage_by_tool=summary.usage_by_tool,
        imported_usage=imported_usage,
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
    current_user: User = Depends(get_current_active_user),
) -> CostHealthResponse:
    """Run the gateway accounting self-check over a lookback window.

    Verifies end-to-end that gateway traffic is recorded, streaming usage is
    captured, requests are priced, usage is provider-reported, and audit
    events are being written — so silent accounting breakage cannot go
    unnoticed.
    """
    account = get_account_or_404(db, current_user)
    result = run_accounting_checks(db, account_id=str(account.id), window_hours=hours)
    return CostHealthResponse(**result)


@router.post("/reprice", response_model=RepriceResponse)
@require_permission("manage_budgets")
def reprice_account_gateway_usage(
    reprice_in: RepriceRequest,
    db: Session = Depends(get_db_session),
    current_user: User = Depends(get_current_active_user),
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
    result = reprice_gateway_usage(
        db,
        account_id=current_user.account_id,
        start=reprice_in.start_date,
        end=reprice_in.end_date,
        only_unpriced=reprice_in.only_unpriced,
        dry_run=reprice_in.dry_run,
    )
    return RepriceResponse(
        submitted_async=False,
        rows_examined=result.rows_examined,
        rows_updated=result.rows_updated,
        rows_skipped=result.rows_skipped,
        cost_before=result.cost_before,
        cost_after=result.cost_after,
        dry_run=result.dry_run,
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
    current_user: User = Depends(get_current_active_user),
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
