"""Optional fleet evaluation after accepted source writes."""

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.plugins.base import get_plugin_manager

logger = logging.getLogger(__name__)


def evaluate_fleet_findings(db: Session, *, account_id: UUID) -> None:
    """Isolate optional evaluation failures from an already committed write.

    Invoke only after the source transaction commits. The service owns policy;
    this integration owns commit/rollback for its findings and notification outbox.
    """
    service = get_plugin_manager().get_service("fleet_findings")
    if service is None:
        return
    try:
        service.evaluate(db, account_id=account_id)
        db.commit()
    except Exception:
        db.rollback()
        logger.error("Optional fleet finding evaluation failed")
