"""Local operator command for bounded replay of parked audit intents."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from uuid import UUID

from preloop.models.crud import audit_outbox as crud_audit_outbox
from preloop.models.db.session import get_session_factory


def _batch_size(value: str) -> int:
    """Accept a finite operator batch between one and one thousand intents."""
    size = int(value)
    if not 1 <= size <= 1000:
        raise argparse.ArgumentTypeError("limit must be between 1 and 1000")
    return size


def main(argv: Sequence[str] | None = None) -> int:
    """Preview or requeue one account's parked intents through the CRUD layer."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True, type=UUID)
    parser.add_argument("--limit", type=_batch_size, default=100)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="requeue the selected batch")
    mode.add_argument("--dry-run", action="store_false", dest="apply")
    parser.set_defaults(apply=False)
    args = parser.parse_args(argv)
    with get_session_factory()() as db:
        count = crud_audit_outbox.requeue_failed(
            db,
            account_id=args.account_id,
            limit=args.limit,
            dry_run=not args.apply,
        )
    verb = "Requeued" if args.apply else "Would requeue"
    print(f"{verb} {count} failed audit intents for account {args.account_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
