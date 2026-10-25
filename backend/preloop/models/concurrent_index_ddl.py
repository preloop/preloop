"""Bound concurrent-index waits separately from blocking schema changes.

PostgreSQL concurrent index operations wait for existing writers and snapshots
without stopping ordinary reads or writes. The migration session's short DDL
lock timeout cancels those waits and can leave an invalid index to rebuild.
Only explicit concurrent-index blocks get this longer, finite budget. The
original session settings are restored even when an operation fails.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from typing import Iterator

import sqlalchemy as sa
from alembic.migration import MigrationContext
from sqlalchemy.exc import SQLAlchemyError

from preloop.models.migration_runtime import sqlstate_of

DEFAULT_LOCK_TIMEOUT_SECONDS = 60
DEFAULT_STATEMENT_TIMEOUT_SECONDS = 120

logger = logging.getLogger("alembic.runtime.concurrent_index")


def _seconds(name: str, default: int, maximum: int) -> int:
    """Read a positive, bounded integer; unsafe/invalid overrides use defaults."""
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if 0 < value <= maximum else default


@contextmanager
def concurrent_index_ddl(context: MigrationContext) -> Iterator[None]:
    """Run only concurrent index statements with bounded PostgreSQL timeouts.

    Args:
        context: The active Alembic context. Entering its autocommit block is
            required by PostgreSQL's concurrent index commands.
    """
    with context.autocommit_block():
        if context.dialect.name != "postgresql":
            yield
            return
        connection = context.bind
        assert connection is not None
        previous_lock, previous_statement = connection.execute(
            sa.text(
                "SELECT current_setting('lock_timeout'), "
                "current_setting('statement_timeout')"
            )
        ).one()
        set_timeouts = sa.text(
            "SELECT set_config('lock_timeout', :lock_timeout, false), "
            "set_config('statement_timeout', :statement_timeout, false)"
        )
        lock_seconds = _seconds(
            "PRELOOP_MIGRATION_CONCURRENT_LOCK_TIMEOUT_SECONDS",
            DEFAULT_LOCK_TIMEOUT_SECONDS,
            300,
        )
        statement_seconds = _seconds(
            "PRELOOP_MIGRATION_CONCURRENT_STATEMENT_TIMEOUT_SECONDS",
            DEFAULT_STATEMENT_TIMEOUT_SECONDS,
            240,
        )
        if lock_seconds >= statement_seconds:
            logger.warning(
                "Concurrent-index lock budget must be shorter than its statement "
                "budget; using finite defaults %ss/%ss.",
                DEFAULT_LOCK_TIMEOUT_SECONDS,
                DEFAULT_STATEMENT_TIMEOUT_SECONDS,
            )
            lock_seconds = DEFAULT_LOCK_TIMEOUT_SECONDS
            statement_seconds = DEFAULT_STATEMENT_TIMEOUT_SECONDS
        started = time.monotonic()
        logger.info(
            "Concurrent-index DDL starting (lock_timeout=%ss, statement_timeout=%ss).",
            lock_seconds,
            statement_seconds,
        )
        try:
            connection.execute(
                set_timeouts,
                {
                    "lock_timeout": f"{lock_seconds}s",
                    "statement_timeout": f"{statement_seconds}s",
                },
            )
            yield
        except SQLAlchemyError as error:
            logger.warning(
                "Concurrent-index DDL failed after %.1fs (SQLSTATE %s); inspect old transactions, "
                "pg_blocking_pids and pg_stat_progress_create_index before retrying. "
                "An interrupted build may leave an invalid index for repair.",
                time.monotonic() - started,
                sqlstate_of(error) or "unknown",
            )
            raise
        else:
            logger.info(
                "Concurrent-index DDL completed in %.1fs.", time.monotonic() - started
            )
        finally:
            connection.execute(
                set_timeouts,
                {
                    "lock_timeout": previous_lock,
                    "statement_timeout": previous_statement,
                },
            )
