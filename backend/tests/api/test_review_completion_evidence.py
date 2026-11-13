"""Restricted HTTP reads after real result commits and injected later failures."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from preloop.models import crud, models
from preloop.models.crud.base import CRUDBase
from preloop.services.execution_tool_evidence import digest
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.trusted_publisher import PublicationError
from tests.api.test_ci_execution_http import client as make_client, owned_run
from tests.api.test_ci_principal import ci_resources as make_resources


@pytest.fixture
def ci_resources(db_session: Session) -> tuple[Any, ...]:
    return make_resources.__wrapped__(db_session)


@pytest.fixture
def client(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    return make_client.__wrapped__(db_session, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "checkpoint_upload",
        "artifact_quota",
        "artifact_capture",
        "log_shipping",
        "lease_revocation",
        "completion_event_queueing",
    ],
)
async def test_captured_result_survives_later_failure(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _, _, token, context, execution = owned_run(db_session, ci_resources)
    orch = FlowExecutionOrchestrator(db_session, execution.flow_id, {}, AsyncMock())
    orch.execution_log = execution
    orch.flow = crud.crud_flow.get(db_session, id=execution.flow_id)
    orch.flow.result_schema = {
        "type": "object",
        "required": ["review"],
        "properties": {"review": {"type": "string"}},
        "additionalProperties": False,
    }
    orch.flow.validate_result = True
    orch._native_result_effective = True
    document = {"review": "Completed findings"}
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(return_value=document),
        get_workspace_snapshot=AsyncMock(
            side_effect=RuntimeError("synthetic checkpoint failure")
        )
        if failure == "checkpoint_upload"
        else None,
        get_evidence_archive=None,
        evidence_transport_error="artifact_quota_exceeded"
        if failure == "artifact_quota"
        else None,
    )
    monkeypatch.setattr(
        orch,
        "_publish_update",
        AsyncMock(side_effect=RuntimeError("synthetic bus failure"))
        if failure == "log_shipping"
        else AsyncMock(),
    )
    if failure == "artifact_capture":
        monkeypatch.setattr(
            orch,
            "_capture_evidence_archive",
            AsyncMock(side_effect=RuntimeError("synthetic required transfer failure")),
        )
        with pytest.raises(RuntimeError, match="synthetic required transfer failure"):
            await orch._capture_result_artifact(executor, "session")
    else:
        assert await orch._capture_result_artifact(executor, "session") == document
    # Rollback after capture proves it was committed, not merely assigned.
    db_session.rollback()
    db_session.refresh(execution)
    assert execution.result == document
    assert execution.result_schema_validation["valid"] is True
    status = "FAILED" if failure == "artifact_capture" else "SUCCEEDED"
    await orch._update_execution_log(status=status, end_time=datetime.now(timezone.utc))
    if failure == "lease_revocation":
        orch._read_only_review_leases = [MagicMock()]
        with patch(
            "preloop.services.publication_credentials.revoke_repository_lease",
            AsyncMock(side_effect=PublicationError("synthetic revoke failure")),
        ):
            await orch._revoke_read_only_review_leases()
    elif failure == "completion_event_queueing":
        with patch(
            "preloop.services.event_webhooks.emitters.emit_flow_execution_finished",
            side_effect=RuntimeError("synthetic outbox failure"),
        ):
            orch._emit_execution_finished_webhook("SUCCEEDED", None)
    db_session.expire_all()
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result"] == document
    assert body["status"] == status
    assert body["completion"]["result_valid"] is True
    assert body["completion"]["result_captured"] == {
        "captured": True,
        "source": "native_structured_result",
        "result_sha256": digest(document),
    }
    assert failure in body["completion"]["post_result_failures"]
    for private_message in (
        "synthetic checkpoint failure",
        "synthetic bus failure",
        "synthetic revoke failure",
        "synthetic outbox failure",
    ):
        assert private_message not in response.text
    assert (
        body["timings"]
        == client.get(
            f"/api/v1/flows/executions/{execution.id}",
            headers={"Authorization": f"Bearer {token}"},
        ).json()["timings"]
    )


def test_gateway_projection_scopes_requests_and_withheld_digest(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
) -> None:
    _, _, token, context, execution = owned_run(db_session, ci_resources)
    other = owned_run(db_session, ci_resources)[-1]
    document = {
        "review": "Completed findings",
        "nested": {"_private": "private-value", "score": 1},
    }
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "FAILED",
            "failure_category": "model_auth",
            "result": document,
            "result_schema_validation": {
                "valid": False,
                "error_count": 1,
                "errors": [{"path": "$/nested", "message": "private-value"}],
            },
        },
    )
    for run, model in [(execution, "example/model"), (other, "foreign-model")]:
        crud.crud_api_usage.log_gateway_request(
            db_session,
            endpoint="/v1/responses",
            method="POST",
            status_code=401,
            duration=12.5,
            account_id=str(context.account_id),
            flow_execution_id=str(run.id),
            provider_name="openrouter",
            error_class="upstream_auth",
            is_retry=False,
            meta_data={
                "upstream_parameters": {
                    "model": model,
                    "reasoning_effort": "low",
                    "max_tokens": 8192,
                    "api_key": "private-value",
                },
                "upstream_retries": 2,
                "error_detail": "private-value",
                "response": "private-value",
            },
        )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "private-value" not in response.text and "foreign-model" not in response.text
    assert body["completion"]["result_captured"]["result_sha256"] == digest(
        body["result"]
    )
    assert body["result_withheld_paths"] == ["/nested/_private"]
    assert body["schema_validation"]["errors"] == [{"path": "$/nested"}]
    assert body["timings"]["terminal_cause"] == "upstream_error"
    assert body["timings"]["first_causal_upstream_error_class"] == "upstream_auth"
    assert body["timings"]["model_request_count"] == 1
    assert body["timings"]["retry_count"] == 2
    assert body["timings"]["total_model_seconds"] == 12.5
    assert body["timings"]["effective_requests"] == [
        {
            "model": "example/model",
            "provider": "openrouter",
            "reasoning_effort": "low",
            "max_output_tokens": 8192,
            "request_count": 1,
        }
    ]


@pytest.mark.parametrize(
    "category,cause",
    [
        ("timeout", "timeout"),
        ("model_auth", "upstream_error"),
        ("no_confirmation", "no_result"),
    ],
)
def test_owned_status_distinguishes_terminal_causes_without_a_result(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    category: str,
    cause: str,
) -> None:
    """A restricted reader can diagnose a run even when no artifact exists."""
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "FAILED",
            "failure_category": category,
            "result": None,
            "launch_requested_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
            "startup_receipt": {
                "agent_started_at": "2030-01-01T00:00:20Z",
                "_completion": {"capture_state": "absent"},
            },
        },
    )
    headers = {"Authorization": f"Bearer {token}"}
    url = f"/api/v1/flows/executions/{execution.id}"
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["timings"]["terminal_cause"] == cause
    assert response.json()["timings"]["startup_seconds"] == 20
    assert client.get(f"{url}/result", headers=headers).status_code == 404


@pytest.mark.asyncio
async def test_publication_exception_in_full_lifecycle_preserves_result(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    orch = FlowExecutionOrchestrator(db_session, execution.flow_id, {}, AsyncMock())
    orch.execution_log = execution
    orch.flow = crud.crud_flow.get(db_session, id=execution.flow_id)
    orch.flow.result_schema = {
        "type": "object",
        "required": ["review"],
        "properties": {"review": {"type": "string"}},
        "additionalProperties": False,
    }
    orch.flow.validate_result = True
    orch._native_result_effective = True
    document = {"review": "Completed findings"}
    executor = SimpleNamespace(
        get_result_artifact=AsyncMock(return_value=document),
        get_evidence_archive=None,
        get_workspace_snapshot=None,
        evidence_transport_error=None,
    )

    async def finish_agent(context: Any) -> tuple[dict[str, Any], str]:
        result = await orch._capture_result_artifact(executor, "session")
        return {"status": "SUCCEEDED", "result": result, "exit_code": 0}, "session"

    for name in (
        "_get_flow_details",
        "_create_execution_log",
        "_sync_runtime_session",
        "_cleanup_temporary_api_token",
    ):
        monkeypatch.setattr(orch, name, MagicMock())
    for name in (
        "_publish_update",
        "_update_commit_status",
        "_notify_terminal",
        "_start_queued_followup",
        "_replay_persisted_runner_logs",
    ):
        monkeypatch.setattr(orch, name, AsyncMock())
    monkeypatch.setattr(orch, "_resolve_native_result", lambda: True)
    monkeypatch.setattr(orch, "_resolve_backport_plan", lambda: None)
    monkeypatch.setattr(orch, "_plan_delivery_report_run", lambda: None)
    monkeypatch.setattr(
        orch, "_prepare_execution_context", AsyncMock(return_value={"prompt": "Review"})
    )
    monkeypatch.setattr(orch, "_run_agent_with_retries", finish_agent)
    monkeypatch.setattr(
        orch,
        "_finish_isolated_publication",
        AsyncMock(side_effect=RuntimeError("synthetic publication failure")),
    )
    with patch("preloop.services.model_routing.validate_execution_model_principal"):
        await orch.run()
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "FAILED"
    assert body["result"] == document
    assert body["completion"]["result_valid"] is True
    assert body["completion"]["post_result_failures"] == ["publication"]
    assert body["timings"]["terminal_cause"] == "post_result_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("verdict", ["approve", "request-changes"])
async def test_valid_native_verdict_needs_no_status_or_success_marker(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    monkeypatch: pytest.MonkeyPatch,
    verdict: str,
) -> None:
    """Strict native output confirms completion even when the review rejects code."""
    from tests.test_flow_orchestrator import _confirmation_executor

    execution = owned_run(db_session, ci_resources)[-1]
    orch = FlowExecutionOrchestrator(db_session, execution.flow_id, {}, AsyncMock())
    orch.execution_log = execution
    orch.flow = crud.crud_flow.get(db_session, id=execution.flow_id)
    orch.flow.agent_type = "codex"
    orch.flow.agent_config = {"native_result": True}
    orch.flow.result_schema = {
        "type": "object",
        "required": ["verdict"],
        "properties": {"verdict": {"enum": ["approve", "request-changes"]}},
        "additionalProperties": False,
    }
    orch.flow.validate_result = True
    orch._native_result_effective = True
    orch._agent_exec_started = True
    monkeypatch.setattr(orch, "_listen_for_commands", AsyncMock())
    monkeypatch.setattr(orch, "_park_if_requested", AsyncMock(return_value=None))
    monkeypatch.setattr(
        orch, "_sync_runtime_tool_activity_metrics", AsyncMock(return_value=None)
    )
    result = await orch._monitor_agent_execution(
        "session", _confirmation_executor(artifact={"verdict": verdict})
    )
    assert orch._result_schema_validation["valid"] is True
    assert result["status"] == "SUCCEEDED"
    assert not result.get("error_message")
    assert not orch._confirmation_nudge_attempted


def test_terminal_capture_in_progress_is_409_then_captured_result_is_200(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
) -> None:
    """A terminal status written by a stop/limit cannot make a transient 404."""
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    crud.crud_flow_execution.record_completion_evidence(
        db_session, execution_id=execution.id, facts={"capture_state": "pending"}
    )
    CRUDBase(models.FlowExecution).update(
        db_session, db_obj=execution, obj_in={"status": "FAILED"}
    )
    url = f"/api/v1/flows/executions/{execution.id}/result"
    headers = {"Authorization": f"Bearer {token}"}
    pending = client.get(url, headers=headers)
    assert pending.status_code == 409, pending.text
    assert pending.json()["detail"]["code"] == "result_pending"
    crud.crud_flow_execution.record_completion_evidence(
        db_session,
        execution_id=execution.id,
        facts={
            "capture_state": "captured",
            "result_captured_at": "2030-01-01T00:00:00Z",
            "result_source": "native_structured_result",
        },
        result={"review": "Done"},
        validation={"valid": True, "errors": [], "error_count": 0},
    )
    assert client.get(url, headers=headers).status_code == 200


@pytest.mark.parametrize("capture_state", [None, "absent"])
def test_controller_metadata_without_an_artifact_is_404(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    capture_state: str | None,
) -> None:
    """Termination metadata is not proof that result.json was captured."""
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    if capture_state:
        crud.crud_flow_execution.record_completion_evidence(
            db_session,
            execution_id=execution.id,
            facts={"capture_state": capture_state},
        )
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "FAILED",
            "result": {"container_termination": {"reason": "Error", "exit_code": 1}},
        },
    )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404, response.text


def test_later_null_result_write_preserves_a_durable_valid_capture(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
) -> None:
    """Recovery without the original controller's memory retains the result."""
    _, _, token, _, execution = owned_run(db_session, ci_resources)
    document = {"review": "Completed findings"}
    crud.crud_flow_execution.record_completion_evidence(
        db_session,
        execution_id=execution.id,
        facts={
            "capture_state": "captured",
            "result_captured_at": "2030-01-01T00:00:00Z",
            "result_source": "native_structured_result",
        },
        result=document,
        validation={"valid": True, "errors": [], "error_count": 0},
    )
    db_session.expire_all()
    crud.crud_flow_execution.update(
        db_session,
        db_obj=execution,
        obj_in={"status": "FAILED", "result": None, "result_schema_validation": None},
    )
    response = client.get(
        f"/api/v1/flows/executions/{execution.id}/result",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["result"] == document
    assert response.json()["completion"]["result_valid"] is True


def test_completion_points_to_the_publication_packages_projection(
    db_session: Session,
    ci_resources: tuple[Any, ...],
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the package's public projection, never an agent-authored claim."""
    from preloop.api.endpoints import flows

    _, _, token, _, execution = owned_run(db_session, ci_resources)
    CRUDBase(models.FlowExecution).update(
        db_session,
        db_obj=execution,
        obj_in={
            "status": "SUCCEEDED",
            "result": {"review": "Done", "platform_review": {"status": "posted"}},
        },
    )
    url = f"/api/v1/flows/executions/{execution.id}/result"
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(url, headers=headers).json()["completion"]["publication"] is None
    project = flows.execution_projection
    monkeypatch.setattr(
        flows,
        "execution_projection",
        lambda row: {**project(row), "platform_review": {"status": "failed"}},
    )
    response = client.get(url, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["completion"]["publication"] == "#/platform_review"
    assert response.json()["completion"]["post_result_failures"] == ["publication"]
    assert response.json()["status"] == "SUCCEEDED"
