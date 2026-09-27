"""Stopping one flow execution: the single code path every stop goes through.

``POST /flows/executions/{execution_id}/command`` with ``{"command": "stop"}``
is the operator's stop. Since #1032 the platform also stops executions on its
own, when the pull request a run is working on is merged, closed or gets a
new head. Both go through :func:`stop_execution`, so an automatic stop tears
down exactly what an operator's stop tears down (the runner assignment, the
agent container, a delegation tree the run is parked on) and nothing is
stopped by flipping a status alone.

Two properties the automatic callers rely on:

* **Idempotent.** An execution that already ended is left as it is: no
  status write, no container call, no command on the bus. A run that
  finishes while its runtime is being torn down keeps its own result, because
  the status write is conditional on the row not being terminal.
* **Says why.** An automatic stop records a sentence in ``stop_reason`` and a
  machine-readable ``stop_source`` (``pr_merged``, ``pr_closed``,
  ``pr_superseded``). The orchestrator rewrites ``error_message`` when the
  agent exits, so the reason lives in columns it does not touch.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from preloop.models.crud import crud_flow, crud_flow_execution, crud_flow_runner
from preloop.services import flow_tree_stop
from preloop.services.runner_service import (
    pool_from_session_reference,
    runner_id_from_session_reference,
)

logger = logging.getLogger(__name__)

#: ``error_message`` written by an operator's stop.
MANUAL_STOP_MESSAGE = "Manually stopped by user"

#: Statuses in which a runtime (runner job, container, or a queue slot) may
#: exist and has to be torn down. A parked run holds no runtime: its stop is
#: the status write alone.
RUNTIME_STATUSES = frozenset({"RUNNING", "STARTING", "INITIALIZING", "PENDING"})


@dataclass(frozen=True)
class StopOutcome:
    """What one call to :func:`stop_execution` did.

    Attributes:
        stopped: True when this call ended the execution.
        status: The execution's status after the call.
    """

    stopped: bool
    status: str


def _status(execution: Any) -> str:
    return str(getattr(execution, "status", "") or "")


async def _tear_down_runtime(db: Any, execution: Any, *, account_id: Any) -> None:
    """Halt the runner job or stop the agent container behind an execution.

    Never raises: the status write that follows is the durable part of the
    stop and must happen even when the runtime cannot be reached.
    """
    from preloop.agents.codex import CodexAgent
    from preloop.agents.container import ContainerAgentExecutor

    execution_id = execution.id
    session_reference = execution.agent_session_reference
    runner_id = runner_id_from_session_reference(session_reference)
    queued_pool = pool_from_session_reference(session_reference)
    if runner_id is not None:
        # Runner-backed execution: the lease reference is not a container
        # or Job name, so a container executor cannot see or stop the
        # runner's process (and only builds an invalid Kubernetes
        # selector trying). Flag the halt so the runner stops the job
        # itself; its output already streams into flow_execution_log.
        # Halt is per assignment: this runner may be running other jobs
        # that nobody asked to stop.
        if crud_flow_runner.request_halt(
            db, runner_id=runner_id, execution_id=execution_id
        ):
            logger.info(
                "Requested halt on runner %s for execution %s", runner_id, execution_id
            )
        return
    if queued_pool is not None:
        # Queued for a private pool: nothing runs yet, so there is no
        # container, Job, or runner to stop. The status write is all that is
        # needed.
        return
    if not session_reference:
        return
    try:
        flow = crud_flow.get(db=db, id=execution.flow_id, account_id=account_id)
        if not flow:
            return
        use_kubernetes = (
            os.getenv("USE_KUBERNETES_FOR_AGENTS", "false").lower() == "true"
        )
        # CodexAgent auto-detects Kubernetes, no need to pass use_kubernetes.
        if flow.agent_type == "codex":
            agent = CodexAgent(config={})
        else:
            agent = ContainerAgentExecutor(
                agent_type=flow.agent_type,
                config={},
                image="dummy-image",
                use_kubernetes=use_kubernetes,
            )

        # Fetch final logs before stopping the container.
        try:
            container_logs = await agent.get_logs(session_reference, tail=5000)
            if container_logs:
                for log_line in container_logs:
                    crud_flow_execution.append_log(
                        db,
                        execution_id=str(execution_id),
                        log_data={
                            "type": "agent_log_line",
                            "payload": {"line": log_line},
                        },
                        commit=False,
                    )
                db.commit()
                logger.info(
                    "Persisted %s log lines to database for execution %s",
                    len(container_logs),
                    execution_id,
                )
        except Exception as log_error:
            logger.error(
                "Failed to fetch and persist logs before stopping: %s", log_error
            )

        await agent.stop(session_reference)
        logger.info(
            "Stopped container %s for execution %s", session_reference, execution_id
        )
    except Exception as error:
        logger.error(
            "Failed to stop container for execution %s: %s", execution_id, error
        )


async def _stop_tree(
    db: Any, execution: Any, *, account_id: Any, nats_client: Any
) -> None:
    """Stop the flows a stopped parent was waiting for (#689). Never raises."""
    try:
        await flow_tree_stop.stop_tree_for_stopped_parent(
            db,
            parent=execution,
            account_id=account_id,
            nats_client=nats_client,
        )
    except Exception:
        logger.exception("Failed to stop the tree of execution %s", execution.id)


async def _send_stop_command(
    execution: Any, *, nats_client: Any, payload: Optional[Dict[str, Any]]
) -> None:
    """Best effort stop command to the orchestrator loop over NATS."""
    try:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        await FlowExecutionOrchestrator.send_command(
            execution_id=str(execution.id),
            command="stop",
            payload=payload,
            nats_client=nats_client,
        )
    except Exception as error:
        logger.warning("Failed to send stop command via NATS: %s", error)


async def stop_execution(
    db: Any,
    execution: Any,
    *,
    account_id: Any,
    nats_client: Any = None,
    error_message: str = MANUAL_STOP_MESSAGE,
    stop_reason: Optional[str] = None,
    stop_source: Optional[str] = None,
    command_payload: Optional[Dict[str, Any]] = None,
) -> StopOutcome:
    """Stop one execution, or do nothing when it already ended.

    Args:
        db: Session the caller owns; the status write is committed on it.
        execution: The execution row, already scoped to ``account_id``.
        account_id: Account the execution belongs to.
        nats_client: Connected NATS client, when the caller has one.
        error_message: What the row says right after the stop.
        stop_reason: Sentence explaining an automatic stop; None for an
            operator's stop.
        stop_source: Machine-readable cause of an automatic stop.
        command_payload: Payload forwarded with the NATS stop command.

    Returns:
        Whether this call stopped the execution, and its status afterwards.
    """
    status_before = _status(execution)
    # A parent parked on the flows it started leaves the park here, before
    # any I/O, and terminally (#689). From this write on, a child reaching a
    # terminal state claims nothing and the sweep lists nothing, so the stop
    # cannot race a resume into existence while the container teardown below
    # takes its seconds. The tree itself is stopped after the status write,
    # once this execution is unambiguously terminal.
    stops_a_tree = flow_tree_stop.parked_on_children(execution)

    if flow_tree_stop.is_terminal_status(status_before):
        # Already ended: nothing to tear down and nothing to rewrite. A
        # stopped tree parent still re-applies its cascade, which is itself
        # idempotent and is how a stop whose tree walk failed is retried.
        if stops_a_tree and status_before.upper() == "STOPPED":
            await _stop_tree(
                db, execution, account_id=account_id, nats_client=nats_client
            )
        return StopOutcome(stopped=False, status=status_before)

    closed_park = False
    if stops_a_tree:
        closed_park = flow_tree_stop.close_children_park(
            db, parent=execution, reason=stop_reason or error_message
        )

    if _status(execution) in RUNTIME_STATUSES:
        await _tear_down_runtime(db, execution, account_id=account_id)

    stopped = (
        crud_flow_execution.mark_stopped(
            db,
            execution_id=execution.id,
            error_message=error_message,
            stop_reason=stop_reason,
            stop_source=stop_source,
            now=datetime.now(timezone.utc),
        )
        or closed_park
    )
    try:
        db.refresh(execution)
    except Exception:  # pragma: no cover - detached or mocked row
        logger.debug("Could not refresh execution %s after the stop", execution.id)

    if not stopped:
        # The run ended on its own while the runtime was being torn down.
        # It keeps its result; there is nothing left to cascade or signal.
        return StopOutcome(stopped=False, status=_status(execution))

    if stops_a_tree:
        await _stop_tree(db, execution, account_id=account_id, nats_client=nats_client)

    await _send_stop_command(
        execution, nats_client=nats_client, payload=command_payload
    )
    return StopOutcome(stopped=True, status="STOPPED")
