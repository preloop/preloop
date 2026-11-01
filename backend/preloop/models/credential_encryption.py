"""Seal stored credentials in place (encryption at rest backfill).

Revision ``20261119_credentials_at_rest`` registers one backfill per target
column in ``agent_usage_backfill``. The sync scheduler then advances them
through :mod:`preloop.models.backfills`, one committed batch at a time, after
the deploy (never inside the migration hook). Each batch walks the table in
primary-key order from the stored cursor, locks the rows, seals plaintext
credentials with :func:`preloop.models.db.encrypted_types.seal` and moves the
cursor. Sealed values are skipped, so a replayed or overlapping batch is a
no-op and a crash resumes from the cursor.

Operators can run the same work by hand::

    python -m preloop.models.credential_encryption --dry-run   # count only
    python -m preloop.models.credential_encryption             # seal all
    python -m preloop.models.credential_encryption --decrypt   # before a
                                                               # rollback

The command scans every row regardless of the scheduler cursors, so it also
seals values a previous release wrote during a rolling upgrade. Output is
counts per column; values are never logged or printed.
"""

from __future__ import annotations

import argparse
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Sequence

from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.db.encrypted_types import (
    count_plaintext_fields,
    is_sealed,
    seal,
    seal_document,
    unseal,
    unseal_document,
)

logger = logging.getLogger("preloop.models.credential_encryption")

#: Rows per batch. The target tables hold configuration (trackers,
#: organizations, webhooks, MCP servers, flows, approval workflows, policy
#: versions): tens to low thousands of rows per account, and one batch
#: rewrites at most this many short values under row locks.
BATCH_SIZE = 200

_NIL = uuid.UUID(int=0)


@dataclass(frozen=True)
class Target:
    """One column whose credentials are sealed in place.

    Attributes:
        name: Registry row name in ``agent_usage_backfill`` (max 32 chars).
        table: Table name.
        column: Column name.
        selector: Secret selector for JSON documents; None for text columns.
        cast: SQL type the bound document is cast to (``json``/``jsonb``).
    """

    name: str
    table: str
    column: str
    selector: Optional[str] = None
    cast: Optional[str] = None


#: Every column sealed by this backfill. Identifiers are constants, never
#: user input. Keep in step with docs/security/credentials-at-rest.md.
TARGETS: tuple[Target, ...] = (
    Target("cred_tracker_api_key", "tracker", "api_key"),
    Target("cred_org_webhook_secret", "organization", "webhook_secret"),
    Target("cred_webhook_secret", "webhook", "secret"),
    Target(
        "cred_mcp_auth_config", "mcp_server", "auth_config", "mcp_auth_config", "jsonb"
    ),
    Target(
        "cred_flow_webhook_config", "flow", "webhook_config", "webhook_secret", "json"
    ),
    Target(
        "cred_approval_config",
        "approval_workflow",
        "approval_config",
        "webhook_secret",
        "json",
    ),
    Target(
        "cred_policy_snapshot",
        "policy_snapshot",
        "snapshot_data",
        "policy_snapshot",
        "jsonb",
    ),
)


@dataclass
class BatchResult:
    """Outcome of one batch over one target."""

    scanned: int = 0
    plaintext: int = 0
    changed: int = 0
    last_id: Optional[uuid.UUID] = None


def _new_value(target: Target, value: Any, decrypt: bool) -> tuple[Any, int]:
    """Return ``(new value, plaintext field count)`` for one stored value."""
    if target.selector is None:
        if not isinstance(value, str) or not value:
            return value, 0
        if decrypt:
            return unseal(value), 0
        return seal(value), 0 if is_sealed(value) else 1
    plaintext = count_plaintext_fields(target.selector, value)
    if decrypt:
        return unseal_document(target.selector, value), plaintext
    return seal_document(target.selector, value), plaintext


def process_batch(
    db: Session,
    target: Target,
    *,
    after: uuid.UUID = _NIL,
    batch_size: int = BATCH_SIZE,
    dry_run: bool = False,
    decrypt: bool = False,
) -> BatchResult:
    """Seal (or decrypt) one batch of rows after ``after`` in id order.

    The caller commits. Values are compared after the transform, so rows that
    are already in the requested state are not written.

    Args:
        db: Database session.
        target: Column to process.
        after: Exclusive lower bound on ``id``.
        batch_size: Maximum rows to read.
        dry_run: Count only; write nothing.
        decrypt: Restore plaintext instead of sealing (rollback aid).

    Returns:
        Counts for the batch and the last id read (None when nothing was left).
    """
    lock = "" if dry_run else " FOR UPDATE"
    rows = db.execute(
        text(
            f'SELECT id, "{target.column}" AS value FROM {target.table} '
            f'WHERE id > :after AND "{target.column}" IS NOT NULL '
            f"ORDER BY id LIMIT :limit{lock}"
        ),
        {"after": after, "limit": batch_size},
    ).all()
    result = BatchResult(scanned=len(rows))
    for row_id, value in rows:
        result.last_id = row_id
        new_value, plaintext = _new_value(target, value, decrypt)
        result.plaintext += plaintext
        if new_value == value:
            continue
        result.changed += 1
        if dry_run:
            continue
        if target.cast is None:
            assignment, bound = f'"{target.column}" = :value', new_value
        else:
            assignment = f'"{target.column}" = CAST(:value AS {target.cast})'
            bound = json.dumps(new_value)
        db.execute(
            text(f"UPDATE {target.table} SET {assignment} WHERE id = :id"),
            {"value": bound, "id": row_id},
        )
    return result


def _registry_step(target: Target) -> Callable[[Session], bool]:
    """Build the scheduler step for one registered target."""

    def step(db: Session) -> bool:
        cursor = db.execute(
            text(
                "SELECT last_usage_id FROM agent_usage_backfill "
                "WHERE name = :name FOR UPDATE"
            ),
            {"name": target.name},
        ).scalar()
        result = process_batch(db, target, after=cursor or _NIL)
        ready = result.last_id is None
        db.execute(
            text(
                "UPDATE agent_usage_backfill SET updated_at = now(), "
                "last_usage_id = COALESCE(:last_id, last_usage_id), "
                "ready = ready OR :ready WHERE name = :name"
            ),
            {"last_id": result.last_id, "ready": ready, "name": target.name},
        )
        return ready

    return step


#: Registry name -> one-batch step, merged into ``backfills.BACKFILLS``.
REGISTERED_STEPS: dict[str, Callable[[Session], bool]] = {
    target.name: _registry_step(target) for target in TARGETS
}


def run_all(
    db: Session,
    *,
    dry_run: bool = False,
    decrypt: bool = False,
    batch_size: int = BATCH_SIZE,
) -> dict[str, BatchResult]:
    """Process every target from the first row, committing each batch.

    Args:
        db: Database session.
        dry_run: Count only; write nothing.
        decrypt: Restore plaintext instead of sealing.
        batch_size: Rows per batch.

    Returns:
        Totals per target name.
    """
    totals: dict[str, BatchResult] = {}
    for target in TARGETS:
        total = BatchResult()
        after = _NIL
        while True:
            batch = process_batch(
                db,
                target,
                after=after,
                batch_size=batch_size,
                dry_run=dry_run,
                decrypt=decrypt,
            )
            if dry_run:
                db.rollback()
            else:
                db.commit()
            if batch.last_id is None:
                break
            after = batch.last_id
            total.scanned += batch.scanned
            total.plaintext += batch.plaintext
            total.changed += batch.changed
        totals[target.name] = total
    return totals


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Seal, count or decrypt stored credentials; print counts only."""
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Count only.")
    mode.add_argument(
        "--decrypt",
        action="store_true",
        help="Restore plaintext, before rolling back to a release without "
        "encryption at rest.",
    )
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    args = parser.parse_args(argv)
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        totals = run_all(
            db,
            dry_run=args.dry_run,
            decrypt=args.decrypt,
            batch_size=args.batch_size,
        )
    finally:
        db.close()
    action = "would change" if args.dry_run else "changed"
    for name, total in totals.items():
        print(
            f"{name}: scanned={total.scanned} plaintext_fields={total.plaintext} "
            f"{action}={total.changed}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
