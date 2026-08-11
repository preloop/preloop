#!/usr/bin/env python
"""Reconcile unpriced OpenRouter gateway rows against the provider's ledger.

OpenRouter's activity endpoint reports actual daily spend per model. This
script distributes those daily totals across our gateway usage rows that
could not be priced at request time (``cost_source='unpriced'``),
proportionally by tokens within each (day x model family) bucket, and tags
them ``cost_source='reconciled'``. Rows priced by the catalog or by the
provider itself are never touched.

Defaults to a DRY RUN: it prints, per day and model family, the ledger
total, our eligible row count and the allocation sum, plus the per-flow-
execution cost deltas — and writes nothing until ``--apply`` is passed.

The OpenRouter key is read from the OPENROUTER_ACTIVITY_KEY environment
variable, never from code or the database. Note the activity endpoint
requires a management/provisioning key and only serves the last 30
completed UTC days.

Usage:
    OPENROUTER_ACTIVITY_KEY=... python scripts/backfill_openrouter_ledger.py \\
        --account-id <uuid> --start 2026-08-04 --end 2026-08-11

    # After reviewing the dry-run output:
    OPENROUTER_ACTIVITY_KEY=... python scripts/backfill_openrouter_ledger.py \\
        --account-id <uuid> --start 2026-08-04 --end 2026-08-11 --apply

In-cluster, run it from a backend pod (which has DB access configured):

    kubectl exec -it deploy/<backend-deployment> -- env \\
        OPENROUTER_ACTIVITY_KEY=<key> \\
        python scripts/backfill_openrouter_ledger.py \\
        --account-id <uuid> --start 2026-08-04 --end 2026-08-11
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone

import click

from preloop.models.db.session import get_db_session
from preloop.services.ledger_backfill import (
    apply_ledger_backfill,
    fetch_openrouter_activity,
    load_unpriced_rows,
    plan_ledger_allocation,
)

PROVIDER_NAME = "openrouter"


def _parse_day(value: str, label: str) -> date:
    """Parse a YYYY-MM-DD CLI argument."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise click.UsageError(f"--{label} must be YYYY-MM-DD, got {value!r}") from exc


@click.command()
@click.option(
    "--account-id",
    required=True,
    help="Account whose unpriced OpenRouter rows are reconciled (no default).",
)
@click.option("--start", "start_str", required=True, help="First UTC day, YYYY-MM-DD.")
@click.option(
    "--end", "end_str", required=True, help="Last UTC day (inclusive), YYYY-MM-DD."
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Persist the allocation. Without this the run is a dry run.",
)
def main(account_id: str, start_str: str, end_str: str, apply_changes: bool) -> None:
    """Reconcile unpriced OpenRouter usage against the provider's daily ledger."""
    start = _parse_day(start_str, "start")
    end = _parse_day(end_str, "end")
    if end < start:
        raise click.UsageError("--end must not be before --start.")
    if (end - start).days > 31:
        raise click.UsageError(
            "Window exceeds 31 days; the activity ledger only serves the "
            "last 30 completed UTC days."
        )
    today_utc = datetime.now(timezone.utc).date()
    oldest_served = today_utc - timedelta(days=30)
    if start < oldest_served:
        raise click.UsageError(
            f"--start {start.isoformat()} is older than the activity "
            f"ledger's horizon ({oldest_served.isoformat()}: the API serves "
            "only the last 30 completed UTC days). Narrow the window."
        )
    if end >= today_utc:
        click.echo(
            f"Note: {end.isoformat()} is not a completed UTC day yet; its "
            "ledger figures may still grow. Rows from an incomplete day are "
            "safe to re-run later (only still-unpriced rows are touched)."
        )

    api_key = os.environ.get("OPENROUTER_ACTIVITY_KEY", "").strip()
    if not api_key:
        raise click.UsageError(
            "Set OPENROUTER_ACTIVITY_KEY to the OpenRouter key to query the "
            "activity ledger with (a management/provisioning key)."
        )

    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    click.echo(f"Fetching OpenRouter activity for {len(days)} day(s)…")
    ledger_entries = fetch_openrouter_activity(api_key, days)
    click.echo(f"Ledger rows: {len(ledger_entries)}")

    db = next(get_db_session())
    try:
        rows = load_unpriced_rows(
            db,
            account_id=account_id,
            provider_name=PROVIDER_NAME,
            start=start,
            end=end,
        )
        click.echo(f"Eligible unpriced {PROVIDER_NAME} rows: {len(rows)}")

        plan = plan_ledger_allocation(ledger_entries, rows)

        mode = "APPLY" if apply_changes else "DRY RUN"
        click.echo(f"\n[{mode}] Allocation per day x model family:")
        for bucket in plan.buckets:
            models = ", ".join(bucket.ledger_models) or "-"
            click.echo(
                f"  {bucket.day} {bucket.family:<28} "
                f"ledger=${bucket.ledger_total:.6f} rows={bucket.row_count} "
                f"allocated=${bucket.allocated_total:.6f} (models: {models})"
            )
        if not plan.buckets:
            click.echo("  (nothing to allocate)")

        if plan.unallocated_ledger:
            click.echo("\nLedger spend with no eligible rows (left alone):")
            for day, family, usd in plan.unallocated_ledger:
                click.echo(f"  {day} {family:<28} ${usd:.6f}")
        if plan.unmatched_rows:
            click.echo("\nUnpriced rows with no ledger spend (left unpriced):")
            for day, family, count in plan.unmatched_rows:
                click.echo(f"  {day} {family:<28} rows={count}")

        deltas = plan.execution_deltas
        if deltas:
            click.echo(f"\nPer-flow-execution cost deltas ({len(deltas)}):")
            for execution_id in sorted(deltas, key=str):
                click.echo(f"  {execution_id}: +${deltas[execution_id]:.6f}")

        total = sum(a.allocated_cost for a in plan.allocations)
        click.echo(
            f"\n[{mode}] rows to reconcile={len(plan.allocations)}, "
            f"total allocated=${total:.6f}"
        )

        if not apply_changes:
            click.echo("Dry run only. Re-run with --apply to persist.")
            return

        updated = apply_ledger_backfill(db, plan)
        click.echo(f"Updated {updated} row(s); execution rollups synced.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - operator-facing script
        click.echo(f"Ledger backfill failed: {exc}", err=True)
        sys.exit(1)
