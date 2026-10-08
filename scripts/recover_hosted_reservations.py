#!/usr/bin/env python
"""Settle stranded hosted reservations with the cost the gateway measured.

Dry run by default: prints every unresolved reservation of the account, the
``api_usage`` row it was matched to, the measured tokens and the cost at the
hosted model's tariff. Nothing is written until ``--apply`` is passed, and
then only matched rows are settled (each with a ``hosted_recovery`` audit).
Rows without an unambiguous measurement are listed as "needs manual review"
and never released.

See ``preloop.services.hosted_reservation_recovery`` for how reservations
are matched to usage rows.

Usage:
    python scripts/recover_hosted_reservations.py \\
        --account-id <uuid> --hosted-model-id <uuid>

    # After reviewing the dry run:
    python scripts/recover_hosted_reservations.py \\
        --account-id <uuid> --hosted-model-id <uuid> --apply
"""

from __future__ import annotations

import sys
import uuid
from datetime import timedelta

import click

from preloop.models.db.session import get_db_session
from preloop.services.hosted_reservation_recovery import recover

COLUMNS = (
    ("reservation_id", 36),
    ("created_at", 26),
    ("reserved_usd", 14),
    ("api_usage_id", 36),
    ("prompt_tokens", 9),
    ("completion_tokens", 9),
    ("actual_usd", 14),
    ("outcome", 20),
)


def _cell(value: object, width: int) -> str:
    text = "-" if value is None else str(value)
    return text[:width].ljust(width)


@click.command()
@click.option("--account-id", type=click.UUID, required=True)
@click.option(
    "--hosted-model-id",
    type=click.UUID,
    required=True,
    help="The system (account NULL) hosted ai_model row that served the calls.",
)
@click.option(
    "--slack-seconds",
    type=int,
    default=5,
    show_default=True,
    help="Clock tolerance around each usage row's request window.",
)
@click.option("--apply", is_flag=True, help="Settle matched rows (default: dry run).")
def main(
    account_id: uuid.UUID, hosted_model_id: uuid.UUID, slack_seconds: int, apply: bool
) -> None:
    db = next(get_db_session())
    try:
        plan = recover(
            db,
            account_id=account_id,
            hosted_model_id=hosted_model_id,
            apply=apply,
            slack=timedelta(seconds=slack_seconds),
        )
        if apply:
            db.commit()
        else:
            db.rollback()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    click.echo(" ".join(_cell(name, width) for name, width in COLUMNS) + " reason")
    for entry in plan:
        click.echo(
            " ".join(_cell(getattr(entry, name), width) for name, width in COLUMNS)
            + f" {entry.reason}"
        )
    matched = [entry for entry in plan if entry.actual_usd is not None]
    review = [entry for entry in plan if entry.actual_usd is None]
    held = sum(float(entry.reserved_usd) for entry in matched)
    cost = sum(float(entry.actual_usd or 0) for entry in matched)
    click.echo(
        f"\n{'APPLIED' if apply else 'DRY RUN'}: {len(plan)} unresolved, "
        f"{len(matched)} matched (held {held:.6f} USD, measured {cost:.6f} USD), "
        f"{len(review)} needs manual review"
    )


if __name__ == "__main__":
    sys.exit(main())
