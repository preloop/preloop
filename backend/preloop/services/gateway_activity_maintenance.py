"""Bounded maintenance of stored model gateway call rows.

Passes over stored ``model_gateway_call`` rows, all batched, idempotent and
resumable:

* **Compaction** rewrites rows stored before slim storage into the slim form
  (:func:`preloop.services.gateway_activity_body.slim_gateway_payload`). It
  only visits rows whose stored (compressed) metadata is larger than 2 KB
  and that carry no compaction marker, so a finished backlog costs one index walk and no detoasting.
* **Flow log compaction** (off by default) does the same for the
  ``model_gateway_call`` rows of ``flow_execution_log``, which flow runs
  write instead of session activity rows. Executions under legal hold are
  skipped.
* **Body retention** (off by default) strips message content from rows older
  than the account's plan history window, keeping tokens, cost and timing.
  Rows are never deleted here; deleting whole sessions is the retention
  purge's job (``RETENTION_PURGE_ENABLED``).

Sessions under legal hold are skipped by both. Each batch is its own
transaction, so stopping a pass at any point loses at most one batch of work
and the next pass carries on. The pass runs hourly from the sync scheduler
(task ``maintain_gateway_activity``) and can be run by hand::

    python -m preloop.services.gateway_activity_maintenance --max-batches 100

Progress is logged per batch (rows visited, bytes before and after) and the
summary is printed as JSON.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from preloop.config import settings
from preloop.models.crud.flow_execution_log import crud_flow_execution_log
from preloop.models.crud.runtime_session_activity import crud_runtime_session_activity
from preloop.services.gateway_activity_body import (
    payload_bytes,
    slim_gateway_payload,
    strip_gateway_bodies,
)

logger = logging.getLogger(__name__)

# Stored size, as Postgres reports it after TOAST compression, below which a
# row is not worth visiting: a slim or stripped row of a typical call is
# under this, and a row this small costs little whatever it holds.
_MIN_STORED_BYTES = 2048

# Where this process's compaction walk got to. Rows written by this release
# are slim when inserted, so the backlog only shrinks: once a walk has passed
# a row it never needs to look at it again, and the next hourly pass resumes
# here instead of re-walking the compacted prefix. Kept in memory on purpose
# (no schema change): a restarted worker walks the table once more, which
# finds nothing to rewrite and costs one index scan. The walk restarts from
# the oldest row once a day so rows it passed over are seen again.
_compaction_cursor: dict[str, Any] = {}
# The same walk over flow_execution_log, which flow runs write to instead.
_flow_log_cursor: dict[str, Any] = {}
_CURSOR_MAX_AGE_S = 24 * 3600


def reset_compaction_cursor() -> None:
    """Forget the in-process compaction cursors (tests and operators)."""
    _compaction_cursor.clear()
    _flow_log_cursor.clear()


@dataclass
class MaintenanceResult:
    """Counts from one maintenance pass."""

    compacted_rows: int = 0
    stripped_rows: int = 0
    batches: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    exhausted: bool = False
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Return the counts as a JSON-serializable dict."""
        return asdict(self)


def _rewrite_batch(
    db: Session,
    rows: list[Any],
    transform: Callable[[Any], Any],
    result: MaintenanceResult,
    *,
    dry_run: bool,
) -> int:
    changed = 0
    for row in rows:
        before = row.metadata_
        after = transform(before)
        if after is before:
            continue
        result.bytes_before += payload_bytes(before)
        result.bytes_after += payload_bytes(after)
        changed += 1
        if not dry_run:
            row.metadata_ = after
    if not dry_run:
        db.commit()
    return changed


def compact_existing_rows(
    db: Session,
    *,
    batch_size: int,
    max_batches: int,
    max_bytes: Optional[int] = None,
    dry_run: bool = False,
    result: Optional[MaintenanceResult] = None,
) -> MaintenanceResult:
    """Rewrite legacy gateway call rows into the slim stored form.

    Args:
        db: Database session. Committed once per batch.
        batch_size: Rows per batch.
        max_batches: Batches before returning; the rest waits for next pass.
        max_bytes: Byte cap for the slim form, defaults to the setting.
        dry_run: Count what would change, write nothing.
        result: Accumulator to add to.

    Returns:
        The accumulated counts. ``exhausted`` is True when no candidate rows
        were left.
    """
    return _compact_walk(
        db,
        fetch=lambda after: crud_runtime_session_activity.gateway_call_batch(
            db, min_stored_bytes=_MIN_STORED_BYTES, limit=batch_size, after=after
        ),
        cursor=_compaction_cursor,
        label="Gateway activity",
        max_batches=max_batches,
        max_bytes=max_bytes,
        dry_run=dry_run,
        result=result,
    )


def compact_flow_log_rows(
    db: Session,
    *,
    batch_size: int,
    max_batches: int,
    max_bytes: Optional[int] = None,
    dry_run: bool = False,
    result: Optional[MaintenanceResult] = None,
) -> MaintenanceResult:
    """Rewrite legacy ``model_gateway_call`` rows of ``flow_execution_log``.

    Flow runs log each model call on their execution's log instead of a
    session activity row, with the same payload, so the same slim form
    applies. Rows of an execution under legal hold are skipped.

    Args:
        db: Database session. Committed once per batch.
        batch_size: Rows per batch.
        max_batches: Batches before returning; the rest waits for next pass.
        max_bytes: Byte cap for the slim form, defaults to the setting.
        dry_run: Count what would change, write nothing.
        result: Accumulator to add to.

    Returns:
        The accumulated counts. ``exhausted`` is True when no candidate rows
        were left.
    """
    return _compact_walk(
        db,
        fetch=lambda after: crud_flow_execution_log.gateway_call_batch(
            db, min_stored_bytes=_MIN_STORED_BYTES, limit=batch_size, after=after
        ),
        cursor=_flow_log_cursor,
        label="Flow log",
        max_batches=max_batches,
        max_bytes=max_bytes,
        dry_run=dry_run,
        result=result,
    )


def _compact_walk(
    db: Session,
    *,
    fetch: Callable[[Optional[tuple[datetime, Any]]], list[Any]],
    cursor: dict[str, Any],
    label: str,
    max_batches: int,
    max_bytes: Optional[int],
    dry_run: bool,
    result: Optional[MaintenanceResult],
) -> MaintenanceResult:
    """Walk keyset pages from ``fetch`` and slim every row it returns."""
    cap = (
        settings.model_gateway_activity_max_stored_bytes
        if max_bytes is None
        else max_bytes
    )
    result = result or MaintenanceResult(dry_run=dry_run)
    started_at = cursor.get("started_at")
    if started_at is None or time.monotonic() - started_at > _CURSOR_MAX_AGE_S:
        # Start a fresh walk at least daily, so rows a walk passed over (a
        # session under legal hold since released) are picked up again.
        cursor.clear()
        cursor["started_at"] = time.monotonic()
    after: Optional[tuple[datetime, Any]] = cursor.get("after")
    for _ in range(max(0, max_batches)):
        rows = fetch(after)
        if not rows:
            result.exhausted = True
            break
        after = (rows[-1].timestamp, rows[-1].id)
        changed = _rewrite_batch(
            db,
            rows,
            lambda value: slim_gateway_payload(value, max_bytes=cap),
            result,
            dry_run=dry_run,
        )
        # Only after the batch committed: a batch that raised is retried by
        # the next pass instead of being skipped until a restart.
        if not dry_run:
            cursor["after"] = after
        result.compacted_rows += changed
        result.batches += 1
        logger.info(
            "%s compaction batch: %d rows, %d rewritten, cursor %s",
            label,
            len(rows),
            changed,
            after[0].isoformat(),
        )
    return result


def strip_expired_bodies(
    db: Session,
    *,
    batch_size: int,
    max_batches: int,
    now: Optional[datetime] = None,
    dry_run: bool = False,
    result: Optional[MaintenanceResult] = None,
) -> MaintenanceResult:
    """Strip message content from rows older than each account's window.

    The window is the account's plan history retention
    (:func:`preloop.services.analytics_history.storage_history_days`); an
    account with unlimited history is skipped. ``max_batches`` is shared by
    all accounts in the pass.
    """
    from preloop.services.analytics_history import storage_history_days

    result = result or MaintenanceResult(dry_run=dry_run)
    current = now or datetime.now(UTC)
    budget = max(0, max_batches)
    for account in crud_runtime_session_activity.maintenance_accounts(db):
        if budget <= 0:
            return result
        days = storage_history_days(db, account=account)
        if days is None or days <= 0:
            continue
        cutoff = current - timedelta(days=days)
        after: Optional[tuple[datetime, Any]] = None
        while budget > 0:
            # No size floor here: retention promises that old content is
            # gone, however short it was. The marker bounds repeat work.
            rows = crud_runtime_session_activity.gateway_call_batch(
                db,
                min_stored_bytes=0,
                limit=batch_size,
                after=after,
                account_id=account.id,
                before_timestamp=cutoff,
                skip_marker="bodies_stripped",
            )
            if not rows:
                break
            after = (rows[-1].timestamp, rows[-1].id)
            budget -= 1
            result.batches += 1
            result.stripped_rows += _rewrite_batch(
                db, rows, strip_gateway_bodies, result, dry_run=dry_run
            )
    result.exhausted = budget > 0
    return result


def run_gateway_activity_maintenance(
    db: Session,
    *,
    batch_size: Optional[int] = None,
    max_batches: Optional[int] = None,
    compaction: Optional[bool] = None,
    retention: Optional[bool] = None,
    flow_logs: Optional[bool] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run one bounded maintenance pass with settings as defaults.

    Returns:
        ``{"compaction": {...}, "flow_log_compaction": {...},
        "retention": {...}, "elapsed_s": float}``; a disabled part reports
        ``None``.
    """
    size = batch_size or settings.gateway_activity_maintenance_batch_size
    batches = (
        settings.gateway_activity_maintenance_max_batches
        if max_batches is None
        else max_batches
    )
    do_compact = (
        settings.gateway_activity_compaction_enabled
        if compaction is None
        else compaction
    )
    do_retain = (
        settings.gateway_activity_body_retention_enabled
        if retention is None
        else retention
    )
    do_flow_logs = (
        settings.flow_log_gateway_compaction_enabled if flow_logs is None else flow_logs
    )
    started = time.monotonic()
    summary: dict[str, Any] = {
        "compaction": None,
        "flow_log_compaction": None,
        "retention": None,
    }
    if do_compact:
        summary["compaction"] = compact_existing_rows(
            db, batch_size=size, max_batches=batches, dry_run=dry_run
        ).as_dict()
    if do_flow_logs:
        summary["flow_log_compaction"] = compact_flow_log_rows(
            db, batch_size=size, max_batches=batches, dry_run=dry_run
        ).as_dict()
    if do_retain:
        summary["retention"] = strip_expired_bodies(
            db, batch_size=size, max_batches=batches, dry_run=dry_run
        ).as_dict()
    summary["elapsed_s"] = round(time.monotonic() - started, 3)
    logger.info("Gateway activity maintenance pass: %s", summary)
    return summary


def main(argv: Optional[list[str]] = None) -> int:
    """Command line entry point for operators."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--retention",
        action="store_true",
        help="Also strip bodies older than each account's plan window.",
    )
    parser.add_argument(
        "--no-compaction", action="store_true", help="Skip the compaction pass."
    )
    parser.add_argument(
        "--flow-logs",
        action="store_true",
        help="Also compact model call rows of flow execution logs.",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        summary = run_gateway_activity_maintenance(
            db,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            compaction=not args.no_compaction,
            retention=True if args.retention else None,
            flow_logs=True if args.flow_logs else None,
            dry_run=args.dry_run,
        )
    finally:
        db.close()
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
