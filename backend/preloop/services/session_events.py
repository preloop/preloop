"""Extension point for account-lifecycle runtime-session signals.

OSS records runtime sessions; processing those recordings into growth or
funnel analytics is EE-plugin territory. When an account's FIRST runtime
session is recorded, the session-recording chokepoint notifies the hook
registered here. Without a registered hook (the open-source default) the
notification is an inert no-op — no event is created, nothing is sent
anywhere.

This mirrors the analysis-model authorizer extension point in
``preloop.services.optimization_gating``: a plugin registers a callable at
startup, OSS consults it at the relevant chokepoint.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Signature: (db, *, account_id=..., occurred_at=...) -> None. The hook runs
# inside the caller's transaction; it must not commit.
FirstSessionHook = Callable[..., None]

_first_session_hook: Optional[FirstSessionHook] = None


def register_first_session_hook(hook: Optional[FirstSessionHook]) -> None:
    """Register (or clear, with ``None``) the first-session hook.

    Called by growth/analytics plugins at startup. Only one hook is
    supported; the last registration wins.

    Args:
        hook: Callable notified when an account's first runtime session is
            recorded.
    """
    global _first_session_hook
    if _first_session_hook is not None and hook is not None:
        logger.info("Replacing previously registered first-session hook")
    _first_session_hook = hook


def get_first_session_hook() -> Optional[FirstSessionHook]:
    """Return the registered hook, or ``None`` when none is registered."""
    return _first_session_hook


def notify_first_session_recorded(
    db: Session,
    *,
    account_id: Any,
    occurred_at: datetime,
) -> None:
    """Notify the registered hook that an account's first session was recorded.

    A no-op when no hook is registered. Hook failures are logged and
    swallowed: session recording must never fail because of an analytics
    side-channel.

    Args:
        db: Database session the session was recorded in (shared transaction).
        account_id: Account whose first runtime session was just recorded.
        occurred_at: When the session was recorded.
    """
    if _first_session_hook is None:
        return
    try:
        _first_session_hook(db, account_id=account_id, occurred_at=occurred_at)
    except Exception:
        logger.warning("First-session hook failed", exc_info=True)
