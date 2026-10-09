#!/usr/bin/env python
"""Repair cache/reasoning token columns on historical Responses usage rows.

Before #1401 the gateway did not read the OpenAI Responses usage shape
(``input_tokens_details.cached_tokens``,
``output_tokens_details.reasoning_tokens``), so those rows were stored with
NULL ``cache_read_tokens`` / ``reasoning_tokens``. The raw provider usage is
retained in ``meta_data.usage_details``; this script re-runs the fixed
extractor over it and fills only the NULL columns.

Defaults to a DRY RUN that reports counts and writes nothing. ``--apply``
writes. Re-running is safe: filled columns are never overwritten.

Cost: pricing reads the retained ``usage_details``, not these columns, so the
repair alone changes no cost. Operator-configured pricing (and account
overrides) only began reading the Responses cache shape with the same fix,
so rows priced from configured pricing before it billed Responses cache reads
at the full input price. Fix those in one of two ways:

- ``--apply --reprice`` re-prices only the repaired rows, with the same
  protections as Apply to past usage (subscription rows and provider-side
  actuals are never rewritten, budget spend is never changed); or
- run the repair, then use "Apply to past usage" on the price override in
  the console for the affected window.

Usage:
    python scripts/repair_usage_token_details.py --account-id <uuid> \\
        --since 2026-09-01 --until 2026-10-09
    # review the counts, then:
    python scripts/repair_usage_token_details.py --account-id <uuid> \\
        --since 2026-09-01 --until 2026-10-09 --apply [--reprice]
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Optional

import click

from preloop.models.db.session import get_db_session
from preloop.services.usage_token_repair import repair_token_details


def _parse_day(value: Optional[str], label: str) -> Optional[datetime]:
    """Parse a YYYY-MM-DD CLI argument to UTC midnight."""
    if value is None:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise click.UsageError(f"--{label} must be YYYY-MM-DD, got {value!r}") from exc
    return datetime.combine(day, time.min, tzinfo=timezone.utc)


@click.command()
@click.option("--account-id", default=None, help="Restrict to one account.")
@click.option("--since", "since_str", default=None, help="First UTC day, YYYY-MM-DD.")
@click.option(
    "--until",
    "until_str",
    default=None,
    help="Exclusive end UTC day, YYYY-MM-DD.",
)
@click.option(
    "--limit",
    default=1000,
    show_default=True,
    type=click.IntRange(min=1),
    help="Maximum candidate rows examined in this run.",
)
@click.option(
    "--dry-run/--apply",
    "dry_run",
    default=True,
    show_default=True,
    help="Dry run (default) reports counts; --apply writes the repair.",
)
@click.option(
    "--reprice",
    is_flag=True,
    default=False,
    help=(
        "With --apply: re-price the repaired rows (protected cost sources "
        "such as subscription are never rewritten; budget spend is unchanged)."
    ),
)
def main(
    account_id: Optional[str],
    since_str: Optional[str],
    until_str: Optional[str],
    limit: int,
    dry_run: bool,
    reprice: bool,
) -> None:
    """Fill NULL cache/reasoning token columns from retained Responses usage."""
    since = _parse_day(since_str, "since")
    until = _parse_day(until_str, "until")
    if since and until and until <= since:
        raise click.UsageError("--until must be after --since.")
    if reprice and dry_run:
        raise click.UsageError("--reprice requires --apply.")

    db = next(get_db_session())
    try:
        result = repair_token_details(
            db,
            account_id=account_id,
            since=since,
            until=until,
            limit=limit,
            apply=not dry_run,
            reprice=reprice,
        )
    finally:
        db.close()
    mode = "DRY RUN" if dry_run else "APPLY"
    click.echo(f"[{mode}] rows examined:   {result.rows_examined}")
    click.echo(f"[{mode}] rows repairable: {result.rows_repairable}")
    for column, count in result.columns_filled.items():
        click.echo(f"[{mode}]   {column}: {count}")
    click.echo(f"[{mode}] rows updated:    {result.rows_updated}")
    if reprice:
        click.echo(f"[{mode}] rows repriced:   {result.rows_repriced}")
    if result.limit_reached:
        click.echo(f"Limit of {limit} rows reached; run again to continue.")
    if dry_run:
        click.echo("Nothing was written. Re-run with --apply to persist.")


if __name__ == "__main__":
    main()
