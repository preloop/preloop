"""Backfill unpriced gateway usage from OpenRouter's daily activity ledger.

Rows recorded while no price could be resolved (``cost_source='unpriced'``,
e.g. the Auto Router before usage accounting landed) can be reconciled
against OpenRouter's own activity endpoint, which reports actual daily spend
per model. The provider only exposes DAILY aggregates, so the method is
honest about that granularity:

- Build (day x model-alias family) buckets.
- Distribute each bucket's ledger total across our unpriced rows in that
  bucket proportionally by tokens (prompt + completion; a deliberate,
  simple weighting — cost is roughly token-linear within one model family
  and one day, and anything fancier would pretend precision the daily
  ledger cannot support).
- Auto Router rows ("auto"/"auto-beta" aliases) have no fixed model family:
  the ledger reports the concrete routed model instead. They therefore
  claim the day's ledger spend left unmatched by any concrete family.

Allocated rows are tagged ``cost_source='reconciled'`` (never ``provider``:
these are apportioned daily figures, not per-request charges) and carry a
``meta_data["reconciled"]`` marker recording the method, ledger day, the
bucket's ledger total and the allocation timestamp.

Scope guard: only rows with ``cost_source='unpriced'`` for an explicitly
given account and provider are ever eligible; rows priced by the catalog or
by the provider itself are untouched.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

import requests
from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_usage

logger = logging.getLogger(__name__)

OPENROUTER_ACTIVITY_URL = "https://openrouter.ai/api/v1/activity"

# Auto Router aliases route to a concrete model chosen per request; the
# activity ledger reports that concrete model, so these families act as a
# wildcard claiming the day's otherwise-unmatched ledger spend.
_AUTO_FAMILIES = frozenset({"auto", "auto-beta"})
_AUTO_BUCKET = "auto*"

# ":free" / ":extended" style variant suffixes on OpenRouter slugs.
_VARIANT_SUFFIX = re.compile(r":[a-z0-9-]+$")
# Trailing date stamps (permaslugs like "gpt-4.1-2025-04-14").
_DATE_SUFFIXES = (re.compile(r"-\d{4}-\d{2}-\d{2}$"), re.compile(r"-\d{8}$"))


def alias_family(name: Optional[str]) -> Optional[str]:
    """Reduce a model alias or ledger slug to a comparable family name.

    Both sides of the reconciliation name models differently (our aliases:
    ``openrouter/deepseek/deepseek-chat``; the ledger: ``deepseek/deepseek-chat``
    or a dated permaslug). Stripping routing prefixes, org paths, variant
    suffixes and date stamps leaves a family key both sides agree on.

    Args:
        name: A model alias from our usage rows or a slug from the ledger.

    Returns:
        The normalized family key, or None for empty input.
    """
    if not isinstance(name, str) or not name.strip():
        return None
    family = name.strip().lower()
    for prefix in ("preloop/", "openrouter/"):
        if family.startswith(prefix):
            family = family[len(prefix) :]
    family = _VARIANT_SUFFIX.sub("", family)
    family = family.split("/")[-1]
    for pattern in _DATE_SUFFIXES:
        family = pattern.sub("", family)
    return family or None


@dataclass(frozen=True)
class LedgerEntry:
    """One (day, model) aggregate from the provider's activity ledger."""

    day: date
    model: str
    usage_usd: float


@dataclass(frozen=True)
class UsageRowInfo:
    """The slice of an unpriced usage row the allocator needs."""

    api_usage_id: uuid.UUID
    day: date
    model_alias: Optional[str]
    weight_tokens: int
    flow_execution_id: Optional[uuid.UUID] = None


@dataclass(frozen=True)
class RowAllocation:
    """One row's share of a bucket's ledger total."""

    api_usage_id: uuid.UUID
    flow_execution_id: Optional[uuid.UUID]
    day: date
    bucket_family: str
    ledger_total: float
    allocated_cost: float


@dataclass
class BucketReport:
    """Per-(day x family) reconciliation summary for the dry-run report."""

    day: date
    family: str
    ledger_total: float
    row_count: int
    allocated_total: float
    ledger_models: Tuple[str, ...] = ()


@dataclass
class BackfillPlan:
    """A complete, not-yet-applied allocation."""

    allocations: List[RowAllocation] = field(default_factory=list)
    buckets: List[BucketReport] = field(default_factory=list)
    # Ledger money with no eligible rows to receive it: (day, family, usd).
    unallocated_ledger: List[Tuple[date, str, float]] = field(default_factory=list)
    # Rows whose family had no ledger spend that day: (day, family, count).
    unmatched_rows: List[Tuple[date, str, int]] = field(default_factory=list)

    @property
    def execution_deltas(self) -> Dict[uuid.UUID, float]:
        """Total newly-allocated cost per flow execution."""
        deltas: Dict[uuid.UUID, float] = defaultdict(float)
        for allocation in self.allocations:
            if allocation.flow_execution_id is not None:
                deltas[allocation.flow_execution_id] += allocation.allocated_cost
        return dict(deltas)


def fetch_openrouter_activity(
    api_key: str, days: Sequence[date], *, timeout: float = 30.0
) -> List[LedgerEntry]:
    """Fetch daily per-model spend from OpenRouter's activity endpoint.

    ``GET /api/v1/activity?date=YYYY-MM-DD`` returns ``{"data": [...]}`` with
    one item per (date, model, endpoint): ``date``, ``model``,
    ``model_permaslug``, ``usage`` (USD credits spent) and
    ``byok_usage_inference`` (USD spent upstream on BYOK), among token and
    request counts. Only the last 30 completed UTC days are available, and
    the endpoint requires a management/provisioning key (regular inference
    keys are rejected).

    Args:
        api_key: OpenRouter key, passed by the operator via environment.
        days: UTC days to fetch.
        timeout: Per-request timeout in seconds.

    Returns:
        Ledger entries with ``usage + byok_usage_inference`` as the USD spend
        (the customer paid both).

    Raises:
        RuntimeError: On authentication/permission failures, with a hint
            about management keys.
        requests.HTTPError: On other non-2xx responses.
    """
    entries: List[LedgerEntry] = []
    for day in sorted(set(days)):
        response = requests.get(
            OPENROUTER_ACTIVITY_URL,
            params={"date": day.isoformat()},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        if response.status_code in (401, 403):
            raise RuntimeError(
                f"OpenRouter activity endpoint rejected the key "
                f"(HTTP {response.status_code}). The /api/v1/activity endpoint "
                "requires a management/provisioning key created at "
                "openrouter.ai/settings — a regular inference key is not "
                "sufficient."
            )
        response.raise_for_status()
        payload = response.json()
        for item in payload.get("data") or []:
            usage = float(item.get("usage") or 0.0)
            byok = float(item.get("byok_usage_inference") or 0.0)
            model = item.get("model") or item.get("model_permaslug") or ""
            item_day = date.fromisoformat(item.get("date", day.isoformat())[:10])
            entries.append(
                LedgerEntry(day=item_day, model=model, usage_usd=usage + byok)
            )
    return entries


def _proportional_shares(total: float, weights: Sequence[int]) -> List[float]:
    """Split ``total`` proportionally by ``weights`` (equal split when all 0)."""
    weight_sum = sum(weights)
    if weight_sum <= 0:
        return [round(total / len(weights), 12) for _ in weights]
    return [round(total * weight / weight_sum, 12) for weight in weights]


def plan_ledger_allocation(
    ledger_entries: Sequence[LedgerEntry],
    rows: Sequence[UsageRowInfo],
) -> BackfillPlan:
    """Allocate daily ledger totals across unpriced rows, without writing.

    Pure function: everything the script prints in a dry run and writes in a
    real run is derived from this plan, so the allocation is unit-testable
    against fixture ledger data.

    Args:
        ledger_entries: Daily per-model spend from the provider ledger.
        rows: Eligible (unpriced) usage rows.

    Returns:
        The full allocation plan with per-bucket reporting detail.
    """
    plan = BackfillPlan()

    ledger_by_day: Dict[date, Dict[str, float]] = defaultdict(
        lambda: defaultdict(float)
    )
    ledger_models: Dict[Tuple[date, str], set] = defaultdict(set)
    for entry in ledger_entries:
        family = alias_family(entry.model)
        if family is None or entry.usage_usd <= 0:
            continue
        ledger_by_day[entry.day][family] += entry.usage_usd
        ledger_models[(entry.day, family)].add(entry.model)

    rows_by_day: Dict[date, Dict[str, List[UsageRowInfo]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for row in rows:
        family = alias_family(row.model_alias) or "unknown"
        if family in _AUTO_FAMILIES:
            family = _AUTO_BUCKET
        rows_by_day[row.day][family].append(row)

    for day in sorted(set(ledger_by_day) | set(rows_by_day)):
        day_ledger = ledger_by_day.get(day, {})
        day_rows = rows_by_day.get(day, {})

        matched_families = sorted(
            family
            for family in day_rows
            if family != _AUTO_BUCKET and family in day_ledger
        )
        for family in matched_families:
            _allocate_bucket(
                plan,
                day=day,
                family=family,
                ledger_total=day_ledger[family],
                bucket_rows=day_rows[family],
                models=tuple(sorted(ledger_models[(day, family)])),
            )

        # The Auto Router's rows claim whatever the day's ledger spent on
        # families no concrete-alias row matched (the routed models).
        remainder_families = sorted(
            family for family in day_ledger if family not in day_rows
        )
        remainder_total = sum(day_ledger[family] for family in remainder_families)
        auto_rows = day_rows.get(_AUTO_BUCKET, [])
        if auto_rows and remainder_total > 0:
            remainder_models: List[str] = []
            for family in remainder_families:
                remainder_models.extend(sorted(ledger_models[(day, family)]))
            _allocate_bucket(
                plan,
                day=day,
                family=_AUTO_BUCKET,
                ledger_total=remainder_total,
                bucket_rows=auto_rows,
                models=tuple(remainder_models),
            )
        else:
            if auto_rows:
                plan.unmatched_rows.append((day, _AUTO_BUCKET, len(auto_rows)))
            for family in remainder_families:
                plan.unallocated_ledger.append((day, family, day_ledger[family]))

        for family, bucket_rows in sorted(day_rows.items()):
            if family == _AUTO_BUCKET or family in day_ledger:
                continue
            plan.unmatched_rows.append((day, family, len(bucket_rows)))

    return plan


def _allocate_bucket(
    plan: BackfillPlan,
    *,
    day: date,
    family: str,
    ledger_total: float,
    bucket_rows: Sequence[UsageRowInfo],
    models: Tuple[str, ...],
) -> None:
    """Append one bucket's proportional allocations to the plan."""
    shares = _proportional_shares(
        ledger_total, [row.weight_tokens for row in bucket_rows]
    )
    for row, share in zip(bucket_rows, shares, strict=True):
        plan.allocations.append(
            RowAllocation(
                api_usage_id=row.api_usage_id,
                flow_execution_id=row.flow_execution_id,
                day=day,
                bucket_family=family,
                ledger_total=round(ledger_total, 12),
                allocated_cost=share,
            )
        )
    plan.buckets.append(
        BucketReport(
            day=day,
            family=family,
            ledger_total=round(ledger_total, 12),
            row_count=len(bucket_rows),
            allocated_total=round(sum(shares), 12),
            ledger_models=models,
        )
    )


def load_unpriced_rows(
    db: Session,
    *,
    account_id: str,
    provider_name: str,
    start: date,
    end: date,
) -> List[UsageRowInfo]:
    """Load eligible unpriced rows for the window as allocator inputs.

    Row timestamps are stored in UTC, so the row's day bucket is simply the
    timestamp's date — matching the ledger's UTC days.

    Args:
        db: Database session.
        account_id: Account scope (explicitly required; no default).
        provider_name: Recorded provider name (e.g. ``openrouter``).
        start: First UTC day (inclusive).
        end: Last UTC day (inclusive).

    Returns:
        Allocator row descriptors, weighted by prompt + completion tokens.
    """
    window_start = datetime.combine(start, time.min)
    window_end = datetime.combine(end + timedelta(days=1), time.min)
    rows: List[UsageRowInfo] = []
    for row in crud_api_usage.iter_unpriced_provider_rows(
        db,
        account_id=account_id,
        provider_name=provider_name,
        start=window_start,
        end=window_end,
    ):
        rows.append(
            UsageRowInfo(
                api_usage_id=row.id,
                day=row.timestamp.date(),
                model_alias=row.model_alias,
                weight_tokens=int(row.prompt_tokens or 0)
                + int(row.completion_tokens or 0),
                flow_execution_id=row.flow_execution_id,
            )
        )
    return rows


def apply_ledger_backfill(db: Session, plan: BackfillPlan) -> int:
    """Write a plan's allocations and refresh affected execution rollups.

    Each row is re-checked to still be ``unpriced`` immediately before the
    write, so a row priced by anything else since planning is left alone.

    Args:
        db: Database session.
        plan: The allocation plan to persist.

    Returns:
        Number of rows updated.
    """
    from preloop.services.usage_repricing import _sync_execution_rollups

    allocated_at = datetime.now(timezone.utc).isoformat()
    updated = 0
    touched_executions = set()
    for allocation in plan.allocations:
        row = crud_api_usage.get(db, id=allocation.api_usage_id)
        if row is None or row.cost_source != "unpriced":
            logger.info("Skipping row %s: no longer unpriced", allocation.api_usage_id)
            continue
        crud_api_usage.update_cost_fields(
            db,
            api_usage_id=allocation.api_usage_id,
            estimated_cost=allocation.allocated_cost,
            cost_source="reconciled",
            meta_data_patch={
                "reconciled": {
                    "method": "ledger_daily_proportional",
                    "ledger_day": allocation.day.isoformat(),
                    "ledger_total": allocation.ledger_total,
                    "allocated_at": allocated_at,
                }
            },
            commit=False,
        )
        updated += 1
        if allocation.flow_execution_id is not None:
            touched_executions.add(allocation.flow_execution_id)
        if updated % 500 == 0:
            db.commit()
    db.commit()

    if touched_executions:
        # Stored per-execution rollups were computed while these rows were
        # unpriced; re-derive them so each run's cost equals the sum of its
        # usage rows again (same path as #211's repricing sync).
        _sync_execution_rollups(db, sorted(touched_executions, key=str))
    return updated
