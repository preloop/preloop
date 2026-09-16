"""Park a parent on its children, resume it when they finish (#633).

``flow_delegation_call`` starts a child and returns; this module is what
happens when the parent actually needs the answer. It is deliberately the
sibling of ``approval_park`` rather than a second parking mechanism: same
three durable steps, same columns, same idempotent claim, different release
condition.

1. **request** (the ``run_flow`` tool call, in the agent's turn): wait in
   process for a short window first, because a child that finishes in ninety
   seconds should not cost a park and a resume. If the children are still
   running when the window closes, write the park request on the parent row
   and hand the agent a structured result telling it to stop.
2. **confirm** (orchestrator monitor): release the container and set
   ``WAITING_FOR_CHILDREN``. The compute seconds written there are agent wall
   clock only, so the flow's timeout budget pauses while the children run.
3. **claim** (a child finishing, or the sweep): claim the parked row exactly
   once and start a resume execution that natively continues the same agent
   session, carrying one completion record per child.

Release condition: every child this execution started is terminal. Terminal
means refused, failed or stopped as much as it means completed, because a
parent that only resumes on success waits forever on the one child that
crashed. A parent side deadline bounds the wait: when it passes the parent is
resumed anyway, with an explicit expired record for each child that is still
running, so the orchestrator writes a report with declared coverage instead
of the platform reporting a missing result.

Honest limitation, the same one the human park has: no harness lets us inject
a value as the return of a tool call in a session that was killed. The child
results arrive as the next turn, naming the executions they belong to, not as
the return value of the ``run_flow`` call the parent made.

Not here, on purpose: cost ceilings (#631), the console execution tree
(#634), approvals raised inside a child (#635), and what happens to children
when an operator stops a parked parent, which is decided and implemented in
``flow_tree_stop`` (#689) and is why this module never touches the stop path.
A deadline here expires the parent's wait and leaves the children running; a
stop there ends them, because the two are not the same event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from preloop.a2a.delegation import DelegationShapeError, task_state_for_status
from preloop.config import settings
from preloop.models.crud import (
    crud_flow,
    crud_flow_execution,
    crud_flow_execution_log,
)
from preloop.services.flow_delegation_call import (
    DELEGATION_DETAILS_KEY,
    DELEGATION_REFUSAL_LOG_TYPE,
    console_url_for,
)

logger = logging.getLogger(__name__)

#: Execution status for a parent that is alive, holds no runtime, and is
#: waiting for the executions it started.
WAITING_FOR_CHILDREN = "WAITING_FOR_CHILDREN"

#: ``park_kind`` this module writes on the execution row.
PARK_KIND = "children"

#: Reserved key on the trigger payload holding the park chain bookkeeping of
#: a run resumed after its children, mirroring ``_answers``.
CHILDREN_KEY = "_children"

#: Prompt block appended to the resolved prompt of such a run.
CHILDREN_PROMPT_KEY = "_children_prompt"

#: Key under ``payload`` carrying the completion records, machine readable,
#: mirroring ``payload.answers`` on a human resume.
CHILDREN_PAYLOAD_KEY = "children"

#: A2A task states that mean a child will not change again. Rejected is in
#: here for the same reason failed and canceled are: the issue's release
#: condition is "terminal", and refused, failed and stopped are all terminal.
_TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
    }
)

#: The kind marker every delegation task record carries (#625).
_TASK_KIND = "delegation_task"

#: How often the in process wait re-reads the children. Short enough that a
#: fast child is noticed, long enough that the wait is not a busy loop.
POLL_INTERVAL_SECONDS = 2.0

#: Bound on the child result document carried into the resumed prompt and
#: payload. Beyond this the record points at the result endpoint instead.
_MAX_RESULT_CHARS = 8000

#: Bound on the ancestry of one resume chain kept in the trigger details.
_MAX_HISTORY = 10


class ChildWaitUnavailableError(Exception):
    """The caller is not a live flow execution, so there is nothing to park."""


@dataclass(frozen=True)
class ChildParkResume:
    """One parent that was resumed, and what it was told about its children."""

    parent_execution_id: str
    resume_execution_id: str
    expired_child_ids: Tuple[str, ...]


def in_process_wait_seconds() -> int:
    """How long ``run_flow(wait=true)`` waits before parking the parent."""
    return max(0, int(settings.flow_delegation_wait_seconds))


def child_wait_deadline_seconds() -> int:
    """How long a parked parent waits for its children before giving up."""
    return max(60, int(settings.flow_delegation_child_wait_seconds))


def is_terminal_child(status: Any) -> bool:
    """Whether a child in this status will never change again.

    Reads the frozen status table from #625 rather than a second list: a
    refused, failed or stopped child is as terminal as a completed one, and a
    child parked on a human is not terminal, because it can still finish.
    A status with no mapping is treated as running, so an unknown state waits
    for the deadline instead of being declared finished.
    """
    try:
        return task_state_for_status(str(status or "")) in _TERMINAL_TASK_STATES
    except DelegationShapeError:
        logger.warning(
            "Child status %r has no A2A mapping; treating it as live", status
        )
        return False


def child_label(child: Any) -> Optional[str]:
    """The label the parent gave this child when it started it, if any."""
    details = getattr(child, "trigger_event_details", None)
    if not isinstance(details, dict):
        return None
    block = details.get(DELEGATION_DETAILS_KEY)
    if not isinstance(block, dict):
        return None
    label = block.get("label")
    return str(label)[:200] if label else None


def _as_utc(value: Any) -> Optional[datetime]:
    """Naive timestamps in this schema are UTC; make that explicit."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _result_artifact(child: Any) -> Optional[Dict[str, Any]]:
    """The child's structured result as an A2A artifact, or a pointer to it.

    A result document can be arbitrarily large and it is about to be carried
    into another agent's prompt, so past ``_MAX_RESULT_CHARS`` the artifact
    keeps the pointer and drops the document. The parent can still read it
    through the execution result endpoint.
    """
    result = getattr(child, "result", None)
    artifact_id = f"{child.id}:result"
    if not isinstance(result, dict) or not result:
        return None
    try:
        encoded = json.dumps(result)
    except (TypeError, ValueError):
        logger.warning("Child %s has a result that is not JSON serialisable", child.id)
        return None
    if len(encoded) > _MAX_RESULT_CHARS:
        return {
            "artifactId": artifact_id,
            "name": "result",
            "description": "Result document, too large to inline",
            "parts": [
                {
                    "text": (
                        f"The result of execution {child.id} is "
                        f"{len(encoded)} characters, too large to carry in a "
                        "prompt. Read it at "
                        f"/flows/executions/{child.id}/result."
                    )
                }
            ],
        }
    return {
        "artifactId": artifact_id,
        "name": "result",
        "description": "Structured result reported by the child",
        "parts": [{"data": result, "mediaType": "application/json"}],
    }


def _state_for(status: str) -> str:
    """A2A state for a child status, without letting an unknown one strand it.

    #625 raises on an unmapped status on purpose, and that is right on the
    call path. Here the record is being built for a parent that is already
    parked, so a raise would strand the run; the true status stays in the
    metadata, which is what keeps the mapping inspectable.
    """
    try:
        return task_state_for_status(status)
    except DelegationShapeError:
        logger.error(
            "Execution status %r has no A2A task state; reporting it as working",
            status,
        )
        return "TASK_STATE_WORKING"


def completion_record(
    child: Any,
    *,
    flow: Any,
    parent_execution_id: Any,
    root_execution_id: Any,
    expired: bool = False,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """One finished child as the A2A task record frozen by #625.

    Args:
        child: The child execution row.
        flow: The flow that child ran.
        parent_execution_id: The parent that started it.
        root_execution_id: Root of the delegation tree.
        expired: True when the parent's child wait deadline passed while this
            child was still running. The record then says so in its status
            message: the metadata keys are a closed set and inventing one
            would be an edit to a frozen schema.
        now: Timestamp for the record, for tests.

    Returns:
        A task record that validates against ``delegation_task``.
    """
    moment = now or datetime.now(UTC)
    status = str(getattr(child, "status", "") or "")
    metadata: Dict[str, Any] = {
        "preloop.ai/kind": _TASK_KIND,
        "preloop.ai/executionId": str(child.id),
        "preloop.ai/parentExecutionId": str(parent_execution_id),
        "preloop.ai/rootExecutionId": (
            str(root_execution_id) if root_execution_id is not None else None
        ),
        "preloop.ai/flowId": str(getattr(flow, "id", getattr(child, "flow_id", ""))),
        "preloop.ai/depth": int(getattr(child, "delegation_depth", 0) or 0),
        "preloop.ai/status": status or "UNKNOWN",
    }
    flow_name = getattr(flow, "name", None)
    if flow_name:
        metadata["preloop.ai/flowName"] = str(flow_name)
    cost = getattr(child, "estimated_cost", None)
    if cost is not None:
        try:
            metadata["preloop.ai/cost"] = max(0.0, float(cost))
        except (TypeError, ValueError):
            # Unparseable cost is omitted rather than crashing the completion
            # record; the rest of the row (id, state, label) is still true.
            pass
    tokens = getattr(child, "total_tokens", None)
    if tokens is not None:
        try:
            metadata["preloop.ai/tokens"] = max(0, int(tokens))
        except (TypeError, ValueError):
            # Same as cost: a garbage token count must not drop the record.
            pass
    console_url = console_url_for(child.id)
    if console_url:
        metadata["preloop.ai/consoleUrl"] = console_url

    record: Dict[str, Any] = {
        "id": str(child.id),
        "contextId": str(root_execution_id or parent_execution_id),
        "status": {
            "state": _state_for(status),
            "timestamp": moment.isoformat(),
        },
        "metadata": metadata,
    }
    if expired:
        record["status"]["message"] = {
            "messageId": f"{child.id}:wait-expired",
            "role": "ROLE_AGENT",
            "parts": [
                {
                    "text": (
                        "The child wait deadline passed while this execution "
                        f"was still {status or 'running'}. Treat it as "
                        "'no result': it was not stopped, it simply did not "
                        "finish in time, and its outcome is not part of what "
                        "you can report."
                    )
                }
            ],
        }
    artifact = _result_artifact(child)
    if artifact is not None:
        record["artifacts"] = [artifact]
    return record


@dataclass(frozen=True)
class ChildOutcome:
    """One child as the resumed parent will read it.

    The A2A record is the frozen shape (#625) and carries no label, because
    its metadata keys are a closed set. The label lives here instead, next to
    the record, which is also where the expiry flag belongs: both are facts
    about this parent's wait, not about the child's task.
    """

    record: Dict[str, Any]
    label: Optional[str] = None
    expired: bool = False

    @property
    def execution_id(self) -> str:
        """The child execution this outcome is about.

        A refusal has no execution row, so the record's own id (the attempt)
        is what identifies it. Never empty: it is the key of the completion
        records in the resumed payload.
        """
        return str(
            self.record.get("metadata", {}).get("preloop.ai/executionId")
            or self.record.get("id")
            or ""
        )

    @property
    def status(self) -> str:
        """The Preloop status the child ended in."""
        return str(self.record.get("metadata", {}).get("preloop.ai/status", "UNKNOWN"))

    @property
    def refusal_reason(self) -> Optional[str]:
        """Which rule declined this call, when nothing ran at all."""
        reason = self.record.get("metadata", {}).get("preloop.ai/refusalReason")
        return str(reason) if reason else None


def _cost_text(record: Dict[str, Any]) -> str:
    """Cost of one child for the prompt table."""
    cost = record.get("metadata", {}).get("preloop.ai/cost")
    if not isinstance(cost, (int, float)):
        return "not recorded"
    return f"${float(cost):.4f}"


def _result_text(outcome: "ChildOutcome") -> str:
    """Where the parent reads this child's result, if there is one."""
    if outcome.refusal_reason:
        return "nothing ran"
    if outcome.record.get("artifacts"):
        return f"attached; also /flows/executions/{outcome.execution_id}/result"
    return "no result artifact"


def children_prompt_block(outcomes: Sequence[ChildOutcome]) -> str:
    """The turn a resumed parent reads instead of the results it never got.

    One row per child, whatever happened to it, so a parent can declare the
    coverage it actually reached instead of reporting a missing result. The
    table is data, not instructions: the only thing it tells the agent to do
    is carry on.
    """
    expired = [outcome for outcome in outcomes if outcome.expired]
    refused = [outcome for outcome in outcomes if outcome.refusal_reason]
    header = (
        "RESUMED AFTER THE FLOWS YOU STARTED FINISHED. While you were parked, "
        f"the {len(outcomes)} run_flow call(s) you made reached a final "
        "state. Their results arrive here, as this turn, and not as the "
        "return value of the run_flow call you made: that agent session was "
        "released while they ran."
    )
    if refused:
        header += (
            f" {len(refused)} of them were refused before anything ran, so "
            "there is no result to wait for: the row names the rule that "
            "declined the call."
        )
    if expired:
        header += (
            f" {len(expired)} of them had still not finished when the child "
            "wait deadline passed and are reported as expired: that is not a "
            "failure and their outcome is unknown, so leave them out of what "
            "you report as covered and say so in your result."
        )
    lines = [
        header,
        "",
        "| execution | flow | label | final state | cost | result |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for outcome in outcomes:
        metadata = outcome.record.get("metadata", {})
        state = outcome.status
        if outcome.expired:
            state = f"{state} (expired: still running at the deadline)"
        elif outcome.refusal_reason:
            state = f"{state} ({outcome.refusal_reason})"
        lines.append(
            "| {execution} | {flow} | {label} | {state} | {cost} | {result} |".format(
                execution=outcome.execution_id,
                flow=str(metadata.get("preloop.ai/flowName") or "unknown"),
                label=outcome.label or "-",
                state=state,
                cost=_cost_text(outcome.record),
                result=_result_text(outcome),
            )
        )
    lines.append(
        "\nThe full records, including each child's structured result, are in "
        f"the trigger payload under `{CHILDREN_PAYLOAD_KEY}`. Continue from "
        "where you stopped: do not start these flows again."
    )
    return "\n".join(lines)


def build_child_resume_details(
    parked: Any, outcomes: Sequence[ChildOutcome]
) -> Dict[str, Any]:
    """Trigger details for the execution that resumes a parent's turn.

    The parked run's own trigger snapshot is carried forward (so the resumed
    orchestrator still sees the release, the schedule slot or the issue it was
    started for) plus the ``_resume`` binding, the completion records in the
    machine readable payload slot, and the prompt block. Shaped exactly like
    ``approval_park.build_resume_details`` because the harness side of a
    resume does not care what the run was waiting for.
    """
    details = deepcopy(getattr(parked, "trigger_event_details", None) or {})
    resume: Dict[str, Any] = {"execution_id": str(parked.id)}
    cli_session = getattr(parked, "cli_session", None)
    if isinstance(cli_session, dict) and cli_session.get("session_id"):
        resume["cli_session"] = cli_session
    details["_resume"] = resume

    payload = details.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    children = payload.get(CHILDREN_PAYLOAD_KEY)
    if not isinstance(children, dict):
        children = {}
    for outcome in outcomes:
        children[outcome.execution_id] = outcome.record
    payload[CHILDREN_PAYLOAD_KEY] = children
    details["payload"] = payload

    chain = details.get(CHILDREN_KEY)
    history = list(chain.get("history", [])) if isinstance(chain, dict) else []
    history.append(
        {
            "wait_id": str(getattr(parked, "park_request_id", "") or ""),
            "children": [outcome.execution_id for outcome in outcomes],
            "expired": [
                outcome.execution_id for outcome in outcomes if outcome.expired
            ],
        }
    )
    details[CHILDREN_KEY] = {
        "resumed_from_execution_id": str(parked.id),
        "consumed_seconds": _chain_compute_seconds(parked),
        "native_resume": "cli_session" in resume,
        "history": history[-_MAX_HISTORY:],
    }
    details[CHILDREN_PROMPT_KEY] = children_prompt_block(outcomes)
    return details


def _chain_compute_seconds(parked: Any) -> int:
    """Agent wall clock this park chain has already spent, in seconds.

    Time spent waiting for the children is not in here, which is what makes
    the flow's timeout budget pause while parked. Prefers the column the
    orchestrator wrote at park time and falls back to the wall clock between
    the start and the park.
    """
    stored = getattr(parked, "parked_compute_seconds", None)
    if isinstance(stored, int) and stored >= 0:
        return stored
    start = _as_utc(getattr(parked, "start_time", None))
    parked_at = _as_utc(getattr(parked, "parked_at", None))
    if start is None or parked_at is None:
        return 0
    return max(0, int((parked_at - start).total_seconds()))


# --- The wait, in process then parked ------------------------------------


def _children_of(db: Any, parent: Any, account_id: Any) -> List[Any]:
    """Every execution this parent started, newest last."""
    return crud_flow_execution.get_children(
        db, parent_execution_id=parent.id, account_id=account_id
    )


def _child_summary(child: Any) -> Dict[str, Any]:
    """One still running child, small enough to hand back in a tool result."""
    flow = getattr(child, "flow", None)
    return {
        "execution_id": str(child.id),
        "flow": str(getattr(flow, "name", "") or ""),
        "label": child_label(child),
        "status": str(getattr(child, "status", "") or ""),
    }


def refusal_outcomes(db: Any, parent: Any) -> List[ChildOutcome]:
    """The run_flow calls this turn made that no rule let start.

    A refusal creates no execution row (#625: refusal is not failure and
    nothing was charged), so it cannot be found by walking the children. It
    is on the calling execution's timeline instead, written by
    ``flow_delegation_call.record_refusal_on_parent``, and it belongs in the
    resumed turn: a parent that asked for six flows and got five has to be
    able to say which one it never got.
    """
    outcomes: List[ChildOutcome] = []
    try:
        rows = crud_flow_execution_log.list_by_type(
            db, execution_id=parent.id, log_type=DELEGATION_REFUSAL_LOG_TYPE
        )
    except Exception:
        logger.exception("Could not read the refused delegations of %s", parent.id)
        return outcomes
    for row in rows:
        metadata = getattr(row, "metadata_", None)
        if not isinstance(metadata, dict):
            continue
        record = metadata.get("task")
        if not isinstance(record, dict):
            continue
        label = metadata.get("label")
        outcomes.append(
            ChildOutcome(record=record, label=str(label) if label else None)
        )
    return outcomes


def _outcomes_for(
    parent: Any, children: Sequence[Any], *, expired_ids: Sequence[str] = ()
) -> List[ChildOutcome]:
    """Completion records for every child of one parent."""
    expired = {str(item) for item in expired_ids}
    root_execution_id = getattr(parent, "root_execution_id", None) or parent.id
    outcomes: List[ChildOutcome] = []
    for child in children:
        outcomes.append(
            ChildOutcome(
                record=completion_record(
                    child,
                    flow=getattr(child, "flow", None),
                    parent_execution_id=parent.id,
                    root_execution_id=root_execution_id,
                    expired=str(child.id) in expired,
                ),
                label=child_label(child),
                expired=str(child.id) in expired,
            )
        )
    return outcomes


def parked_payload(
    *,
    wait_id: Any,
    expires_at: Optional[datetime],
    pending: Sequence[Dict[str, Any]],
    finished: Sequence[Dict[str, Any]],
) -> str:
    """The structured result a parked ``run_flow`` call returns to the agent.

    Says the opposite of a polling instruction on purpose: the run is about to
    be suspended, so working on or polling for these children is wasted turn.
    """
    payload = {
        "status": "parked_for_children",
        "wait_id": str(wait_id),
        "waiting_for": list(pending),
        "already_finished": list(finished),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "message": (
            "The flows you started are still running, so this execution is "
            "being parked: stop working, do not poll, and do not treat this "
            "as a failure. It holds no container while they run. When the "
            "last one finishes the run resumes with one completion record per "
            "child, and if the child wait deadline passes first it resumes "
            "anyway with the unfinished ones marked expired."
        ),
    }
    return json.dumps(payload)


def finished_payload(outcomes: Sequence[ChildOutcome]) -> str:
    """The result of a wait that never had to park: every child is terminal."""
    return json.dumps(
        {
            "status": "children_finished",
            "children": [outcome.record for outcome in outcomes],
            "message": (
                "Every flow you started finished inside the in process wait, "
                "so this execution was never parked. One A2A task record per "
                "child is above, each carrying its final state and its result "
                "artifact when it reported one."
            ),
        }
    )


async def wait_for_children(
    *,
    account_id: Any,
    parent_execution_id: Any,
    wait_seconds: Optional[int] = None,
) -> str:
    """Wait for this execution's children, in process then parked (#633).

    Called by ``run_flow`` when the agent passed ``wait``. Waits for every
    child this execution started, not only the one just created: a fan out is
    started with several calls and waited for once, and a parent that parked
    on one child while another ran would have to park twice.

    Args:
        account_id: Account of the calling execution.
        parent_execution_id: The calling execution, from its runtime identity.
        wait_seconds: Override for the in process window, for tests.

    Returns:
        A JSON result for the agent: the completion records when everything
        finished inside the window, the park notice when the run is being
        suspended, or an explanation when the wait is not available.

    Raises:
        ChildWaitUnavailableError: The caller is not a live flow execution.
    """
    from preloop.models.db.session import get_session_factory

    session_factory = get_session_factory()
    window = in_process_wait_seconds() if wait_seconds is None else max(0, wait_seconds)
    deadline = time.monotonic() + window
    while True:
        with session_factory() as db:
            parent = crud_flow_execution.get(
                db, id=str(parent_execution_id), account_id=str(account_id)
            )
            if parent is None:
                raise ChildWaitUnavailableError(
                    "waiting for children is only available inside a flow execution"
                )
            flow = crud_flow.get(db, id=str(parent.flow_id))
            if flow is None:
                raise ChildWaitUnavailableError(
                    "waiting for children is only available inside a flow execution"
                )
            children = _children_of(db, parent, flow.account_id)
            if not children:
                refused = refusal_outcomes(db, parent)
                if refused:
                    return finished_payload(refused)
                return json.dumps(
                    {
                        "status": "no_children",
                        "message": (
                            "This execution has not started any flow, so there "
                            "is nothing to wait for."
                        ),
                    }
                )
            pending = [
                child for child in children if not is_terminal_child(child.status)
            ]
            if not pending:
                return finished_payload(
                    _outcomes_for(parent, children) + refusal_outcomes(db, parent)
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _request_child_park(db, parent=parent, children=children)
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, remaining)))


def _request_child_park(db: Any, *, parent: Any, children: Sequence[Any]) -> str:
    """Write the park request and answer the agent, or explain why not.

    The first of the three durable steps. Nothing is released here: the
    orchestrator sees the request on its next poll, stops the container and
    confirms the park, exactly as it does for a human one.
    """
    wait_id = uuid.uuid4()
    expires_at = datetime.now(UTC) + timedelta(seconds=child_wait_deadline_seconds())
    pending = [
        _child_summary(child)
        for child in children
        if not is_terminal_child(child.status)
    ]
    finished = [
        _child_summary(child) for child in children if is_terminal_child(child.status)
    ]
    try:
        parked = crud_flow_execution.request_park(
            db,
            execution_id=parent.id,
            approval_request_id=wait_id,
            expires_at=expires_at,
            kind=PARK_KIND,
        )
    except Exception:
        logger.exception("Could not record the child park request for %s", parent.id)
        parked = False
    if not parked:
        logger.info(
            "Execution %s could not be parked on its children; it keeps waiting "
            "in place",
            parent.id,
        )
        return json.dumps(
            {
                "status": "wait_unavailable",
                "waiting_for": pending,
                "already_finished": finished,
                "message": (
                    "This execution could not be parked on its children (it is "
                    "already parked, or no longer live). The children keep "
                    "running: finish your turn and report the coverage you "
                    "have, naming the executions that were still running."
                ),
            }
        )
    logger.info(
        "Execution %s parked on %s child execution(s) (wait %s, expires %s)",
        parent.id,
        len(pending),
        wait_id,
        expires_at,
    )
    return parked_payload(
        wait_id=wait_id, expires_at=expires_at, pending=pending, finished=finished
    )


# --- The resume, claimed exactly once ------------------------------------


async def notify_parent_child_finished(
    child_execution_id: Any,
) -> Optional[ChildParkResume]:
    """A child reached a terminal status: resume its parent if it is ready.

    Called from the orchestrator's terminal notification, the same place the
    finished webhook is emitted. Never raises: a child that finished must not
    fail because of what its parent does next, and the sweep retries.
    """
    from preloop.models.db.session import get_session_factory

    try:
        session_factory = get_session_factory()
        with session_factory() as db:
            child = crud_flow_execution.get(db, id=str(child_execution_id))
            if child is None:
                return None
            parent_id = getattr(child, "parent_execution_id", None)
            if parent_id is None:
                return None
    except Exception:
        logger.exception(
            "Could not read the parent of finished execution %s", child_execution_id
        )
        return None
    try:
        return await resume_parent_if_ready(parent_id)
    except Exception:
        logger.exception(
            "Could not resume the parent of finished execution %s; the sweep "
            "will retry",
            child_execution_id,
        )
        return None


async def resume_parent_if_ready(
    parent_execution_id: Any, *, now: Optional[datetime] = None
) -> Optional[ChildParkResume]:
    """Resume a parent parked on children, once every child is terminal.

    Idempotent by construction, which is the whole point: two children
    finishing in the same instant both arrive here, both build the same
    records, and the conditional claim lets exactly one of them create the
    resume execution.

    Args:
        parent_execution_id: The parked parent.
        now: Clock, for the deadline check and for tests.

    Returns:
        What was resumed, or None when the parent is not parked, is not ready,
        or was claimed by somebody else.
    """
    from preloop.models.db.session import get_session_factory

    moment = now or datetime.now(UTC)
    session_factory = get_session_factory()
    with session_factory() as db:
        parent = crud_flow_execution.get(db, id=str(parent_execution_id), refresh=True)
        if parent is None or str(parent.status) != WAITING_FOR_CHILDREN:
            return None
        flow = crud_flow.get(db, id=str(parent.flow_id))
        if flow is None:
            logger.warning(
                "Parent %s is parked on children but has no flow; leaving it parked",
                parent.id,
            )
            return None
        children = _children_of(db, parent, flow.account_id)
        pending = [child for child in children if not is_terminal_child(child.status)]
        expires_at = _as_utc(getattr(parent, "park_expires_at", None))
        deadline_passed = expires_at is not None and expires_at <= moment
        if pending and not deadline_passed:
            return None
        expired_ids = [str(child.id) for child in pending]
        if expired_ids:
            logger.info(
                "Child wait deadline passed for execution %s with %s child(ren) "
                "still running; resuming with expired records",
                parent.id,
                len(expired_ids),
            )
        outcomes = _outcomes_for(
            parent, children, expired_ids=expired_ids
        ) + refusal_outcomes(db, parent)
        details = build_child_resume_details(parent, outcomes)
        wait_id = getattr(parent, "park_request_id", None)
        if not crud_flow_execution.claim_parked_children_for_resume(
            db, execution_id=parent.id, wait_id=wait_id
        ):
            logger.info(
                "Parent %s was already claimed for resume by another child",
                parent.id,
            )
            return None
        try:
            resume_id = await _start_resume_execution(db, flow, parent, details)
        except Exception:
            logger.exception(
                "Failed to start the resume of parent %s; releasing the claim",
                parent.id,
            )
            _release_claim(db, parent.id)
            return None
        return ChildParkResume(
            parent_execution_id=str(parent.id),
            resume_execution_id=str(resume_id),
            expired_child_ids=tuple(expired_ids),
        )


def _release_claim(db: Any, execution_id: Any) -> None:
    """Return an unconsumed claim to WAITING_FOR_CHILDREN for the sweep."""
    try:
        crud_flow_execution.release_children_claim(db, execution_id=execution_id)
    except Exception:
        logger.exception("Could not release the resume claim on %s", execution_id)


async def _start_resume_execution(
    db: Any, flow: Any, parked: Any, details: Dict[str, Any]
) -> Any:
    """Create and dispatch the execution that continues a parked parent.

    The claim is marked consumed in the same transaction as the PENDING
    insert, so a crash cannot leave a resume nobody linked or a claim the
    sweep would use twice. Dispatch happens after the commit: a failed
    dispatch must not roll that write back.

    The resume inherits the parked run's lineage (parent, root, depth) rather
    than starting a new tree: a parent that is itself somebody's child stays
    that child, its own parent keeps waiting for it, and the delegation depth
    guard is not reset by a park.
    """
    from preloop.models.schemas.flow_execution import FlowExecutionCreate
    from preloop.services.flow_execution_dispatcher import (
        dispatch_execute,
        flow_execution_worker_enabled,
    )
    from preloop.services.model_routing import prepare_execution_routing

    # Pin the model and harness of the run being continued: a native session
    # cannot be restored into a different harness.
    details = prepare_execution_routing(
        db, flow, details, source_execution=parked, pin_kind="continuation"
    )
    execution = crud_flow_execution.create(
        db,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status="PENDING",
            trigger_event_details=details,
            parent_execution_id=getattr(parked, "parent_execution_id", None),
            root_execution_id=getattr(parked, "root_execution_id", None),
            delegation_depth=int(getattr(parked, "delegation_depth", 0) or 0),
        ),
    )
    if not crud_flow_execution.mark_park_resumed(
        db,
        execution_id=parked.id,
        resume_execution_id=execution.id,
        commit=False,
    ):
        db.rollback()
        raise RuntimeError(
            f"Parked parent {parked.id} was not a live RESUMING claim; refusing "
            "to leave an unlinked resume execution"
        )
    db.commit()
    db.refresh(execution)
    try:
        crud_flow_execution.append_log(
            db,
            execution_id=parked.id,
            log_data={
                "type": "milestone",
                "message": (
                    "Resumed after its child executions finished; continued as "
                    f"execution {execution.id}"
                ),
                "metadata": {
                    "milestone": "execution_resumed",
                    "resume_execution_id": str(execution.id),
                    "wait_id": str(getattr(parked, "park_request_id", "") or ""),
                    "native_resume": bool(
                        details.get("_resume", {}).get("cli_session")
                    ),
                },
            },
        )
    except Exception:
        logger.exception(
            "Could not log the resume of parent %s as %s", parked.id, execution.id
        )
    try:
        if flow_execution_worker_enabled():
            await dispatch_execute(execution.id)
        else:
            from preloop.services.flow_trigger_service import FlowTriggerService

            await FlowTriggerService(db)._start_flow_execution(
                flow, details, None, precreated_execution=execution
            )
    except Exception:
        logger.exception(
            "Failed to dispatch the resume of parent %s as %s; the PENDING "
            "execution is committed and will not be created again",
            parked.id,
            execution.id,
        )
    logger.info(
        "Parent %s resumed as %s after its children finished (native_resume=%s)",
        parked.id,
        execution.id,
        bool(details.get("_resume", {}).get("cli_session")),
    )
    return execution.id


async def sweep_child_parks(now: Optional[datetime] = None) -> Dict[str, int]:
    """Close out parents parked on children that nothing else finished.

    The completion hook is a message, and a message can be lost: a worker that
    dies between the child's terminal write and the parent's resume leaves a
    parent nobody will ever wake. One pass over the parked rows, after
    reclaiming stale claims, resumes every parent whose children are all
    terminal and every parent whose child wait deadline has passed.
    """
    from preloop.models.db.session import get_session_factory

    moment = now or datetime.now(UTC)
    counts = {"resumed": 0, "expired": 0, "reclaimed": 0}
    session_factory = get_session_factory()
    parked_ids: List[Any] = []
    with session_factory() as db:
        try:
            counts["reclaimed"] = crud_flow_execution.reclaim_stale_children_claims(
                db, now=moment
            )
        except Exception:
            logger.exception("Could not reclaim stale child park claims")
        try:
            parked_ids = [
                row.id for row in crud_flow_execution.list_parked_on_children(db)
            ]
        except Exception:
            logger.exception("Could not list executions parked on children")
            return counts
    for parent_id in parked_ids:
        try:
            resumed = await resume_parent_if_ready(parent_id, now=moment)
        except Exception:
            logger.exception("Could not resume parent %s from the sweep", parent_id)
            continue
        if resumed is None:
            continue
        counts["resumed"] += 1
        counts["expired"] += len(resumed.expired_child_ids)
    return counts
