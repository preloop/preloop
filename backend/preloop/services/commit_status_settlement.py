"""Replace pending commit statuses that no terminal path replaced.

A run sets a pending commit status when it starts and replaces it with the
final one when it ends. Every terminal path does that itself; this sweep is
the safety net for the run whose finishing worker died after writing the
terminal status and before setting the final commit status, and for the
terminal writes that never reach the orchestrator (a container that ended
while no worker watched it, the stale-execution monitor).

The record lives on the execution (``startup_receipt._commit_status``); see
:meth:`preloop.models.crud.flow_execution.CRUDFlowExecution.
claim_final_commit_status`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from preloop.models.crud import crud_flow, crud_flow_execution

logger = logging.getLogger(__name__)

#: Only executions that ended this recently are settled.
SETTLE_WINDOW = timedelta(days=7)
#: Leave the finishing worker this long to set the status itself.
SETTLE_AFTER = timedelta(minutes=2)
#: A final status claimed but not confirmed this long ago is taken over.
CLAIM_STALE_AFTER = timedelta(minutes=10)
#: Executions settled per pass.
BATCH_SIZE = 50


def _attempts(execution: Any) -> int:
    """Final-status attempts recorded on an execution so far."""
    final = crud_flow_execution.commit_status_record(execution).get("final")
    return int(final.get("attempts") or 0) if isinstance(final, dict) else 0


def _count_attempt(session: Any, execution: Any) -> None:
    """Record a pass that could not post, toward the attempt limit."""
    if crud_flow_execution.claim_final_commit_status(
        session,
        execution_id=execution.id,
        state=str(execution.status),
        stale_after=CLAIM_STALE_AFTER,
    ):
        crud_flow_execution.record_final_commit_status(
            session, execution_id=execution.id, posted=False
        )


async def settle_commit_statuses(limit: int = BATCH_SIZE) -> dict[str, int]:
    """Set the final commit status of terminal runs left pending.

    Bounded (``limit`` executions per pass) and idempotent: a final status
    that was posted is recorded and never posted again. Logs one line per
    pass with the counts.

    Args:
        limit: Executions examined in this pass.

    Returns:
        Counts: ``found``, ``settled``, ``failed`` (the provider refused or
        the status could not be posted) and ``skipped`` (another path holds
        the claim, or no status can be set for the run any more).
    """
    from preloop.models.db.session import get_db_session
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    counts = {"found": 0, "settled": 0, "failed": 0, "skipped": 0}
    session = next(get_db_session())
    try:
        ids = crud_flow_execution.unsettled_commit_status_ids(
            session,
            ended_within=SETTLE_WINDOW,
            settle_after=SETTLE_AFTER,
            limit=limit,
        )
        counts["found"] = len(ids)
        for execution_id in ids:
            try:
                execution: Any = crud_flow_execution.get(session, id=execution_id)
                flow = (
                    crud_flow.get(session, id=str(execution.flow_id))
                    if execution is not None
                    else None
                )
                if execution is None or flow is None:
                    if execution is not None:
                        _count_attempt(session, execution)
                    counts["skipped"] += 1
                    continue
                continuation = getattr(execution, "resume_execution_id", None)
                if continuation is not None:
                    # A released park: the continuation run sets its own
                    # pending and final status on the same commit and
                    # context. Posting here would set it a second time.
                    if crud_flow_execution.claim_final_commit_status(
                        session,
                        execution_id=execution.id,
                        state=str(execution.status),
                        stale_after=CLAIM_STALE_AFTER,
                    ):
                        crud_flow_execution.record_final_commit_status(
                            session,
                            execution_id=execution.id,
                            posted=False,
                            handed_to=continuation,
                        )
                    counts["skipped"] += 1
                    continue
                orchestrator = FlowExecutionOrchestrator(
                    session,
                    flow_id=uuid.UUID(str(execution.flow_id)),
                    trigger_event_data=dict(execution.trigger_event_details or {}),
                    nats_client=None,  # type: ignore[arg-type]
                )
                orchestrator.execution_log = execution
                orchestrator.flow = flow
                orchestrator._final_status_stale_after = CLAIM_STALE_AFTER
                before = _attempts(execution)
                await orchestrator.settle_commit_status(
                    str(execution.status),
                    error_message=execution.error_message,
                    result=execution.result
                    if isinstance(execution.result, dict)
                    else None,
                )
                session.refresh(execution)
                final = crud_flow_execution.commit_status_record(execution).get("final")
                if isinstance(final, dict) and final.get("posted_at"):
                    counts["settled"] += 1
                elif isinstance(final, dict) and final.get("failed_at"):
                    counts["failed"] += 1
                else:
                    counts["skipped"] += 1
                if not (isinstance(final, dict) and final.get("posted_at")) and (
                    _attempts(execution) == before
                ):
                    # This pass attempted nothing: no tracker or commit can
                    # be resolved for the run any more. Count it so the row
                    # leaves the batch after a bounded number of passes
                    # instead of starving runs that can still settle.
                    _count_attempt(session, execution)
            except Exception as error:
                session.rollback()
                counts["failed"] += 1
                logger.error(
                    "Commit status settlement failed for %s: %s",
                    execution_id,
                    type(error).__name__,
                )
    finally:
        session.close()
    logger.info(
        "Commit status settlement: %s found, %s settled, %s failed, %s skipped",
        counts["found"],
        counts["settled"],
        counts["failed"],
        counts["skipped"],
    )
    return counts
