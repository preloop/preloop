"""Shared thread-pool helpers for short-lived database sessions."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, TypeVar

from preloop.models.db.session import _safe_close_db_session, get_db_session
from preloop.models.models import User
from sqlalchemy.orm import Session

_db_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="preloop_db_")
T = TypeVar("T")
logger = logging.getLogger(__name__)


def detach_user(db: Session, user: Optional[User]) -> Optional[User]:
    """Load scalar attributes and detach a user from the SQLAlchemy session."""
    if user is None:
        return None
    _ = user.id, user.account_id, user.username, user.email, user.is_active
    db.expunge(user)
    return user


def submit_off_loop(operation: Callable[[], None]) -> None:
    """Schedule ``operation`` on the shared DB thread pool without waiting.

    Used for work that must not block the caller's thread or the event loop,
    such as the policy-deny webhook enqueue (a fresh session, select, count,
    insert, and commit). Exceptions raised by ``operation`` are logged here
    so they are not left unretrieved on the pool Future.
    """

    def _run() -> None:
        try:
            operation()
        except Exception:
            logger.exception("Off-loop database work failed")

    _db_executor.submit(_run)


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
