"""Shared thread-pool helpers for short-lived database sessions."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, TypeVar

from sqlalchemy.orm import Session

from preloop.models.db.session import get_db_session, _safe_close_db_session

_db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="preloop_db_")
T = TypeVar("T")


def run_db_sync(operation: Callable[[Session], T]) -> T:
    """Run a database operation in a short-lived session."""
    db = next(get_db_session())
    try:
        return operation(db)
    finally:
        _safe_close_db_session(db)


async def run_db_async(operation: Callable[[Session], T]) -> T:
    """Run a database operation off the event loop with a short-lived session."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_db_executor, lambda: run_db_sync(operation))
