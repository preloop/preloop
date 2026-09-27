"""Transient MCP and checkout inputs for host-exec leases."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.agents.runner_launch import prepare_runner_delivery
from preloop.services.host_exec_delivery import (
    HostExecDeliveryError,
    build_host_exec_checkout,
    host_exec_checkout_path,
    hydrate_host_exec_job,
)

HEAD_SHA = "a" * 40
TRACKER_ID = "tracker-1"


def _pr_context(**clone):
    """Checkout context for a pull request trigger with one tracker token."""
    return {
        "flow_id": str(uuid4()),
        "flow_name": "PR Reviewer",
        "execution_id": str(uuid4()),
        "account_id": uuid4(),
        "git_clone_config": {"enabled": True, **clone},
        "trigger_event_data": {
            "payload": {
                "pull_request": {
                    "number": 5,
                    "head": {"sha": HEAD_SHA, "ref": "feature/fix"},
                    "base": {"ref": "main"},
                }
            }
        },
        "trigger_project_id": None,
        "trigger_tracker_id": TRACKER_ID,
        "git_credentials_map": {
            TRACKER_ID: {"token": "ghs_example_token", "tracker_type": "github"}
        },
    }


def test_checkout_plan_pins_pull_request_head_with_scoped_credential():
    plan = build_host_exec_checkout(
        _pr_context(
            repositories=[
                {"repository_url": "https://user:old@example.test/org/app.git"}
            ],
            git_user_name="Reviewer",
            git_user_email="reviewer@example.test",
        )
    )
    assert plan == {
        "repositories": [
            {
                "url": "https://example.test/org/app.git",
                "path": "workspace-1",
                "branch": "main",
                "commit": HEAD_SHA,
                "fetch_refs": ["feature/fix", "pull/5/head"],
                "username": "x-access-token",
                "token": "ghs_example_token",
            }
        ],
        "git_user_name": "Reviewer",
        "git_user_email": "reviewer@example.test",
    }


def test_checkout_plan_honours_pin_and_clone_path():
    plan = build_host_exec_checkout(
        _pr_context(
            repositories=[
                {
                    "repository_url": "https://example.test/org/app.git",
                    "clone_path": "/workspace",
                    "pin_sha": "b" * 40,
                },
                {
                    "repository_url": "https://example.test/org/lib.git",
                    "clone_path": "vendor/lib",
                    "branch": "release",
                },
            ]
        )
    )
    first, second = plan["repositories"]
    assert first["path"] == "workspace"
    assert first["commit"] == "b" * 40
    assert "branch" not in first and "fetch_refs" not in first
    assert second["path"] == "workspace/vendor/lib"
    assert second["branch"] == "release"
    assert second["commit"] == HEAD_SHA


def test_checkout_plan_without_resolvable_repository_fails_clearly():
    context = _pr_context(repositories=[{"clone_path": "/workspace"}])
    context["trigger_event_data"] = {}
    with pytest.raises(HostExecDeliveryError, match="no repository URL"):
        build_host_exec_checkout(context)
    assert build_host_exec_checkout(_pr_context()) is None


@pytest.mark.parametrize("path", ["/", "/..", "../x", "/workspace/../../etc"])
def test_checkout_path_rejects_escape(path):
    with pytest.raises(HostExecDeliveryError):
        host_exec_checkout_path(path)


def _host_job(execution_id):
    return {
        "execution_id": str(execution_id),
        "flow_id": str(uuid4()),
        "agent_type": "copilot",
        "prompt": "review",
        "model_identifier": None,
        "host_exec_profile": "copilot-seat",
        "timeout_seconds": 600,
        "agent_config": {"host_exec_profile": "copilot-seat"},
        "completion_protocol": "host_exec",
    }


def _patch_rows(monkeypatch, *, tools=None, clone=None):
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        allowed_mcp_tools=tools,
        allowed_mcp_servers=None,
        git_clone_config=clone,
        custom_commands=None,
    )
    execution = SimpleNamespace(id=uuid4(), flow_id=flow.id, trigger_event_details={})
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow_execution.get",
        MagicMock(return_value=execution),
    )
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow.get",
        MagicMock(return_value=flow),
    )
    session = SimpleNamespace(id=uuid4())
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_runtime_session.get_by_source",
        MagicMock(return_value=session),
    )
    mint = MagicMock(return_value=("flow-mcp-token", uuid4()))
    monkeypatch.setattr(
        "preloop.services.flow_runtime_token.create_flow_runtime_token", mint
    )
    return flow, execution, session, mint


@pytest.mark.asyncio
async def test_hydrate_mints_mcp_token_only_for_flows_with_tools(monkeypatch):
    flow, execution, session, mint = _patch_rows(monkeypatch)
    job = _host_job(execution.id)
    assert await hydrate_host_exec_job(MagicMock(), job) == job
    mint.assert_not_called()

    flow.allowed_mcp_tools = [{"name": "get_pull_request"}]
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    assert hydrated["host_exec_mcp"] == {"token": "flow-mcp-token"}
    assert "host_exec_mcp" not in job
    assert mint.call_args.kwargs["execution_id"] == execution.id
    assert mint.call_args.kwargs["runtime_session_id"] == session.id


@pytest.mark.asyncio
async def test_hydrate_adds_checkout_from_orchestrator_context(monkeypatch):
    _, execution, _, _ = _patch_rows(
        monkeypatch,
        clone={
            "enabled": True,
            "repositories": [{"repository_url": "https://example.test/o/r.git"}],
        },
    )
    prepare = AsyncMock(
        return_value=_pr_context(
            repositories=[{"repository_url": "https://example.test/o/r.git"}]
        )
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator."
        "_get_flow_details",
        MagicMock(),
    )
    monkeypatch.setattr(
        "preloop.services.flow_orchestrator.FlowExecutionOrchestrator."
        "prepare_host_exec_checkout_context",
        prepare,
    )
    job = _host_job(execution.id)
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    repo = hydrated["host_exec_checkout"]["repositories"][0]
    assert repo["token"] == "ghs_example_token"
    assert repo["commit"] == HEAD_SHA
    assert "host_exec_checkout" not in job


@pytest.mark.asyncio
async def test_delivery_maps_host_preparation_errors_to_launch_error(monkeypatch):
    _, execution, _, mint = _patch_rows(
        monkeypatch,
        tools=[{"name": "get_pull_request"}],
        clone={"enabled": True, "create_pull_request": True},
    )
    job = _host_job(execution.id)
    delivered = await prepare_runner_delivery(MagicMock(), job)
    assert "pull request" in delivered["launch_error"]
    assert "host_exec_mcp" not in delivered
    mint.assert_not_called()

    mint.return_value = (None, None)
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow.get",
        MagicMock(
            return_value=SimpleNamespace(
                id=uuid4(),
                account_id=uuid4(),
                allowed_mcp_tools=[{"name": "x"}],
                allowed_mcp_servers=None,
                git_clone_config=None,
                custom_commands=None,
            )
        ),
    )
    delivered = await prepare_runner_delivery(MagicMock(), job)
    assert delivered["launch_error"].startswith("Could not mint")


@pytest.mark.asyncio
async def test_delivery_fails_when_execution_is_gone(monkeypatch):
    monkeypatch.setattr(
        "preloop.services.host_exec_delivery.crud_flow_execution.get",
        MagicMock(return_value=None),
    )
    delivered = await prepare_runner_delivery(MagicMock(), _host_job(uuid4()))
    assert delivered["launch_error"] == "Host execution no longer exists"
