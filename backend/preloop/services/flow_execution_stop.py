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
  ``pr_superseded``; ``manual`` for an operator's stop). The orchestrator
  rewrites ``error_message`` when the agent exits, so the reason lives in
  columns it does not touch.

And two that make a stop durable rather than best effort:

* **Recorded intent.** Every stop writes ``stop_requested_at``. Launch
  admission refuses a row that carries it (or is already terminal), and the
  orchestrator's monitor polls it, so a stop issued while the runtime is
  still being prepared, when nobody is listening for the NATS command yet,
  still prevents the run.
* **Requested is not confirmed.** ``STOPPED`` says the stop was accepted.
  ``stop_confirmed_at`` is only written once the runtime is verified gone
  (``AgentExecutor.is_stopped``). When the teardown failed, or there was no
  runtime reference to tear down yet, it stays null and ``stop_reason``
  says why; the orchestrator (or the recovery pass that resumes it) confirms
  termination later.

Every call is written to the audit log as ``flow_execution_stop_requested``.
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

#: Audit action written for every stop request, whoever issued it.
STOP_REQUESTED_AUDIT_ACTION = "flow_execution_stop_requested"

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


@dataclass(frozen=True)
class TeardownOutcome:
    """What tearing the runtime down established.

    Attributes:
        confirmed: True only when the runtime is verified gone (or there
            never was one to run, such as a queue slot).
        unconfirmed_reason: Why termination could not be confirmed, when it
            is worth recording (teardown raised, no runtime reference yet).
            None when confirmation is simply pending (a Kubernetes Job still
            deleting, a runner that has not acknowledged its halt yet).
    """

    confirmed: bool
    unconfirmed_reason: Optional[str] = None
    #: No runtime reference: confirmed after all when the row turns out
    #: never to have been admitted (decided atomically by the status write).
    confirm_if_never_launched: bool = False


#: ``stop_reason`` suffix when the stop found no runtime reference to tear
#: down: the orchestrator may still be creating it, and refuses or tears it
#: down when it sees the stopped row.
NO_RUNTIME_REFERENCE_REASON = (
    "termination not confirmed: no runtime reference at stop time"
)


def _status(execution: Any) -> str:
    return str(getattr(execution, "status", "") or "")


async def _tear_down_runtime(
    db: Any, execution: Any, *, account_id: Any
) -> TeardownOutcome:
    """Halt the runner job or stop the agent container behind an execution.

    Never raises: the status write that follows is the durable part of the
    stop and must happen even when the runtime cannot be reached. What it
    returns says whether the runtime is verified gone.
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
        # Confirmed when the runner acknowledges the halt (its terminal
        # report confirms the stop).
        return TeardownOutcome(confirmed=False)
    if queued_pool is not None:
        # Queued for a private pool: nothing runs yet, so there is no
        # container, Job, or runner to stop. The status write is all that is
        # needed.
        return TeardownOutcome(confirmed=True)
    if not session_reference:
        # Still being prepared: the orchestrator refuses admission or tears
        # down what it created when it sees the stopped row, and confirms.
        return TeardownOutcome(
            confirmed=False,
            unconfirmed_reason=NO_RUNTIME_REFERENCE_REASON,
            confirm_if_never_launched=True,
        )
    try:
        flow = crud_flow.get(db=db, id=execution.flow_id, account_id=account_id)
        if not flow:
            return TeardownOutcome(
                confirmed=False,
                unconfirmed_reason="termination not confirmed: flow not found",
            )
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
            "Stop requested for runtime %s of execution %s",
            session_reference,
            execution_id,
        )
    except Exception as error:
        logger.error(
            "Failed to stop container for execution %s: %s", execution_id, error
        )
        return TeardownOutcome(
            confirmed=False,
            unconfirmed_reason=(
                f"termination not confirmed: runtime teardown failed "
                f"({type(error).__name__}: {error})"
            )[:300],
        )
    # ``stop()`` returning is not termination: a Kubernetes Job deletion is
    # only accepted, its pods still have their grace period. Ask the runtime.
    try:
        confirmed = await agent.is_stopped(session_reference) is True
    except Exception as error:
        logger.warning(
            "Could not verify termination of %s for execution %s: %s",
            session_reference,
            execution_id,
            error,
        )
        confirmed = False
    return TeardownOutcome(confirmed=confirmed)


def _audit_stop_request(
    db: Any,
    execution: Any,
    *,
    account_id: Any,
    user_id: Any,
    status_before: str,
    outcome: StopOutcome,
    stop_source: Optional[str],
    teardown: Optional[TeardownOutcome],
) -> None:
    """Write the stop request to the audit log. Never raises."""
    try:
        from preloop.models.crud import crud_audit_log

        crud_audit_log.log_action(
            db,
            account_id=account_id,
            user_id=user_id,
            action=STOP_REQUESTED_AUDIT_ACTION,
            resource_type="flow_execution",
            resource_id=str(execution.id),
            status="success" if outcome.stopped else "noop",
            details={
                "flow_id": str(getattr(execution, "flow_id", "") or ""),
                "status_before": status_before,
                "status_after": outcome.status,
                "stop_source": getattr(execution, "stop_source", None)
                or stop_source
                or (None if user_id is None else "manual"),
                "stop_requested_at": _iso(
                    getattr(execution, "stop_requested_at", None)
                ),
                "stop_confirmed_at": _iso(
                    getattr(execution, "stop_confirmed_at", None)
                ),
                "termination_confirmed": getattr(execution, "stop_confirmed_at", None)
                is not None,
                "unconfirmed_reason": teardown.unconfirmed_reason
                if teardown is not None
                and getattr(execution, "stop_confirmed_at", None) is None
                else None,
            },
        )
    except Exception:
        logger.exception("Failed to audit the stop of execution %s", execution.id)
        try:
            db.rollback()
        except Exception:  # pragma: no cover - session already unusable
            pass


def _iso(value: Any) -> Optional[str]:
    return value.isoformat() if isinstance(value, datetime) else None


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
    user_id: Any = None,
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
        user_id: Who asked, for the audit row; None for a platform stop.

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
        outcome = StopOutcome(stopped=False, status=status_before)
        _audit_stop_request(
            db,
            execution,
            account_id=account_id,
            user_id=user_id,
            status_before=status_before,
            outcome=outcome,
            stop_source=stop_source,
            teardown=None,
        )
        return outcome

    closed_park = False
    if stops_a_tree:
        closed_park = flow_tree_stop.close_children_park(
            db,
            parent=execution,
            reason=stop_reason or error_message,
            stop_source=stop_source,
        )

    teardown: Optional[TeardownOutcome] = None
    if _status(execution) in RUNTIME_STATUSES:
        teardown = await _tear_down_runtime(db, execution, account_id=account_id)

    stopped = (
        crud_flow_execution.mark_stopped(
            db,
            execution_id=execution.id,
            error_message=error_message,
            stop_reason=stop_reason,
            stop_source=stop_source,
            now=datetime.now(timezone.utc),
            # A parked run holds no runtime: nothing is left to terminate.
            confirmed=teardown.confirmed if teardown is not None else True,
            unconfirmed_reason=(
                teardown.unconfirmed_reason if teardown is not None else None
            ),
            confirm_if_never_launched=(
                teardown.confirm_if_never_launched if teardown is not None else False
            ),
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
        outcome = StopOutcome(stopped=False, status=_status(execution))
        _audit_stop_request(
            db,
            execution,
            account_id=account_id,
            user_id=user_id,
            status_before=status_before,
            outcome=outcome,
            stop_source=stop_source,
            teardown=teardown,
        )
        return outcome

    _audit_stop_request(
        db,
        execution,
        account_id=account_id,
        user_id=user_id,
        status_before=status_before,
        outcome=StopOutcome(stopped=True, status="STOPPED"),
        stop_source=stop_source,
        teardown=teardown,
    )

    if stops_a_tree:
        await _stop_tree(db, execution, account_id=account_id, nats_client=nats_client)

    await _send_stop_command(
        execution, nats_client=nats_client, payload=command_payload
    )
    return StopOutcome(stopped=True, status="STOPPED")
