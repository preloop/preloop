"""Stopping a parent that is parked on the flows it started (#689).

The decision under test is "the children stop with the parent", so every
test here asserts on execution rows after a real stop: which rows are
terminal, what each one says about why it changed, and what the stopped
parent declares about the coverage it reached. Nothing is asserted on log
output, and the race is driven rather than slept on.
"""

import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.models.crud import crud_flow, crud_flow_execution
from preloop.models.models.flow_execution import STOP_COVERAGE_KEY
from preloop.models.schemas.flow import FlowCreate
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.services import flow_tree_stop
from preloop.services.flow_child_wait import (
    resume_parent_if_ready,
    sweep_child_parks,
)

pytestmark = pytest.mark.asyncio


# --- harness ---------------------------------------------------------------


@contextmanager
def _one_session(db_session):
    """Hand every module the test transaction instead of a new session."""
    yield db_session


@pytest.fixture(autouse=True)
def _module_sessions(db_session, monkeypatch):
    monkeypatch.setattr(
        "preloop.models.db.session.get_session_factory",
        lambda: (lambda: _one_session(db_session)),
    )


@pytest.fixture(autouse=True)
def _no_nats():
    """The stop path talks to NATS best effort; nothing here needs a broker."""
    with (
        patch(
            "preloop.sync.services.event_bus.get_nats_client",
            new=AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "preloop.services.flow_orchestrator.FlowExecutionOrchestrator.send_command",
            new_callable=AsyncMock,
        ) as send_command,
    ):
        yield send_command


def _flow(db_session, account_id, name):
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name=name,
            prompt_template="do the thing",
            agent_type="codex",
            agent_config={},
            allowed_mcp_tools=[{"tool_name": "run_flow"}],
        ),
        account_id=account_id,
    )


def _execution(db_session, flow, *, parent=None, status="RUNNING", details=None, **kw):
    execution = crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id,
            status=status,
            trigger_event_details=details,
            parent_execution_id=parent.id if parent is not None else None,
            root_execution_id=(
                (parent.root_execution_id or parent.id) if parent is not None else None
            ),
            delegation_depth=(
                int(parent.delegation_depth or 0) + 1 if parent is not None else 0
            ),
            **kw,
        ),
    )
    db_session.flush()
    return execution


def _child(db_session, flow, parent, *, status="RUNNING", label=None, cost=None, **kw):
    child = _execution(
        db_session,
        flow,
        parent=parent,
        status=status,
        details={
            "source": "flow_delegation",
            "payload": {},
            "delegation": {
                "parent_execution_id": str(parent.id),
                "label": label,
                "depth": 1,
            },
        },
        **kw,
    )
    if cost is not None:
        child.estimated_cost = cost
        db_session.flush()
    return child


def _park(db_session, parent, *, compute_seconds=120, expires_in=3600):
    """The two durable steps before a claim: request, then confirm."""
    wait_id = uuid.uuid4()
    crud_flow_execution.request_park(
        db_session,
        execution_id=parent.id,
        approval_request_id=wait_id,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
        kind="children",
    )
    crud_flow_execution.confirm_park(
        db_session,
        execution_id=parent.id,
        compute_seconds=compute_seconds,
        kind="children",
    )
    db_session.refresh(parent)
    return wait_id


def _stop(client, execution):
    response = client.post(
        f"/api/v1/flows/executions/{execution.id}/command",
        json={"command": "stop", "payload": {}},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "stopped"}
    return response


def _coverage(db_session, parent):
    db_session.refresh(parent)
    return (parent.trigger_event_details or {}).get(STOP_COVERAGE_KEY)


@pytest.fixture
def parent_flow(db_session, test_user):
    return _flow(db_session, test_user.account_id, "Portfolio Review")


@pytest.fixture
def child_flow(db_session, test_user):
    return _flow(db_session, test_user.account_id, "Repository Review")


@pytest.fixture
def parent(db_session, parent_flow):
    return _execution(db_session, parent_flow, details={"payload": {"topic": "audit"}})


# --- the decision, applied -------------------------------------------------


async def test_stopping_a_parent_parked_on_three_children_leaves_none_running(
    client, db_session, parent, child_flow
):
    """Criterion 1: no non terminal child and no park row, on the rows."""
    children = [
        _child(db_session, child_flow, parent, label=f"shard {index}")
        for index in range(3)
    ]
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(parent)
    assert parent.status == "STOPPED"
    assert parent.end_time is not None
    for child in children:
        db_session.refresh(child)
        assert child.status == "STOPPED", child.id
        assert child.end_time is not None

    # The park is closed, not merely unclaimed: nothing lists it and the
    # deadline that would have expired it is gone.
    assert parent.park_expires_at is None
    assert parent.resume_execution_id is None
    parked_ids = {
        str(row.id) for row in crud_flow_execution.list_parked_on_children(db_session)
    }
    assert str(parent.id) not in parked_ids


async def test_a_grandchild_is_stopped_with_the_tree(
    client, db_session, parent, child_flow
):
    """A child that delegated further is not a place work survives a stop."""
    child = _child(db_session, child_flow, parent)
    grandchild = _child(db_session, child_flow, child)
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(child)
    db_session.refresh(grandchild)
    assert child.status == "STOPPED"
    assert grandchild.status == "STOPPED"


async def test_a_child_waiting_for_a_human_is_stopped_and_cannot_be_resumed(
    client, db_session, parent, child_flow
):
    """A subtree with a question in it is still a subtree the operator ended.

    The parent's own deadline leaves such a child alone (the window is the
    child's); a stop does not, and the answer that arrives afterwards must
    not restart it.
    """
    child = _child(db_session, child_flow, parent)
    approval_request_id = uuid.uuid4()
    crud_flow_execution.request_park(
        db_session,
        execution_id=child.id,
        approval_request_id=approval_request_id,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        kind="human",
    )
    crud_flow_execution.confirm_park(
        db_session, execution_id=child.id, compute_seconds=30, kind="human"
    )
    db_session.refresh(child)
    assert child.status == "WAITING_FOR_HUMAN"
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(child)
    assert child.status == "STOPPED"
    assert child.park_expires_at is None
    assert not crud_flow_execution.claim_parked_for_resume(
        db_session,
        execution_id=child.id,
        approval_request_id=approval_request_id,
    )
    db_session.refresh(child)
    assert child.status == "STOPPED"


async def test_each_stopped_child_records_why_it_changed(
    client, db_session, parent, child_flow
):
    """Criterion 6: the reason is on the row, so the tree can show it."""
    child = _child(db_session, child_flow, parent)
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(child)
    assert child.stop_source == "parent_stop"
    assert str(parent.id) in (child.stop_reason or "")
    assert "was stopped by an operator" in (child.stop_reason or "")
    # Durable intent too, so a runtime that outlives this request is torn
    # down by the same poll an account halt uses.
    assert child.stop_requested_at is not None

    response = client.get(f"/api/v1/flows/executions/{parent.id}/tree")
    assert response.status_code == 200, response.text
    rows = {row["id"]: row for row in response.json()["executions"]}
    assert rows[str(child.id)]["status"] == "STOPPED"
    assert str(parent.id) in rows[str(child.id)]["stop_reason"]


async def test_a_child_that_already_finished_keeps_its_status_and_cost(
    client, db_session, parent, child_flow
):
    """Discarding work is the cost of this decision; rewriting it is not."""
    finished = _child(db_session, child_flow, parent, status="SUCCEEDED", cost=0.25)
    finished.result = {"summary": "clean"}
    running = _child(db_session, child_flow, parent)
    db_session.flush()
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(finished)
    db_session.refresh(running)
    assert finished.status == "SUCCEEDED"
    assert float(finished.estimated_cost) == 0.25
    assert finished.result == {"summary": "clean"}
    assert finished.stop_reason is None
    assert running.status == "STOPPED"

    coverage = _coverage(db_session, parent)
    assert coverage["counts"] == {
        "completed": 1,
        "stopped": 1,
        "finished_first": 0,
        "left_running": 0,
    }


# --- the race --------------------------------------------------------------


async def test_a_child_finishing_during_the_stop_resumes_nothing_and_is_kept(
    client, db_session, parent, child_flow
):
    """Criterion 2: the completion races the stop, from inside the stop.

    The child finishes and notifies its parent after the park is closed and
    while the walk is still running, which is the window the design has to
    survive: it must neither resume a stopped parent nor lose its own
    terminal state and cost.
    """
    first = _child(db_session, child_flow, parent, label="shard 0")
    racer = _child(db_session, child_flow, parent, label="shard 1")
    _park(db_session, parent)

    raced = {}

    async def _finish_the_racer(db, execution, *, nats_client=None):
        if raced:
            return
        racer.status = "SUCCEEDED"
        racer.estimated_cost = 0.42
        racer.result = {"findings": 3}
        db_session.flush()
        raced["resume"] = await resume_parent_if_ready(parent.id)

    with patch.object(flow_tree_stop, "_signal_runtime", new=_finish_the_racer):
        _stop(client, parent)

    # Nothing resumed the stopped parent.
    assert raced["resume"] is None
    db_session.refresh(parent)
    assert parent.status == "STOPPED"
    assert parent.resume_execution_id is None

    # And nothing was lost: the racer keeps its own outcome and its cost.
    db_session.refresh(racer)
    db_session.refresh(first)
    assert racer.status == "SUCCEEDED"
    assert float(racer.estimated_cost) == 0.42
    assert racer.result == {"findings": 3}
    assert first.status == "STOPPED"

    coverage = _coverage(db_session, parent)
    assert coverage["counts"]["stopped"] == 1
    assert coverage["counts"]["left_running"] == 0
    assert coverage["tree_cost_usd"] == pytest.approx(0.42)
    by_id = {row["execution_id"]: row for row in coverage["children"]}
    assert by_id[str(racer.id)]["status"] == "SUCCEEDED"
    assert by_id[str(racer.id)]["cost_usd"] == pytest.approx(0.42)


async def test_a_child_that_goes_terminal_mid_write_is_not_overwritten(
    client, db_session, parent, child_flow
):
    """The same race one step later: the row changed after the walk read it.

    The walk holds a row it read as running; by the time it writes, the
    child has committed a terminal state from another process. The write is
    conditional, so it matches nothing, and the coverage reports the child as
    having finished during the stop rather than as stopped by it.
    """
    from preloop.models.models.flow_execution import FlowExecution

    first = _child(db_session, child_flow, parent, label="shard 0")
    racer = _child(db_session, child_flow, parent, label="shard 1")
    _park(db_session, parent)

    raced = {}

    async def _finish_the_racer(db, execution, *, nats_client=None):
        if raced:
            return
        raced["done"] = True
        # Bypass the identity map on purpose: another process committed
        # this, so the row the walk is holding is stale, exactly as it is
        # in production.
        db_session.query(FlowExecution).filter(FlowExecution.id == racer.id).update(
            {"status": "SUCCEEDED", "estimated_cost": 0.42},
            synchronize_session=False,
        )
        db_session.flush()

    with patch.object(flow_tree_stop, "_signal_runtime", new=_finish_the_racer):
        _stop(client, parent)

    db_session.refresh(racer)
    db_session.refresh(first)
    assert racer.status == "SUCCEEDED"
    assert float(racer.estimated_cost) == 0.42
    assert racer.stop_reason is None
    assert first.status == "STOPPED"

    coverage = _coverage(db_session, parent)
    assert coverage["counts"]["finished_first"] == 1
    assert coverage["counts"]["stopped"] == 1
    by_id = {row["execution_id"]: row for row in coverage["children"]}
    assert by_id[str(racer.id)]["outcome"] == "finished_first"
    assert by_id[str(racer.id)]["status"] == "SUCCEEDED"
    # The cost it ended with, not the cost the walk read before the race.
    assert by_id[str(racer.id)]["cost_usd"] == pytest.approx(0.42)
    assert coverage["tree_cost_usd"] == pytest.approx(0.42)


async def test_a_resume_that_won_the_claim_is_stopped_with_the_tree(
    client, db_session, parent, child_flow
):
    """The other order: the last child resumed the parent, then stop arrives.

    The resume is this parent carrying on, so it is part of what the operator
    stopped. It is not a child of the parked row (it carries that row's own
    lineage), which is why the walk cannot find it and the park link is
    followed instead.
    """
    child = _child(db_session, child_flow, parent, status="SUCCEEDED", cost=0.1)
    _park(db_session, parent)

    with (
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new_callable=AsyncMock,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.model_routing.prepare_execution_routing",
            side_effect=lambda db, flow, details, **kwargs: details,
        ),
    ):
        resumed = await resume_parent_if_ready(parent.id)
    assert resumed is not None

    db_session.refresh(parent)
    assert parent.resume_execution_id is not None
    resume_id = parent.resume_execution_id

    _stop(client, parent)

    resume = crud_flow_execution.get(db_session, id=str(resume_id))
    db_session.refresh(resume)
    assert resume.status == "STOPPED"
    assert str(parent.id) in (resume.stop_reason or "")
    db_session.refresh(child)
    assert child.status == "SUCCEEDED"


# --- what the parent says, and what happens next ---------------------------


async def test_the_stopped_parent_declares_its_coverage_and_the_tree_cost(
    client, db_session, parent, child_flow
):
    """Criterion 3: coverage and cost on the record, not in a log line."""
    parent.estimated_cost = 0.5
    db_session.flush()
    done = _child(db_session, child_flow, parent, status="SUCCEEDED", cost=0.25)
    failed = _child(db_session, child_flow, parent, status="FAILED", cost=0.05)
    running = _child(db_session, child_flow, parent, cost=0.2)
    _park(db_session, parent)

    _stop(client, parent)

    coverage = _coverage(db_session, parent)
    assert coverage["decision"] == "stop_children_with_parent"
    assert coverage["schema_version"] == 1
    assert coverage["children_total"] == 3
    assert coverage["counts"]["completed"] == 2
    assert coverage["counts"]["stopped"] == 1
    assert coverage["counts"]["left_running"] == 0
    assert coverage["own_cost_usd"] == pytest.approx(0.5)
    assert coverage["tree_cost_usd"] == pytest.approx(1.0)
    assert coverage["truncated"] is False

    by_id = {row["execution_id"]: row for row in coverage["children"]}
    assert by_id[str(done.id)]["outcome"] == "completed"
    assert by_id[str(failed.id)]["outcome"] == "completed"
    assert by_id[str(failed.id)]["status"] == "FAILED"
    assert by_id[str(running.id)]["outcome"] == "stopped"
    assert by_id[str(running.id)]["flow_name"] == "Repository Review"


async def test_the_sweep_does_not_resume_a_stopped_parent(
    client, db_session, parent, child_flow
):
    """Criterion 4: run the sweep after the stop and assert it did nothing."""
    child = _child(db_session, child_flow, parent)
    _park(db_session, parent)

    _stop(client, parent)

    counts = await sweep_child_parks()
    assert counts["resumed"] == 0

    db_session.refresh(parent)
    db_session.refresh(child)
    assert parent.status == "STOPPED"
    assert parent.resume_execution_id is None
    assert child.status == "STOPPED"

    # A late notification from the child is refused by the same claim.
    assert await resume_parent_if_ready(parent.id) is None
    db_session.refresh(parent)
    assert parent.status == "STOPPED"


async def test_stopping_a_parent_with_no_children_is_the_stop_it_always_was(
    client, db_session, parent
):
    """Criterion 5: nothing about a childless stop changes."""
    before = dict(parent.trigger_event_details or {})

    _stop(client, parent)

    db_session.refresh(parent)
    assert parent.status == "STOPPED"
    assert parent.error_message == "Manually stopped by user"
    assert parent.trigger_event_details == before
    assert STOP_COVERAGE_KEY not in (parent.trigger_event_details or {})
    assert parent.stop_source is None


async def test_a_run_that_never_waited_for_its_children_is_not_a_tree_stop(
    client, db_session, parent, child_flow
):
    """The bound of this issue: a stop cascades from a park, not from lineage.

    A run that started flows and never waited for them is not parked on
    anything, so stopping it is the single execution stop it has always
    been. Whether fire and forget children should also be stopped is a
    product question this issue does not open.
    """
    child = _child(db_session, child_flow, parent)

    _stop(client, parent)

    db_session.refresh(parent)
    db_session.refresh(child)
    assert parent.status == "STOPPED"
    assert child.status == "RUNNING"
    assert _coverage(db_session, parent) is None


# --- the pieces, directly --------------------------------------------------


async def test_the_walk_is_bounded_and_says_so(
    db_session, test_user, parent, child_flow
):
    """A walk over data is bounded rather than trusted."""
    for _ in range(3):
        _child(db_session, child_flow, parent)

    with patch.object(flow_tree_stop, "MAX_TREE_NODES", 2):
        rows, truncated = flow_tree_stop.collect_subtree(
            db_session, parent=parent, account_id=test_user.account_id
        )

    assert len(rows) == 2
    assert truncated is True


async def test_a_runner_backed_child_is_flagged_to_halt(
    client, db_session, test_user, parent, child_flow
):
    """A runner's process is not a container: it is asked to halt its job."""
    from preloop.models import models

    runner = models.FlowRunner(
        account_id=test_user.account_id,
        name="halt-runner",
        token_hash="local-test",
        status="busy",
        reported_status="RUNNING",
    )
    db_session.add(runner)
    db_session.flush()
    child = _child(db_session, child_flow, parent)
    child.agent_session_reference = f"runner:{runner.id}:{child.id}"
    runner.current_execution_id = child.id
    db_session.flush()
    _park(db_session, parent)

    _stop(client, parent)

    db_session.refresh(runner)
    db_session.refresh(child)
    assert runner.halt_requested is True
    assert child.status == "STOPPED"


def test_the_terminal_set_matches_the_delegation_rollup():
    """Two spellings of "this execution cannot change any more", pinned.

    The CRUD layer cannot import a service and the rollup cannot import the
    CRUD constant without a cycle, so the sets are written twice. A stop that
    treated a status as live while the rollup treated it as finished would
    rewrite a terminal row.
    """
    from preloop.services.flow_delegation_budget import TERMINAL_STATUSES

    assert crud_flow_execution.TERMINAL_EXECUTION_STATUSES == TERMINAL_STATUSES
