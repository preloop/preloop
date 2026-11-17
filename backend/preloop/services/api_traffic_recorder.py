"""Aggregate REST traffic per minute and keep every problem request.

``ApiUsageMiddleware`` hands every ``/api/v1`` request to this recorder,
whatever credential it carried and whatever its status. The request path
only touches memory: a counter keyed by (minute, account, principal kind,
method, route template, status class) and, for a 4xx/5xx or an empty list
after a create, one event record. A background thread flushes both every
``API_TRAFFIC_FLUSH_SECONDS`` through ``crud_api_traffic`` in one short
session.

Volume: a client polling one route 27 times a minute costs one rollup row
per minute (1,440 a day) instead of 38,880 request rows. Problem events are
individual rows, kept for ``API_TRAFFIC_EVENT_RETENTION_DAYS`` (at least 7).

Nothing here may slow or fail a request. Memory is bounded: past
``MAX_ROLLUP_KEYS`` distinct series or ``MAX_PENDING_EVENTS`` events in one
flush interval, the excess is counted as dropped (the rollup still counts an
overflowing event's request when its series already exists). A failed flush
puts the data back, within the same bounds, for the next attempt; after
``MAX_CONSECUTIVE_FAILURES`` failures in a row the pending data is dropped
and counted, so one row the database rejects cannot block later traffic.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

logger = logging.getLogger(__name__)

DEFAULT_FLUSH_SECONDS = 10.0
MAX_ROLLUP_KEYS = 20_000
MAX_PENDING_EVENTS = 20_000
LOG_THROTTLE_SECONDS = 60.0
#: Consecutive failed flushes after which pending data is dropped, so a
#: row the database always rejects cannot block every later flush.
MAX_CONSECUTIVE_FAILURES = 3

UNMATCHED_ROUTE = "<unmatched>"


@dataclass(frozen=True)
class TrafficPrincipal:
    """Who made a request, as far as authentication established it.

    Attributes:
        kind: ``session``, ``api_key``, ``runtime_key``, ``ci_identity`` or
            ``anonymous``.
        principal_id: User id for sessions, key id for API and runtime keys,
            CI principal id for CI identities.
        account_id: The principal's account.
    """

    kind: str
    principal_id: Optional[UUID] = None
    account_id: Optional[UUID] = None


ANONYMOUS = TrafficPrincipal(kind="anonymous")


@dataclass(frozen=True)
class TrafficSample:
    """One finished request, reduced to the fields we keep."""

    started_at: datetime
    principal: TrafficPrincipal
    method: str
    route_template: str
    status_code: int
    duration_ms: int
    reason_code: Optional[str] = None
    empty_after_create: bool = False


RollupKey = tuple[datetime, Optional[UUID], str, str, str, int]


@dataclass
class RollupCounts:
    """Counts accumulated for one rollup key."""

    request_count: int = 0
    total_duration_ms: int = 0
    max_duration_ms: int = 0

    def add(self, duration_ms: int, count: int = 1) -> None:
        """Count ``count`` requests that took ``duration_ms`` in total."""
        self.request_count += count
        self.total_duration_ms += duration_ms
        self.max_duration_ms = max(self.max_duration_ms, duration_ms)

    def merge(self, other: "RollupCounts") -> None:
        """Fold another key's counts into these."""
        self.request_count += other.request_count
        self.total_duration_ms += other.total_duration_ms
        self.max_duration_ms = max(self.max_duration_ms, other.max_duration_ms)


@dataclass
class _Pending:
    """Data waiting for the next flush."""

    rollups: dict[RollupKey, RollupCounts] = field(default_factory=dict)
    events: list[TrafficSample] = field(default_factory=list)


def minute_bucket(moment: datetime) -> datetime:
    """Return the start of the UTC minute containing ``moment``."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _env_float(name: str, default: float) -> float:
    """Read a positive float environment variable with a safe fallback."""
    try:
        value = float(os.getenv(name, default))
    except ValueError:
        return default
    return value if value > 0 else default


class ApiTrafficRecorder:
    """In-memory aggregation with periodic flushes on one daemon thread."""

    def __init__(
        self,
        *,
        writer: Optional[Callable[[dict[RollupKey, RollupCounts], list], None]] = None,
        flush_seconds: Optional[float] = None,
        max_rollup_keys: int = MAX_ROLLUP_KEYS,
        max_pending_events: int = MAX_PENDING_EVENTS,
    ) -> None:
        """Configure the recorder.

        Args:
            writer: Persists one flush. Defaults to ``write_traffic_flush``.
            flush_seconds: Interval between flushes
                (``API_TRAFFIC_FLUSH_SECONDS``, default 10).
            max_rollup_keys: Distinct series held between flushes.
            max_pending_events: Problem events held between flushes.
        """
        self._writer = writer or write_traffic_flush
        self._flush_seconds = flush_seconds or _env_float(
            "API_TRAFFIC_FLUSH_SECONDS", DEFAULT_FLUSH_SECONDS
        )
        self._max_keys = max_rollup_keys
        self._max_events = max_pending_events
        self._lock = threading.Lock()
        self._pending = _Pending()
        self._dropped = 0
        self._failed_flushes = 0
        self._consecutive_failures = 0
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_log = 0.0

    # -- request path -------------------------------------------------------

    def record(self, sample: TrafficSample) -> None:
        """Count one finished request; never raises, never blocks on I/O."""
        try:
            self._record(sample)
        except Exception:  # pragma: no cover - telemetry must not fail requests
            logger.debug("Traffic sample could not be recorded", exc_info=True)

    def _record(self, sample: TrafficSample) -> None:
        """Add the sample to the pending rollups and events."""
        key: RollupKey = (
            minute_bucket(sample.started_at),
            sample.principal.account_id,
            sample.principal.kind,
            sample.method[:10],
            sample.route_template[:255],
            sample.status_code // 100,
        )
        is_event = sample.status_code >= 400 or sample.empty_after_create
        with self._lock:
            counts = self._pending.rollups.get(key)
            if counts is None:
                if len(self._pending.rollups) >= self._max_keys:
                    self._dropped += 1
                else:
                    counts = self._pending.rollups[key] = RollupCounts()
            if counts is not None:
                counts.add(sample.duration_ms)
            if is_event:
                if len(self._pending.events) >= self._max_events:
                    self._dropped += 1
                else:
                    self._pending.events.append(sample)
        self._ensure_started()

    # -- flushing -----------------------------------------------------------

    def flush(self) -> bool:
        """Write everything pending now.

        Returns:
            True when the write succeeded (or there was nothing to write).
        """
        with self._lock:
            pending, self._pending = self._pending, _Pending()
        if not pending.rollups and not pending.events:
            return True
        try:
            self._writer(pending.rollups, pending.events)
            self._consecutive_failures = 0
            return True
        except Exception:
            self._failed_flushes += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                self._consecutive_failures = 0
                with self._lock:
                    self._dropped += len(pending.events) + sum(
                        c.request_count for c in pending.rollups.values()
                    )
            else:
                self._restore(pending)
            now = time.monotonic()
            if now - self._last_log >= LOG_THROTTLE_SECONDS:
                self._last_log = now
                logger.error(
                    "API traffic flush failed (%d failures so far); the data "
                    "is kept for the next attempt within its memory bound.",
                    self._failed_flushes,
                )
            return False

    def _restore(self, pending: _Pending) -> None:
        """Put a failed flush back in front of newer data, within bounds."""
        with self._lock:
            current = self._pending
            for key, counts in pending.rollups.items():
                existing = current.rollups.get(key)
                if existing is not None:
                    existing.merge(counts)
                elif len(current.rollups) < self._max_keys:
                    current.rollups[key] = counts
                else:
                    self._dropped += counts.request_count
            room = max(0, self._max_events - len(current.events))
            kept = pending.events[:room]
            self._dropped += len(pending.events) - len(kept)
            current.events[:0] = kept

    def stats(self) -> dict[str, int]:
        """Counters for tests and health reporting."""
        with self._lock:
            return {
                "pending_series": len(self._pending.rollups),
                "pending_events": len(self._pending.events),
                "dropped": self._dropped,
                "failed_flushes": self._failed_flushes,
            }

    def _ensure_started(self) -> None:
        """Start the flush thread on first use."""
        if self._thread is not None or self._stop.is_set():
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="api_traffic_flush", daemon=True
            )
            self._thread.start()

    def _run(self) -> None:
        """Flush on an interval until stopped."""
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def shutdown(self, timeout: float = 5.0) -> None:
        """Stop the flush thread and write what is pending once."""
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self.flush()


def write_traffic_flush(
    rollups: dict[RollupKey, RollupCounts], events: list[TrafficSample]
) -> None:
    """Persist one flush in a single short-lived session.

    Args:
        rollups: Counts per rollup key.
        events: Problem requests to keep individually.
    """
    from preloop.models.crud import crud_api_traffic
    from preloop.models.db.session import get_session_factory

    session = get_session_factory()()
    try:
        crud_api_traffic.add_rollups(
            session,
            rows=[
                {
                    "bucket_start": key[0],
                    "account_id": key[1],
                    "principal_kind": key[2],
                    "method": key[3],
                    "route_template": key[4],
                    "status_class": key[5],
                    "request_count": counts.request_count,
                    "total_duration_ms": counts.total_duration_ms,
                    "max_duration_ms": counts.max_duration_ms,
                }
                for key, counts in rollups.items()
            ],
            commit=False,
        )
        crud_api_traffic.add_events(
            session, events=[event_row(sample) for sample in events], commit=False
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def event_row(sample: TrafficSample) -> dict[str, Any]:
    """Return the ``api_traffic_event`` column values for one sample."""
    from preloop.models.models.api_traffic import (
        EVENT_KIND_EMPTY_AFTER_CREATE,
        EVENT_KIND_ERROR,
    )

    return {
        "occurred_at": sample.started_at,
        "kind": EVENT_KIND_ERROR
        if sample.status_code >= 400
        else EVENT_KIND_EMPTY_AFTER_CREATE,
        "account_id": sample.principal.account_id,
        "principal_kind": sample.principal.kind,
        "principal_id": sample.principal.principal_id,
        "method": sample.method[:10],
        "route_template": sample.route_template[:255],
        "status_code": sample.status_code,
        "duration_ms": sample.duration_ms,
        "reason_code": (sample.reason_code or None) and sample.reason_code[:64],
    }


_recorder: Optional[ApiTrafficRecorder] = None
_recorder_lock = threading.Lock()


def get_api_traffic_recorder() -> ApiTrafficRecorder:
    """Return the process-wide recorder, creating it on first use."""
    global _recorder
    if _recorder is None:
        with _recorder_lock:
            if _recorder is None:
                _recorder = ApiTrafficRecorder()
    return _recorder


def shutdown_api_traffic_recorder(timeout: float = 5.0) -> None:
    """Flush and stop the process-wide recorder during shutdown."""
    global _recorder
    with _recorder_lock:
        recorder, _recorder = _recorder, None
    if recorder is not None:
        recorder.shutdown(timeout=timeout)
