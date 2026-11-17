"""A finished native review is not lost to a missing field or to the clock.

Orchestrator-level: a fake agent executor stands in for the container, the
monitor and the finalize gates (result schema, review integrity with its
inspection floor) are the real ones. No database or model provider is used.
"""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.agents.base import AgentExecutionResult, AgentStatus
from preloop.agents.codex import NATIVE_RESULT_REPAIR_CONTINUATIONS
from preloop.services.flow_orchestrator import (
    ExecutionDeadline,
    FlowExecutionOrchestrator,
    TimeoutBudget,
    _utcnow,
)
from tests.agents.test_native_result_repair import SCHEMA, _blocks, _context, _run
from tests.agents.test_native_result_repair import review as make_review

HEAD = "a" * 40
BASE = "b" * 40


def orchestrator() -> FlowExecutionOrchestrator:
    """A restricted-CI read-only native review with isolated persistence."""
    orch = FlowExecutionOrchestrator(MagicMock(), "flow", {}, MagicMock())
    orch.flow = SimpleNamespace(
        result_schema=SCHEMA,
        validate_result=True,
        git_clone_config={"publication_mode": "read_only"},
        agent_config={"native_result": True},
        agent_type="codex",
        prompt_template="Review",
        timeout_seconds=300,
    )
    orch.execution_log = SimpleNamespace(
        id="execution",
        ci_review_binding={"head_sha": HEAD, "base_sha": BASE},
        startup_receipt=None,
    )
    orch._native_result_effective = True
    orch._agent_exec_started = True
    orch._persist_cra_result_boundary = lambda value, **kwargs: value
    orch._store_captured_result = MagicMock()
    orch._capture_evidence_archive = AsyncMock()
    orch._capture_workspace_snapshot = AsyncMock()
    orch._record_completion = MagicMock()
    orch.execution_logger = MagicMock()
    return orch


def executor(artifact: Any, status: AgentStatus) -> AsyncMock:
    """A fake container that reports ``status`` and holds ``artifact``."""
    fake = AsyncMock()
    fake.get_status = AsyncMock(return_value=status)
    fake.get_result = AsyncMock(
        return_value=AgentExecutionResult(
            status=AgentStatus.SUCCEEDED,
            session_reference="native-session",
            output_summary="agent finished",
            exit_code=0,
        )
    )
    fake.get_result_artifact = AsyncMock(
        return_value={
            "_preloop_native_result_candidates_v1": {"native": artifact, "file": None}
        }
    )

    async def no_logs(session_reference: str) -> Any:
        if False:  # pragma: no cover - makes this an async generator
            yield

    fake.stream_logs = no_logs
    return fake


async def finalize(
    orch: FlowExecutionOrchestrator, fake: AsyncMock, *, inspected: bool = True
) -> tuple[str, dict[str, Any]]:
    """Monitor the fake agent, then apply the finalize gates in order."""
    with (
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.get_stop_request",
            return_value=None,
        ),
        patch(
            "preloop.models.crud.execution_tool_evidence.inspection_summary",
            return_value={"diff_commands": int(inspected)},
        ),
        patch.object(orch, "_listen_for_commands", AsyncMock()),
        patch.object(orch, "_stream_logs_to_nats", AsyncMock()),
        patch.object(orch, "_publish_update", AsyncMock()),
        patch.object(orch, "_park_if_requested", AsyncMock(return_value=None)),
        patch.object(orch, "_check_no_progress", AsyncMock(return_value=False)),
        patch.object(
            orch, "_sync_runtime_tool_activity_metrics", AsyncMock(return_value=None)
        ),
        patch("preloop.services.flow_orchestrator.asyncio.sleep", AsyncMock()),
    ):
        result = await orch._monitor_agent_execution("native-session", fake)
        status, result = orch._apply_result_schema_gate(result["status"], result)
        status, result, _ = orch._apply_review_integrity_gate(
            status, result, result.get("result")
        )
    return status, result


# --- defect 1: invalid then valid answer -----------------------------------


@pytest.mark.asyncio
async def test_repaired_answer_finalizes_as_a_review(tmp_path: Path) -> None:
    calls, answer, _ = _run(
        tmp_path, _blocks(_context()), [make_review(findings=...), make_review()]
    )
    assert calls == 2
    document = json.loads(answer.read_text())
    # The fixture repository's SHAs differ from the bound ones; rebind.
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert status == "SUCCEEDED", result.get("error_message")
    assert orch._result_schema_validation["valid"] is True
    assert result["result"]["findings"]


@pytest.mark.asyncio
async def test_answer_still_invalid_after_repairs_fails_as_before(
    tmp_path: Path,
) -> None:
    calls, answer, _ = _run(tmp_path, _blocks(_context()), [make_review(findings=...)])
    assert calls == 1 + NATIVE_RESULT_REPAIR_CONTINUATIONS
    document = json.loads(answer.read_text())
    document.update(head_sha=HEAD, base_sha=BASE)
    orch = orchestrator()
    status, result = await finalize(orch, executor(document, AgentStatus.SUCCEEDED))
    assert status == "FAILED"
    assert result["failure_category"] == "result_schema_invalid"
    assert "$.findings" in result["error_message"]
    assert orch._result_schema_validation["errors"][0]["path"] == "$.findings"


# --- defect 2: valid answer, then the time limit ---------------------------


def expired(orch: FlowExecutionOrchestrator) -> None:
    """Put the execution past its 300 s budget before the first poll."""
    budget = TimeoutBudget(seconds=300, source="flow")
    orch._execution_timeout_budget = lambda: budget  # type: ignore[method-assign]
    orch._deadline = ExecutionDeadline(
        anchor=_utcnow() - timedelta(seconds=305), budget_seconds=300
    )


@pytest.mark.asyncio
async def test_ready_review_at_the_time_limit_finalizes_as_a_review() -> None:
    orch = orchestrator()
    expired(orch)
    fake = executor(make_review(head_sha=HEAD, base_sha=BASE), AgentStatus.RUNNING)
    status, result = await finalize(orch, fake)
    assert status == "SUCCEEDED", result.get("error_message")
    assert result["error_message"] is None
    assert result["result"]["verdict"] == "changes_requested"
    orch._record_completion.assert_any_call(time_limit_after_result=True)
    milestones = [c.args[0] for c in orch.execution_logger.log_milestone.mock_calls]
    assert "agent_execution_timeout" in milestones
    assert "native_result_ready_at_time_limit" in milestones
    fake.stop.assert_awaited()


@pytest.mark.parametrize(
    "document,inspected",
    [
        (make_review(head_sha=HEAD, base_sha=BASE, findings=...), True),
        (make_review(head_sha=HEAD, base_sha=HEAD), True),
        (make_review(head_sha=HEAD, base_sha=BASE, review="..."), True),
        (make_review(head_sha=HEAD, base_sha=BASE), False),
        (None, True),
    ],
    ids=["schema", "binding", "degenerate", "not-inspected", "no-result"],
)
@pytest.mark.asyncio
async def test_time_limit_keeps_timeout_when_any_check_fails(
    document: Any, inspected: bool
) -> None:
    orch = orchestrator()
    expired(orch)
    status, result = await finalize(
        orch, executor(document, AgentStatus.RUNNING), inspected=inspected
    )
    assert status == "FAILED"
    assert "timed out" in result["error_message"].lower() or "timeout" in (
        result["error_message"].lower()
    )
    milestones = [c.args[0] for c in orch.execution_logger.log_milestone.mock_calls]
    assert "native_result_ready_at_time_limit" not in milestones


@pytest.mark.asyncio
async def test_time_limit_without_native_results_is_unchanged() -> None:
    orch = orchestrator()
    orch._native_result_effective = False
    orch.flow.agent_config = {}
    expired(orch)
    status, result = await finalize(
        orch, executor(make_review(head_sha=HEAD, base_sha=BASE), AgentStatus.RUNNING)
    )
    assert status == "FAILED"
    assert result["error_message"]
