"""Production adapters and flow terminal hooks for security maintenance."""

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.services.issue_lifecycle_worker import lifecycle_worker_db
from preloop.services.security_maintenance import (
    ENVELOPE_KEY,
    SecurityMaintenanceService,
)
from preloop.services.security_maintenance_refs import StaleCompletionError

logger = logging.getLogger(__name__)


async def maintenance_execution_finished(
    db: Session, execution: models.FlowExecution, flow: models.Flow
) -> None:
    """Consume a bound maintenance execution. Failures stay retryable."""
    details = execution.trigger_event_details or {}
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    envelope = payload.get(ENVELOPE_KEY) or details.get(ENVELOPE_KEY)
    if not isinstance(envelope, dict) or not envelope.get("item_id"):
        return
    if not flow.account_id:
        return
    with lifecycle_worker_db(fallback=db) as worker_db:
        service = SecurityMaintenanceService(
            worker_db, account_id=UUID(str(flow.account_id))
        )
        try:
            await service.finish_execution(execution)
        except StaleCompletionError:
            logger.info(
                "Ignored stale security-maintenance completion for execution %s",
                execution.id,
            )
