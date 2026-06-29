"""Short-lived database session helpers for WebSocket handlers."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from preloop.models.models import User
from preloop.services.db_executor import run_db_async

__all__ = ["detach_user", "run_db_async"]


def detach_user(db: Session, user: Optional[User]) -> Optional[User]:
    """Load scalar attributes and detach a user from the SQLAlchemy session."""
    if user is None:
        return None
    # Touch commonly used attributes while the session is still valid.
    _ = user.id, user.account_id, user.username, user.email, user.is_active
    db.expunge(user)
    return user
