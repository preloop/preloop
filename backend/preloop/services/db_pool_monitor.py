"""SQLAlchemy connection pool observability.

Makes pool saturation visible before it turns into ``QueuePool limit
reached`` errors (a deployment observed exactly that under concurrent
pending approvals). Two integrations, both intentionally small:

* OpenTelemetry observable gauges on the meter configured by
  ``services/otel_export.py``. When OTLP export is disabled the global
  meter provider is a no-op, so registration is free.
* A periodic WARNING log whenever an engine's checked-out connections
  reach ``DB_POOL_WARN_RATIO`` (default 80%) of its ceiling
  (``pool_size + max_overflow``).

Gated by the ``DB_MONITORING_ENABLED`` env var (default enabled). The
monitor never creates engines: roles that never build one of the two
engines simply report nothing for it.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 30
DEFAULT_WARN_RATIO = 0.8

_FALSE_VALUES = {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable with a safe fallback."""
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid %s=%r; using default %s", name, value, default)
        return default


def db_monitoring_enabled() -> bool:
    """Return whether pool monitoring is enabled (DB_MONITORING_ENABLED)."""
    return os.getenv("DB_MONITORING_ENABLED", "true").strip().lower() not in (
        _FALSE_VALUES
    )


def _snapshot(pool: Any, engine_name: str, max_overflow: int) -> Dict[str, Any]:
    """Read one pool's counters. Never raises; pools expose these methods."""
    size = int(pool.size())
    checked_out = int(pool.checkedout())
    # QueuePool.overflow() can be negative while the base pool is not full.
    overflow_in_use = max(int(pool.overflow()), 0)
    ceiling = size + max_overflow
    return {
        "engine": engine_name,
        "size": size,
        "checked_out": checked_out,
        "checked_in": int(pool.checkedin()),
        "overflow_in_use": overflow_in_use,
        "ceiling": ceiling,
        "status": pool.status(),
    }


def collect_pool_stats() -> List[Dict[str, Any]]:
    """Snapshot both engines' pools; engines not yet created are skipped."""
    from preloop.models.db.session import (
        _env_int,
        get_async_engine_if_initialized,
        get_engine_if_initialized,
    )

    max_overflow = _env_int("DATABASE_MAX_OVERFLOW", 40)
    stats: List[Dict[str, Any]] = []
    sync_engine = get_engine_if_initialized()
    if sync_engine is not None:
        stats.append(_snapshot(sync_engine.pool, "sync", max_overflow))
    async_engine = get_async_engine_if_initialized()
    if async_engine is not None:
        stats.append(_snapshot(async_engine.sync_engine.pool, "async", max_overflow))
    return stats


def register_otel_pool_gauges() -> bool:
    """Register observable pool gauges on the process meter provider.

    Uses the same OpenTelemetry metrics stack that services/otel_export.py
    configures; when OTLP export is off the default no-op meter absorbs
    the registration. Returns True when registration succeeded.
    """
    try:
        from opentelemetry import metrics
        from opentelemetry.metrics import Observation

        meter = metrics.get_meter("preloop.db_pool")

        def _used(_options) -> List[Observation]:
            return [
                Observation(s["checked_out"], {"preloop.db.engine": s["engine"]})
                for s in collect_pool_stats()
            ]

        def _overflow(_options) -> List[Observation]:
            return [
                Observation(s["overflow_in_use"], {"preloop.db.engine": s["engine"]})
                for s in collect_pool_stats()
            ]

        def _ceiling(_options) -> List[Observation]:
            return [
                Observation(s["ceiling"], {"preloop.db.engine": s["engine"]})
                for s in collect_pool_stats()
            ]

        meter.create_observable_gauge(
            "db.client.connections.usage",
            callbacks=[_used],
            unit="{connection}",
            description="Checked-out SQLAlchemy connections per engine",
        )
        meter.create_observable_gauge(
            "db.client.connections.overflow",
            callbacks=[_overflow],
            unit="{connection}",
            description="Overflow SQLAlchemy connections currently in use",
        )
        meter.create_observable_gauge(
            "db.client.connections.max",
            callbacks=[_ceiling],
            unit="{connection}",
            description="Connection ceiling per engine (pool_size + max_overflow)",
        )
        return True
    except Exception:
        logger.debug("Could not register OTLP pool gauges", exc_info=True)
        return False


class DbPoolMonitor:
    """Background task that periodically checks pool saturation."""

    def __init__(
        self,
        interval_seconds: Optional[int] = None,
        warn_ratio: Optional[float] = None,
    ) -> None:
        """Initialize the monitor.

        Args:
            interval_seconds: Seconds between checks (DB_MONITORING_INTERVAL).
            warn_ratio: Fraction of the ceiling that triggers a WARNING
                (DB_POOL_WARN_RATIO).
        """
        if interval_seconds is None:
            from preloop.models.db.session import _env_int

            interval_seconds = _env_int(
                "DB_MONITORING_INTERVAL", DEFAULT_INTERVAL_SECONDS
            )
        self.interval_seconds = interval_seconds
        if warn_ratio is None:
            warn_ratio = _env_float("DB_POOL_WARN_RATIO", DEFAULT_WARN_RATIO)
        self.warn_ratio = warn_ratio
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def check_once(self) -> List[Dict[str, Any]]:
        """Check both pools once; log a WARNING for any near-saturated pool.

        Returns:
            The collected pool snapshots (also used by tests).
        """
        stats = collect_pool_stats()
        for snapshot in stats:
            ceiling = snapshot["ceiling"]
            if ceiling <= 0:
                continue
            if snapshot["checked_out"] >= self.warn_ratio * ceiling:
                logger.warning(
                    "DB connection pool nearing exhaustion: engine=%s "
                    "checked_out=%d/%d (%.0f%%), overflow_in_use=%d, "
                    "checked_in=%d. Requests beyond the ceiling wait up to "
                    "pool_timeout and then fail; look for code holding a "
                    "session across a wait. Pool status: %s",
                    snapshot["engine"],
                    snapshot["checked_out"],
                    ceiling,
                    100.0 * snapshot["checked_out"] / ceiling,
                    snapshot["overflow_in_use"],
                    snapshot["checked_in"],
                    snapshot["status"],
                )
        return stats

    async def start(self) -> None:
        """Start the periodic check task and register OTLP gauges."""
        if self._running:
            return
        self._running = True
        register_otel_pool_gauges()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "DB pool monitor started (interval=%ss, warn_ratio=%s)",
            self.interval_seconds,
            self.warn_ratio,
        )

    async def stop(self) -> None:
        """Stop the periodic check task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the monitor loop task.
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                self.check_once()
            except Exception:
                logger.error("Error in DB pool monitor check", exc_info=True)
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break


_monitor_instance: Optional[DbPoolMonitor] = None


def get_db_pool_monitor() -> DbPoolMonitor:
    """Get or create the global DB pool monitor instance."""
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = DbPoolMonitor()
    return _monitor_instance
