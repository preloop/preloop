"""Host-exec flows in an authorized directory (#1669)."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.agents.remote_runner import RemoteRunnerExecutor
from preloop.models.crud.flow_runner import crud_flow_runner
from preloop.services import runner_service
from preloop.services.host_exec import (
    host_exec_flow_error,
    host_exec_workspace,
    normalize_host_exec_advertisements,
    runner_offers_authorized_directory,
)
from preloop.services.runner_service import lease_job

WORKSPACE = {"kind": "authorized_directory", "id": "dir_ee"}


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


def test_workspace_shape_is_validated() -> None:
    assert host_exec_workspace({}) is None
    assert host_exec_workspace({"workspace": WORKSPACE}) == {
        **WORKSPACE,
        "allow_dirty": False,
    }
    assert host_exec_workspace(
        {"agent_config": {"workspace": {**WORKSPACE, "allow_dirty": True}}}
    ) == {**WORKSPACE, "allow_dirty": True}
    for bad, fragment in [
        ("dir_ee", "must be an object"),
        ({"kind": "path", "path": "/srv/repo"}, "unsupported keys"),
        ({"kind": "path"}, "authorized_directory"),
        ({"kind": "authorized_directory", "id": "../x"}, "authorized directory id"),
        ({**WORKSPACE, "allow_dirty": "yes"}, "boolean"),
        ({**WORKSPACE, "credential": "x"}, "unsupported keys"),
    ]:
        with pytest.raises(ValueError, match=fragment):
            host_exec_workspace({"workspace": bad})


def test_flow_error_checks_workspace_for_host_flows_only() -> None:
    config = {"harness": "claude_code", "workspace": WORKSPACE}
    assert host_exec_flow_error(agent_type="claude_code", agent_config=config) is None
    assert "authorized_directory" in (
        host_exec_flow_error(
            agent_type="claude_code",
            agent_config={"harness": "claude_code", "workspace": {"kind": "path"}},
        )
        or ""
    )
    assert "cannot also clone" in (
        host_exec_flow_error(
            agent_type="claude_code",
            agent_config=config,
            git_clone_config={"enabled": True, "repositories": []},
        )
        or ""
    )
    assert (
        host_exec_flow_error(
            agent_type="claude_code",
            agent_config=config,
            git_clone_config={"enabled": False},
        )
        is None
    )
    # Persistent and Docker flows own agent_config.workspace themselves.
    assert (
        host_exec_flow_error(agent_type="codex", agent_config={"workspace": "x"})
        is None
    )


def _runner(
    account_id: Any,
    directories: Optional[List[Dict[str, Any]]] = None,
    *,
    name: str = "mac",
) -> SimpleNamespace:
    capabilities = normalize_host_exec_advertisements(
        [{"name": "claude", "capabilities": ["host_exec", "claude_code"]}]
    )
    capabilities["authorized_directories"] = directories or []
    return SimpleNamespace(
        id=uuid4(),
        account_id=account_id,
        name=name,
        labels=[],
        registered_by_user_id=uuid4(),
        status="online",
        flow_execution_consent="always",
        last_heartbeat=datetime.now(timezone.utc),
        free_slots=1,
        paused=False,
        accept_untagged=False,
        capabilities=capabilities,
        harness_inventory=None,
    )


def test_runner_offers_authorized_directory() -> None:
    account_id = uuid4()
    writable = {"id": "dir_ee", "label": "ee", "mode": "write", "harnesses": "all"}
    assert runner_offers_authorized_directory(
        _runner(account_id, [writable]), "dir_ee", "claude_code"
    )
    assert not runner_offers_authorized_directory(
        _runner(account_id, [writable]), "dir_other", "claude_code"
    )
    assert not runner_offers_authorized_directory(
        _runner(account_id, [{**writable, "mode": "read_only"}]),
        "dir_ee",
        "claude_code",
    )
    assert not runner_offers_authorized_directory(
        _runner(account_id, [{**writable, "harnesses": ["copilot_cli"]}]),
        "dir_ee",
        "claude_code",
    )
    assert runner_offers_authorized_directory(
        _runner(account_id, [{**writable, "harnesses": ["claude_code"]}]),
        "dir_ee",
        "claude_code",
    )


def test_lease_payload_carries_the_workspace() -> None:
    config = {
        "harness": "claude_code",
        "workspace": {**WORKSPACE, "allow_dirty": True},
    }
    flow = SimpleNamespace(
        agent_type="claude_code",
        agent_config=config,
        runner_pool=None,
        git_clone_config=None,
        custom_commands=None,
        timeout_seconds=None,
        ai_model=None,
        account_id=uuid4(),
    )
    executor = RemoteRunnerExecutor(
        "claude_code",
        config,
        db=MagicMock(),
        pool="auto",
        account_id=uuid4(),
        flow=flow,
    )
    payload = executor._lease_payload(
        execution_id=uuid4(), flow_id=uuid4(), prompt="tidy"
    )
    assert payload["host_exec_workspace"] == {**WORKSPACE, "allow_dirty": True}
    assert payload["host_exec_profile"] == "claude"


def test_lease_job_skips_runners_without_the_directory(monkeypatch) -> None:
    account_id = uuid4()
    without = _runner(account_id, name="other")
    with_dir = _runner(
        account_id,
        [{"id": "dir_ee", "label": "ee", "mode": "write", "harnesses": "all"}],
    )
    monkeypatch.setattr(
        crud_flow_runner, "find_matching", lambda db, **kw: [without, with_dir]
    )
    claimed: List[Any] = []

    def _claim(db, *, runner_id):
        claimed.append(runner_id)
        return with_dir if runner_id == with_dir.id else without

    monkeypatch.setattr(crud_flow_runner, "claim_free_slot", _claim)
    monkeypatch.setattr(runner_service, "emit_runner_updated", lambda *a, **k: None)
    monkeypatch.setattr(runner_service, "_runner_may_accept", lambda *a, **k: True)
    monkeypatch.setattr(
        crud_flow_runner,
        "create_assignment",
        lambda db, **kw: SimpleNamespace(reported_status=None, **kw),
    )
    result = lease_job(
        MagicMock(),
        account_id=account_id,
        pool="mac",
        execution_id=uuid4(),
        payload={
            "host_exec_profile": "claude",
            "agent_type": "claude_code",
            "completion_protocol": "host_exec",
            "host_exec_workspace": {**WORKSPACE, "allow_dirty": False},
        },
    )
    assert result is with_dir
    assert claimed == [with_dir.id]


def test_workspace_refuses_server_fallback() -> None:
    config = {
        "harness": "claude_code",
        "workspace": WORKSPACE,
        "harness_fallback": "fallback_server",
        "fallback_model_identifier": "gpt-5",
    }
    assert "cannot fall back to hosted compute" in (
        host_exec_flow_error(agent_type="claude_code", agent_config=config) or ""
    )
    config["harness_fallback"] = "fail"
    assert host_exec_flow_error(agent_type="claude_code", agent_config=config) is None


@pytest.mark.parametrize(
    ("agent_type", "harness", "refused"),
    [
        ("cursor", "cursor_cli", True),
        ("devin", "devin_cli", True),
        ("copilot", "copilot_cli", False),
        ("claude_code", "claude_code", False),
    ],
)
def test_workspace_with_preloop_tools_per_harness(
    agent_type: str, harness: str, refused: bool
) -> None:
    config = {"harness": harness, "workspace": WORKSPACE}
    error = host_exec_flow_error(
        agent_type=agent_type, agent_config=config, uses_mcp=True
    )
    assert bool(error and "MCP configuration" in error) is refused
    assert host_exec_flow_error(agent_type=agent_type, agent_config=config) is None


def test_orchestrator_never_falls_back_with_a_workspace(monkeypatch) -> None:
    """A saved row that predates the save-time check still never runs hosted."""
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(crud_flow_runner, "find_matching", lambda db, **kw: [])
    config = {
        "harness": "claude_code",
        "harness_fallback": "fallback_server",
        "fallback_model_identifier": "gpt-5.2",
        "workspace": WORKSPACE,
    }
    orchestrator = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
    orchestrator.db = MagicMock()
    orchestrator.agent_type = "claude_code"
    orchestrator.ai_model = None
    orchestrator.trigger_event_data = {}
    orchestrator.execution_log = SimpleNamespace(routing_reason=None, billing_mode=None)
    orchestrator.flow = SimpleNamespace(
        agent_type="claude_code",
        runner_pool="mac",
        account_id=uuid4(),
        agent_config=config,
    )
    with pytest.raises(ValueError, match="cannot fall back to hosted compute"):
        orchestrator._apply_harness_routing_decision()
    assert orchestrator.agent_type == "claude_code"
    assert orchestrator.execution_log.routing_reason is None
