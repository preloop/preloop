"""What happens to the children when an operator stops their parent (#689).

#633 gave a parent the ability to park on the flows it started. It left one
question open, deliberately: an operator stops a parent that is parked on
three running children. Do the children stop with it, or do they finish and
have their results reaped into the stopped parent's record?

**The decision implemented here is: the children stop with the parent.**

The reason is money and the reason is trust, and they point the same way. An
operator who stops a tree stops it because it is costing too much or because
it is doing the wrong thing; a stop that leaves the expensive half of the
work running is not the stop anybody asked for. The alternative, letting the
children finish and reaping them, keeps work already paid for, but it keeps
it for a parent that will never read it: the parked run's agent session is
gone, nothing resumes a stopped execution, and the results would land in a
record nobody is waiting on while the spend keeps climbing with no ceiling an
operator can see. Discarded work is a cost the operator chose. Spend after a
stop is a cost the operator was not told about.

What the operator sees: the parent goes ``STOPPED`` with a coverage record
saying which children had finished, which this stop ended and what the tree
had cost at that moment; each child this stop ended goes ``STOPPED`` with a
``stop_reason`` naming the parent, which is what the execution tree renders.
A child that had already finished keeps its result, its status and its cost,
including one that finished while the stop was in flight.

The concurrency story is one conditional UPDATE per row, the same pattern the
park uses:

* The parent leaves ``WAITING_FOR_CHILDREN`` before any child is touched, so
  a child finishing at that instant cannot claim a park that is already
  closed, and the sweep never sees the row again.
* If that child won the race and a resume execution already exists, the
  resume is stopped as part of the tree: the tree is what the operator
  stopped, and a resume is the parent carrying on.
* Every child write is conditional on the child not being terminal, so a
  child that finished first keeps everything it recorded.

Not here, on purpose: how one execution is stopped (unchanged, this reuses
the durable stop intent the account kill switch already writes), partial
stops, restarting a stopped tree, and any billing adjustment. A child that
was stopped was still paid for.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from preloop.models.crud import crud_flow_execution, crud_flow_runner
from preloop.models.models.flow_execution import STOP_COVERAGE_KEY
from preloop.services.runner_service import runner_id_from_session_reference

logger = logging.getLogger(__name__)

#: The decision this module implements, recorded on every stopped tree so a
#: record written today still says which semantics produced it.
DECISION = "stop_children_with_parent"

#: Park vocabulary this module reads, from the same closed set the CRUD layer
#: writes (``crud_flow_execution.PARK_KIND_CHILDREN``).
PARK_KIND_CHILDREN = "children"
WAITING_FOR_CHILDREN_STATUS = "WAITING_FOR_CHILDREN"

#: Shape version of the coverage block, for the same reason.
COVERAGE_SCHEMA_VERSION = 1

#: Largest tree one stop walks. The depth and fan out caps already bound a
#: tree far below this, but they are settings, and a walk that trusts a
#: setting has no bound at all. A tree past the cap is stopped as far as the
#: walk got and says so in ``truncated``, rather than being walked forever.
MAX_TREE_NODES = 1000

#: Hard stop on the depth of the walk, independent of the delegation depth
#: cap: lineage cannot loop, but a walk over data is bounded rather than
#: trusted.
MAX_TREE_DEPTH = 32

#: Most per child rows the coverage record carries. Beyond this the counts
#: and the totals are still exact; only the itemised list is cut.
MAX_COVERAGE_CHILDREN = 200


def is_terminal_status(status: Any) -> bool:
    """True when an execution can no longer change on its own."""
    return str(status or "").upper() in crud_flow_execution.TERMINAL_EXECUTION_STATUSES


def parked_on_children(execution: Any) -> bool:
    """Whether this execution is (or is about to be) parked on its children.

    True from the moment ``run_flow(wait=true)`` writes the park request,
    which is before the orchestrator has released the runtime, and stays
    true while the park is claimed for a resume. That whole window is what
    "a parent parked on children" means to an operator pressing stop; a run
    that started flows and never waited for them is not in it, and its stop
    is the single execution stop it has always been.
    """
    return (
        str(getattr(execution, "park_kind", "") or "") == PARK_KIND_CHILDREN
        or str(getattr(execution, "status", "") or "") == WAITING_FOR_CHILDREN_STATUS
    )


def stop_reason_for(parent_execution_id: Any) -> str:
    """What a row this stop changed says about why it changed.

    One sentence, on the row itself rather than in a log: the execution tree
    renders it next to the state, and an operator reading a child three days
    later should not have to find the parent to learn why it ended.
    """
    return (
        "Stopped because the execution that started it "
        f"({parent_execution_id}) was stopped by an operator"
    )


def _execution_cost(execution: Any) -> float:
    """What one execution has cost so far, in USD, never negative."""
    try:
        return max(0.0, float(getattr(execution, "estimated_cost", 0) or 0))
    except (TypeError, ValueError):  # pragma: no cover - column is Numeric
        return 0.0


def collect_subtree(db: Any, *, parent: Any, account_id: Any) -> Tuple[List[Any], bool]:
    """Every execution under one parent, breadth first, bounded.

    Walks ``parent_execution_id`` rather than matching the lineage root: the
    parent may itself be somebody's child, and stopping it must not reach its
    siblings. Returns the rows and whether the walk hit a bound, so a partial
    answer can be reported as partial instead of read as complete.
    """
    rows: List[Any] = []
    seen = {str(parent.id)}
    frontier = [parent]
    truncated = False
    for _ in range(MAX_TREE_DEPTH):
        if not frontier:
            break
        next_frontier: List[Any] = []
        for node in frontier:
            try:
                children = list(
                    crud_flow_execution.get_children(
                        db, parent_execution_id=node.id, account_id=account_id
                    )
                )
            except Exception:
                logger.exception("Could not read the children of execution %s", node.id)
                truncated = True
                continue
            for child in children:
                key = str(child.id)
                if key in seen:
                    continue
                seen.add(key)
                if len(rows) >= MAX_TREE_NODES:
                    truncated = True
                    return rows, truncated
                rows.append(child)
                next_frontier.append(child)
        frontier = next_frontier
    if frontier:
        truncated = True
    return rows, truncated


def _resume_execution_of(db: Any, parent: Any) -> Optional[Any]:
    """The execution that claimed this parent's park, if one won the race.

    A resume is not a child (it carries the parked run's own lineage, not a
    link to it), so the subtree walk cannot find it. It is still part of what
    the operator stopped: it is this run, carrying on.
    """
    resume_id = getattr(parent, "resume_execution_id", None)
    if resume_id is None:
        return None
    try:
        return crud_flow_execution.get(db, id=str(resume_id))
    except Exception:
        logger.exception("Could not read the resume execution of %s", parent.id)
        return None


async def _signal_runtime(db: Any, execution: Any, *, nats_client: Any = None) -> None:
    """Best effort teardown of a runtime that may still be alive.

    The durable part of the stop is already written (``stop_requested_at`` on
    the row, which the orchestrator loop and the runner both poll, exactly as
    they do for an account halt). This only makes it prompt: a runner is
    flagged to halt its job and a container backed run is sent the same NATS
    stop command a single execution stop sends. Neither is allowed to fail
    the stop: the row is terminal either way and the poll is the guarantee.
    """
    session_reference = getattr(execution, "agent_session_reference", None)
    runner_id = runner_id_from_session_reference(session_reference)
    if runner_id is not None:
        try:
            runner = crud_flow_runner.get(db, id=runner_id)
            if runner is not None:
                runner.halt_requested = True
                db.add(runner)
                db.commit()
        except Exception:
            logger.exception(
                "Could not flag the runner of execution %s to halt", execution.id
            )
        return
    try:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        await FlowExecutionOrchestrator.send_command(
            execution_id=str(execution.id),
            command="stop",
            payload=None,
            nats_client=nats_client,
        )
    except Exception as error:
        logger.warning(
            "Could not send the stop command for execution %s: %s",
            execution.id,
            error,
        )


def _record_on_child(db: Any, execution: Any, *, parent_execution_id: Any) -> None:
    """Put the reason on the child's own timeline as well as its row."""
    try:
        crud_flow_execution.append_log(
            db,
            execution_id=str(execution.id),
            log_data={
                "type": "milestone",
                "message": stop_reason_for(parent_execution_id),
                "metadata": {
                    "milestone": "stopped_with_parent",
                    "parent_execution_id": str(parent_execution_id),
                    "decision": DECISION,
                },
            },
        )
    except Exception:
        logger.exception("Could not log the parent stop on execution %s", execution.id)


def _child_row(execution: Any, *, outcome: str, status: str) -> Dict[str, Any]:
    """One line of the coverage record."""
    flow = getattr(execution, "flow", None)
    return {
        "execution_id": str(execution.id),
        "flow_name": str(getattr(flow, "name", "") or "") or None,
        "status": status,
        "outcome": outcome,
        "cost_usd": round(_execution_cost(execution), 4),
    }


def build_coverage(
    *,
    decided_at: datetime,
    rows: Sequence[Dict[str, Any]],
    own_cost_usd: float,
    tree_cost_usd: float,
    truncated: bool,
) -> Dict[str, Any]:
    """The coverage a stopped parent declares, as stored on its own record.

    Counts and totals are over the whole walk; the itemised list is capped,
    because a record is read by a person and a thousand rows is a table, not
    a record. ``finished_first`` is its own outcome rather than being folded
    into ``completed``: a child that beat the stop by a second is the case
    this design is most often asked about.
    """
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    return {
        "schema_version": COVERAGE_SCHEMA_VERSION,
        "decision": DECISION,
        "stopped_at": decided_at.isoformat(),
        "children_total": len(rows),
        "counts": {
            "completed": counts.get("completed", 0),
            "stopped": counts.get("stopped", 0),
            "finished_first": counts.get("finished_first", 0),
            "left_running": counts.get("left_running", 0),
        },
        "children": [dict(row) for row in rows[:MAX_COVERAGE_CHILDREN]],
        "children_truncated": truncated or len(rows) > MAX_COVERAGE_CHILDREN,
        "own_cost_usd": round(own_cost_usd, 4),
        "tree_cost_usd": round(tree_cost_usd, 4),
        "truncated": truncated,
    }


async def stop_tree_for_stopped_parent(
    db: Any,
    *,
    parent: Any,
    account_id: Any,
    nats_client: Any = None,
    now: Optional[datetime] = None,
) -> Optional[Dict[str, Any]]:
    """Apply the stop to everything the stopped parent had running (#689).

    Call it with the parent already stopped, or about to be: the park is
    closed first by :func:`close_children_park`, then this walks the tree.

    Args:
        db: Session the caller owns; every write here is committed on it.
        parent: The execution an operator stopped.
        account_id: Account of that execution, for the lineage reads.
        nats_client: Connected NATS client, when the caller has one.
        now: Timestamp of the stop, for tests.

    Returns:
        The coverage record written on the parent, or None when the parent
        started nothing, in which case nothing at all is written and the stop
        is exactly the single execution stop it has always been.
    """
    moment = now or datetime.now(UTC)
    descendants, truncated = collect_subtree(db, parent=parent, account_id=account_id)
    resume = _resume_execution_of(db, parent)
    targets: List[Any] = list(descendants)
    if resume is not None:
        targets.append(resume)
    if not targets:
        return None

    reason = stop_reason_for(parent.id)
    rows: List[Dict[str, Any]] = []
    for execution in targets:
        status_before = str(getattr(execution, "status", "") or "")
        if is_terminal_status(status_before):
            rows.append(
                _child_row(execution, outcome="completed", status=status_before)
            )
            continue
        stopped = False
        try:
            stopped = crud_flow_execution.stop_for_parent_stop(
                db, execution_id=execution.id, reason=reason, now=moment
            )
        except Exception:
            logger.exception(
                "Could not stop execution %s with its parent %s",
                execution.id,
                parent.id,
            )
        if not stopped:
            # The row refused the write, so it is terminal now: either the
            # child finished while the stop was in flight, or something else
            # ended it. Read it back rather than guessing which.
            refreshed = crud_flow_execution.get(db, id=str(execution.id), refresh=True)
            status_after = str(getattr(refreshed, "status", status_before) or "")
            outcome = (
                "finished_first" if is_terminal_status(status_after) else "left_running"
            )
            if outcome == "left_running":
                logger.warning(
                    "Execution %s is still %s after its parent %s was stopped",
                    execution.id,
                    status_after,
                    parent.id,
                )
            rows.append(
                _child_row(
                    refreshed if refreshed is not None else execution,
                    outcome=outcome,
                    status=status_after,
                )
            )
            continue
        _record_on_child(db, execution, parent_execution_id=parent.id)
        await _signal_runtime(db, execution, nats_client=nats_client)
        rows.append(_child_row(execution, outcome="stopped", status="STOPPED"))

    own_cost = _execution_cost(parent)
    coverage = build_coverage(
        decided_at=moment,
        rows=rows,
        own_cost_usd=own_cost,
        # Summed from the rows rather than from the objects the walk started
        # with, so a child that finished mid walk contributes the cost it
        # ended with rather than the cost it had when the walk read it.
        tree_cost_usd=own_cost + sum(float(row["cost_usd"]) for row in rows),
        truncated=truncated,
    )
    try:
        crud_flow_execution.record_stop_coverage(
            db, execution_id=parent.id, coverage=coverage
        )
    except Exception:
        logger.exception("Could not record the stop coverage of %s", parent.id)
    try:
        crud_flow_execution.append_log(
            db,
            execution_id=str(parent.id),
            log_data={
                "type": "milestone",
                "message": (
                    "Stopped with the flows it started: "
                    f"{coverage['counts']['stopped']} stopped, "
                    f"{coverage['counts']['completed']} already finished, "
                    f"{coverage['counts']['finished_first']} finished during "
                    f"the stop; tree cost ${coverage['tree_cost_usd']:.4f}"
                ),
                "metadata": {
                    "milestone": "tree_stopped",
                    STOP_COVERAGE_KEY: coverage,
                },
            },
        )
    except Exception:
        logger.exception("Could not log the stop coverage of %s", parent.id)
    logger.info(
        "Stopped the tree of execution %s: %s child execution(s), %s stopped, "
        "tree cost $%.4f",
        parent.id,
        coverage["children_total"],
        coverage["counts"]["stopped"],
        coverage["tree_cost_usd"],
    )
    return coverage


def close_children_park(
    db: Any, *, parent: Any, now: Optional[datetime] = None
) -> bool:
    """Take a parent parked on children out of the park, terminally.

    Done before anything else in a stop, and before any I/O: from the moment
    it returns, a child reaching a terminal state cannot claim the park and
    the sweep cannot list the row. Returns True when this call closed the
    park; False when the row was not parked on children (a plain running
    execution, or a park a child had already claimed).
    """
    if str(getattr(parent, "status", "") or "") != WAITING_FOR_CHILDREN_STATUS:
        return False
    closed = crud_flow_execution.close_children_park_for_stop(
        db,
        execution_id=parent.id,
        reason="Manually stopped by user",
        now=now or datetime.now(UTC),
    )
    if closed:
        try:
            db.refresh(parent)
        except Exception:  # pragma: no cover - detached or mocked row
            logger.debug("Could not refresh %s after closing its park", parent.id)
    return closed
