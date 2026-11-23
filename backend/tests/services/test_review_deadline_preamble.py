"""A bound read-only reviewer is told the time limit the monitor enforces.

The prompt is assembled before admission and the deadline is anchored at
admission, so the preamble carries a placeholder that the orchestrator fills
in when it fixes the deadline. These tests check the stated time against the
orchestrator's own ``ExecutionDeadline``, that other prompts are untouched,
and that continuation and repair sessions are told the time left.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.agents.container import RUN_DEADLINE_EPOCH_CONTEXT_KEY
from preloop.services import flow_orchestrator as orchestrator_module
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.read_only_review import (
    REVIEW_DEADLINE_PLACEHOLDER,
    review_deadline_text,
    review_output_limit_text,
    state_review_deadline,
)
from tests.agents.test_native_result_repair import _blocks, _context, _run
from tests.agents.test_native_result_repair import review as make_review
from tests.services.test_read_only_review import review  # noqa: F401 (fixture)


class Clock:
    """A wall clock the test moves."""

    def __init__(self) -> None:
        # Real time: the shell's epoch is computed from ``time.time()``.
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now


def _orchestrator(timeout_seconds: Any, admitted: datetime) -> Any:
    orch = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data={"source": "github", "payload": {}},
        nats_client=AsyncMock(),
    )
    orch.flow = SimpleNamespace(id=uuid4(), timeout_seconds=timeout_seconds)
    orch.execution_log = SimpleNamespace(
        id=uuid4(), launch_requested_at=admitted, start_time=None
    )
    return orch


async def _start(orch: Any, prompt: str, **extra: Any) -> dict[str, Any]:
    """Run ``_start_agent_session`` and return the context the runtime got."""
    captured: List[dict] = []

    async def start(context: dict[str, Any]) -> str:
        captured.append(dict(context))
        return "session"

    executor = MagicMock()
    executor.start = AsyncMock(side_effect=start)
    with (
        patch.object(
            orchestrator_module, "create_executor_for_execution", return_value=executor
        ),
        patch.object(
            orchestrator_module.crud_flow_execution,
            "admit_runtime_start",
            return_value=True,
        ),
        # Launch preflights not under test here.
        patch.object(orch, "_refuse_hosted_approval_gap", MagicMock()),
        patch.object(orch, "_refuse_invalid_hook_binding", MagicMock()),
    ):
        await orch._start_agent_session(
            {"agent_type": "codex", "agent_config": {}, "prompt": prompt, **extra}
        )
    return captured[0]


async def _bound_prompt(review: tuple[Any, Any]) -> str:  # noqa: F811
    """The preamble ``prepare_read_only_review`` builds for a bound review."""
    from preloop.services.read_only_review import prepare_read_only_review

    binding, flow = review
    context: dict[str, Any] = {"git_clone_config": flow.git_clone_config}
    with (
        patch(
            "preloop.models.crud.crud_ci_execution.authorize_dispatch",
            return_value=binding,
        ),
        patch(
            "preloop.models.crud.crud_tracker.get_by_id_and_account",
            return_value=MagicMock(),
        ),
    ):
        await prepare_read_only_review(
            MagicMock(), flow, MagicMock(), context, mint=False
        )
    return context["prompt"]


def _stated(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.mark.parametrize("startup_seconds", [0, 95])
@pytest.mark.asyncio
async def test_bound_preamble_states_the_enforced_deadline(
    review: tuple[Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    startup_seconds: int,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    admitted = clock.now - timedelta(seconds=startup_seconds)
    prompt = await _bound_prompt(review)
    assert REVIEW_DEADLINE_PLACEHOLDER in prompt
    orch = _orchestrator(600, admitted)

    context = await _start(orch, prompt)

    enforced = orch._deadline
    assert enforced.anchor == admitted
    assert enforced.deadline == admitted + timedelta(seconds=600)
    sent = context["prompt"]
    assert REVIEW_DEADLINE_PLACEHOLDER not in sent
    assert "600 seconds in total" in sent
    assert f"stopped at {_stated(enforced.deadline)} (UTC)" in sent
    assert "`date -u`" in sent
    # Slow start-up is inside the budget: the stated time does not move.
    assert _stated(admitted + timedelta(seconds=600)) in sent
    # The shell's deadline is the same instant, to the second.
    assert (
        abs(context[RUN_DEADLINE_EPOCH_CONTEXT_KEY] - enforced.deadline.timestamp())
        <= 1
    )
    head, _, rest = sent.partition("Time limit:")
    assert "Head SHA:" in head and "Base ref:" in head
    assert "never report" in rest.lower() or "Never report" in rest


@pytest.mark.asyncio
async def test_flow_without_a_limit_states_the_deployment_default(
    review: tuple[Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    monkeypatch.setattr(
        orchestrator_module.settings, "flow_execution_max_wait_seconds", 2400
    )
    orch = _orchestrator(None, clock.now)

    context = await _start(orch, await _bound_prompt(review))

    assert orch._deadline.budget_seconds == 2400
    assert "2400 seconds in total" in context["prompt"]
    assert _stated(clock.now + timedelta(seconds=2400)) in context["prompt"]


@pytest.mark.parametrize(
    "prompt",
    ["Fix the failing test.", "", "Time limit: written by the flow owner.\n"],
)
@pytest.mark.asyncio
async def test_other_prompts_are_unchanged(
    prompt: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    context = await _start(_orchestrator(600, clock.now), prompt)
    assert context["prompt"] == prompt


@pytest.mark.asyncio
async def test_placeholder_never_reaches_the_model_without_a_deadline(
    review: tuple[Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    orch = _orchestrator(600, clock.now)
    monkeypatch.setattr(
        orch, "_execution_deadline", MagicMock(side_effect=RuntimeError("no row"))
    )

    context = await _start(orch, await _bound_prompt(review))

    assert REVIEW_DEADLINE_PLACEHOLDER not in context["prompt"]
    assert "Time limit: this run has a time limit." in context["prompt"]
    assert "seconds in total" not in context["prompt"]


@pytest.mark.asyncio
async def test_confirmation_nudge_prompt_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    context = await _start(
        _orchestrator(600, clock.now), "Did it finish?", confirmation_nudge=True
    )
    assert context["prompt"] == "Did it finish?"


def test_state_review_deadline_replaces_once() -> None:
    context = {"prompt": "A\n" + REVIEW_DEADLINE_PLACEHOLDER + "B"}
    at = datetime(2026, 11, 20, 12, 10, 0, 999, tzinfo=timezone.utc)
    state_review_deadline(context, 600, at)
    assert context["prompt"] == (
        "A\n" + review_deadline_text(600, at) + review_output_limit_text(10_000) + "B"
    )
    assert "2026-11-20T12:10:00Z" in context["prompt"]
    before = context["prompt"]
    state_review_deadline(context, 900, at)
    assert context["prompt"] == before


# --- continuation and repair sessions --------------------------------------


def _prompts(tmp_path: Path) -> list[str]:
    state = tmp_path / "state"
    return [
        (state / f"prompt-{n}").read_text()
        for n in range(1, int((state / "calls").read_text()) + 1)
    ]


def test_continuation_and_repair_sessions_state_the_time_left(
    tmp_path: Path,
) -> None:
    import time

    deadline = int(time.time()) + 500
    context = _context(**{RUN_DEADLINE_EPOCH_CONTEXT_KEY: deadline})
    # Session 1 reads nothing; session 2 reads but answers off-schema;
    # session 3 repairs it.
    calls, _, _ = _run(
        tmp_path,
        _blocks(context),
        [make_review(), make_review(findings=...), make_review()],
        inspect_from=2,
    )
    assert calls == 3
    first, continuation, repair = _prompts(tmp_path)
    assert "Time left" not in first
    stamp = datetime.fromtimestamp(deadline, timezone.utc)
    for prompt in (continuation, repair):
        line = prompt.rsplit("Time left: ", 1)[1]
        left = int(line.split(" ", 1)[0])
        assert 480 <= left <= 500
        assert f"stopped at {_stated(stamp)} (UTC)" in line
        assert "`date -u`" in line
        assert "do not report a pass" in line


def test_continuations_outside_a_bound_review_are_unchanged() -> None:
    import time

    deadline = {RUN_DEADLINE_EPOCH_CONTEXT_KEY: int(time.time()) + 500}
    not_review = _context(git_clone_config={}, **deadline)
    assert "_pl_time_left_note" not in _blocks(not_review)
    no_deadline = _context()
    assert "_pl_time_left_note" not in _blocks(no_deadline)
    assert json.dumps(_blocks(not_review)) == json.dumps(
        _blocks(_context(git_clone_config={}, **deadline))
    )


# --- a queued private-runner review is leased later from the stored prompt --


@pytest.fixture
def private_review() -> tuple[Any, Any]:
    """A bound review on a named private runner pool's Codex host profile."""
    from tests.services.test_read_only_review_private_runner import (
        review as private_fixture,
    )

    binding, flow = private_fixture.__wrapped__()
    flow.custom_commands = None
    flow.timeout_seconds = 600
    flow.ai_model = None
    return binding, flow


def _delayed_lease(flow: Any, execution: Any) -> dict[str, Any]:
    """The payload ``RemoteRunnerExecutor.get_status`` leases a queued job with."""
    from preloop.agents.remote_runner import RemoteRunnerExecutor

    executor = RemoteRunnerExecutor(
        "codex",
        {},
        db=MagicMock(),
        pool=flow.runner_pool,
        account_id=flow.account_id,
        flow=flow,
        execution=execution,
    )
    with patch(
        "preloop.agents.remote_runner.flow_launch_fingerprint", return_value="fp"
    ):
        # Same call as the delayed lease: no orchestrator context, the
        # stored prompt.
        return executor._lease_payload(
            execution_id=execution.id,
            flow_id=flow.id,
            prompt=execution.resolved_input_prompt,
            flow=flow,
        )


@pytest.mark.asyncio
async def test_queued_private_runner_review_is_leased_with_the_stated_limit(
    private_review: tuple[Any, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    binding, flow = private_review
    admitted = clock.now - timedelta(seconds=30)
    prompt = await _bound_prompt(private_review)
    orch = _orchestrator(600, admitted)
    orch.flow = flow
    execution = orch.execution_log
    execution.ci_review_binding = {"head_sha": binding.head_sha}
    execution.agent_session_reference = None
    execution.trigger_event_details = {}
    # Stored before admission, as the run loop does at STARTING.
    execution.resolved_input_prompt = prompt

    async def update(status: Any = None, **fields: Any) -> None:
        for key, value in fields.items():
            setattr(execution, key, value)

    monkeypatch.setattr(orch, "_update_execution_log", update)
    # No runner is online: start() queues and the lease happens later.
    await _start(orch, prompt)

    payload = _delayed_lease(flow, execution)

    sent = payload["prompt"]
    assert REVIEW_DEADLINE_PLACEHOLDER not in sent
    assert "600 seconds in total" in sent
    assert f"stopped at {_stated(admitted + timedelta(seconds=600))} (UTC)" in sent
    assert sent.count("Time limit:") == 1
    # A host profile runs one CLI session on the runner: there is no
    # server-built script, so no continuation or repair session to inform.
    assert payload["completion_protocol"] == "host_exec"
    assert "launch" not in payload and "script" not in payload


@pytest.mark.asyncio
async def test_a_retry_keeps_the_one_stated_limit(
    review: tuple[Any, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = Clock()
    monkeypatch.setattr(orchestrator_module, "_utcnow", clock)
    orch = _orchestrator(600, clock.now)
    stored: list[str] = []

    async def update(status: Any = None, **fields: Any) -> None:
        stored.append(fields["resolved_input_prompt"])

    monkeypatch.setattr(orch, "_update_execution_log", update)
    context = {
        "agent_type": "codex",
        "agent_config": {},
        "prompt": await _bound_prompt(review),
    }
    executor = MagicMock()
    executor.start = AsyncMock(return_value="session")
    with (
        patch.object(
            orchestrator_module, "create_executor_for_execution", return_value=executor
        ),
        patch.object(
            orchestrator_module.crud_flow_execution,
            "admit_runtime_start",
            return_value=True,
        ),
        patch.object(orch, "_refuse_hosted_approval_gap", MagicMock()),
        patch.object(orch, "_refuse_invalid_hook_binding", MagicMock()),
    ):
        await orch._start_agent_session(context)
        first = context["prompt"]
        clock.now += timedelta(seconds=120)
        # A retry reuses the context; the deadline is shared.
        await orch._start_agent_session(context)

    assert context["prompt"] == first
    assert first.count("Time limit:") == 1
    # Stored once, at the first admission, and identical to what was sent.
    assert stored == [first]


def test_lease_never_carries_an_unfilled_placeholder(
    private_review: tuple[Any, Any],
) -> None:
    """A row stored before the limit was persisted still leases cleanly."""
    binding, flow = private_review
    execution = SimpleNamespace(
        id=uuid4(),
        ci_review_binding={"head_sha": binding.head_sha},
        agent_session_reference=None,
        trigger_event_details={},
        resolved_input_prompt="Head SHA: x\n" + REVIEW_DEADLINE_PLACEHOLDER + "Go.",
    )

    sent = _delayed_lease(flow, execution)["prompt"]

    assert REVIEW_DEADLINE_PLACEHOLDER not in sent
    assert "Time limit: this run has a time limit." in sent
    assert "seconds in total" not in sent


@pytest.mark.asyncio
async def test_runner_launch_never_carries_an_unfilled_placeholder() -> None:
    from preloop.agents.runner_launch import build_runner_launch
    from preloop.utils.execve_limits import prompt_transport_env

    captured: list[str] = []

    def transport(prompt: str) -> dict[str, str]:
        captured.append(prompt)
        return prompt_transport_env(prompt)

    with patch("preloop.agents.runner_launch.prompt_transport_env", transport):
        await build_runner_launch(
            {
                "agent_type": "codex",
                "agent_config": {},
                "model_identifier": "example-model",
                "model_provider": "openai",
                "account_api_token": "token",
                "prompt": "A\n" + REVIEW_DEADLINE_PLACEHOLDER + "B",
                "execution_id": str(uuid4()),
                "flow_id": str(uuid4()),
            }
        )

    assert captured and REVIEW_DEADLINE_PLACEHOLDER not in captured[0]
    assert "Time limit: this run has a time limit." in captured[0]


def test_preamble_names_the_tool_output_limit() -> None:
    """The reviewer is told the per-result limit and how to read past it."""
    context = {
        "prompt": "A\n" + REVIEW_DEADLINE_PLACEHOLDER + "B",
        "model_identifier": "example/custom",
        "model_provider": "example",
        "model_parameters": {"context_window": 128_000},
    }
    state_review_deadline(context, 600, None)
    prompt = context["prompt"]
    assert "limited to 12800 tokens" in prompt
    assert "cut in the middle" in prompt
    assert "per file or in line ranges" in prompt
    assert "reached its end" in prompt
