"""Run ``alembic upgrade head`` the way a live deployment needs it run.

``python -m preloop.models.migrate`` is what the Helm ``pre-upgrade`` hook
executes instead of bare ``alembic upgrade head``. The difference is the retry
loop: the migration session uses a short ``lock_timeout``
(``preloop.models.migration_runtime``), so a revision that collides with live
traffic fails in seconds. Without a retry the release would fail with it; with
one, the next attempt resumes at the first revision that did not commit and
usually walks straight through.

Only lock contention is retried. A revision with a bug fails on the first
attempt, with its own traceback, as it should.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Optional, Sequence

from alembic import command
from alembic.config import Config

from preloop.models.migration_runtime import (
    lock_timeout,
    max_attempts,
    retry_delay_bounds,
    run_with_lock_retries,
    sqlstate_of,
)

logger = logging.getLogger("preloop.models.migrate")

# This module lives in backend/preloop/models/, next to alembic.ini.
MODELS_DIR = Path(__file__).resolve().parent


def alembic_config() -> Config:
    """Load alembic.ini with an absolute script location.

    ``script_location`` in alembic.ini is relative to the models package, so
    resolving it here lets the job run from any working directory.
    """
    config = Config(str(MODELS_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(MODELS_DIR / "alembic"))
    return config


def upgrade(revision: str = "head") -> None:
    """Upgrade once, no retries. Exposed so tests can drive it directly."""
    command.upgrade(alembic_config(), revision)


def _log_retry(attempt: int, delay: float, error: BaseException) -> None:
    logger.warning(
        "Migration attempt %s hit lock contention (SQLSTATE %s); retrying in %.1fs. Locks released by committed revisions are kept.",
        attempt,
        sqlstate_of(error) or "unknown",
        delay,
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Entry point: upgrade to ``head`` with bounded retries on lock waits."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "revision",
        nargs="?",
        default="head",
        help="Target revision (default: head).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    low, high = retry_delay_bounds()
    logger.info(
        "Upgrading database to %s (lock_timeout=%s, up to %s attempts, "
        "%.0f-%.0fs backoff, one transaction per revision).",
        args.revision,
        lock_timeout(),
        max_attempts(),
        low,
        high,
    )
    run_with_lock_retries(
        lambda: upgrade(args.revision),
        on_retry=_log_retry,
    )
    logger.info("Database is at %s.", args.revision)
    return 0


if __name__ == "__main__":
    sys.exit(main())
