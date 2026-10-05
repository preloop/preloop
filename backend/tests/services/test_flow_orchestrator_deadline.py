"""``timeout_seconds`` is one wall-clock deadline measured from launch.

The monitor used to count its own 5 second sleeps from the moment monitoring
began: startup, the time spent inside status and log calls, and every retry
attempt were all outside the budget. These tests drive the orchestrator with
a fake clock in which starting the runtime and every status call take real
(fake) time, and check that one deadline covers startup, monitoring and
retries, and that nothing resets it.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from preloop.agents.base import AgentStatus
from preloop.agents.container import (
    RUNTIME_DEADLINE_CONTEXT_KEY as CONTAINER_DEADLINE_KEY,
    runtime_deadline_seconds,
)
from preloop.models.crud import crud_account, crud_flow, crud_user
from preloop.models.models import Account, Flow
from preloop.models.models.user import User
from preloop.models.schemas.flow import FlowCreate
from preloop.services import flow_orchestrator as orchestrator_module
from preloop.services.flow_orchestrator import (
    RUNTIME_DEADLINE_CONTEXT_KEY,
    RUNTIME_DEADLINE_GRACE_SECONDS,
    ExecutionDeadline,
    FlowExecutionOrchestrator,
    TimeoutBudget,
)

BUDGET = 300
SLOW_START = 120
SLOW_STATUS = 10
POLL = 5


class FakeClock:
    """A wall clock that only moves when the code under test spends time."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@pytest.fixture
def account(db_session: Session) -> Account:
    return crud_account.create(
        db_session,
        obj_in={"organization_name": f"Deadline {uuid4().hex[:8]}", "is_active": True},
    )


@pytest.fixture
def user(db_session: Session, account: Account) -> User:
    created = crud_user.create(
        db_session,
        obj_in={
            "account_id": account.id,
            "email": f"deadline_{uuid4().hex[:8]}@example.com",
            "username": f"deadline_{uuid4().hex[:8]}",
            "full_name": "Deadline",
            "is_active": True,
            "email_verified": True,
            "hashed_password": "x",
            "user_source": "local",
        },
    )
    db_session.flush()
    account.primary_user_id = created.id
    db_session.add(account)
    db_session.commit()
    return created


@pytest.fixture
def flow(db_session: Session, account: Account, user: User) -> Flow:
    return crud_flow.create(
        db=db_session,
        flow_in=FlowCreate(
            name="Deadline flow",
            trigger_event_source="github",
            trigger_event_types=["pull_request_updated"],
            prompt_template="Review: {{payload.pull_request.title}}",
            agent_type="codex",
            agent_config={},
            account_id=account.id,
            timeout_seconds=BUDGET,
        ),
        account_id=account.id,
    )


@pytest.fixture
def clock(monkeypatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", fake)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", fake.sleep)
    for name in ("_listen_for_commands", "_stream_logs_to_nats", "_cleanup_monitoring"):
        monkeypatch.setattr(FlowExecutionOrchestrator, name, AsyncMock())
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_capture_result_artifact",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        FlowExecutionOrchestrator,
        "_sync_runtime_tool_activity_metrics",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(FlowExecutionOrchestrator, "_update_commit_status", AsyncMock())
    return fake


def _orchestrator(db_session, flow) -> FlowExecutionOrchestrator:
    nats = AsyncMock()
    nats.is_connected = True
    return FlowExecutionOrchestrator(
        db=db_session,
        flow_id=flow.id,
        trigger_event_data={
            "source": "github",
            "type": "pull_request_updated",
            "event_id": f"evt_{uuid4().hex[:8]}",
            "payload": {
                "pull_request": {"title": "Add a feature", "number": 7},
                "repository": "example-org/example-repo",
            },
            "account_id": str(uuid4()),
        },
        nats_client=nats,
    )


def _slow_executor(clock: FakeClock, status_calls: List[datetime]) -> MagicMock:
    executor = MagicMock()
    executor.streams_logs_externally = False

    async def start(_context):
        clock.advance(SLOW_START)  # image pull, Job scheduling
        return f"agent-deadline-{uuid4().hex[:6]}"

    async def get_status(_reference):
        clock.advance(SLOW_STATUS)  # a slow API server
        status_calls.append(clock.now)
        return AgentStatus.RUNNING

    executor.start = AsyncMock(side_effect=start)
    executor.get_status = AsyncMock(side_effect=get_status)
    executor.get_logs = AsyncMock(return_value=[])
    executor.stop = AsyncMock()
    executor.is_stopped = AsyncMock(return_value=True)
    executor.cleanup = AsyncMock()
    executor.probe_workspace_changed = AsyncMock(return_value=None)
    return executor


def _milestones(orchestrator, name):
    return [
        entry
        for entry in orchestrator.execution_logger.get_milestones()
        if entry["milestone"] == name
    ]


@pytest.mark.asyncio
async def test_one_deadline_covers_startup_monitoring_and_retries(
    db_session, flow, clock
):
    """Slow startup, slow status calls, a transient failure and a retry.

    Attempt 1 starts (120 s) and fails transiently after 100 s; the retry
    backs off 15 s and starts again (120 s). The budget is spent by then,
    so attempt 2's monitor times out at once instead of getting 300 more
    seconds of its own.
    """
    status_calls: List[datetime] = []
    executor = _slow_executor(clock, status_calls)
    orchestrator = _orchestrator(db_session, flow)
    real_monitor = FlowExecutionOrchestrator._monitor_agent_execution
    attempts: List[dict] = []

    async def monitor(self, session_reference, agent_executor):
        deadline = self._execution_deadline(self._execution_timeout_budget())
        attempts.append(
            {"deadline": deadline, "remaining": deadline.remaining_seconds()}
        )
        if len(attempts) == 1:
            clock.advance(100)
            return {
                "status": "FAILED",
                "error_message": "Upstream model provider timed out (HTTP 504).",
                "exit_code": 1,
                "actions_taken": [],
                "mcp_usage_logs": [],
            }
        return await real_monitor(self, session_reference, agent_executor)

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(FlowExecutionOrchestrator, "_monitor_agent_execution", monitor),
        patch.object(orchestrator_module.settings, "flow_execution_max_attempts", 2),
        patch.object(
            orchestrator_module.settings, "flow_execution_retry_backoff_seconds", 15
        ),
    ):
        await orchestrator.run()

    row = orchestrator.execution_log
    db_session.refresh(row)
    launch = row.launch_requested_at
    assert launch is not None

    # Both attempts ran against the same deadline object, anchored at launch.
    assert len(attempts) == 2
    deadline = attempts[0]["deadline"]
    assert attempts[1]["deadline"] is deadline
    assert abs((deadline.anchor - launch).total_seconds()) < 5
    assert deadline.deadline == deadline.anchor + timedelta(seconds=BUDGET)
    # Startup counted before the first attempt was even monitored (the
    # anchor is the real admission time, a few ms after the fake clock's
    # start, hence the one second of slack)...
    assert attempts[0]["remaining"] <= BUDGET - SLOW_START + 1
    # ...and attempt 2 got what was left, not a fresh budget.
    assert attempts[1]["remaining"] <= 0

    assert row.status == "FAILED"
    assert row.failure_category == "timeout"
    timeouts = _milestones(orchestrator, "agent_execution_timeout")
    assert len(timeouts) == 1
    elapsed = timeouts[0]["details"]["elapsed_seconds"]
    # 120 + 100 + 15 + 120 of real (fake) time had passed: the message says
    # so instead of claiming the budget's round number.
    assert elapsed >= SLOW_START + 100 + 15 + SLOW_START - 1
    assert f"timed out after {elapsed} seconds" in row.error_message
    assert f"budget {BUDGET} seconds from launch" in row.error_message
    executor.stop.assert_awaited()


@pytest.mark.asyncio
async def test_slow_status_calls_spend_the_budget(db_session, flow, clock):
    """Time inside status calls counts: the run ends near the deadline."""
    status_calls: List[datetime] = []
    executor = _slow_executor(clock, status_calls)
    orchestrator = _orchestrator(db_session, flow)

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(orchestrator_module.settings, "flow_execution_max_attempts", 1),
    ):
        await orchestrator.run()

    row = orchestrator.execution_log
    db_session.refresh(row)
    deadline = orchestrator._deadline
    assert row.status == "FAILED"
    assert status_calls, "the monitor polled at least once"
    # Every status call happened before the deadline plus one poll cycle;
    # the old counter would have kept polling until 300 s of *sleeps*.
    last = max(status_calls)
    assert last <= deadline.deadline + timedelta(seconds=SLOW_STATUS + POLL)
    # 180 s were left after startup; at 15 s per poll that is 12 polls, not
    # the 60 the sleep counter allowed.
    assert len(status_calls) <= (BUDGET - SLOW_START) // (SLOW_STATUS + POLL) + 1
    assert len(_milestones(orchestrator, "agent_execution_timeout")) == 1


@pytest.mark.asyncio
async def test_agent_status_elapsed_is_wall_clock(db_session, flow, clock):
    """The ``agent_status`` event reports real seconds since launch."""
    status_calls: List[datetime] = []
    executor = _slow_executor(clock, status_calls)
    orchestrator = _orchestrator(db_session, flow)
    published: List[dict] = []
    original_publish = FlowExecutionOrchestrator._publish_update

    async def publish(self, event_type, payload):
        if event_type == "agent_status":
            published.append(dict(payload))
        return await original_publish(self, event_type, payload)

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(FlowExecutionOrchestrator, "_publish_update", publish),
        patch.object(orchestrator_module.settings, "flow_execution_max_attempts", 1),
    ):
        await orchestrator.run()

    assert published
    # Measured at the top of the first poll: the 120 s startup is in it.
    assert published[0]["elapsed"] >= SLOW_START - 1
    gaps = [
        b["elapsed"] - a["elapsed"]
        for a, b in zip(published, published[1:], strict=False)
    ]
    assert gaps and all(gap >= SLOW_STATUS + POLL - 1 for gap in gaps)


@pytest.mark.asyncio
async def test_retry_is_not_started_without_budget_left(db_session, flow, clock):
    """No backoff into the deadline, no attempt that could not run."""
    executor = _slow_executor(clock, [])
    orchestrator = _orchestrator(db_session, flow)
    calls: List[str] = []

    async def monitor(self, session_reference, agent_executor):
        calls.append(session_reference)
        clock.advance(BUDGET - SLOW_START - 20)
        return {
            "status": "FAILED",
            "error_message": "Upstream model provider timed out (HTTP 504).",
            "exit_code": 1,
            "actions_taken": [],
            "mcp_usage_logs": [],
        }

    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(FlowExecutionOrchestrator, "_monitor_agent_execution", monitor),
        patch.object(orchestrator_module.settings, "flow_execution_max_attempts", 3),
        patch.object(
            orchestrator_module.settings, "flow_execution_retry_backoff_seconds", 15
        ),
    ):
        await orchestrator.run()

    assert len(calls) == 1
    skipped = _milestones(orchestrator, "execution_retry_skipped_deadline")
    assert len(skipped) == 1
    assert skipped[0]["details"]["remaining_seconds"] <= 21


@pytest.mark.asyncio
async def test_kubernetes_job_deadline_is_the_remaining_budget_plus_grace(
    db_session, flow, clock
):
    executor = _slow_executor(clock, [])
    orchestrator = _orchestrator(db_session, flow)
    contexts: List[dict] = []

    async def start(context):
        contexts.append(dict(context))
        return "agent-k8s-deadline"

    executor.start = AsyncMock(side_effect=start)
    with (
        patch(
            "preloop.services.flow_orchestrator.create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(
            FlowExecutionOrchestrator,
            "_monitor_agent_execution",
            AsyncMock(
                return_value={
                    "status": "SUCCEEDED",
                    "output_summary": "ok",
                    "error_message": None,
                    "actions_taken": [],
                    "mcp_usage_logs": [],
                }
            ),
        ),
    ):
        await orchestrator.run()

    value = contexts[0][RUNTIME_DEADLINE_CONTEXT_KEY]
    assert BUDGET + RUNTIME_DEADLINE_GRACE_SECONDS - 5 <= value
    assert value <= BUDGET + RUNTIME_DEADLINE_GRACE_SECONDS
    assert runtime_deadline_seconds(contexts[0]) == value


@pytest.mark.parametrize(
    ("value", "expected"),
    [(600, 600), ("900", 900), (0, None), (-5, None), (None, None), (True, None)],
)
def test_runtime_deadline_seconds_accepts_only_positive_seconds(value, expected):
    assert runtime_deadline_seconds({CONTAINER_DEADLINE_KEY: value}) == expected


def test_runtime_deadline_context_key_is_shared():
    """The writer and the Kubernetes reader use one constant."""
    assert CONTAINER_DEADLINE_KEY is RUNTIME_DEADLINE_CONTEXT_KEY
    assert CONTAINER_DEADLINE_KEY == "runtime_deadline_seconds"
    assert runtime_deadline_seconds({CONTAINER_DEADLINE_KEY: 480}) == 480


@pytest.mark.asyncio
async def test_confirmation_nudge_backstop_uses_nudge_timeout(monkeypatch):
    """A near-deadline main run must not shorten a longer nudge Job.

    The nudge re-enters ``_start_agent_session`` on the same orchestrator.
    With 10 seconds left on the main deadline and a 600 second nudge timeout,
    the nudge Job's backstop is 600 plus grace, not 10 plus grace.
    """
    clock = FakeClock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    monkeypatch.setattr(
        orchestrator_module.settings,
        "flow_confirmation_nudge_timeout_seconds",
        600,
    )
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data={"source": "github", "payload": {}},
        nats_client=AsyncMock(),
    )
    orchestrator.execution_log = SimpleNamespace(id=uuid4())
    orchestrator._deadline = ExecutionDeadline(
        anchor=clock.now - timedelta(seconds=BUDGET - 10),
        budget_seconds=BUDGET,
    )
    assert orchestrator._deadline.remaining_seconds() == 10

    captured: List[dict] = []

    async def start(context):
        captured.append(dict(context))
        return "agent-nudge-deadline"

    executor = MagicMock()
    executor.start = AsyncMock(side_effect=start)
    base = {"agent_type": "codex", "agent_config": {}}
    with (
        patch.object(
            orchestrator_module,
            "create_executor_for_execution",
            return_value=executor,
        ),
        patch.object(
            orchestrator_module.crud_flow_execution,
            "admit_runtime_start",
            return_value=True,
        ),
    ):
        await orchestrator._start_agent_session(dict(base))
        await orchestrator._start_agent_session({**base, "confirmation_nudge": True})

    assert (
        captured[0][RUNTIME_DEADLINE_CONTEXT_KEY] == 10 + RUNTIME_DEADLINE_GRACE_SECONDS
    )
    assert (
        captured[1][RUNTIME_DEADLINE_CONTEXT_KEY]
        == 600 + RUNTIME_DEADLINE_GRACE_SECONDS
    )


def test_deadline_counts_real_time_not_just_sleeps(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    deadline = ExecutionDeadline(anchor=clock.now, budget_seconds=BUDGET)
    clock.advance(250)  # spent inside awaited calls, no sleep at all
    deadline.credit_sleep(5)
    assert deadline.elapsed_seconds() == 250
    assert deadline.remaining_seconds() == 50
    clock.advance(50)
    assert deadline.expired()


def test_timeout_message_reports_real_elapsed_seconds():
    budget = TimeoutBudget(seconds=BUDGET, source="flow")
    assert budget.timeout_message(BUDGET) == budget.timeout_message()
    message = budget.timeout_message(374)
    assert message.startswith(
        "Execution timed out after 374 seconds (budget 300 seconds from launch)"
    )
    assert "this flow's timeout budget" in message


def test_resumed_monitoring_keeps_the_launch_anchor(monkeypatch):
    """A worker that takes over reads the same anchor from the row."""
    clock = FakeClock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    launched = clock.now - timedelta(seconds=200)
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data={"source": "github", "payload": {}},
        nats_client=AsyncMock(),
    )
    execution_id = uuid4()
    row = SimpleNamespace(
        launch_requested_at=launched,
        start_time=launched - timedelta(seconds=30),
    )
    orchestrator.execution_log = SimpleNamespace(id=execution_id)
    with patch.object(
        orchestrator_module.crud_flow_execution, "get", return_value=row
    ) as get_row:
        deadline = orchestrator._execution_deadline(
            TimeoutBudget(seconds=BUDGET, source="flow")
        )
    get_row.assert_called_once_with(orchestrator.db, id=execution_id, refresh=True)
    assert deadline.anchor == launched
    assert deadline.remaining_seconds() == 100
