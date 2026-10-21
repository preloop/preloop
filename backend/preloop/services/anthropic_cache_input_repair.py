"""One post-rollout pass that converts straggler Anthropic input rows (#1955).

Migration ``20261106_anthropic_cache_input`` runs before the new pods start,
while pods on the previous release keep serving and keep writing raw (cache
exclusive) Anthropic input. Once this release has been serving for a while,
every API replica runs the same idempotent conversion once over recent rows,
so nothing written during the rollout stays in the old shape.

Remove this module once every deployment has run a release that includes it.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from preloop.models.crud import crud_api_usage
from preloop.models.db.session import get_db_session
from preloop.services.service_roles import (
    background_passes_allowed,
    current_service_role,
)

logger = logging.getLogger(__name__)

#: Wait this long after startup, so a rolling update has finished.
REPAIR_DELAY_SECONDS = 15 * 60

#: How far back before startup the pass looks. A rollout that takes longer
#: than this between the migration and the new pods is not expected.
REPAIR_LOOKBACK = timedelta(hours=24)


def run_anthropic_cache_input_repair_once(since: datetime) -> int:
    """Convert raw Anthropic input rows written since ``since``.

    Args:
        since: Earliest row timestamp to look at.

    Returns:
        How many rows were converted.
    """
    db = next(get_db_session())
    try:
        return crud_api_usage.repair_anthropic_cache_input(db, since=since)
    finally:
        db.close()


class AnthropicCacheInputRepair:
    """Run the conversion once, a delay after startup."""

    def __init__(self, delay_seconds: Optional[float] = None) -> None:
        """Create the one-shot repair.

        Args:
            delay_seconds: Seconds to wait before the pass.
        """
        self.delay = (
            REPAIR_DELAY_SECONDS if delay_seconds is None else float(delay_seconds)
        )
        self.since = datetime.now(timezone.utc) - REPAIR_LOOKBACK
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Schedule the pass, unless this role runs no background passes."""
        if self._task is not None:
            return
        if not background_passes_allowed():
            logger.info(
                "Anthropic cache input repair not scheduled for %s role.",
                current_service_role(),
            )
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the pass if it has not finished."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return
        try:
            changed = await asyncio.to_thread(
                run_anthropic_cache_input_repair_once, self.since
            )
            if changed:
                logger.info("Stored %s Anthropic usage row(s) cache inclusive", changed)
        except Exception:
            logger.error("Anthropic cache input repair failed", exc_info=True)
