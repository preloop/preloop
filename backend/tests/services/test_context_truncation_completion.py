"""Missing confirmation describes observed work, with upstream causes first."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator


@pytest.mark.asyncio
@pytest.mark.parametrize("gateway_turns", [0, 3])
async def test_no_tools_no_marker_reports_observed_turns_without_native_hint(
    gateway_turns: int,
) -> None:
    """A claimed review in prose must not imply changing the result preset fixes it."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._refetch_exited_session_logs = AsyncMock(
        return_value=[
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 8194}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 16388}}),
        ]
    )
    orchestrator._scan_inplace_nudge_markers = MagicMock()
    orchestrator.execution_logger = MagicMock()
    orchestrator._run_confirmation_nudge = AsyncMock(
        return_value={"outcome": "no_confirmation"}
    )
    orchestrator._accept_wider_completion_signal = MagicMock(return_value=False)
    orchestrator._inplace_nudge_seen = True
    orchestrator._inplace_nudge_unsupported = False
    orchestrator.tool_calls_count = 0
    orchestrator.agent_type = "codex"
    orchestrator._native_result_effective = False
    orchestrator.execution_log = (
        SimpleNamespace(id="execution-example") if gateway_turns else None
    )
    orchestrator.db = MagicMock()
    result = SimpleNamespace(
        status=SimpleNamespace(value="SUCCEEDED"), exit_code=0, error_message=None
    )
    with (
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.current_failure",
            return_value=None,
        ),
        patch(
            "preloop.models.crud.crud_api_usage.get_gateway_usage_for_execution",
            return_value={"api_requests": gateway_turns},
        ),
    ):
        status, message, _ = await orchestrator._resolve_missing_confirmation(
            MagicMock(), "session-example", result, None
        )
    assert status == "FAILED"
    assert f"{max(2, gateway_turns)} model turns, 0 tool calls" in message
    assert "native_result" not in message
    assert "FLOW_EXECUTION_SUCCESS" in message


def test_truncation_failure_survives_agent_success() -> None:
    """Even an asserted success cannot overwrite a gateway truncation verdict."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.execution_log = SimpleNamespace(id="execution-example")
    orchestrator.db = MagicMock()
    with (
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.current_status",
            return_value="FAILED",
        ),
        patch(
            "preloop.services.flow_orchestrator.crud_flow_execution.current_failure",
            return_value={
                "failure_category": "context_truncated_suspected",
                "error_message": "The model server appears to have truncated the prompt.",
            },
        ),
    ):
        fields = {}
        assert orchestrator._guard_status_write("SUCCEEDED", fields) == "FAILED"
    assert fields["failure_category"] == "context_truncated_suspected"
