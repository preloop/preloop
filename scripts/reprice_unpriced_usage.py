#!/usr/bin/env python
"""Backfill costs for gateway usage rows recorded as unpriced.

Historical rows recorded while a model was missing from the price catalog
carry ``estimated_cost = NULL`` and ``cost_source = 'unpriced'``. Once pricing
resolves for that model, this script recomputes their cost from the tokens
already stored on each row, so past dashboards become accurate retroactively.

Defaults to a DRY RUN: it reports what would change and writes nothing until
``--apply`` is passed. Subscription-covered rows are skipped (their $0 is
correct by construction) and budget spend is never rewritten, because spend
was charged at request time and repricing is analytics-only.

Examples:
    # See what would change for one account, writing nothing.
    python scripts/reprice_unpriced_usage.py \\
        --account-id 5796260a-ff3d-4362-a656-c78df2d439b4 --days 30

    # Apply it.
    python scripts/reprice_unpriced_usage.py \\
        --account-id 5796260a-ff3d-4362-a656-c78df2d439b4 --days 30 --apply

    # Every account with unpriced rows.
    python scripts/reprice_unpriced_usage.py --all-accounts --days 30 --apply
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import List

import click
from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session
from preloop.services.model_price_catalog import load_catalog
from preloop.services.usage_repricing import reprice_gateway_usage


def _accounts_with_unpriced_rows(db: Session, start: datetime) -> List[str]:
    """Return account ids holding unpriced gateway rows in the window.

    Args:
        db: Database session.
        start: Inclusive lower bound on the usage timestamp.

    Returns:
        Account ids as strings, most affected first.
    """
    rows = db.execute(
        text(
            """
            SELECT account_id, count(*) AS n
            FROM api_usage
            WHERE action_type = 'model_gateway'
              AND cost_source = 'unpriced'
              AND estimated_cost IS NULL
              AND total_tokens > 0
              AND timestamp >= :start
            GROUP BY account_id
            ORDER BY n DESC
            """
        ),
        {"start": start},
    ).fetchall()
    return [str(row[0]) for row in rows if row[0]]


@click.command()
@click.option("--account-id", default=None, help="Account to reprice.")
@click.option(
    "--all-accounts",
    is_flag=True,
    default=False,
    help="Reprice every account that has unpriced rows in the window.",
)
@click.option("--days", default=30, show_default=True, help="Lookback in days.")
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Persist changes. Without this the run is a dry run.",
)
def main(
    account_id: str | None, all_accounts: bool, days: int, apply_changes: bool
) -> None:
    """Reprice unpriced gateway usage rows for one or all accounts."""
    if not account_id and not all_accounts:
        raise click.UsageError("Pass --account-id <uuid> or --all-accounts.")

    # Ensure the price catalog (and its live-lookup fallbacks) are loaded, or
    # every row would simply be found unpriced again.
    load_catalog()

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    db = next(get_db_session())
    try:
        targets = (
            _accounts_with_unpriced_rows(db, start) if all_accounts else [account_id]
        )
        if not targets:
            click.echo("No accounts with unpriced usage in the window.")
            return

        mode = "APPLY" if apply_changes else "DRY RUN"
        click.echo(f"[{mode}] {len(targets)} account(s), window {start} .. {end}")

        total_updated = 0
        total_delta = 0.0
        for target in targets:
            result = reprice_gateway_usage(
                db,
                account_id=target,
                start=start,
                end=end,
                only_unpriced=True,
                dry_run=not apply_changes,
            )
            delta = result.cost_after - result.cost_before
            total_updated += result.rows_updated
            total_delta += delta
            click.echo(
                f"  {target}: examined={result.rows_examined} "
                f"updated={result.rows_updated} skipped={result.rows_skipped} "
                f"cost {result.cost_before:.4f} -> {result.cost_after:.4f} "
                f"(+{delta:.4f})"
            )

        click.echo(
            f"[{mode}] total rows updated={total_updated}, "
            f"total cost recovered=${total_delta:.4f}"
        )
        if not apply_changes:
            click.echo("Dry run only. Re-run with --apply to persist.")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 - operator-facing script
        click.echo(f"Repricing failed: {exc}", err=True)
        sys.exit(1)
