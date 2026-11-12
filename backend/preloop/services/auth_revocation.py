"""Publish committed session revocations and recheck live JWT connections."""

import json
import logging

from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.orm import Session

from preloop.api.auth.jwt import reject_revoked_token
from preloop.schemas.auth import TokenData
from preloop.models.crud import crud_user
from preloop.models.crud.auth_revocation import (
    committed_revocations,
    discard_revocations,
)
from preloop.services.db_executor import run_db_async
from preloop.sync.services.realtime_publisher import publish_realtime

logger = logging.getLogger(__name__)
REVOCATION_SUBJECT = "auth.revoked"
REVOCATION_RECHECK_SECONDS = 60.0


def _publish_committed(db: Session) -> None:
    """Hand committed identifiers to the bounded, thread-safe NATS publisher."""
    for notification in committed_revocations(db):
        try:
            publish_realtime(REVOCATION_SUBJECT, json.dumps(notification).encode())
        except Exception:
            # The committed revoke remains effective. Every JWT socket also
            # rechecks independently of event delivery.
            logger.warning("Live session revocation notification failed")


def install_revocation_notifications() -> None:
    """Install application-owned commit hooks, including for plugin callers."""
    if not event.contains(Session, "after_commit", _publish_committed):
        event.listen(Session, "after_commit", _publish_committed)
        event.listen(Session, "after_soft_rollback", discard_revocations)


async def session_is_revoked(token_data: TokenData) -> bool:
    """Check current identity, membership, CLI-session and extension revocation.

    Each check opens a fresh short-lived session off the event loop. Transient
    database failures propagate so a failed lookup is never mistaken for a
    positive revocation result.
    """

    def check(db: Session) -> bool:
        user = crud_user.get(db, id=token_data.sub)
        if user is None:
            return True
        try:
            reject_revoked_token(db, user, token_data)
        except HTTPException as exc:
            if exc.status_code in {401, 403}:
                return True
            raise
        return False

    return await run_db_async(check)
