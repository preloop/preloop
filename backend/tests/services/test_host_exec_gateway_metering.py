"""Server truth for host-profile gateway metering, and bypass refusal."""

from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from preloop.services.flow_failure_category import (
    FAILURE_CATEGORY_GATEWAY_BYPASSED,
    derive_failure_category,
)
from preloop.services.host_exec import finalize_runner_completion
from preloop.services.host_exec_delivery import hydrate_host_exec_job
from preloop.services.host_exec_gateway import (
    EXECUTION_MARKER_HEADER,
    HOST_EXEC_GATEWAY_LEASE_KEY,
    apply_host_gateway_metering,
    decode_execution_marker,
)


def _lease(harness="codex_cli", read_only=False):
    job = {
        "completion_protocol": "host_exec",
        "host_exec": {"harness": harness, "profile": "p", "model": None},
    }
    if read_only:
        job["host_exec_read_only"] = {"mode": "read_only", "commit": "a" * 40}
    return job


def _result(output_tokens=7):
    return {
        "status": "success",
        "harness": "codex_cli",
        "usage": {"input_tokens": 100, "output_tokens": output_tokens},
        "gateway_metered": False,
    }


def _count(monkeypatch, requests):
    monkeypatch.setattr(
        "preloop.models.crud.crud_api_usage.get_gateway_usage_for_execution",
        MagicMock(return_value={"api_requests": requests}),
    )


def _apply(status="SUCCEEDED", result=None, job=None, config=None):
    return apply_host_gateway_metering(
        MagicMock(),
        status=status,
        error=None,
        result=_result() if result is None else result,
        pending_job=_lease() if job is None else job,
        agent_config=config or {},
        execution_id=uuid4(),
    )


def test_metered_host_run_reports_gateway_metered_true(monkeypatch):
    _count(monkeypatch, 3)
    status, error, result = _apply()
    assert (status, error) == ("SUCCEEDED", None)
    assert result["gateway_metered"] is True
    assert result["gateway_request_count"] == 3


def test_bypassed_host_run_fails_with_gateway_bypassed(monkeypatch):
    _count(monkeypatch, 0)
    status, error, result = _apply()
    assert status == "FAILED"
    assert error.startswith("gateway_bypassed:")
    assert result["gateway_metered"] is False
    assert result["failure_category"] == FAILURE_CATEGORY_GATEWAY_BYPASSED
    assert (
        derive_failure_category(status="FAILED", error_message=error)
        == FAILURE_CATEGORY_GATEWAY_BYPASSED
    )


def test_opt_out_keeps_an_unmetered_run_but_says_so(monkeypatch):
    _count(monkeypatch, 0)
    status, _error, result = _apply(config={"allow_unmetered_host_model": True})
    assert status == "SUCCEEDED"
    assert result["gateway_metered"] is False
    assert result["gateway_bypass_allowed"] is True


def test_read_only_review_never_allows_the_opt_out(monkeypatch):
    _count(monkeypatch, 0)
    status, error, _result_ = _apply(
        job=_lease(read_only=True), config={"allow_unmetered_host_model": True}
    )
    assert status == "FAILED"
    assert error.startswith("gateway_bypassed:")


def test_harness_without_a_gateway_route_is_only_recorded(monkeypatch):
    _count(monkeypatch, 0)
    status, _error, result = _apply(job=_lease(harness="copilot_cli"))
    assert status == "SUCCEEDED"
    assert result["gateway_metered"] is False


def test_run_without_model_output_is_not_a_bypass(monkeypatch):
    _count(monkeypatch, 0)
    result = {"status": "failure", "harness": "codex_cli"}
    status, error, _ = _apply(status="FAILED", result=result)
    assert status == "FAILED" and error is None


def test_agent_json_cannot_claim_metering():
    """The completion validator still overrides an agent-authored claim."""
    lease = {
        **_lease(),
        "agent_type": "codex",
        "host_exec_profile": "p",
        "completion_protocol": "host_exec",
    }
    message = {
        "status": "SUCCEEDED",
        "completion_protocol": "host_exec",
        "host_exec_profile": "p",
        "exit_code": 0,
        "result": {**_result(), "gateway_metered": True},
    }
    _status, _error, result = finalize_runner_completion(message, pending_job=lease)
    assert result["gateway_metered"] is False


def _patch_rows(monkeypatch, agent_type):
    flow = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        allowed_mcp_tools=None,
        allowed_mcp_servers=None,
        git_clone_config=None,
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
    job = {
        "execution_id": str(execution.id),
        "flow_id": str(flow.id),
        "agent_type": agent_type,
        "prompt": "review",
        "host_exec_profile": "seat",
        "agent_config": {"host_exec_profile": "seat"},
        "completion_protocol": "host_exec",
    }
    return flow, execution, job


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_type", ["codex", "claude_code"])
async def test_delivery_adds_the_execution_marker(monkeypatch, agent_type):
    flow, execution, job = _patch_rows(monkeypatch, agent_type)
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    delivery = hydrated[HOST_EXEC_GATEWAY_LEASE_KEY]
    assert delivery["header"] == EXECUTION_MARKER_HEADER
    claims = decode_execution_marker(delivery["marker"])
    assert claims == {
        "account_id": flow.account_id,
        "flow_id": flow.id,
        "execution_id": execution.id,
    }
    assert HOST_EXEC_GATEWAY_LEASE_KEY not in job


@pytest.mark.asyncio
async def test_delivery_skips_harnesses_without_a_gateway_route(monkeypatch):
    _flow, _execution, job = _patch_rows(monkeypatch, "copilot")
    hydrated = await hydrate_host_exec_job(MagicMock(), job)
    assert HOST_EXEC_GATEWAY_LEASE_KEY not in hydrated


def test_host_sessions_report_gateway_metering_from_gateway_rows(monkeypatch):
    from preloop.services import host_exec_usage

    monkeypatch.setattr(
        host_exec_usage.crud_api_usage,
        "list_imported_rows_for_flow_execution",
        MagicMock(return_value=[]),
    )
    _count(monkeypatch, 2)
    summary = host_exec_usage.summarize_host_exec_usage(
        MagicMock(), account_id=uuid4(), execution_id=uuid4()
    )
    assert summary["gateway_metered"] is True
    _count(monkeypatch, 0)
    summary = host_exec_usage.summarize_host_exec_usage(
        MagicMock(), account_id=uuid4(), execution_id=uuid4()
    )
    assert summary["gateway_metered"] is False


@pytest.mark.asyncio
async def test_settled_count_waits_for_a_late_streamed_row(monkeypatch):
    from preloop.services.host_exec_gateway import count_gateway_requests_settled

    counts = iter([0, 0, 3])
    monkeypatch.setattr(
        "preloop.services.host_exec_gateway._gateway_request_count",
        lambda _db, _id: next(counts),
    )
    count = await count_gateway_requests_settled(
        MagicMock(), execution_id=uuid4(), pending_job=_lease(), delay_seconds=0
    )
    assert count == 3


@pytest.mark.asyncio
async def test_settled_count_does_not_wait_for_unroutable_harnesses(monkeypatch):
    from preloop.services.host_exec_gateway import count_gateway_requests_settled

    calls = []
    monkeypatch.setattr(
        "preloop.services.host_exec_gateway._gateway_request_count",
        lambda _db, _id: calls.append(1) or 0,
    )
    count = await count_gateway_requests_settled(
        MagicMock(),
        execution_id=uuid4(),
        pending_job=_lease(harness="copilot_cli"),
        delay_seconds=0,
    )
    assert count == 0 and len(calls) == 1


@pytest.mark.asyncio
async def test_settled_count_does_not_wait_without_model_output(monkeypatch):
    from preloop.services.host_exec_gateway import count_gateway_requests_settled

    calls = []
    monkeypatch.setattr(
        "preloop.services.host_exec_gateway._gateway_request_count",
        lambda _db, _id: calls.append(1) or 0,
    )
    await count_gateway_requests_settled(
        MagicMock(),
        execution_id=uuid4(),
        pending_job=_lease(),
        status="FAILED",
        result={"status": "failure"},
        delay_seconds=0,
    )
    assert len(calls) == 1
