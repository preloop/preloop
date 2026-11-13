"""Regression coverage for controller-owned review completion evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.review_completion_evidence import (
    completion_projection,
    timings_projection,
)


@pytest.mark.asyncio
async def test_result_is_stored_before_a_later_capture_failure() -> None:
    """A checkpoint transfer cannot discard a verdict already read."""
    orch = FlowExecutionOrchestrator(MagicMock(), uuid4(), {}, AsyncMock())
    orch.execution_log = SimpleNamespace(id=uuid4(), result=None)
    orch.flow = SimpleNamespace(result_schema=None, validate_result=False)
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(return_value={"review": "ok"})
    )
    with (
        patch.object(
            orch,
            "_capture_evidence_archive",
            AsyncMock(side_effect=RuntimeError("transfer failed")),
        ),
        patch.object(orch, "_capture_workspace_snapshot", AsyncMock()),
        patch.object(
            orch, "_persist_cra_result_boundary", side_effect=lambda doc, **_: doc
        ),
        patch.object(orch, "_store_captured_result", create=True) as store,
    ):
        with pytest.raises(RuntimeError, match="transfer failed"):
            await orch._capture_result_artifact(executor, "session")
    store.assert_called_once()


@pytest.mark.parametrize(
    "status,category,expected",
    [
        ("SUCCEEDED", None, "completed"),
        ("FAILED", "timeout", "timeout"),
        ("FAILED", "budget_exceeded", "budget"),
        ("FAILED", "default_limit_reached", "budget"),
        ("FAILED", "model_auth", "upstream_error"),
        ("FAILED", "provider_billing", "upstream_error"),
        ("FAILED", "agent_no_progress", "no_result"),
        ("STOPPED", None, "stopped"),
        ("FAILED", "setup_failed", "failed"),
        ("RUNNING", None, None),
    ],
)
def test_terminal_causes_and_recorded_phase_boundaries(
    status: str, category: str | None, expected: str | None
) -> None:
    execution = SimpleNamespace(
        status=status,
        failure_category=category,
        result=None,
        created_at="2030-01-01T00:00:00Z",
        launch_requested_at="2030-01-01T00:00:04Z",
        end_time="2030-01-01T00:01:00Z",
        startup_receipt={
            "agent_started_at": "2030-01-01T00:00:20Z",
            "_phases": {
                "clone_started": "2030-01-01T00:00:06Z",
                "clone_finished": "2030-01-01T00:00:10Z",
                "setup_started": "2030-01-01T00:00:11Z",
                "setup_finished": "2030-01-01T00:00:19Z",
            },
            "_completion": {"finalization_started_at": "2030-01-01T00:00:55Z"},
        },
    )
    timing = timings_projection(
        execution,
        [
            {
                "duration": 12,
                "upstream_retries": 1,
                "is_retry": False,
                "error_class": "upstream_auth",
                "upstream_parameters": {"reasoning_effort": "low", "max_tokens": 8192},
            }
        ],
    )
    assert timing["terminal_cause"] == expected
    assert timing["queue_wait_seconds"] == 4
    assert timing["startup_seconds"] == 16
    assert timing["clone_seconds"] == 4
    assert timing["setup_seconds"] == 8
    assert timing["finalization_seconds"] == 5
    assert timing["first_model_response_seconds"] is None
    assert timing["first_causal_upstream_error_class"] == (
        "upstream_auth" if expected == "upstream_error" else None
    )


def test_legacy_missing_evidence_is_not_inferred_from_exit_or_registration() -> None:
    execution = SimpleNamespace(
        status="FAILED",
        failure_category="agent_no_progress",
        result=None,
        startup_receipt=None,
        result_schema_validation=None,
    )
    completion = completion_projection(execution)
    assert completion["agent_finished"] is None
    assert completion["result_captured"] == {
        "captured": False,
        "source": None,
        "result_sha256": None,
    }
    assert completion["result_valid"] is None
    timing = timings_projection(execution, [{"duration": 10}])
    assert timing["retry_count"] is None
    assert timing["effective_requests"][0]["model"] is None
    assert timing["startup_seconds"] is None


def test_completion_event_waits_for_capture_to_settle() -> None:
    """An externally terminal row cannot announce completion during capture."""
    from preloop.services.event_webhooks.emitters import emit_flow_execution_finished

    execution = SimpleNamespace(
        id=uuid4(),
        status="FAILED",
        startup_receipt={"_completion": {"capture_state": "pending"}},
        result=None,
    )
    with patch(
        "preloop.services.event_webhooks.emitters.outbox.enqueue_event"
    ) as queue:
        emit_flow_execution_finished(
            MagicMock(), execution, SimpleNamespace(), status="FAILED"
        )
        queue.assert_not_called()
        execution.startup_receipt["_completion"]["capture_state"] = "absent"
        emit_flow_execution_finished(
            MagicMock(), execution, SimpleNamespace(), status="FAILED"
        )
        queue.assert_called_once()


@pytest.mark.asyncio
async def test_valid_capture_is_not_replaced_by_later_failure_document() -> None:
    orch = FlowExecutionOrchestrator(MagicMock(), uuid4(), {}, AsyncMock())
    orch.flow = SimpleNamespace(
        result_schema={"type": "object", "required": ["review"]}, validate_result=True
    )
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(
            side_effect=[{"review": "Done"}, {"error": "checkpoint failed"}]
        )
    )
    with (
        patch.object(orch, "_capture_evidence_archive", AsyncMock()),
        patch.object(orch, "_capture_workspace_snapshot", AsyncMock()),
        patch.object(
            orch, "_persist_cra_result_boundary", side_effect=lambda doc, **_: doc
        ),
    ):
        assert await orch._capture_result_artifact(executor, "session") == {
            "review": "Done"
        }
        assert await orch._capture_result_artifact(executor, "session") == {
            "review": "Done"
        }
    assert orch._result_schema_validation["valid"] is True
    assert "already captured" in orch._retry_decision(
        {"status": "FAILED", "exit_code": 1}
    )


@pytest.mark.parametrize("mode", ["native", "transcode"])
def test_effective_controls_match_the_local_upstream_wire(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Exercise Responses and translated Chat against a local fake provider."""
    from preloop.services.openai_gateway import OpenAIGatewayService
    from tests.services.test_openai_compatible_reasoning_wire import codex, rig, stub

    recorded: list[dict[str, object]] = []
    server, seen = stub(200)
    try:
        client = rig(
            monkeypatch,
            "openai-compatible",
            f"http://127.0.0.1:{server.server_port}/v1",
            mode,
        )

        def record(
            service: OpenAIGatewayService, *args: object, **kwargs: object
        ) -> None:
            recorded.append(dict(service._last_upstream_parameters))

        monkeypatch.setattr(OpenAIGatewayService, "_record_gateway_request", record)
        monkeypatch.setattr(OpenAIGatewayService, "_defer_stream_record", record)
        payload = codex()
        payload["max_output_tokens"] = 2048
        response = client.post("/openai/v1/responses", json=payload)
        assert response.status_code == 200
        assert recorded
        body = seen[-1]["body"]
        actual = recorded[-1]
        assert str(actual["model"]).endswith(body["model"])
        assert (
            actual["max_tokens"]
            == body.get("max_output_tokens", body.get("max_tokens"))
            == 2048
        )
        effort = body.get("reasoning_effort") or body.get("reasoning", {}).get("effort")
        assert actual.get("reasoning_effort") == effort
        assert effort == ("medium" if mode == "native" else None)
        assert set(actual) <= {"model", "max_tokens", "reasoning_effort"}
    finally:
        server.shutdown()
        server.server_close()
