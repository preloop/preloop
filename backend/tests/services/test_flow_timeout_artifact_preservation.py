"""Timeout cleanup preserves available artifacts before destructive runtime stop."""

import asyncio
import io
import json
import tarfile
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from preloop.services import flow_orchestrator as module
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator, TimeoutBudget


def expired_monitor() -> FlowExecutionOrchestrator:
    """Use the real monitor and capture paths without a database or live runtime."""
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator._execution_timeout_budget = lambda: TimeoutBudget(900, "flow")
    orchestrator._execution_deadline = lambda budget: SimpleNamespace(
        deadline=datetime.now(timezone.utc),
        elapsed_seconds=lambda: 900,
        expired=lambda: True,
    )
    orchestrator.execution_logger = MagicMock()
    orchestrator.execution_logger.get_actions_taken.return_value = []
    orchestrator.execution_logger.get_mcp_usage_logs.return_value = []
    orchestrator._listen_for_commands = AsyncMock()
    orchestrator._stream_logs_to_nats = AsyncMock()
    orchestrator._cleanup_monitoring = AsyncMock()
    orchestrator._name_stream_stall = MagicMock()
    orchestrator._capture_evidence_archive = AsyncMock()
    orchestrator._capture_workspace_snapshot = AsyncMock()
    orchestrator._evidence_bytes_for_result_extract = AsyncMock(return_value=None)
    orchestrator._persist_cra_result_boundary = MagicMock(
        side_effect=lambda artifact, session_reference: artifact
    )
    orchestrator._check_result_schema = MagicMock()
    return orchestrator


@pytest.mark.asyncio
async def test_timeout_preserves_artifact_before_job_deletion() -> None:
    orchestrator = expired_monitor()
    artifact = {"status": "success", "summary": "Completed review"}
    pod_exists = True
    executor = MagicMock(cleanup=AsyncMock())

    async def delete_job(reference: str) -> None:
        nonlocal pod_exists
        pod_exists = False

    async def capture(reference: str) -> dict | None:
        # Kubernetes foreground deletion removes the logs/files needed by
        # artifact retrieval; Docker's retained-container semantics differ.
        return artifact if pod_exists else None

    executor.stop = AsyncMock(side_effect=delete_job)
    executor.get_result_artifact = AsyncMock(side_effect=capture)
    result = await orchestrator._monitor_agent_execution("example-job", executor)

    assert result["result"] == artifact
    assert result["status"] == "FAILED"
    assert "timed out" in result["error_message"]
    orchestrator._check_result_schema.assert_called_once_with(artifact)
    executor.stop.assert_awaited_once_with("example-job")
    executor.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_hung_result_getter_is_bounded_and_runtime_still_stops(
    monkeypatch: Any,
) -> None:
    orchestrator = expired_monitor()
    monkeypatch.setattr(module, "TIMEOUT_ARTIFACT_CAPTURE_SECONDS", 0.01)
    canceled = asyncio.Event()

    async def capture(reference: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            canceled.set()

    executor = MagicMock(
        stop=AsyncMock(),
        cleanup=AsyncMock(),
        get_result_artifact=AsyncMock(side_effect=capture),
    )
    result = await asyncio.wait_for(
        orchestrator._monitor_agent_execution("example-job", executor), timeout=1
    )
    assert canceled.is_set()
    assert result["status"] == "FAILED"
    assert "timed out" in result["error_message"]
    assert result["result"] is None
    executor.stop.assert_awaited_once()
    executor.cleanup.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transfer", ["_capture_evidence_archive", "_capture_workspace_snapshot"]
)
async def test_slow_bulk_transfer_preserves_small_result_and_validation(
    monkeypatch: Any, transfer: str
) -> None:
    orchestrator = expired_monitor()
    monkeypatch.setattr(module, "TIMEOUT_ARTIFACT_CAPTURE_SECONDS", 0.01)
    artifact = {"status": "success", "summary": "Completed review"}

    async def hang(*args: Any) -> None:
        await asyncio.Event().wait()

    setattr(orchestrator, transfer, AsyncMock(side_effect=hang))
    executor = MagicMock(
        stop=AsyncMock(),
        cleanup=AsyncMock(),
        get_result_artifact=AsyncMock(return_value=artifact),
    )
    result = await asyncio.wait_for(
        orchestrator._monitor_agent_execution("example-job", executor), timeout=1
    )
    assert result["result"] == artifact
    assert result["status"] == "FAILED"
    orchestrator._persist_cra_result_boundary.assert_called_once_with(
        artifact, session_reference="example-job"
    )
    orchestrator._check_result_schema.assert_called_once_with(artifact)
    executor.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_capture_error_keeps_timeout_and_stops_runtime() -> None:
    orchestrator = expired_monitor()
    orchestrator._capture_evidence_archive = AsyncMock(
        side_effect=RuntimeError("unavailable")
    )
    executor = MagicMock(
        stop=AsyncMock(),
        cleanup=AsyncMock(),
        get_result_artifact=AsyncMock(return_value=None),
    )
    result = await orchestrator._monitor_agent_execution("example-job", executor)
    assert result["status"] == "FAILED"
    assert "timed out" in result["error_message"]
    assert result["result"] is None
    executor.stop.assert_awaited_once()
    executor.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancellation_during_capture_still_stops_runtime() -> None:
    orchestrator = expired_monitor()
    entered = asyncio.Event()

    async def capture(reference: str) -> None:
        entered.set()
        await asyncio.Event().wait()

    executor = MagicMock(
        stop=AsyncMock(),
        cleanup=AsyncMock(),
        get_result_artifact=AsyncMock(side_effect=capture),
    )
    task = asyncio.create_task(
        orchestrator._monitor_agent_execution("example-job", executor)
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    executor.stop.assert_awaited_once()
    executor.cleanup.assert_awaited_once()


@pytest.mark.asyncio
async def test_evidence_fallback_survives_slow_workspace_capture(
    monkeypatch: Any,
) -> None:
    orchestrator = expired_monitor()
    monkeypatch.setattr(module, "TIMEOUT_ARTIFACT_CAPTURE_SECONDS", 0.01)
    artifact = {"status": "success", "summary": "Completed review"}
    payload = json.dumps(artifact).encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("result.json")
        entry.size = len(payload)
        archive.addfile(entry, io.BytesIO(payload))
    orchestrator._evidence_bytes_for_result_extract = AsyncMock(
        return_value=buffer.getvalue()
    )

    async def hang(*args: Any) -> None:
        await asyncio.Event().wait()

    orchestrator._capture_workspace_snapshot = AsyncMock(side_effect=hang)
    executor = MagicMock(
        stop=AsyncMock(),
        cleanup=AsyncMock(),
        get_result_artifact=AsyncMock(return_value=None),
    )
    result = await asyncio.wait_for(
        orchestrator._monitor_agent_execution("example-job", executor), timeout=1
    )
    assert result["result"] == artifact
    assert result["status"] == "FAILED"
    orchestrator._check_result_schema.assert_called_once_with(artifact)
    executor.stop.assert_awaited_once()
