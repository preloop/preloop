"""Policy for the stale-claim reaper: who runs a pass, and how often a row
may be republished.

The reaper is the deploy-handoff safety net: it re-publishes executions whose
owning worker died so another worker adopts them. Its defect was that
"nobody has claimed this" and "nobody has a free slot for this" look the
same from the database, and that every replica ran the same pass. On
2026-09-15 three replicas republished sixteen unclaimed executions every
thirty seconds, about ninety publishes a minute into a queue that could not
absorb one of them.

Three bounds live here:

* a lease, so one pass runs per interval whatever the replica count
  (``preloop.models.crud.crud_flow_execution.stale_claim_reaper_lease``);
* a per-execution backoff that doubles from the reclaim interval up to a
  cap, persisted on the row so every replica reads the same schedule;
* a capacity probe, so a pass that cannot possibly be drained is skipped.

Nothing here decides whether an execution is *stale*. That stays with the
claim heartbeat: the backoff only decides how loudly the reaper may repeat
itself about a row that never gets claimed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from preloop.config import settings

logger = logging.getLogger(__name__)

#: Advisory-lock key for "one reaper pass at a time, instance wide".
STALE_CLAIM_REAPER_LOCK_KEY = "flow_execution_stale_claim_reaper"

#: Growth factor of the per-execution backoff: 30s, 60s, 2m, 4m, ...
BACKOFF_FACTOR = 2

#: Never let a misconfigured interval collapse the backoff to nothing.
MIN_BACKOFF_SECONDS = 5

#: Re-dispatches a terminal execution gets to finish its stop. Such a run is
#: only recovered to confirm that its runtime is gone; when that has not
#: happened after this many attempts, more attempts will not do it either, and
#: the reaper settles the row instead of publishing it again.
MAX_TERMINAL_REDISPATCHES = 5


def reaper_interval_seconds() -> int:
    """How often a reaper pass runs, and the first backoff step."""
    raw = getattr(settings, "flow_execution_reclaim_interval_seconds", 30) or 30
    try:
        return max(MIN_BACKOFF_SECONDS, int(raw))
    except (TypeError, ValueError):
        return 30


def backoff_cap_seconds() -> int:
    """Longest gap between two re-dispatches of the same execution."""
    raw = getattr(settings, "flow_execution_redispatch_backoff_max_seconds", 900) or 900
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        return 900
    return max(reaper_interval_seconds(), cap)


def redispatch_backoff_seconds(
    attempts: int,
    *,
    base_seconds: Optional[int] = None,
    max_seconds: Optional[int] = None,
) -> int:
    """Seconds to wait before re-dispatching an execution again.

    ``attempts`` is how many times the reaper has already published this
    execution without it ever being claimed. Zero means "never published,
    publish now". From there the gap doubles: 30s, 60s, 2m, 4m, ... until it
    reaches the cap. Over an hour that is a handful of publishes rather than
    the 120 an unconditional 30 second pass produced.

    Args:
        attempts: Re-dispatches already made for this execution.
        base_seconds: First gap; defaults to the reclaim interval.
        max_seconds: Cap on the gap; defaults to the configured cap.

    Returns:
        Delay in seconds; 0 when the execution may be published immediately.
    """
    if attempts <= 0:
        return 0
    base = base_seconds if base_seconds is not None else reaper_interval_seconds()
    base = max(MIN_BACKOFF_SECONDS, int(base))
    cap = max_seconds if max_seconds is not None else backoff_cap_seconds()
    cap = max(base, int(cap))
    # Exponent is bounded before the shift so a long-lived row cannot make
    # this compute a number with thousands of digits.
    steps = min(attempts - 1, 32)
    return int(min(cap, base * (BACKOFF_FACTOR**steps)))


def is_redispatch_due(
    *,
    attempts: int,
    last_redispatch_at: Optional[datetime],
    now: Optional[datetime] = None,
    base_seconds: Optional[int] = None,
    max_seconds: Optional[int] = None,
) -> bool:
    """Whether the backoff for this execution has elapsed.

    A row with a counter but no timestamp (an upgrade, a hand-edited row) is
    treated as due: the safety net errs towards recovering work.
    """
    if attempts <= 0 or last_redispatch_at is None:
        return True
    moment = now or datetime.now(timezone.utc)
    if last_redispatch_at.tzinfo is None:
        last_redispatch_at = last_redispatch_at.replace(tzinfo=timezone.utc)
    delay = redispatch_backoff_seconds(
        attempts, base_seconds=base_seconds, max_seconds=max_seconds
    )
    return moment - last_redispatch_at >= timedelta(seconds=delay)


@dataclass
class ReaperPassSummary:
    """Counts for one pass, logged as a single line.

    A storm has to show up as a number. Before this, every candidate logged
    its own line on every pass, which is how an incident looked like normal
    traffic repeated ninety times a minute.
    """

    candidates: int = 0
    redispatched: int = 0
    skipped_backoff: int = 0
    skipped_account_cap: int = 0
    skipped_no_capacity: int = 0
    failed: int = 0
    settled: int = 0

    def as_log_line(self) -> str:
        """One human-readable line with every count in it."""
        return (
            "Stale-claim reaper pass: candidates=%s re-dispatched=%s "
            "skipped_backoff=%s skipped_account_cap=%s "
            "skipped_no_capacity=%s failed=%s settled=%s"
            % (
                self.candidates,
                self.redispatched,
                self.skipped_backoff,
                self.skipped_account_cap,
                self.skipped_no_capacity,
                self.failed,
                self.settled,
            )
        )
