"""Admission probes apply to new runs, never trusted resumptions of model work."""

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import litellm
import pytest
from sqlalchemy.orm import Session

from preloop.agents.base import AgentStatus
from preloop.config import settings
from preloop.models import crud, models
from preloop.models.crud import flow_artifact
from preloop.models.schemas.flow_execution import FlowExecutionCreate
from preloop.models.schemas.flow import FlowUpdate
from preloop.services.audit import service as audit_service
from preloop.services import approval_park, execution_recovery, flow_execution_runner
from preloop.services.flow_artifacts import artifact_reference
from preloop.services.flow_feedback import _reconcile, resolve_native_checkpoint
from preloop.services.flow_feedback_provider import FeedbackState
from preloop.services.flow_orchestrator import FlowExecutionOrchestrator
from preloop.services.flow_trigger_service import FlowTriggerService
from preloop.services.model_credential_probe import CredentialProbeResult
from preloop.services.model_gateway_auth import ModelGatewayAuthContext, NO_BEARER_TOKEN
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.model_routing import (
    ModelCredentialsRejectedError,
    prepare_execution_routing,
)
from preloop.services.openai_gateway import OpenAIGatewayService
from tests.services.test_model_credential_health import STORED_KEY, _stored_model
from tests.services.test_model_routing import _flow, _patch_dispatch


@pytest.fixture(autouse=True)
def isolate_gateway_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep audit enabled and external notification delivery off in this suite."""
    monkeypatch.setattr(audit_service, "_audit_service", audit_service.AuditService())
    monkeypatch.setattr(
        "preloop.services.openai_gateway.enqueue_gateway_5xx_alert", MagicMock()
    )
    monkeypatch.setattr(
        "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice",
        MagicMock(),
    )


@pytest.fixture
def started_run(db_session: Session, test_user: models.User) -> SimpleNamespace:
    """Persist an admitted run, then record a later rejection of its stored key."""
    model = _stored_model(
        db_session, test_user.account_id, meta_data={"gateway": {"enabled": True}}
    )
    flow = _flow(db_session, test_user, ai_model_id=model.id)
    details = prepare_execution_routing(db_session, flow, {})
    source = crud.crud_flow_execution.create(
        db_session,
        obj_in=FlowExecutionCreate(
            flow_id=flow.id, status="RUNNING", trigger_event_details=details
        ),
    )
    source = crud.crud_flow_execution.update(
        db_session,
        db_obj=source,
        obj_in={
            "agent_session_reference": "synthetic-existing-session",
            "cli_session": {"agent_type": "codex", "session_id": str(uuid4())},
        },
    )
    assert model.credentials_rejected is False
    crud.crud_secret_reference.record_upstream_auth_rejection(
        db_session,
        ai_model_id=model.id,
        provider="openrouter",
        status_code=401,
        message="Invalid API key",
        now=datetime.now(UTC),
        commit=True,
    )
    db_session.refresh(model)
    assert model.credentials_rejected is True
    return SimpleNamespace(model=model, flow=flow, source=source, user=test_user)


async def _fail_at_next_model_call(
    db: Session,
    run: SimpleNamespace,
    execution: models.FlowExecution,
    *,
    claim: bool = False,
) -> None:
    """Drive real resume finalization from a mocked upstream authentication failure."""
    backend = MagicMock()
    backend.completion.side_effect = litellm.AuthenticationError(
        message="Invalid API key",
        llm_provider="openrouter",
        model="deepseek/deepseek-chat",
    )
    gateway = OpenAIGatewayService(
        db,
        ModelGatewayAuthContext(token=NO_BEARER_TOKEN, user=run.user),
        upstream_backend=backend,
        skip_runtime_session_resolution=True,
    )
    orch = FlowExecutionOrchestrator(
        db,
        flow_id=run.flow.id,
        trigger_event_data=execution.trigger_event_details,
        nats_client=AsyncMock(),
    )
    orch.execution_log = execution

    async def next_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        """The runtime's next ordinary call reaches the provider with the stored key."""
        with patch.object(gateway, "_check_budget", return_value=None):
            with pytest.raises(ModelGatewayAPIError) as error:
                gateway.create_chat_completion(
                    {
                        "model": str(run.model.id),
                        "messages": [{"role": "user", "content": "Continue"}],
                    }
                )
        assert error.value.status_code == 401
        assert error.value.error_class == "upstream_auth"
        return {"status": "FAILED", "error_message": "401 Invalid API key"}

    executor = MagicMock(aclose=AsyncMock(), cleanup=AsyncMock())
    with (
        patch("preloop.agents.create_executor_for_execution", return_value=executor),
        patch.object(orch, "_monitor_agent_execution", side_effect=next_call),
        patch.object(orch, "_notify_terminal", new=AsyncMock()),
        patch.object(orch, "_sync_runtime_session"),
        patch.object(orch, "_revoke_execution_runtime_tokens"),
        patch(
            "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
        ),
    ):
        if claim:
            with (
                patch.object(
                    flow_execution_runner, "get_db_session", return_value=iter([db])
                ),
                patch.object(flow_execution_runner, "get_nats_client", new=AsyncMock()),
                patch.object(
                    flow_execution_runner,
                    "FlowExecutionOrchestrator",
                    return_value=orch,
                ),
                patch.object(db, "close"),
            ):
                result = await flow_execution_runner.claim_and_run_execution(
                    str(execution.id), resume=True
                )
                assert result["final_status"] == "FAILED"
        else:
            await flow_execution_runner.resume_existing_execution(
                orch, "synthetic-session"
            )
    backend.completion.assert_called_once()
    assert backend.completion.call_args.kwargs["api_key"] == STORED_KEY
    db.refresh(execution)
    assert execution.status == "FAILED"
    assert execution.failure_category == "model_auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["inline", "worker"])
async def test_approved_park_resumes_without_probe(
    db_session: Session,
    started_run: SimpleNamespace,
    worker: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native approval decision creates its linked continuation despite a bad key."""
    run = started_run
    request_id = uuid4()
    crud.crud_flow_execution.update(
        db_session,
        db_obj=run.source,
        obj_in={"status": "WAITING_FOR_HUMAN", "park_request_id": request_id},
    )
    request = SimpleNamespace(
        id=request_id,
        status="approved",
        tool_name="Bash",
        tool_args={"_preloop_native_park": {"execution_id": str(run.source.id)}},
        approver_comment="Approved",
        responses=[],
        resolved_at=datetime.now(UTC),
    )
    monkeypatch.setattr(settings, "flow_execution_worker_enabled", worker)
    with (
        patch(
            "preloop.models.db.session.get_session_factory",
            return_value=lambda: nullcontext(db_session),
        ),
        patch.object(approval_park, "_load_request", return_value=request),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new=AsyncMock(),
        ) as dispatch,
        patch.object(
            FlowTriggerService, "_start_flow_execution", new=AsyncMock()
        ) as inline,
        patch(
            "preloop.services.model_credential_probe._probe_rejected_model_credentials",
            return_value=CredentialProbeResult(True, "Invalid API key"),
        ) as probe,
    ):
        resumed = await approval_park.resume_parked_executions(request_id)
        assert len(resumed) == 1
        probe.assert_not_called()
        db_session.refresh(run.source)
        assert str(run.source.resume_execution_id) == resumed[0]
        continuation = crud.crud_flow_execution.get(db_session, id=resumed[0])
        assert continuation.trigger_event_details["_resume"]["execution_id"] == str(
            run.source.id
        )
        assert (
            continuation.trigger_event_details["_answers"]["history"][-1]["status"]
            == "approved"
        )
        assert dispatch.await_count == int(worker)
        assert inline.await_count == int(not worker)
        await _fail_at_next_model_call(db_session, run, continuation)
        probe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("worker", [False, True], ids=["inline", "worker"])
async def test_restart_recovery_resumes_without_probe(
    db_session: Session,
    started_run: SimpleNamespace,
    worker: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both restart paths reattach and retain normal upstream-auth failure handling."""
    import asyncio

    run = started_run
    monkeypatch.setattr(settings, "flow_execution_worker_enabled", worker)
    service = execution_recovery.ExecutionRecoveryService()
    executor = MagicMock(
        get_status=AsyncMock(return_value=AgentStatus.RUNNING), aclose=AsyncMock()
    )

    async def finish(*args: Any, **kwargs: Any) -> bool:
        """Represent the recovered monitor with real resume finalization."""
        await _fail_at_next_model_call(db_session, run, run.source, claim=worker)
        return True

    with (
        patch.object(execution_recovery, "get_nats_client", new=AsyncMock()),
        patch("preloop.agents.create_executor_for_execution", return_value=executor),
        patch.object(service, "_resume_monitoring_task", side_effect=finish),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_resume",
            side_effect=finish,
        ) as dispatch,
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new=AsyncMock(),
        ) as execute,
        patch(
            "preloop.services.model_credential_probe._probe_rejected_model_credentials",
            return_value=CredentialProbeResult(True, "Invalid API key"),
        ) as probe,
    ):
        assert await service.recover_orphaned_executions(db_session) == 1
        await asyncio.gather(*service.recovery_tasks)
        assert dispatch.call_count == int(worker)
        execute.assert_not_called()
        probe.assert_not_called()
    db_session.refresh(run.source)
    assert run.source.status == "FAILED"
    assert run.source.failure_category == "model_auth"


@pytest.mark.asyncio
@pytest.mark.parametrize("persistent", [False, True], ids=["hosted", "persistent"])
async def test_checkpoint_continuation_resumes_without_probe(
    db_session: Session,
    started_run: SimpleNamespace,
    test_tracker: models.Tracker,
    persistent: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reserved feedback turn still resolves its original checkpoint with a bad key."""
    run = started_run
    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    crud.crud_flow.update(
        db_session,
        db_obj=run.flow,
        flow_in=FlowUpdate(
            agent_config={
                "feedback": {"enabled": True, "debounce_seconds": 0},
                **({"execution_path": "persistent"} if persistent else {}),
            },
        ),
        account_id=run.user.account_id,
    )
    crud.crud_flow_execution.update(
        db_session, db_obj=run.source, obj_in={"status": "SUCCEEDED"}
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    thread = crud.crud_flow_feedback.register(
        db_session,
        values={
            "account_id": run.user.account_id,
            "flow_id": run.flow.id,
            "tracker_id": test_tracker.id,
            "repository_id": "123",
            "pr_number": "7",
            "pr_url": "https://example.com/repo/pull/7",
            "provider": "github",
            "branch": "implementation-7",
            "context": {"trigger": {}},
            "policy": {"debounce_seconds": 0},
            "latest_execution_id": run.source.id,
            "active_execution_id": None,
            "due_at": now,
            "expires_at": now + timedelta(days=1),
        },
    )
    artifact = flow_artifact.store(
        db_session,
        values={
            "account_id": run.user.account_id,
            "flow_id": run.flow.id,
            "execution_id": run.source.id,
            "thread_id": str(thread.id),
            "kind": "native_session",
            "manifest": {},
            "manifest_sha256": "a" * 64,
            "ciphertext": b"synthetic-encrypted-checkpoint",
            "expires_at": now + timedelta(hours=1),
        },
        quota_bytes=1024,
        require_execution_open=False,
    )
    session = {
        **run.source.cli_session,
        "artifact_reference": artifact_reference(artifact).model_dump(mode="json"),
    }
    crud.crud_flow_execution.update(
        db_session, db_obj=run.source, obj_in={"cli_session": session}
    )
    provider = SimpleNamespace(
        read=AsyncMock(
            return_value=FeedbackState(
                "synthetic-head",
                feedback=[
                    {
                        "event_key": "review-1",
                        "kind": "review",
                        "head_sha": "synthetic-head",
                        "payload": {"body": "Fix test"},
                    }
                ],
            )
        )
    )
    with (
        patch(
            "preloop.services.flow_feedback.FeedbackProvider.for_thread",
            new=AsyncMock(return_value=provider),
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.flow_execution_worker_enabled",
            return_value=True,
        ),
        patch(
            "preloop.services.flow_execution_dispatcher.dispatch_execute",
            new=AsyncMock(),
        ) as dispatch,
        patch(
            "preloop.services.model_credential_probe._probe_rejected_model_credentials",
            return_value=CredentialProbeResult(True, "Invalid API key"),
        ) as probe,
    ):
        claim = crud.crud_flow_feedback.claim_due(db_session, now=now)[0]
        await _reconcile(db_session, *claim, now=now)
        db_session.refresh(thread)
        assert thread.active_execution_id is not None
        continuation = crud.crud_flow_execution.get(
            db_session, id=thread.active_execution_id
        )
        dispatch.assert_awaited_once_with(continuation.id)
        restored = resolve_native_checkpoint(
            db_session,
            account_id=run.user.account_id,
            flow_id=run.flow.id,
            execution_id=continuation.id,
            resume=continuation.trigger_event_details["_resume"],
        )
        assert restored == session
        await _fail_at_next_model_call(db_session, run, continuation)
        probe.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 429, 500, 503, "timeout"])
@pytest.mark.parametrize("provider", ["openrouter", "openai"])
async def test_failed_run_retry_tests_stored_key_once(
    db_session: Session,
    started_run: SimpleNamespace,
    status: int | str,
    provider: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new retry is admitted only after a fresh verdict; transient errors admit."""
    run = started_run
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-ambient-key")
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-other-ambient-key")
    crud.crud_flow_execution.update(
        db_session, db_obj=run.source, obj_in={"status": "FAILED"}
    )
    if provider == "openai":
        crud.crud_ai_model.update(
            db_session,
            db_obj=run.model,
            obj_in={
                "provider_name": "openai",
                "model_identifier": "gpt-4o",
                "api_endpoint": "https://api.openai.com/v1",
            },
        )
    backend = MagicMock()
    backend.completion.return_value = {
        "usage": {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "total_tokens": 2,
        }
    }
    errors = {
        401: litellm.AuthenticationError,
        429: litellm.RateLimitError,
        500: litellm.InternalServerError,
        503: litellm.ServiceUnavailableError,
        "timeout": litellm.Timeout,
    }
    if status in errors:
        backend.completion.side_effect = errors[status](
            message="Synthetic provider failure",
            model="gpt-4o",
            llm_provider="openai",
        )
    request = httpx.Request("GET", "https://openrouter.ai/api/v1/key")
    nats, start = _patch_dispatch()
    with (
        nats,
        start as dispatch,
        patch("preloop.services.model_credential_probe.httpx.Client") as client,
        patch(
            "preloop.services.openai_gateway.get_model_gateway_backend",
            return_value=backend,
        ),
        patch(
            "preloop.services.model_credential_alerts.enqueue_credential_rejection_notice"
        ),
    ):
        get = client.return_value.__enter__.return_value.get
        get.return_value = httpx.Response(
            status if isinstance(status, int) else 500,
            request=request,
            json={"error": {"message": "Invalid API key"}},
        )
        if status == "timeout":
            get.side_effect = httpx.ReadTimeout("Timed out", request=request)
        service = FlowTriggerService(db_session)
        if status == 401:
            with pytest.raises(ModelCredentialsRejectedError):
                await service.trigger_flow(
                    flow_id=run.flow.id, retry_of_execution_id=run.source.id
                )
            dispatch.assert_not_awaited()
            assert [
                row.id
                for row in crud.crud_flow_execution.get_multi(
                    db_session, flow_id=run.flow.id
                )
            ] == [run.source.id]
        else:
            result = await service.trigger_flow(
                flow_id=run.flow.id, retry_of_execution_id=run.source.id
            )
            assert str(result["id"]) != str(run.source.id)
            retry = crud.crud_flow_execution.get(db_session, id=result["id"])
            assert retry.retry_of_execution_id == run.source.id
            dispatch.assert_awaited_once()
        if provider == "openrouter":
            get.assert_called_once_with(
                "https://openrouter.ai/api/v1/key",
                headers={"Authorization": f"Bearer {STORED_KEY}"},
            )
            backend.completion.assert_not_called()
        else:
            client.assert_not_called()
            backend.completion.assert_called_once()
            kwargs = backend.completion.call_args.kwargs
            assert kwargs["api_key"] == STORED_KEY
            assert kwargs["max_tokens"] == 1
            assert kwargs["num_retries"] == 0
            assert kwargs["timeout"] == 5
            assert "fallbacks" not in kwargs
    usage = crud.crud_api_usage.get_multi(
        db_session, account_id=str(run.user.account_id)
    )
    assert len(usage) == 1
    assert usage[0].endpoint == (
        "/openai/v1/credential-probe"
        if provider == "openrouter"
        else "/openai/v1/chat/completions"
    )
    expected_status = 503 if status == "timeout" else status
    if provider == "openai" and status == 500:
        expected_status = 502
    assert usage[0].status_code == expected_status
    # The gateway stores zero for every count when no usage was returned.
    expected_part = 1 if provider == "openai" and status == 200 else 0
    assert usage[0].prompt_tokens == expected_part
    assert usage[0].completion_tokens == expected_part
    assert usage[0].total_tokens == expected_part * 2
    audit = crud.crud_audit_log.get_by_account(
        db_session, account_id=run.user.account_id, action="model_gateway_request"
    )
    assert len(audit) == 1
    assert STORED_KEY not in str(audit[0].details)


@pytest.mark.parametrize("pin_kind", [None, "continuation", "retry"])
def test_resume_payload_without_trusted_source_still_tests_key(
    db_session: Session,
    started_run: SimpleNamespace,
    pin_kind: str | None,
) -> None:
    """Caller-supplied resume data cannot claim the continuation exemption."""
    run = started_run
    with patch(
        "preloop.services.model_credential_probe._probe_rejected_model_credentials",
        return_value=CredentialProbeResult(True, "Invalid API key"),
    ) as probe:
        with pytest.raises(ModelCredentialsRejectedError):
            prepare_execution_routing(
                db_session,
                run.flow,
                {"_resume": {"execution_id": str(run.source.id)}},
                pin_kind=pin_kind,
            )
        probe.assert_called_once()
