"""Periodic end of runner-hosted sessions whose runner went away (#1482).

A runner that stays offline past a session's idle timeout ends it with
``runner_offline``; a runner that reconnects sooner resumes it. Each pass is
idempotent (only live sessions are touched and ending one is a guarded state
change), so several API replicas running it at once is safe.
"""

import asyncio
import logging
from typing import Optional

from preloop.models.db.session import get_db_session
from preloop.services.runner_sessions import sweep_runner_sessions
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)

logger = logging.getLogger(__name__)

#: Seconds between passes. The idle timeout is minutes, so a minute of
#: slack is invisible to the operator.
RUNNER_SESSION_SWEEP_INTERVAL_SECONDS = 60


def run_runner_session_sweep_once() -> int:
    """One synchronous pass with its own session.

    Returns:
        How many sessions ended.
    """
    db = next(get_db_session())
    try:
        return sweep_runner_sessions(db)
    finally:
        db.close()


class RunnerSessionSweeper:
    """Periodic asyncio task, modeled on the issue cost rebuild sweeper."""

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        """Create the sweeper.

        Args:
            check_interval_seconds: Seconds between passes.
        """
        self.check_interval = int(
            check_interval_seconds or RUNNER_SESSION_SWEEP_INTERVAL_SECONDS
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
            return
        if not background_passes_allowed():
            logger.info(
                "Runner session sweeper not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._sweep_loop())

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
                ended = await asyncio.to_thread(run_runner_session_sweep_once)
                if ended:
                    logger.info("Runner session sweep ended %s session(s)", ended)
            except Exception:
                logger.error("Error in runner session sweep", exc_info=True)


_sweeper_instance: Optional[RunnerSessionSweeper] = None


def get_runner_session_sweeper() -> RunnerSessionSweeper:
    """Get or create the global runner session sweeper."""
    global _sweeper_instance
    if _sweeper_instance is None:
        _sweeper_instance = RunnerSessionSweeper()
    return _sweeper_instance
