"""Bounded, payload-free attribution of connections currently checked out."""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from greenlet import getcurrent
from sqlalchemy import event
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)
MAX_TRACKED_HOLDS = 128
MAX_REPORTED_HOLDS = 5
MAX_WALKED_FRAMES = 64
LONG_HOLD_SECONDS = 5.0
RECENT_HOLD_TTL_SECONDS = 300.0
# Frame walks run only once utilization is material, including saturation.
CALLSITE_UTILIZATION_RATIO = 0.5
_PACKAGE_ROOT = str(Path(__file__).parents[2]) + os.sep
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_./<>-]")
_ATTRIBUTE = "_preloop_pool_hold_diagnostics"


@dataclass(frozen=True)
class ConnectionHold:
    """Only timing and code identity; never retain frames or connection objects."""

    started_at: float
    acquired_at: tuple[str, ...]


def _capture_callsite(frame_limit: int) -> tuple[str, ...]:
    """Walk frame metadata without inspecting locals, source lines or SQL."""
    frames: list[str] = []
    frame = sys._getframe(1)
    parent = getcurrent().parent
    try:
        for _ in range(MAX_WALKED_FRAMES):
            if frame is None:
                if parent is None:
                    break
                frame = parent.gr_frame
                parent = parent.parent
                if frame is None:
                    continue
            filename = frame.f_code.co_filename
            if filename.startswith(_PACKAGE_ROOT) and filename != __file__:
                relative = filename[len(_PACKAGE_ROOT) :]
                frames.append(
                    f"{_SAFE_NAME.sub('_', relative)[:160]}:"
                    f"{_SAFE_NAME.sub('_', frame.f_code.co_name)[:80]}:"
                    f"{frame.f_lineno}"
                )
                if len(frames) >= frame_limit:
                    break
            frame = frame.f_back
    finally:
        del frame
        del parent
    return tuple(frames)


def _should_capture_callsite(pool: Any) -> bool:
    """Walk frames only when checkout volume is already near saturation.

    QueuePool counters only. Unbounded overflow (``max_overflow < 0``) uses
    ``size`` as the capacity baseline. Keep capturing through saturation.
    """
    overflow = pool._max_overflow
    size = pool.size()
    capacity = size + overflow if overflow >= 0 else size
    if capacity <= 0:
        return False
    return pool.checkedout() >= CALLSITE_UTILIZATION_RATIO * capacity


class PoolHoldDiagnostics:
    """Track at most a fixed number of live checkouts for one request engine."""

    def __init__(self, engine: Engine, frame_limit: int) -> None:
        self._pool: Any = None
        self._generation = 0
        self._frame_limit = frame_limit
        self._holds: dict[int, ConnectionHold] = {}
        self._lock = threading.Lock()
        self._recent: deque[tuple[float, float, tuple[str, ...]]] = deque(
            maxlen=MAX_REPORTED_HOLDS
        )
        self._last_saturation: float | None = None
        self._last_evidence: float | None = None
        self._listeners = (
            ("checkout", self._checkout),
            ("checkin", self._release),
            ("invalidate", self._release),
            ("close", self._release),
            ("detach", self._release),
        )
        try:
            self._bind_pool(engine.pool)
            event.listen(engine, "engine_disposed", self._disposed)
        except Exception:
            self._unbind_pool()
            raise

    def _unbind_pool(self) -> None:
        if self._pool is not None:
            for name, callback in self._listeners:
                if event.contains(self._pool, name, callback):
                    event.remove(self._pool, name, callback)

    def _bind_pool(self, pool: Any) -> None:
        self._unbind_pool()
        with self._lock:
            self._generation += 1
            self._pool = pool
            self._holds.clear()
            self._recent.clear()
            self._last_saturation = None
            self._last_evidence = None
        # Pool.recreate copies listeners. Remove copies before re-registering.
        for name, callback in self._listeners:
            if event.contains(pool, name, callback):
                event.remove(pool, name, callback)
            event.listen(pool, name, callback)

    def _disposed(self, engine: Engine) -> None:
        try:
            self._bind_pool(engine.pool)
        except Exception:
            # Optional observation must never prevent engine disposal/checkout.
            with self._lock:
                self._holds.clear()
                self._recent.clear()
                self._last_saturation = None
                self._last_evidence = None

    def _checkout(self, _connection: Any, record: Any, _proxy: Any) -> None:
        try:
            with self._lock:
                generation = self._generation
                pool = self._pool
            now = monotonic()
            # These are QueuePool counters only, never a query or connection.
            overflow = pool._max_overflow
            saturated = overflow >= 0 and pool.checkedout() >= (pool.size() + overflow)
            capture_callsite = saturated or _should_capture_callsite(pool)
            with self._lock:
                if generation != self._generation:
                    return
                if saturated:
                    self._last_saturation = now
                    self._last_evidence = now
                if len(self._holds) >= MAX_TRACKED_HOLDS:
                    return
            # Healthy fast path skips the frame walk; timing is still tracked.
            acquired_at = (
                _capture_callsite(self._frame_limit) if capture_callsite else ()
            )
            hold = ConnectionHold(now, acquired_at)
            with self._lock:
                if (
                    generation == self._generation
                    and len(self._holds) < MAX_TRACKED_HOLDS
                ):
                    self._holds[id(record)] = hold
        except Exception:
            # Never stringify the exception: it might contain driver payloads.
            return

    def _release(self, _connection: Any, record: Any, *_args: Any) -> None:
        try:
            now = monotonic()
            with self._lock:
                hold = self._holds.pop(id(record), None)
                if (
                    hold is not None
                    and now - hold.started_at >= LONG_HOLD_SECONDS
                    and self._last_saturation is not None
                    and hold.started_at <= self._last_saturation
                ):
                    self._recent.append((now, now - hold.started_at, hold.acquired_at))
                    self._last_evidence = now
        except Exception:
            return

    def snapshot(self) -> dict[str, Any]:
        """Return only bounded ages and acquisition signatures for warning logs."""
        now = monotonic()
        with self._lock:
            holds = sorted(self._holds.values(), key=lambda hold: hold.started_at)
            while self._recent and now - self._recent[0][0] > RECENT_HOLD_TTL_SECONDS:
                self._recent.popleft()
            recent = list(self._recent)
            evidence_age = (
                max(0.0, now - self._last_evidence)
                if self._last_evidence is not None
                else None
            )
        return {
            "tracked": len(holds),
            "capacity": MAX_TRACKED_HOLDS,
            "recent_saturation_seconds_ago": (
                round(evidence_age, 3)
                if evidence_age is not None and evidence_age <= RECENT_HOLD_TTL_SECONDS
                else None
            ),
            "recent_released": [
                {
                    "held_seconds": round(duration, 3),
                    "released_seconds_ago": round(max(0.0, now - released), 3),
                    "acquired_at": callsite,
                }
                for released, duration, callsite in sorted(
                    recent, key=lambda item: item[1], reverse=True
                )
            ],
            "oldest": [
                {
                    "held_seconds": round(max(0.0, now - hold.started_at), 3),
                    "acquired_at": hold.acquired_at,
                }
                for hold in holds[:MAX_REPORTED_HOLDS]
            ],
            # The oldest holds can all predate callsite sampling. Report the
            # oldest sampled holds separately so their evidence stays visible.
            "oldest_attributed": [
                {
                    "held_seconds": round(max(0.0, now - hold.started_at), 3),
                    "acquired_at": hold.acquired_at,
                }
                for hold in [hold for hold in holds if hold.acquired_at][
                    :MAX_REPORTED_HOLDS
                ]
            ],
        }


def install_pool_hold_diagnostics(engine: Engine) -> None:
    """Install once on an application engine; health engines must not call this."""
    if os.getenv("DB_POOL_HOLD_DIAGNOSTICS", "true").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return
    if isinstance(getattr(engine, _ATTRIBUTE, None), PoolHoldDiagnostics):
        return
    try:
        full_stack = os.getenv("DB_POOL_HOLD_STACKS", "false").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        setattr(engine, _ATTRIBUTE, PoolHoldDiagnostics(engine, 8 if full_stack else 3))
    except Exception:
        logger.warning("DB pool hold diagnostics unavailable for a request engine")


def collect_pool_holds(engine: Engine) -> dict[str, Any] | None:
    """Read already-installed tracking without creating engines or connections."""
    tracker = getattr(engine, _ATTRIBUTE, None)
    return tracker.snapshot() if isinstance(tracker, PoolHoldDiagnostics) else None
