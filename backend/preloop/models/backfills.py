"""Run data backfills after a deploy, never inside the migration hook.

Alembic revisions run in the Helm ``pre-upgrade`` hook, which Helm waits on for
five minutes. A revision that rewrites history row by row can outlast that
budget on a large dataset, fail the release and keep running in the
background. So revisions only change schema (tables, triggers, indexes) and
*register* a backfill: a row in ``agent_usage_backfill`` with a name, a cursor
(``last_usage_id``) and a completion flag (``ready``).

This module owns the batch statement for every registered backfill and runs the
pending ones in bounded, resumable batches. Each batch commits on its own, so a
crash or a pod restart resumes from the cursor. Readers that depend on a
backfill keep their previous query path until ``ready`` is true.

Hosts: the sync scheduler publishes ``run_data_backfills`` shortly after start
and then on an interval; operators can also run
``python -m preloop.models.backfills``.
"""

from __future__ import annotations

import argparse
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence, Union

from sqlalchemy import text
from sqlalchemy.orm import Session

from preloop.models.credential_encryption import REGISTERED_STEPS

logger = logging.getLogger("preloop.models.backfills")

#: Registry table created by revision ``20261109_agent_usage_totals``.
REGISTRY_TABLE = "agent_usage_backfill"

# Stored agent usage totals (revision 20261109_agent_usage_totals). Counts up
# to 500 historical gateway requests per call, records the cursor, and returns
# ``ready`` (true once a call finds nothing left). The ``agent_usage_counted``
# ledger makes a replayed batch a no-op; source row locks serialize the batch
# with concurrent updates and deletes, which the triggers count. Cursor and
# readiness updates are monotonic: an overlapping pass with a stale snapshot
# must not rewind progress or undo completion when it finally acquires locks.
AGENT_USAGE_TOTALS_BATCH = """
WITH batch AS MATERIALIZED (
 SELECT u AS usage, u.id FROM api_usage u
 WHERE u.action_type='model_gateway' AND u.account_id IS NOT NULL
 AND u.id > COALESCE((SELECT last_usage_id FROM agent_usage_backfill WHERE name='agents'), '00000000-0000-0000-0000-000000000000'::uuid)
 ORDER BY u.id LIMIT 500 FOR UPDATE OF u
), claimed AS (
 INSERT INTO agent_usage_counted(usage_id) SELECT id FROM batch
 ON CONFLICT DO NOTHING RETURNING usage_id
), applied AS MATERIALIZED (
 SELECT agent_usage_apply(batch.usage,1) FROM batch JOIN claimed ON claimed.usage_id=batch.id
)
UPDATE agent_usage_backfill SET
 last_usage_id=GREATEST(last_usage_id,(SELECT id FROM batch ORDER BY id DESC LIMIT 1)),
 ready=ready OR NOT EXISTS(SELECT 1 FROM batch)
WHERE name='agents' AND (SELECT count(*) FROM applied)>=0
RETURNING ready
"""

#: One batch of a backfill: a SQL statement, or a callable for batches that
#: need Python (such as encryption). Either advances the registry row's cursor
#: and returns the new ``ready`` value; the runner commits.
BackfillStep = Union[str, Callable[[Session], bool]]

#: Registered backfills: registry row name -> one-batch step. The credential
#: steps come from revision ``20261119_credentials_at_rest``.
BACKFILLS: dict[str, BackfillStep] = {
    "agents": AGENT_USAGE_TOTALS_BATCH,
    **REGISTERED_STEPS,
}


@dataclass
class BackfillResult:
    """Outcome of one runner pass for one backfill."""

    name: str
    batches: int = 0
    ready: bool = False
    errors: list[str] = field(default_factory=list)


def pending_backfills(db: Session) -> list[str]:
    """Return the registered backfills whose registry row is not ready.

    Args:
        db: Database session.

    Returns:
        Names in registry order. Empty when the registry table does not exist
        yet (the schema has not reached the registering revision).
    """
    if db.execute(text("SELECT to_regclass(:t)"), {"t": REGISTRY_TABLE}).scalar():
        rows = db.execute(
            text(f"SELECT name FROM {REGISTRY_TABLE} WHERE NOT ready")
        ).scalars()
        waiting = set(rows)
        db.commit()
        return [name for name in BACKFILLS if name in waiting]
    db.commit()
    return []


def run_pending_backfills(
    db: Session,
    *,
    time_budget_seconds: float = 60.0,
    max_batches: Optional[int] = None,
    clock: Callable[[], float] = time.monotonic,
) -> list[BackfillResult]:
    """Advance every pending backfill, one committed batch at a time.

    Stops when each backfill reports ``ready`` or when the time or batch budget
    runs out; the next pass resumes from the stored cursor.

    Args:
        db: Database session. Each batch is committed on it.
        time_budget_seconds: Wall-clock budget for the whole pass.
        max_batches: Optional cap on batches per backfill.
        clock: Monotonic clock, injectable for tests.

    Returns:
        One result per backfill that was pending at the start of the pass.
    """
    deadline = clock() + time_budget_seconds
    results: list[BackfillResult] = []
    for name in pending_backfills(db):
        result = BackfillResult(name=name)
        results.append(result)
        while not result.ready:
            if max_batches is not None and result.batches >= max_batches:
                break
            if clock() >= deadline:
                break
            try:
                step = BACKFILLS[name]
                if callable(step):
                    ready = step(db)
                else:
                    ready = db.execute(text(step)).scalar_one()
                db.commit()
            except Exception as exc:  # one failing backfill must not block others
                db.rollback()
                logger.exception("Backfill %s batch failed", name)
                result.errors.append(str(exc))
                break
            result.batches += 1
            result.ready = bool(ready)
        logger.info(
            "Backfill %s: %d batch(es) this pass, ready=%s",
            name,
            result.batches,
            result.ready,
        )
    return results


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run pending backfills until done or the budget runs out."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--time-budget",
        type=float,
        default=3600.0,
        help="Seconds to run before stopping (default: 3600).",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    from preloop.models.db.session import get_db_session

    db = next(get_db_session())
    try:
        results = run_pending_backfills(db, time_budget_seconds=args.time_budget)
    finally:
        db.close()
    return 1 if any(r.errors for r in results) else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
