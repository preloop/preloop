"""Production adapters and flow terminal hooks for security maintenance."""

import logging
from uuid import UUID

from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import crud_security_maintenance
from preloop.services.issue_lifecycle_worker import lifecycle_worker_db
from preloop.services.security_maintenance import SecurityMaintenanceService
from preloop.services.security_maintenance_refs import StaleCompletionError

logger = logging.getLogger(__name__)


async def maintenance_execution_finished(
    db: Session, execution: models.FlowExecution, flow: models.Flow
) -> None:
    """Consume a bound maintenance execution. Failures stay retryable."""
    details = execution.trigger_event_details or {}
    payload = details.get("payload") if isinstance(details.get("payload"), dict) else {}
    envelope = payload.get("security_maintenance") or details.get(
        "security_maintenance"
    )
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


async def sweep_security_maintenance(db: Session) -> dict[str, object]:
    """Retry pending dispatch and approval expiry without a user GET."""
    account_ids = crud_security_maintenance.list_reconcile_account_ids(db)
    reconciled = 0
    skipped = 0
    for account_id in account_ids:
        with lifecycle_worker_db(fallback=db) as worker_db:
            service = SecurityMaintenanceService(worker_db, account_id=account_id)
            result = await service.sweep()
            if result.get("acquired"):
                reconciled += int(result.get("reconciled") or 0)
            else:
                skipped += 1
    return {
        "accounts": len(account_ids),
        "reconciled": reconciled,
        "skipped": skipped,
    }
