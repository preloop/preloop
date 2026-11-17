"""Deactivate run-scoped API keys whose execution or runtime session ended.

Flow executions and runtime sessions mint their own API keys. Those keys
expire and the normal end-of-run paths revoke them, but a run that ended
through a path that skipped the revoke (a crash, a recovery sweep, an older
release) left its key active. Expired keys cannot authenticate, yet they
stay ``is_active`` and pile up in key counts.

This pass finds active keys bound (in ``context_data``) to a flow execution
or runtime session that has ended, or that have expired, and deactivates
them in bounded batches with one audit row per batch and account. Keys
without such a binding, which includes every key a person created by hand,
and durable managed-agent credentials are never touched.

Operators run it with::

    python -m preloop.services.run_key_maintenance            # dry run
    python -m preloop.services.run_key_maintenance --apply

A scheduled task (``deactivate_ended_run_keys``) applies it hourly.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models.crud import crud_api_key, crud_audit_log

logger = logging.getLogger(__name__)

DEFAULT_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 20
AUDIT_ACTION = "run_keys_deactivated"


@dataclass
class RunKeyMaintenanceResult:
    """What one pass found and did.

    Attributes:
        dry_run: True when nothing was changed.
        batches: Batches read.
        matched: Keys that qualified, per reason.
        deactivated: Keys deactivated (0 in a dry run).
        complete: False when the pass stopped at ``max_batches`` with more
            keys possibly left.
    """

    dry_run: bool
    batches: int = 0
    matched: Counter = field(default_factory=Counter)
    deactivated: int = 0
    complete: bool = True

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly summary (counts only)."""
        return {
            "mode": "dry-run" if self.dry_run else "apply",
            "batches": self.batches,
            "matched": dict(self.matched),
            "matched_total": sum(self.matched.values()),
            "deactivated": self.deactivated,
            "complete": self.complete,
        }


def deactivate_ended_run_keys(
    db: Session,
    *,
    dry_run: bool = True,
    batch_size: int = DEFAULT_BATCH_SIZE,
    max_batches: int = DEFAULT_MAX_BATCHES,
    account_id: Optional[UUID] = None,
    now: Optional[datetime] = None,
) -> RunKeyMaintenanceResult:
    """Run one bounded pass.

    Args:
        db: Database session; each applied batch commits on its own.
        dry_run: Count only, change nothing.
        batch_size: Keys per batch.
        max_batches: Batches per pass.
        account_id: Restrict to one account.
        now: Current time (naive or aware UTC); defaults to now.

    Returns:
        Counts per reason and keys deactivated.
    """
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    result = RunKeyMaintenanceResult(dry_run=dry_run)
    cursor: Optional[Any] = None
    for _ in range(max_batches):
        rows = crud_api_key.ended_run_key_batch(
            db,
            now=moment,
            after_id=cursor,
            limit=batch_size,
            account_id=account_id,
        )
        if not rows:
            break
        result.batches += 1
        cursor = rows[-1][0]
        by_account: dict[Any, list[tuple[Any, str]]] = defaultdict(list)
        for key_id, key_account_id, reason in rows:
            result.matched[reason] += 1
            by_account[key_account_id].append((key_id, reason))
        if not dry_run:
            result.deactivated += _apply_batch(db, by_account, result.batches)
        if len(rows) < batch_size:
            break
    else:
        result.complete = False
    return result


def _apply_batch(
    db: Session, by_account: dict[Any, list[tuple[Any, str]]], batch_number: int
) -> int:
    """Deactivate one batch and audit it per account in one transaction."""
    deactivated = 0
    try:
        for key_account_id, items in by_account.items():
            changed = crud_api_key.deactivate_ids(
                db, key_ids=[key_id for key_id, _ in items]
            )
            deactivated += changed
            crud_audit_log.log_action(
                db,
                account_id=key_account_id,
                user_id=None,
                action=AUDIT_ACTION,
                resource_type="api_key",
                status="success",
                details={
                    "reason_code": "run_ended",
                    "batch": batch_number,
                    "deactivated": changed,
                    "by_reason": dict(Counter(reason for _, reason in items)),
                    "key_ids": [str(key_id) for key_id, _ in items],
                },
                commit=False,
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return deactivated


def main(argv: Optional[list[str]] = None) -> None:
    """Command-line entry point; prints counts only."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Default.")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--account-id", type=UUID)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--max-batches", type=int, default=DEFAULT_MAX_BATCHES)
    args = parser.parse_args(argv)
    from preloop.models.db.session import get_db_session

    sessions = get_db_session()
    db = next(sessions)
    try:
        result = deactivate_ended_run_keys(
            db,
            dry_run=not args.apply,
            batch_size=max(1, args.batch_size),
            max_batches=max(1, args.max_batches),
            account_id=args.account_id,
        )
        print(json.dumps(result.as_dict(), sort_keys=True))
    except Exception as exc:
        db.rollback()
        raise SystemExit("Run key maintenance failed: " + type(exc).__name__) from None
    finally:
        sessions.close()


if __name__ == "__main__":
    main()
