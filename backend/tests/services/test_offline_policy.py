"""Offline policy for runner-pinned flows (#1508): ``fail`` and ``reroute``.

Lid close cannot be exercised here; the presence signals stand in for it:
``sleep_signaled_at`` (``going_to_sleep``) with a stale heartbeat is
``asleep``, a stale heartbeat without it is ``offline``.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.agents.base import AgentStatus
from preloop.agents.remote_runner import OfflinePolicyError, RemoteRunnerExecutor
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services import runner_service
from preloop.services.host_exec import (
    harness_selector_config_error,
    offline_policy_mode,
)
from preloop.services.runner_service import (
    lease_job,
    runner_away_reason,
    runner_eligible_for_harness,
)

FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "personal_runners"
    / "harness_inventory_copilot_signed_in.json"
)
HARNESS_PAYLOAD = {
    "host_exec_profile": "copilot",
    "agent_type": "copilot",
    "completion_protocol": "host_exec",
    "host_exec": {"harness": "copilot_cli", "profile": "copilot"},
}


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
    inventory = copy.deepcopy(json.loads(FIXTURE.read_text()))
    for entry in inventory["entries"]:
        if entry["harness"] == "copilot_cli":
            entry.update(changes)
    return inventory


def _runner(
    account_id: Any,
    *,
    presence: str = "online",
    owner: Any = None,
    billing: str = "seat",
    name: str = "laptop",
) -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    reachable = presence in ("online", "sleeping_soon")
    return SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        name=name,
        hostname=name,
        labels=[],
        registered_by_user_id=owner or uuid4(),
        flow_execution_consent="always",
        status="online" if reachable else "offline",
        last_heartbeat=now if reachable else now - timedelta(hours=1),
        sleep_signaled_at=(
            now - timedelta(minutes=1)
            if presence in ("asleep", "sleeping_soon")
            else None
        ),
        paused=False,
        free_slots=1,
        os_idle_seconds=None,
        presence_state=None,
        presence_since=None,
        harness_inventory=_inventory(billing=billing),
        capabilities={
            "host_exec_profiles": [
                {
                    "name": "copilot",
                    "capabilities": ["host_exec", "copilot_cli", "stdout"],
                }
            ]
        },
    )


# --- config ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ({"offline_policy": "queue"}, "queue is not available yet"),
        ({"offline_policy": "later"}, "must be fail or reroute"),
        ({"offline_policy": "reroute", "runner_id": None}, "runner_id"),
    ],
)
def test_offline_policy_config_errors(config, fragment) -> None:
    base: Dict[str, Any] = {"harness": "copilot_cli", "runner_id": str(uuid4())}
    base.update(config)
    if base.get("runner_id") is None:
        base.pop("runner_id")
    assert fragment in (harness_selector_config_error("copilot", base) or "")


@pytest.mark.parametrize("policy", ["fail", "reroute"])
def test_offline_policy_accepted_on_pinned_harness_flow(policy) -> None:
    from preloop.models.schemas.flow import FlowCreate

    config = {
        "harness": "copilot_cli",
        "runner_id": str(uuid4()),
        "offline_policy": policy,
    }
    assert harness_selector_config_error("copilot", config) is None
    flow = FlowCreate(
        name="x", prompt_template="y", agent_type="copilot", agent_config=config
    )
    assert flow.agent_config["offline_policy"] == policy


def test_flow_schema_rejects_queue_with_a_clear_message() -> None:
    from pydantic import ValidationError

    from preloop.models.schemas.flow import FlowCreate

    with pytest.raises(ValidationError, match="queue is not available yet"):
        FlowCreate(
            name="x",
            prompt_template="y",
            agent_type="copilot",
            agent_config={
                "harness": "copilot_cli",
                "runner_id": str(uuid4()),
                "offline_policy": "queue",
            },
        )


def test_default_policy_is_fail() -> None:
    assert offline_policy_mode({}) == "fail"
    assert offline_policy_mode({"offline_policy": "reroute"}) == "reroute"
    assert offline_policy_mode({"agent_config": {"offline_policy": "reroute"}}) == (
        "reroute"
    )


# --- presence -> reason -----------------------------------------------------


@pytest.mark.parametrize(
    ("presence", "reason"),
    [
        ("online", None),
        ("asleep", "runner_asleep"),
        # Said going_to_sleep, socket not yet dropped: already asleep.
        ("sleeping_soon", "runner_asleep"),
        # macOS and Windows send no pre-sleep signal: a lid close is offline.
        ("offline", "runner_offline"),
    ],
)
def test_runner_away_reason(presence, reason) -> None:
    assert runner_away_reason(_runner(uuid4(), presence=presence)) == reason


def test_runner_away_reason_missing_runner_is_not_away() -> None:
    assert runner_away_reason(None) is None


def test_sleep_signaled_runner_is_not_harness_eligible() -> None:
    account_id = uuid4()
    runner = _runner(account_id, presence="sleeping_soon")
    ok, reason, _ = runner_eligible_for_harness(
        None,
        runner,
        account_id=account_id,
        harness="copilot_cli",
        pinned_runner_id=runner.id,
    )
    assert (ok, reason) == (False, "runner_asleep")


# --- executor ---------------------------------------------------------------


def _execution() -> SimpleNamespace:
    execution_id = uuid4()
    return SimpleNamespace(
        id=execution_id,
        flow_id=uuid4(),
        runner_id=None,
        agent_session_reference=f"runner:queued:auto:{execution_id}",
        start_time=datetime.now(timezone.utc),
        status="PENDING",
        error_message=None,
        end_time=None,
        resolved_input_prompt="review",
        model_output_summary=None,
        result=None,
        routing_reason=None,
        billing_mode=None,
    )


def _executor(config: Dict[str, Any]) -> RemoteRunnerExecutor:
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
        "copilot", config, db=MagicMock(), pool="auto", account_id=uuid4(), flow=flow
    )


def _world(monkeypatch, execution, pinned) -> List[Dict[str, Any]]:
    logs: List[Dict[str, Any]] = []
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.get",
        lambda *a, **k: execution,
    )
    monkeypatch.setattr(
        "preloop.agents.remote_runner.crud_flow_execution.append_log",
        lambda db, execution_id, log_data, commit=True: logs.append(log_data),
    )
    monkeypatch.setattr(
        crud_flow_runner,
        "get_fresh",
        lambda db, runner_id: pinned if runner_id == pinned.id else None,
    )
    return logs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("presence", "reason"),
    [("asleep", "runner_asleep"), ("offline", "runner_offline")],
)
async def test_default_fail_fails_in_one_routing_cycle(
    monkeypatch, presence, reason
) -> None:
    execution = _execution()
    pinned = _runner(uuid4(), presence=presence)
    logs = _world(monkeypatch, execution, pinned)
    calls: List[Dict[str, Any]] = []

    def _lease(*args, **kwargs):
        calls.append(kwargs)
        kwargs["outcome"].update(routing_reason="runner_offline")
        return None

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _lease)
    executor = _executor({"harness": "copilot_cli", "runner_id": str(pinned.id)})
    with pytest.raises(OfflinePolicyError, match=reason):
        await executor.start({"execution_id": str(execution.id)})
    assert len(calls) == 1, "fail must not try another runner"
    assert execution.routing_reason == reason
    assert "offline policy fail" in execution.error_message
    assert [log["metadata"] for log in logs] == [
        {
            "milestone": "offline_policy",
            "offline_policy": "fail",
            "reason": reason,
            "from_runner_id": str(pinned.id),
            "to_runner_id": None,
        }
    ]
    assert logs[0]["type"] == "milestone"


@pytest.mark.asyncio
async def test_policy_does_not_fire_for_a_reachable_runner(monkeypatch) -> None:
    """Online but signed out is harness_fallback's case, not the offline one."""
    execution = _execution()
    pinned = _runner(uuid4())
    logs = _world(monkeypatch, execution, pinned)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job",
        lambda *a, **k: k["outcome"].update(routing_reason="harness_signed_out"),
    )
    executor = _executor({"harness": "copilot_cli", "runner_id": str(pinned.id)})
    reference = await executor.start({"execution_id": str(execution.id)})
    assert reference.startswith("runner:queued:")
    assert execution.routing_reason == "harness_signed_out"
    assert logs == []


@pytest.mark.asyncio
async def test_reroute_leases_another_runner_and_logs_it(monkeypatch) -> None:
    execution = _execution()
    pinned = _runner(uuid4(), presence="asleep", name="laptop")
    other = _runner(pinned.account_id, name="desktop")
    logs = _world(monkeypatch, execution, pinned)
    calls: List[Dict[str, Any]] = []

    def _lease(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("reroute_from") == pinned.id:
            kwargs["outcome"].update(billing_mode="seat", consent_status="approved")
            return other
        kwargs["outcome"].update(routing_reason="runner_offline")
        return None

    async def _deliver(db, payload, context=None):
        return payload

    pushed: List[Any] = []

    async def _push(runner_id, payload):
        pushed.append(runner_id)

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _lease)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.prepare_runner_delivery", _deliver
    )
    monkeypatch.setattr("preloop.agents.remote_runner._push_job", _push)
    executor = _executor(
        {
            "harness": "copilot_cli",
            "runner_id": str(pinned.id),
            "offline_policy": "reroute",
        }
    )
    reference = await executor.start({"execution_id": str(execution.id)})
    assert reference == f"runner:{other.id}:{execution.id}"
    assert calls[0]["required_runner_id"] == pinned.id
    assert calls[1]["reroute_from"] == pinned.id
    assert calls[1]["required_billing"] == "seat"
    assert calls[1].get("required_runner_id") is None
    assert execution.runner_id == other.id
    assert execution.routing_reason is None
    assert pushed == [other.id]
    assert logs[0]["metadata"]["offline_policy"] == "reroute"
    assert logs[0]["metadata"]["to_runner_id"] == str(other.id)
    assert "moved the run to desktop" in logs[0]["message"]


@pytest.mark.asyncio
async def test_reroute_without_a_candidate_fails_visibly(monkeypatch) -> None:
    execution = _execution()
    pinned = _runner(uuid4(), presence="asleep")
    logs = _world(monkeypatch, execution, pinned)

    def _lease(*args, **kwargs):
        kwargs["outcome"].update(routing_reason="runner_offline")
        return None

    monkeypatch.setattr("preloop.agents.remote_runner.lease_job", _lease)
    executor = _executor(
        {
            "harness": "copilot_cli",
            "runner_id": str(pinned.id),
            "offline_policy": "reroute",
        }
    )
    with pytest.raises(OfflinePolicyError, match="no other runner you may use"):
        await executor.start({"execution_id": str(execution.id)})
    assert execution.routing_reason == "runner_asleep"
    assert logs[0]["metadata"]["offline_policy"] == "reroute"
    assert logs[0]["metadata"]["to_runner_id"] is None


@pytest.mark.asyncio
async def test_queued_run_fails_when_pinned_runner_falls_asleep(monkeypatch) -> None:
    execution = _execution()
    pinned = _runner(uuid4(), presence="asleep")
    _world(monkeypatch, execution, pinned)
    monkeypatch.setattr(
        "preloop.agents.remote_runner.lease_job",
        lambda *a, **k: k["outcome"].update(routing_reason="runner_asleep"),
    )
    executor = _executor({"harness": "copilot_cli", "runner_id": str(pinned.id)})
    executor._owner_runner_id = lambda execution: (None, True)
    status = await executor.get_status(execution.agent_session_reference)
    assert status == AgentStatus.FAILED
    assert execution.status == "FAILED"
    assert execution.routing_reason == "runner_asleep"
    assert execution.end_time is not None


# --- reroute candidates through the real lease ---------------------------------


def _lease_world(monkeypatch, account_id, author_id, candidates):
    from preloop.models.crud import crud_flow

    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kw: list(candidates)
    )
    claimed: List[Any] = []

    def _claim(db, *, runner_id):
        claimed.append(runner_id)
        return next(row for row in candidates if row.id == runner_id)

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kw: SimpleNamespace(reported_status=None, **kw),
    )
    monkeypatch.setattr(runner_service, "emit_runner_updated", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_service, "_runner_registered_by_admin", lambda *a, **k: False
    )
    monkeypatch.setattr(runner_service, "_user_is_account_admin", lambda db, u: False)
    monkeypatch.setattr(
        runner_service.crud_user,
        "get",
        lambda db, id=None: SimpleNamespace(
            id=id, account_id=account_id, is_active=True
        ),
    )
    flow_id = uuid4()
    monkeypatch.setattr(
        crud_flow,
        "get",
        lambda db, id=None, **kw: SimpleNamespace(
            id=flow_id, created_by_user_id=author_id
        ),
    )
    monkeypatch.setattr("preloop.plugins.account_hooks._authorizer", None)
    db = MagicMock()
    db.get.return_value = None
    monkeypatch.setattr(
        "preloop.models.crud.crud_flow_execution.get",
        lambda *a, **k: SimpleNamespace(flow_id=flow_id),
    )
    return db, claimed


def _reroute(db, account_id, pinned, billing: Optional[str] = "seat"):
    outcome: Dict[str, Any] = {}
    leased = lease_job(
        db,
        account_id=account_id,
        pool="auto",
        execution_id=uuid4(),
        payload=copy.deepcopy(HARNESS_PAYLOAD),
        outcome=outcome,
        reroute_from=pinned.id,
        required_billing=billing,
    )
    return leased, outcome


def test_reroute_refuses_a_weaker_metered_candidate(monkeypatch) -> None:
    """A seat run is never moved to a gateway-metered path."""
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, presence="asleep", owner=author)
    metered = _runner(account_id, owner=author, billing="metered", name="vm")
    db, claimed = _lease_world(monkeypatch, account_id, author, [pinned, metered])
    leased, outcome = _reroute(db, account_id, pinned)
    assert leased is None
    assert claimed == []
    assert outcome["routing_reason"] == "no_runner_with_harness"


def test_reroute_refuses_a_runner_the_author_may_not_use(monkeypatch) -> None:
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, presence="asleep", owner=author)
    someone_else = _runner(account_id, name="colleague")
    db, claimed = _lease_world(monkeypatch, account_id, author, [someone_else])
    leased, outcome = _reroute(db, account_id, pinned)
    assert leased is None
    assert claimed == []
    assert outcome["routing_reason"] == "owner_has_no_runner"


def test_reroute_never_picks_the_pinned_runner(monkeypatch) -> None:
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, owner=author)
    db, claimed = _lease_world(monkeypatch, account_id, author, [pinned])
    leased, _outcome = _reroute(db, account_id, pinned)
    assert leased is None
    assert claimed == []


def test_reroute_leases_the_authors_other_seat_runner(monkeypatch) -> None:
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, presence="asleep", owner=author)
    metered = _runner(account_id, owner=author, billing="metered", name="vm")
    desktop = _runner(account_id, owner=author, name="desktop")
    db, claimed = _lease_world(
        monkeypatch, account_id, author, [pinned, metered, desktop]
    )
    leased, outcome = _reroute(db, account_id, pinned)
    assert leased is desktop
    assert claimed == [desktop.id]
    assert outcome["billing_mode"] == "seat"


def test_reroute_refuses_personal_runners_for_an_authorless_flow(
    monkeypatch,
) -> None:
    account_id = uuid4()
    pinned = _runner(account_id, presence="asleep")
    desktop = _runner(account_id, name="desktop")
    db, claimed = _lease_world(monkeypatch, account_id, None, [desktop])
    leased, _outcome = _reroute(db, account_id, pinned)
    assert leased is None
    assert claimed == []


def test_reroute_needs_a_harness_selection(monkeypatch) -> None:
    account_id = uuid4()
    pinned = _runner(account_id, presence="asleep")
    assert (
        lease_job(
            MagicMock(),
            account_id=account_id,
            pool="auto",
            execution_id=uuid4(),
            payload={"host_exec_profile": "copilot", "agent_type": "copilot"},
            reroute_from=pinned.id,
        )
        is None
    )


# --- hosted fallback ----------------------------------------------------------


def _orchestrator(account_id, config):
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.agent_type = "copilot"
    orchestrator.ai_model = None
    orchestrator.trigger_event_data = {}
    orchestrator.execution_log = SimpleNamespace(routing_reason=None, billing_mode=None)
    orchestrator.flow = SimpleNamespace(
        id=uuid4(),
        agent_type="copilot",
        runner_pool=None,
        account_id=account_id,
        agent_config=config,
    )
    return orchestrator


@pytest.mark.parametrize(
    ("other_billing", "falls_back"), [("seat", False), ("metered", True)]
)
def test_reroute_beats_hosted_fallback_only_with_an_equal_runner(
    monkeypatch, other_billing, falls_back
) -> None:
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, presence="asleep", owner=author)
    other = _runner(account_id, owner=author, billing=other_billing, name="desktop")
    _lease_world(monkeypatch, account_id, author, [pinned, other])
    model = SimpleNamespace(id=uuid4(), model_identifier="gpt-5.2")
    monkeypatch.setattr(
        "preloop.models.crud.crud_ai_model.get_all_for_account",
        lambda db, **kw: [model],
    )
    monkeypatch.setattr(
        "preloop.services.model_routing.model_usable_for_agent",
        lambda candidate, agent_type: agent_type == "codex",
    )
    orchestrator = _orchestrator(
        account_id,
        {
            "harness": "copilot_cli",
            "runner_id": str(pinned.id),
            "offline_policy": "reroute",
            "harness_fallback": "fallback_server",
            "fallback_model_identifier": "gpt-5.2",
        },
    )
    orchestrator._apply_harness_routing_decision()
    assert (orchestrator.agent_type == "codex") is falls_back
    assert (orchestrator.execution_log.routing_reason == "fell_back_to_server") is (
        falls_back
    )


@pytest.mark.parametrize(
    "change",
    [
        {"free_slots": 0},
        {"paused": True},
        {"flow_execution_consent": "never"},
        {"capabilities": {"host_exec_profiles": []}},
    ],
)
def test_reroute_candidate_that_cannot_take_the_lease_keeps_hosted_fallback(
    monkeypatch, change
) -> None:
    """Review on #2276: a busy, paused, refusing or profile-less runner must
    not suppress ``fallback_server`` and then leave the reroute empty."""
    account_id = uuid4()
    author = uuid4()
    pinned = _runner(account_id, presence="asleep", owner=author)
    other = _runner(account_id, owner=author, name="desktop")
    for key, value in change.items():
        setattr(other, key, value)
    _lease_world(monkeypatch, account_id, author, [pinned, other])
    model = SimpleNamespace(id=uuid4(), model_identifier="gpt-5.2")
    monkeypatch.setattr(
        "preloop.models.crud.crud_ai_model.get_all_for_account",
        lambda db, **kw: [model],
    )
    monkeypatch.setattr(
        "preloop.services.model_routing.model_usable_for_agent",
        lambda candidate, agent_type: agent_type == "codex",
    )
    orchestrator = _orchestrator(
        account_id,
        {
            "harness": "copilot_cli",
            "runner_id": str(pinned.id),
            "offline_policy": "reroute",
            "harness_fallback": "fallback_server",
            "fallback_model_identifier": "gpt-5.2",
        },
    )
    orchestrator._apply_harness_routing_decision()
    assert orchestrator.agent_type == "codex"
    assert orchestrator.execution_log.routing_reason == "fell_back_to_server"


def test_slept_runner_with_stale_heartbeat_reports_runner_asleep() -> None:
    """Review on #2276: the canonical asleep case, signal then silence."""
    account_id = uuid4()
    runner = _runner(account_id, presence="asleep")
    ok, reason, _ = runner_eligible_for_harness(
        None,
        runner,
        account_id=account_id,
        harness="copilot_cli",
        pinned_runner_id=runner.id,
    )
    assert (ok, reason) == (False, "runner_asleep")
