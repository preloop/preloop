"""Purge discovery candidates that no workstation has reported for 90 days.

Discovery data is inventory, not evidence: a candidate nobody has seen for
the retention window describes a tool that was removed or a workstation
that stopped reporting, and keeping it would only grow the salted-hash
record of machines. Unlike the account retention purge this one is always
on, because the window is part of the feature's privacy promise.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from preloop.models.crud import crud_discovered_agent_candidate
from preloop.models.db.session import get_db_session
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)

logger = logging.getLogger(__name__)

#: Once a day is plenty for a 90 day window.
DEFAULT_INTERVAL_SECONDS = 24 * 60 * 60


def run_discovery_candidate_purge_once() -> int:
    """One synchronous pass with its own session.

    Returns:
        Rows deleted.
    """
    db = next(get_db_session())
    try:
        deleted = crud_discovered_agent_candidate.purge_stale(db)
        if deleted:
            logger.info("Purged %s stale discovery candidate(s)", deleted)
        return deleted
    finally:
        db.close()


class DiscoveryCandidatePurgeSweeper:
    """Periodic asyncio task, modeled on the issue cost rebuild sweeper."""

    def __init__(self, check_interval_seconds: Optional[int] = None) -> None:
        self.check_interval = int(check_interval_seconds or DEFAULT_INTERVAL_SECONDS)
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the background task, unless this role must not sweep."""
        if self._running:
            return
        if not background_passes_allowed():
            logger.info(
                "Discovery candidate purge not started for %s role.",
                current_service_role(),
            )
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())

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
                # Expected: stop() cancels the loop.
                pass

    async def _loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            try:
                await asyncio.to_thread(run_discovery_candidate_purge_once)
            except Exception:
                logger.error("Error in discovery candidate purge pass", exc_info=True)


_sweeper: Optional[DiscoveryCandidatePurgeSweeper] = None


def get_discovery_candidate_purge_sweeper() -> DiscoveryCandidatePurgeSweeper:
    """Process-wide sweeper instance."""
    global _sweeper
    if _sweeper is None:
        _sweeper = DiscoveryCandidatePurgeSweeper()
    return _sweeper
