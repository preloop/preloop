"""Hosted and private-runner persist ingestion share the same CRA boundary."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from preloop.cra.persist import apply_cra_persist_boundary
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

from .conftest import clone, make_evidence_archive


def _event_data() -> dict[str, Any]:
    return {
        "source": "github",
        "type": "issue_created",
        "event_id": "evt_123456",
        "payload": {"repository": "example/repo"},
        "account_id": str(uuid4()),
    }


def _hosted_orchestrator(*, prompt: str | None) -> FlowExecutionOrchestrator:
    orchestrator = FlowExecutionOrchestrator(
        db=MagicMock(),
        flow_id=uuid4(),
        trigger_event_data=_event_data(),
        nats_client=MagicMock(),
    )
    orchestrator._agent_exec_started = True
    orchestrator.execution_log = type(
        "Log", (), {"id": uuid4(), "resolved_input_prompt": prompt}
    )()
    orchestrator.flow = type("Flow", (), {"prompt_template": prompt})()
    orchestrator._sync_runtime_tool_activity_metrics = AsyncMock(return_value=None)
    orchestrator._capture_evidence_archive = AsyncMock(return_value=None)
    orchestrator._capture_workspace_snapshot = AsyncMock(return_value=None)
    return orchestrator


@pytest.mark.asyncio
async def test_hosted_path_persists_valid_fail_as_completed(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    payload["valid"] = False
    payload["minimum_elements"]["passed"] = False
    payload["verdict"] = "fail"
    prompt = (
        "Required shape (preloop.cra.sbomaudit/v1): "
        '{ "schema": "preloop.cra.sbomaudit/v1" }'
    )
    orchestrator = _hosted_orchestrator(prompt=prompt)
    executor = AsyncMock()
    executor.get_result_artifact = AsyncMock(return_value=payload)
    artifact = await orchestrator._capture_result_artifact(executor, "hosted-session")
    status, _error = orchestrator._apply_cra_fail_closed("SUCCEEDED", None)
    assert status == "SUCCEEDED"
    assert artifact is not None
    assert artifact["verdict"] == "fail"


@pytest.mark.asyncio
async def test_hosted_path_expected_cra_missing_schema_fails() -> None:
    prompt = (
        "Required shape (preloop.cra.releaseaudit/v1): "
        '{ "schema": "preloop.cra.releaseaudit/v1" }'
    )
    orchestrator = _hosted_orchestrator(prompt=prompt)
    executor = AsyncMock()
    executor.get_result_artifact = AsyncMock(return_value={"status": "success"})
    artifact = await orchestrator._capture_result_artifact(executor, "hosted-session")
    status, _error = orchestrator._apply_cra_fail_closed("SUCCEEDED", None)
    assert status == "FAILED"
    assert artifact is not None
    assert artifact["error"] in {"cra_result_invalid", "cra_result_missing"}
    assert "raw" in artifact
    original = "container OOM after the audit command"
    failed_status, combined = orchestrator._apply_cra_fail_closed("FAILED", original)
    assert failed_status == "FAILED"
    assert combined is not None
    assert original in combined
    assert "failed contract validation" in combined


def test_private_runner_path_wraps_malformed_known_schema(
    vulnscan_result: dict[str, Any],
) -> None:
    payload = clone(vulnscan_result)
    del payload["gate"]
    prompt = (
        "Required shape (preloop.cra.vulnscan/v1): "
        '{ "schema": "preloop.cra.vulnscan/v1" }'
    )
    decision = apply_cra_persist_boundary(payload, prompt=prompt)
    assert decision.invalid
    assert decision.fail_closed_status == "FAILED"
    assert decision.artifact is not None
    assert decision.artifact["raw"]["schema"] == "preloop.cra.vulnscan/v1"


def test_private_runner_non_cra_completion_unchanged() -> None:
    payload = {"status": "success", "harness": "cursor_cli"}
    decision = apply_cra_persist_boundary(
        payload, prompt="Implement a focused fix and open a pull request."
    )
    assert not decision.invalid
    assert decision.validation.skipped
    assert decision.artifact == payload


@pytest.mark.asyncio
async def test_hosted_extracts_result_from_evidence_archive(
    sbomaudit_result: dict[str, Any],
) -> None:
    payload = clone(sbomaudit_result)
    payload["valid"] = False
    payload["minimum_elements"]["passed"] = False
    payload["verdict"] = "fail"
    prompt = (
        "Required shape (preloop.cra.sbomaudit/v1): "
        '{ "schema": "preloop.cra.sbomaudit/v1" }'
    )
    orchestrator = _hosted_orchestrator(prompt=prompt)
    archive = make_evidence_archive(payload)

    async def stash_archive(*_args: Any, **_kwargs: Any) -> None:
        orchestrator._evidence_archive = archive

    orchestrator._capture_evidence_archive = stash_archive
    executor = AsyncMock()
    executor.get_result_artifact = AsyncMock(return_value=None)
    artifact = await orchestrator._capture_result_artifact(executor, "hosted-session")
    status, _error = orchestrator._apply_cra_fail_closed("SUCCEEDED", None)
    assert status == "SUCCEEDED"
    assert artifact is not None
    assert artifact["verdict"] == "fail"
    assert artifact["schema"] == "preloop.cra.sbomaudit/v1"
