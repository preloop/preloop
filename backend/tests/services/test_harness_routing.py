"""Harness-aware routing (#1481, contract B): eligibility, lease, fallback."""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.agents.base import AgentStatus
from preloop.agents.factory import create_executor_for_execution
from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.models.schemas.flow_runner import HarnessInventory
from preloop.services import runner_service
from preloop.services.host_exec import (
    HARNESS_FALLBACK_CONTEXT_KEY,
    harness_selector_config_error,
    host_exec_effective_profile,
    host_exec_flow_error,
    host_exec_harness_selector,
)
from preloop.services.runner_service import (
    harness_options,
    harness_routing_reason,
    lease_job,
    runner_eligible_for_harness,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "personal_runners"
    / "harness_inventory_copilot_signed_in.json"
)


@pytest.fixture(autouse=True)
def runtime_admission_allowed(monkeypatch):
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.admit_runtime_start",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get_stop_request",
        lambda *args, **kwargs: None,
    )


def _inventory(**changes: Any) -> Dict[str, Any]:
    """The contract fixture, validated, with the Copilot entry changed."""
    raw = json.loads(FIXTURE.read_text())
    HarnessInventory.model_validate(raw)
    inventory = copy.deepcopy(raw)
    for entry in inventory["entries"]:
        if entry["harness"] == "copilot_cli":
            entry.update(changes)
    return inventory


def _runner(
    account_id: Any,
    *,
    online: bool = True,
    inventory: Optional[Dict[str, Any]] = None,
    free_slots: int = 1,
    name: str = "jonas-laptop",
    profiles: Optional[list] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        name=name,
        labels=[],
        registered_by_user_id=uuid4(),
        status="online" if online else "offline",
        last_heartbeat=datetime.now(timezone.utc)
        - (timedelta(0) if online else timedelta(hours=2)),
        free_slots=free_slots,
        harness_inventory=_inventory() if inventory is None else inventory,
        capabilities={
            "host_exec_profiles": (
                [
                    {
                        "name": "copilot",
                        "capabilities": [
                            "host_exec",
                            "copilot_cli",
                            "stdout",
                            "cancel",
                        ],
                    }
                ]
                if profiles is None
                else profiles
            )
        },
    )


# --- eligibility matrix ---------------------------------------------------


@pytest.mark.parametrize(
    ("online", "changes", "model", "expected"),
    [
        (True, {}, None, None),
        (False, {}, None, "runner_offline"),
        (True, {"login_state": "signed_out"}, None, "harness_signed_out"),
        (True, {"login_state": "unknown"}, None, "harness_signed_out"),
        (True, {"enabled": False}, None, "harness_disabled"),
        (True, {}, "claude-sonnet-4.6", None),
        # Copilot cannot list models (no probed source): unlisted is allowed.
        (True, {}, "gpt-9-unlisted", None),
        (
            True,
            {"models": [{"id": "gpt-5.2", "source": "probed"}]},
            "gpt-9-unlisted",
            "model_not_available",
        ),
        (
            True,
            {"models": [{"id": "gpt-5.2", "source": "probed"}]},
            "gpt-5.2",
            None,
        ),
        (
            True,
            {"support_level": "presence_only"},
            None,
            "no_runner_with_harness",
        ),
    ],
)
def test_eligibility_matrix(online, changes, model, expected) -> None:
    account_id = uuid4()
    runner = _runner(account_id, online=online, inventory=_inventory(**changes))
    ok, reason, entry = runner_eligible_for_harness(
        None,
        runner,
        account_id=account_id,
        harness="copilot_cli",
        model=model,
        pinned_runner_id=runner.id,
    )
    assert ok is (expected is None)
    assert reason == expected
    if expected != "no_runner_with_harness":
        assert entry["harness"] == "copilot_cli"


def test_inventory_unknown_runner_is_never_harness_eligible() -> None:
    account_id = uuid4()
    runner = _runner(account_id)
    runner.harness_inventory = None
    ok, reason, _ = runner_eligible_for_harness(
        None,
        runner,
        account_id=account_id,
        harness="copilot_cli",
        pinned_runner_id=runner.id,
    )
    assert (ok, reason) == (False, "no_runner_with_harness")


def test_other_account_runner_is_not_eligible() -> None:
    runner = _runner(uuid4())
    ok, reason, _ = runner_eligible_for_harness(
        None,
        runner,
        account_id=uuid4(),
        harness="copilot_cli",
        pinned_runner_id=runner.id,
    )
    assert (ok, reason) == (False, "owner_has_no_runner")


def test_owner_rule_pin_pool_and_admin(monkeypatch) -> None:
    account_id = uuid4()
    runner = _runner(account_id, name="jonas-laptop")
    other = uuid4()
    # Pinned to a different runner.
    assert (
        runner_eligible_for_harness(
            None,
            runner,
            account_id=account_id,
            harness="copilot_cli",
            pinned_runner_id=other,
        )[1]
        == "owner_has_no_runner"
    )
    # An explicit pool that names the runner.
    assert (
        runner_eligible_for_harness(
            None,
            runner,
            account_id=account_id,
            harness="copilot_cli",
            pool="jonas-laptop",
        )[0]
        is True
    )
    # Auto pool: only runners registered by an account admin.
    monkeypatch.setattr(
        runner_service, "_runner_registered_by_admin", lambda *a, **k: False
    )
    assert (
        runner_eligible_for_harness(
            MagicMock(),
            runner,
            account_id=account_id,
            harness="copilot_cli",
            pool="auto",
        )[1]
        == "owner_has_no_runner"
    )
    monkeypatch.setattr(
        runner_service, "_runner_registered_by_admin", lambda *a, **k: True
    )
    assert (
        runner_eligible_for_harness(
            MagicMock(),
            runner,
            account_id=account_id,
            harness="copilot_cli",
            pool="auto",
        )[0]
        is True
    )


def test_routing_reason_names_the_furthest_runner() -> None:
    account_id = uuid4()
    signed_out = _runner(account_id, inventory=_inventory(login_state="signed_out"))
    offline = _runner(account_id, online=False)
    no_inventory = _runner(account_id)
    no_inventory.harness_inventory = None
    count, reason = harness_routing_reason(
        None,
        [no_inventory, signed_out, offline],
        account_id=account_id,
        harness="copilot_cli",
        pool="jonas-laptop",
    )
    assert (count, reason) == (0, "runner_offline")
    assert harness_routing_reason(
        None, [], account_id=account_id, harness="copilot_cli", pool="x"
    ) == (0, "owner_has_no_runner")
    assert harness_routing_reason(
        None,
        [no_inventory],
        account_id=account_id,
        harness="copilot_cli",
        pool="jonas-laptop",
    ) == (0, "no_runner_with_harness")


# --- flow config ------------------------------------------------------------


def test_selector_and_effective_profile() -> None:
    config = {"harness": "copilot_cli", "copilot_model": "auto"}
    assert host_exec_harness_selector("copilot", config) == "copilot_cli"
    assert host_exec_effective_profile("copilot", config) == "copilot"
    assert host_exec_harness_selector("cursor", config) is None
    pinned = {"harness": "copilot_cli", "host_exec_profile": "copilot-review"}
    # Explicit profile beats the inferred harness route.
    assert host_exec_harness_selector("copilot", pinned) is None
    assert host_exec_effective_profile("copilot", pinned) == "copilot-review"


@pytest.mark.parametrize(
    ("agent_type", "config", "fragment"),
    [
        ("copilot", {"harness": "cursor_cli"}, "requires agent_type cursor"),
        ("copilot", {"harness": "claude_desktop"}, "must be one of"),
        ("copilot", {"harness": "copilot_cli", "runner_id": "x"}, "UUID"),
        (
            "copilot",
            {"harness": "copilot_cli", "harness_fallback": "later"},
            "queue, fallback_server or fail",
        ),
        (
            "copilot",
            {"harness": "copilot_cli", "harness_queue_timeout_seconds": 59},
            "between 60 and 86400",
        ),
        (
            "copilot",
            {"harness": "copilot_cli", "harness_fallback": "fallback_server"},
            "requires agent_config.fallback_model_identifier",
        ),
    ],
)
def test_selector_config_errors(agent_type, config, fragment) -> None:
    assert fragment in (harness_selector_config_error(agent_type, config) or "")


def test_flow_error_accepts_harness_instead_of_profile() -> None:
    assert (
        host_exec_flow_error(
            agent_type="copilot",
            agent_config={"harness": "copilot_cli"},
            runner_pool=None,
        )
        is None
    )
    assert "hosted compute" in host_exec_flow_error(
        agent_type="copilot",
        agent_config={"harness": "copilot_cli"},
        runner_pool="server",
    )
    assert "host_exec_profile or agent_config.harness" in host_exec_flow_error(
        agent_type="copilot", agent_config={}, runner_pool=None
    )


def test_flow_schema_rejects_mismatched_harness() -> None:
    from pydantic import ValidationError

    from preloop.models.schemas.flow import FlowCreate

    with pytest.raises(ValidationError, match="requires agent_type copilot"):
        FlowCreate(
            name="x",
            prompt_template="y",
            agent_type="cursor",
            agent_config={"harness": "copilot_cli"},
        )
    flow = FlowCreate(
        name="x",
        prompt_template="y",
        agent_type="copilot",
        agent_config={
            "harness": "copilot_cli",
            "harness_fallback": "fail",
            "harness_queue_timeout_seconds": 600,
        },
    )
    assert flow.agent_config["harness"] == "copilot_cli"


# --- lease payload ------------------------------------------------------------


def _executor(config: Dict[str, Any], *, pool: str = "auto") -> RemoteRunnerExecutor:
    flow = SimpleNamespace(
        agent_type="copilot",
        agent_config=config,
        runner_pool=None,
        git_clone_config=None,
        custom_commands=None,
        timeout_seconds=None,
        ai_model=None,
        account_id=uuid4(),
    )
    return RemoteRunnerExecutor(
        "copilot", config, db=MagicMock(), pool=pool, account_id=uuid4(), flow=flow
    )


def test_lease_payload_carries_harness_selection_and_no_argv() -> None:
    executor = _executor({"harness": "copilot_cli", "copilot_model": "gpt-5.2"})
    payload = executor._lease_payload(
        execution_id=uuid4(), flow_id=uuid4(), prompt="review"
    )
    assert payload["host_exec"] == {
        "harness": "copilot_cli",
        "profile": "copilot",
        "model": "gpt-5.2",
    }
    assert payload["host_exec_profile"] == "copilot"
    assert payload["agent_config"] == {"host_exec_profile": "copilot"}
    assert payload["completion_protocol"] == "host_exec"
    dumped = json.dumps(payload)
    for forbidden in ("argv", "executable", "account_api_token", "image"):
        assert forbidden not in dumped


def test_profile_pinned_956_flow_lease_is_unchanged() -> None:
    """A #956 flow pinned by profile name routes exactly as before."""
    executor = _executor(
        {"host_exec_profile": "copilot-review", "harness": "copilot_cli"}
    )
    payload = executor._lease_payload(
        execution_id=uuid4(), flow_id=uuid4(), prompt="review"
    )
    assert payload["host_exec_profile"] == "copilot-review"
    assert "host_exec" not in payload


def test_lease_job_routes_harness_to_eligible_runner_only(monkeypatch) -> None:
    account_id = uuid4()
    signed_out = _runner(
        account_id, inventory=_inventory(login_state="signed_out"), free_slots=3
    )
    eligible = _runner(account_id, inventory=_inventory(billing="seat"))
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kw: [signed_out, eligible]
    )

    def _claim(db, *, runner_id):
        assert runner_id == eligible.id, "must not claim a signed-out runner"
        return eligible

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim)
    monkeypatch.setattr(runner_service, "emit_runner_updated", lambda *a, **k: None)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kw: SimpleNamespace(reported_status=None, **kw),
    )
    outcome: Dict[str, Any] = {}
    result = lease_job(
        MagicMock(),
        account_id=account_id,
        pool="jonas-laptop",
        execution_id=uuid4(),
        payload={
            "host_exec_profile": "copilot",
            "agent_type": "copilot",
            "model_identifier": "not-in-profile-list",
            "completion_protocol": "host_exec",
            "host_exec": {
                "harness": "copilot_cli",
                "profile": "copilot",
                "model": "not-in-profile-list",
            },
        },
        outcome=outcome,
    )
    assert result is eligible
    assert outcome == {"billing_mode": "seat"}


def test_lease_job_names_the_reason_when_queued(monkeypatch) -> None:
    account_id = uuid4()
    offline = _runner(account_id, online=False)
    monkeypatch.setattr(
        crud_flow_runner,
        "find_matching",
        lambda db, **kw: [] if kw.get("online_only", True) else [offline],
    )
    outcome: Dict[str, Any] = {}
    assert (
        lease_job(
            MagicMock(),
            account_id=account_id,
            pool="jonas-laptop",
            execution_id=uuid4(),
            payload={
                "host_exec_profile": "copilot",
                "agent_type": "copilot",
                "host_exec": {"harness": "copilot_cli", "profile": "copilot"},
            },
            outcome=outcome,
        )
        is None
    )
    assert outcome == {"routing_reason": "runner_offline"}


# --- queue, fail, fallback ----------------------------------------------------


def _execution(started: datetime) -> SimpleNamespace:
    execution_id = uuid4()
    return SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:auto:{execution_id}",
        start_time=started,
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="review",
        model_output_summary=None,
        result=None,
        routing_reason="runner_offline",
        billing_mode=None,
    )


@pytest.mark.asyncio
async def test_queue_timeout_uses_flow_timeout_and_names_reason(monkeypatch) -> None:
    execution = _execution(datetime.now(timezone.utc) - timedelta(seconds=700))
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    executor = _executor(
        {"harness": "copilot_cli", "harness_queue_timeout_seconds": 600}
    )
    status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.FAILED
    assert execution.routing_reason == "queue_timeout"
    assert "queue_timeout" in execution.error_message
    assert "last reason runner_offline" in execution.error_message


@pytest.mark.asyncio
async def test_queue_waits_before_flow_timeout(monkeypatch) -> None:
    """16 minutes is past the old 15-minute default, inside the 30 min one."""
    execution = _execution(datetime.now(timezone.utc) - timedelta(minutes=16))
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job",
        lambda *a, **k: k["outcome"].update(routing_reason="harness_signed_out"),
    )
    executor = _executor({"harness": "copilot_cli"})
    executor._owner_runner_id = lambda execution: (None, True)
    status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.PENDING
    assert execution.routing_reason == "harness_signed_out"


@pytest.mark.asyncio
async def test_fail_mode_fails_at_start_with_reason(monkeypatch) -> None:
    execution = _execution(datetime.now(timezone.utc))
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job",
        lambda *a, **k: k["outcome"].update(routing_reason="no_runner_with_harness"),
    )
    executor = _executor({"harness": "copilot_cli", "harness_fallback": "fail"})
    with pytest.raises(ValueError, match="no_runner_with_harness"):
        await executor.start({"execution_id": str(execution.id)})
    assert execution.routing_reason == "no_runner_with_harness"


@pytest.mark.asyncio
async def test_leased_956_profile_run_is_billed_as_seat(monkeypatch) -> None:
    execution = _execution(datetime.now(timezone.utc))
    runner = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job", lambda *a, **k: runner
    )

    async def _deliver(db, payload, context=None):
        return payload

    pushed = []

    async def _push(runner_id, payload):
        pushed.append(payload)

    monkeypatch.setattr(
        "preloop.agents.remote_runner.prepare_runner_delivery", _deliver
    )
    monkeypatch.setattr("preloop.agents.remote_runner._push_job", _push)
    executor = _executor({"host_exec_profile": "copilot-review"})
    await executor.start(
        {
            "execution_id": str(execution.id),
            "agent_type": "copilot",
            "agent_config": {"host_exec_profile": "copilot-review"},
        }
    )
    assert execution.billing_mode == "seat"
    assert execution.routing_reason is None
    assert "host_exec" not in pushed[0]


def test_factory_queues_harness_flow_without_online_runner() -> None:
    flow = SimpleNamespace(
        runner_pool=None,
        account_id=uuid4(),
        agent_config={"harness": "copilot_cli"},
        account=SimpleNamespace(default_runner_pool=None),
    )
    db = MagicMock()
    executor = create_executor_for_execution(
        "copilot",
        {"host_exec_profile": "copilot"},
        flow=flow,
        db=db,
        execution_context={"agent_config": {"host_exec_profile": "copilot"}},
    )
    assert isinstance(executor, RemoteRunnerExecutor)
    assert executor.pool == "auto"


def test_factory_runs_server_fallback_on_hosted_harness() -> None:
    from preloop.agents.codex import CodexAgent

    flow = SimpleNamespace(
        runner_pool="jonas-laptop",
        account_id=uuid4(),
        agent_config={"harness": "copilot_cli"},
    )
    executor = create_executor_for_execution(
        "codex",
        {},
        flow=flow,
        db=MagicMock(),
        execution_context={HARNESS_FALLBACK_CONTEXT_KEY: True},
    )
    assert isinstance(executor, CodexAgent)


@pytest.mark.parametrize("nested", [False, True])
def test_orchestrator_falls_back_to_server_pool(monkeypatch, nested) -> None:
    """Also for the doubly wrapped ``{"agent_config": {...}}`` storage shape."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    account_id = uuid4()
    offline = _runner(account_id, online=False)
    model = SimpleNamespace(id=uuid4(), model_identifier="gpt-5.2")
    unusable = SimpleNamespace(id=uuid4(), model_identifier="gpt-5.2")
    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda db, **kw: [offline])
    monkeypatch.setattr(
        "preloop.models.crud.crud_ai_model.get_all_for_account",
        lambda db, **kw: [
            SimpleNamespace(id=uuid4(), model_identifier="x"),
            unusable,
            model,
        ],
    )
    monkeypatch.setattr(
        "preloop.services.model_routing.model_usable_for_agent",
        lambda candidate, agent_type: (
            candidate is not unusable and agent_type == "codex"
        ),
    )
    config = {
        "harness": "copilot_cli",
        "harness_fallback": "fallback_server",
        "fallback_model_identifier": "gpt-5.2",
    }
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.agent_type = "copilot"
    orchestrator.ai_model = None
    orchestrator.trigger_event_data = {}
    orchestrator.execution_log = SimpleNamespace(
        routing_reason=None, billing_mode=None
    )
    orchestrator.flow = SimpleNamespace(
        agent_type="copilot",
        runner_pool="jonas-laptop",
        account_id=account_id,
        agent_config={"agent_config": config} if nested else config,
    )
    orchestrator._apply_harness_routing_decision()
    assert orchestrator.agent_type == "codex"
    assert orchestrator.ai_model is model
    assert orchestrator._harness_fell_back is True
    assert orchestrator.execution_log.routing_reason == "fell_back_to_server"
    assert orchestrator.execution_log.billing_mode == "metered"


def test_orchestrator_keeps_harness_when_a_runner_is_eligible(monkeypatch) -> None:
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    account_id = uuid4()
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kw: [_runner(account_id)]
    )
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.agent_type = "copilot"
    orchestrator.ai_model = None
    orchestrator.trigger_event_data = {}
    orchestrator.execution_log = SimpleNamespace(routing_reason=None)
    orchestrator.flow = SimpleNamespace(
        agent_type="copilot",
        runner_pool="jonas-laptop",
        account_id=account_id,
        agent_config={
            "harness": "copilot_cli",
            "harness_fallback": "fallback_server",
            "fallback_model_identifier": "gpt-5.2",
        },
    )
    orchestrator._apply_harness_routing_decision()
    assert orchestrator.agent_type == "copilot"
    assert orchestrator.execution_log.routing_reason is None


# --- editor options -------------------------------------------------------------


def test_harness_options_counts_two_runners() -> None:
    account_id = uuid4()
    online = _runner(account_id, name="laptop")
    offline = _runner(account_id, name="desktop", online=False)
    foreign = _runner(uuid4(), name="theirs")
    hidden = _runner(account_id, name="not-mine")
    result = harness_options(
        None,
        [online, offline, foreign, hidden],
        account_id=account_id,
        usable=lambda runner: runner is not hidden,
    )
    assert [item["harness"] for item in result["harnesses"]] == ["copilot_cli"]
    copilot = result["harnesses"][0]
    assert copilot["runners_online"] == 1
    assert copilot["runners_total"] == 2
    assert copilot["billing"] == "seat"
    assert {m["id"]: m["runners_online"] for m in copilot["models"]} == {
        "auto": 1,
        "gpt-5.2": 1,
        "claude-sonnet-4.6": 1,
    }
    assert [(r["name"], r["eligible"], r["reason"]) for r in copilot["runners"]] == [
        ("laptop", True, None),
        ("desktop", False, "runner_offline"),
    ]
    # Presence-only harnesses (claude_desktop in the fixture) are not routable.
    assert "claude_desktop" not in json.dumps(result, default=str)
