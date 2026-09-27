"""Scheduled rebuild of the per-issue cost rollup.

The terminal hooks that record issue cost facts are best effort. When one
fails, or an execution ends on a path without a hook, its cost would stay
out of the report until someone calls ``POST /cost/by-issue/rebuild``. This
sweeper runs the same rebuild on a timer for a recent window
(``issue_cost_rollup.scheduled_rebuild``).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from preloop.config import settings
from preloop.models.db.session import get_db_session
from preloop.services import issue_cost_rollup
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)

logger = logging.getLogger(__name__)


def run_scheduled_rebuild_once() -> issue_cost_rollup.ScheduledRebuildSummary:
    """One synchronous pass with its own session.

    Returns:
        The pass summary.
    """
    db = next(get_db_session())
    try:
        return issue_cost_rollup.scheduled_rebuild(
            db,
            lookback=timedelta(hours=int(settings.issue_cost_rebuild_lookback_hours)),
            per_account_limit=int(
                settings.issue_cost_rebuild_max_executions_per_account
            ),
        )
    finally:
        db.close()


class IssueCostRebuildSweeper:
    """Periodic asyncio task, modeled on the retention purge sweeper.

    Started from the app lifespan on the API role only. The pass is
    synchronous CRUD, so it runs in a thread and the event loop stays
    responsive. It sleeps one interval before its first pass, so a restart
    loop does not turn into a rebuild loop.
    """

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        """Create the sweeper.

        Args:
            check_interval_seconds: Seconds between passes; defaults to
                ``ISSUE_COST_REBUILD_INTERVAL_SECONDS``.
        """
        self.check_interval = int(
            check_interval_seconds
            if check_interval_seconds is not None
            else settings.issue_cost_rebuild_interval_seconds
        )
        self._running = False
        self._task: Optional[asyncio.Task[None]] = None

    @property
    def running(self) -> bool:
        """Whether the background task is running."""
        return self._running

    async def start(self) -> None:
        """Start the background task, unless this role runs no passes."""
        if self._running:
            logger.warning("Issue cost rebuild sweeper is already running")
            return
        if not background_passes_allowed():
            logger.info(
                "Issue cost rebuild sweeper not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())
        logger.info(
            "Issue cost rebuild sweeper started (check_interval=%ss, lookback=%sh)",
            self.check_interval,
            settings.issue_cost_rebuild_lookback_hours,
        )

    async def stop(self) -> None:
        """Stop the background task."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                # Expected when stop() cancels the sweep loop task.
                pass

    async def _sweep_loop(self) -> None:
        """Wait one interval, then run a pass; repeat until stopped."""
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(run_scheduled_rebuild_once)
            except Exception:
                logger.error("Error in issue cost rebuild pass", exc_info=True)


_sweeper_instance: Optional[IssueCostRebuildSweeper] = None


def get_issue_cost_rebuild_sweeper() -> IssueCostRebuildSweeper:
    """Get or create the global issue cost rebuild sweeper."""
    global _sweeper_instance
    if _sweeper_instance is None:
        _sweeper_instance = IssueCostRebuildSweeper()
    return _sweeper_instance
