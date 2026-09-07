"""Disposable PostgreSQL artifact transaction, encryption and isolation tests."""

import io
import tarfile
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from preloop.api.endpoints.flow_artifacts import mint_artifact_capability, router
from preloop.models import models
from preloop.models.crud import flow_artifact as crud
from preloop.models.db.session import get_db_session
from preloop.services.flow_artifacts import get_artifact, put_artifact
from backend.tests.services.test_flow_artifacts import archive_with


@pytest.fixture
def scope(db_session, test_user):
    flow = models.Flow(
        name="Artifact test",
        prompt_template="test",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="RUNNING",
        trigger_event_details={"_session_thread_id": "thread-test"},
    )
    db_session.add(execution)
    db_session.flush()
    return {
        "account_id": test_user.account_id,
        "flow_id": flow.id,
        "thread_id": "thread-test",
        "execution_id": execution.id,
    }


def test_encrypted_roundtrip_and_tenant_isolation(db_session, scope) -> None:
    body = archive_with("workspace/source", b"unpublished source")
    ref = put_artifact(db_session, **scope, kind="workspace", archive=body)
    row = crud.get(
        db_session,
        artifact_id=ref.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    assert b"unpublished source" not in row.ciphertext
    assert bytes(row.ciphertext) != body
    read_scope = {key: value for key, value in scope.items() if key != "execution_id"}
    assert get_artifact(db_session, **read_scope, reference=ref) == body
    with pytest.raises(ValueError, match="missing"):
        get_artifact(db_session, **{**read_scope, "account_id": uuid4()}, reference=ref)
    with pytest.raises(ValueError, match="missing"):
        get_artifact(
            db_session, **{**read_scope, "thread_id": "other-thread"}, reference=ref
        )


def test_cleanup_respects_lease_and_reports_expiry(db_session, scope) -> None:
    ref = put_artifact(
        db_session,
        **scope,
        kind="workspace",
        archive=archive_with("workspace/source"),
    )
    row = crud.get(
        db_session,
        artifact_id=ref.artifact_id,
        **{key: value for key, value in scope.items() if key != "execution_id"},
    )
    now = datetime.now(UTC)
    row.expires_at = now - timedelta(seconds=1)
    row.lease_until = now + timedelta(minutes=1)
    db_session.commit()
    assert crud.cleanup(db_session, now=now) == 0
    assert crud.cleanup(db_session, now=now + timedelta(minutes=2)) == 1
    db_session.refresh(row)
    assert row.ciphertext is None
    assert row.availability == "expired"


def test_interrupted_or_invalid_upload_does_not_replace_latest(
    db_session, scope
) -> None:
    first = put_artifact(
        db_session,
        **scope,
        kind="workspace",
        archive=archive_with("workspace/source"),
    )
    with pytest.raises(ValueError, match="corrupt"):
        put_artifact(db_session, **scope, kind="workspace", archive=b"interrupted")
    latest = crud.latest(db_session, **scope, kind="workspace")
    assert latest.id == first.artifact_id


def test_scoped_http_roundtrip_and_closed_execution(db_session, scope) -> None:
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    token = mint_artifact_capability(**scope, kind="workspace", operation="put")
    url = f"/flows/executions/{scope['execution_id']}/artifacts"
    response = client.put(
        url,
        headers={"Authorization": "Bearer " + token},
        content=archive_with("workspace/source"),
    )
    assert response.status_code == 200, response.text
    from preloop.models.schemas.flow_artifact import ArtifactReference

    reference = ArtifactReference.model_validate(response.json())
    get_token = mint_artifact_capability(
        **scope, kind="workspace", operation="get", reference=reference
    )
    response = client.get(url, headers={"Authorization": "Bearer " + get_token})
    assert response.status_code == 200
    assert (
        client.get(url, headers={"Authorization": "Bearer " + token}).status_code == 403
    )
    wrong = mint_artifact_capability(
        **{**scope, "thread_id": "wrong"}, kind="workspace", operation="put"
    )
    assert (
        client.put(
            url, headers={"Authorization": "Bearer " + wrong}, content=b"anything"
        ).status_code
        == 403
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "SUCCEEDED"
    db_session.commit()
    assert (
        client.put(
            url, headers={"Authorization": "Bearer " + token}, content=b"anything"
        ).status_code
        == 409
    )


def _evidence_body(payload: bytes = b"findings", result: bytes | None = None) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        data = payload
        info = tarfile.TarInfo("evidence/findings.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        result_bytes = result if result is not None else b'{"verdict":"fail"}'
        info = tarfile.TarInfo("result.json")
        info.size = len(result_bytes)
        archive.addfile(info, io.BytesIO(result_bytes))
    return stream.getvalue()


def _apply_private_runner_completion(
    db_session, execution, scope, message: dict, *, pending_job: dict | None = None
) -> str:
    """Drive the same finalize + persist path the WebSocket complete handler uses."""
    from preloop.services.host_exec import (
        apply_runner_completion_to_execution,
        finalize_runner_completion,
    )

    pending = pending_job or {"launch_version": 1, "agent_type": "codex"}
    status, error, result = finalize_runner_completion(message, pending_job=pending)
    apply_runner_completion_to_execution(
        db_session,
        execution,
        account_id=scope["account_id"],
        status=status,
        error=error,
        result=result,
        message=message,
        pending_job=pending,
    )
    db_session.commit()
    return status


def test_evidence_roundtrip_integrity_isolation_and_failed_run(
    db_session, scope
) -> None:
    import os

    from preloop.services.flow_artifacts import (
        EvidenceUnavailableError,
        load_evidence,
        put_artifact,
    )

    oversized = os.urandom(2 * 1024 * 1024 + 2048)
    body = _evidence_body(oversized)
    assert len(body) > 2 * 1024 * 1024
    ref = put_artifact(db_session, **scope, kind="evidence", archive=body)
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "FAILED"
    db_session.commit()
    loaded, receipt = load_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert loaded == body
    assert receipt["status"] == "available"
    assert receipt["transport"] == "direct"
    assert receipt["object_lock"] is False
    with pytest.raises(EvidenceUnavailableError) as missing:
        load_evidence(db_session, account_id=uuid4(), execution=execution)
    assert missing.value.code == "missing"
    wrong_execution = models.FlowExecution(
        flow_id=scope["flow_id"],
        status="FAILED",
        trigger_event_details={"_session_thread_id": "thread-test"},
    )
    db_session.add(wrong_execution)
    db_session.flush()
    with pytest.raises(EvidenceUnavailableError) as cross:
        load_evidence(
            db_session, account_id=scope["account_id"], execution=wrong_execution
        )
    assert cross.value.code == "missing"
    row = crud.get(
        db_session,
        artifact_id=ref.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    row.ciphertext = b"tampered"
    db_session.commit()
    with pytest.raises(EvidenceUnavailableError) as failed:
        load_evidence(db_session, account_id=scope["account_id"], execution=execution)
    assert failed.value.code == "failed"


def test_evidence_expiry_respects_lease_and_longer_retention(
    db_session, scope, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from preloop.config import settings
    from preloop.services.flow_artifacts import EvidenceUnavailableError, load_evidence

    monkeypatch.setattr(settings, "flow_evidence_retention_hours", 720)
    monkeypatch.setattr(settings, "workspace_snapshot_ttl_hours", 0)
    body = _evidence_body()
    ref = put_artifact(db_session, **scope, kind="evidence", archive=body)
    workspace = put_artifact(
        db_session, **scope, kind="workspace", archive=archive_with("workspace/source")
    )
    now = datetime.now(UTC)
    evidence_row = crud.get(
        db_session,
        artifact_id=ref.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    workspace_row = crud.get(
        db_session,
        artifact_id=workspace.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    workspace_row.expires_at = now - timedelta(seconds=1)
    evidence_row.lease_until = now + timedelta(minutes=5)
    evidence_row.expires_at = now - timedelta(seconds=1)
    db_session.commit()
    assert crud.cleanup(db_session, now=now) == 1
    db_session.refresh(evidence_row)
    db_session.refresh(workspace_row)
    assert evidence_row.ciphertext is not None
    assert workspace_row.ciphertext is None
    assert crud.cleanup(db_session, now=now + timedelta(minutes=6)) == 1
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    with pytest.raises(EvidenceUnavailableError) as expired:
        load_evidence(db_session, account_id=scope["account_id"], execution=execution)
    assert expired.value.code == "expired"


def test_legacy_column_download_and_http_evidence_upload(db_session, scope) -> None:
    from preloop.services.flow_artifacts import inspect_evidence, load_evidence

    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.evidence_archive = _evidence_body()
    db_session.commit()
    receipt = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert receipt["transport"] == "legacy"
    assert receipt["status"] == "available"
    body, loaded_receipt = load_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert body == bytes(execution.evidence_archive)
    assert loaded_receipt["status"] == "available"
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    token = mint_artifact_capability(**scope, kind="evidence", operation="put")
    url = f"/flows/executions/{scope['execution_id']}/artifacts"
    uploaded = client.put(
        url,
        headers={"Authorization": "Bearer " + token},
        content=_evidence_body(b"direct-upload"),
    )
    assert uploaded.status_code == 200, uploaded.text
    execution.status = "FAILED"
    db_session.commit()
    body, receipt = load_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        extracted = archive.extractfile("evidence/findings.json")
        assert extracted is not None
        assert extracted.read() == b"direct-upload"
    assert receipt["transport"] == "direct"
    assert receipt["kind"] == "evidence"
    assert receipt["integrity_verified"] is True


@pytest.mark.parametrize("run_status", ["SUCCEEDED", "FAILED"])
def test_uploaded_evidence_discoverable_via_execution_apis(
    db_session, scope, test_user, run_status
) -> None:
    """Private/hosted PUT is visible on result, evidence-status, and evidence APIs."""
    from preloop.api.auth import get_current_active_user
    from preloop.api.endpoints import flows as flow_endpoints

    body = _evidence_body()
    token = mint_artifact_capability(**scope, kind="evidence", operation="put")
    app = FastAPI()
    app.include_router(router)
    app.include_router(flow_endpoints.router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: test_user
    client = TestClient(app)
    uploaded = client.put(
        f"/flows/executions/{scope['execution_id']}/artifacts",
        headers={"Authorization": "Bearer " + token},
        content=body,
    )
    assert uploaded.status_code == 200, uploaded.text
    reference = uploaded.json()
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    row = crud.get(
        db_session,
        artifact_id=reference["artifact_id"],
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    assert row is not None
    message = {
        "status": run_status,
        "launch_version": 1,
        "completion_protocol": "docker_v1",
        "exit_code": 0 if run_status == "SUCCEEDED" else 1,
        "result": {
            "schema": "preloop.cra.vulnscan/v1",
            "verdict": "fail" if run_status == "FAILED" else "pass",
            "status": "success" if run_status == "SUCCEEDED" else "failure",
        },
        "evidence_upload": "uploaded",
    }
    _apply_private_runner_completion(db_session, execution, scope, message)
    db_session.refresh(execution)

    status = client.get(f"/flows/executions/{execution.id}/evidence-status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "available"
    assert payload["kind"] == "evidence"
    assert payload["integrity_verified"] is False
    assert payload["artifact_id"] == str(reference["artifact_id"])
    assert payload["digest"] == payload["sha256"]

    result = client.get(f"/flows/executions/{execution.id}/result")
    assert result.status_code == 200
    body_json = result.json()
    assert body_json["status"] == run_status
    assert body_json["evidence"]["status"] == "available"
    assert body_json["evidence"]["integrity_verified"] is False

    download = client.get(f"/flows/executions/{execution.id}/evidence")
    assert download.status_code == 200
    assert download.content == body
    assert download.headers["x-preloop-evidence-kind"] == "evidence"
    assert download.headers["x-preloop-evidence-integrity"] == "verified"
    assert download.headers["x-preloop-evidence-sha256"] == payload["sha256"]


def _private_complete_message(
    run_status: str, *, evidence_upload: str, result: dict | None = None
) -> dict:
    payload = result or {
        "status": "success" if run_status == "SUCCEEDED" else "failure",
        "verdict": "pass" if run_status == "SUCCEEDED" else "fail",
    }
    return {
        "status": run_status,
        "launch_version": 1,
        "completion_protocol": "docker_v1",
        "exit_code": 0 if run_status == "SUCCEEDED" else 1,
        "result": payload,
        "evidence_upload": evidence_upload,
    }


@pytest.mark.parametrize("run_status", ["SUCCEEDED", "FAILED"])
@pytest.mark.asyncio
async def test_final_failed_upload_does_not_accept_early_put(
    db_session, scope, monkeypatch, run_status
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from preloop.config import settings
    from preloop.services.flow_artifacts import (
        EvidenceUnavailableError,
        inspect_evidence,
        load_evidence,
        public_evidence_status,
    )
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    trap = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"trap")
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    flow = db_session.get(models.Flow, scope["flow_id"])
    message = _private_complete_message(
        run_status,
        evidence_upload="failed",
        result={
            "status": "success" if run_status == "SUCCEEDED" else "failure",
            "evidence_upload": "uploaded",
        },
    )
    reported = _apply_private_runner_completion(db_session, execution, scope, message)
    assert reported == run_status
    db_session.refresh(execution)
    assert execution.evidence_receipt["status"] == "failed"
    assert "evidence_upload" not in (execution.result or {})
    status = public_evidence_status(execution)
    assert status["status"] == "failed"
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["status"] == "failed"
    with pytest.raises(EvidenceUnavailableError) as failed:
        load_evidence(db_session, account_id=scope["account_id"], execution=execution)
    assert failed.value.code == "failed"

    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db_session
    orchestrator.execution_log = execution
    orchestrator.flow = flow
    orchestrator.execution_logger = Mock()
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator._evidence_artifact_id = None
    executor = SimpleNamespace(evidence_transport_error=None, get_evidence_archive=None)
    await orchestrator._capture_evidence_archive(executor, "job")
    assert orchestrator._evidence_receipt["status"] == "failed"
    latest = crud.latest(db_session, **scope, kind="evidence")
    assert latest is not None and latest.id == trap.artifact_id


@pytest.mark.asyncio
async def test_sequential_direct_archives_extract_matching_identity(
    db_session, scope, monkeypatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from preloop.config import settings
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    first = put_artifact(
        db_session,
        **scope,
        kind="evidence",
        archive=_evidence_body(b"first", result=b'{"verdict":"fail"}'),
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    flow = db_session.get(models.Flow, scope["flow_id"])
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db_session
    orchestrator.execution_log = execution
    orchestrator.flow = flow
    orchestrator.execution_logger = Mock()
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator._evidence_artifact_id = None
    orchestrator._workspace_snapshot = None
    executor = SimpleNamespace(
        evidence_transport_error=None,
        get_evidence_archive=None,
        get_result_artifact=None,
        get_workspace_snapshot=None,
    )
    first_result = await orchestrator._capture_result_artifact(executor, "job")
    assert first_result is not None and first_result["verdict"] == "fail"
    assert orchestrator._evidence_receipt["artifact_id"] == str(first.artifact_id)

    second = put_artifact(
        db_session,
        **scope,
        kind="evidence",
        archive=_evidence_body(b"second", result=b'{"verdict":"pass"}'),
    )
    second_result = await orchestrator._capture_result_artifact(executor, "job")
    assert second_result is not None and second_result["verdict"] == "pass"
    receipt = orchestrator._evidence_receipt
    assert receipt["artifact_id"] == str(second.artifact_id)
    row = crud.get(
        db_session,
        artifact_id=second.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    assert receipt["sha256"] == (row.manifest or {}).get("sha256")
    assert orchestrator._evidence_artifact_id == str(second.artifact_id)


@pytest.mark.parametrize("run_status", ["SUCCEEDED", "FAILED"])
def test_bound_receipt_ignores_later_unrelated_latest(
    db_session, scope, run_status
) -> None:
    from preloop.services.flow_artifacts import inspect_evidence, load_evidence
    from preloop.utils.encryption import _get_fernet

    first = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"bound")
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    reported = _apply_private_runner_completion(
        db_session,
        execution,
        scope,
        _private_complete_message(run_status, evidence_upload="uploaded"),
    )
    assert reported == run_status
    db_session.refresh(execution)
    assert execution.evidence_receipt["artifact_id"] == str(first.artifact_id)

    later_body = _evidence_body(b"unrelated-latest")
    now = datetime.now(UTC)
    late = models.FlowArtifact(
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
        execution_id=scope["execution_id"],
        kind="evidence",
        manifest={"kind": "evidence", "sha256": "aa" * 32, "size_bytes": 1},
        manifest_sha256="bb" * 32,
        ciphertext=_get_fernet().encrypt(later_body),
        availability="available",
        expires_at=now + timedelta(hours=1),
    )
    db_session.add(late)
    db_session.flush()
    late.created_at = datetime.now() + timedelta(minutes=1)
    db_session.commit()
    latest = crud.latest(db_session, **scope, kind="evidence")
    assert latest is not None and latest.id == late.id

    db_session.refresh(execution)
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["artifact_id"] == str(first.artifact_id)
    body, loaded = load_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert loaded["artifact_id"] == str(first.artifact_id)
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as archive:
        extracted = archive.extractfile("evidence/findings.json")
        assert extracted is not None
        assert extracted.read() == b"bound"


def test_late_upload_after_terminal_close_is_rejected(db_session, scope) -> None:
    first = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"bound")
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    _apply_private_runner_completion(
        db_session,
        execution,
        scope,
        _private_complete_message("SUCCEEDED", evidence_upload="uploaded"),
    )
    with pytest.raises(ValueError, match="artifact_execution_closed"):
        put_artifact(
            db_session,
            **scope,
            kind="evidence",
            archive=_evidence_body(b"late"),
        )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    client = TestClient(app)
    token = mint_artifact_capability(**scope, kind="evidence", operation="put")
    response = client.put(
        f"/flows/executions/{scope['execution_id']}/artifacts",
        headers={"Authorization": "Bearer " + token},
        content=_evidence_body(b"late-http"),
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "artifact_execution_closed"
    latest = crud.latest(db_session, **scope, kind="evidence")
    assert latest is not None and latest.id == first.artifact_id


def test_live_inspect_refreshes_latest_before_finalization(db_session, scope) -> None:
    from preloop.services.flow_artifacts import inspect_evidence

    first = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"early")
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["artifact_id"] == str(first.artifact_id)
    second = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"later")
    )
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["artifact_id"] == str(second.artifact_id)


DIRECT_EVIDENCE_LEASE = {
    "launch_version": 1,
    "agent_type": "codex",
    "evidence_direct_upload": True,
}


@pytest.mark.parametrize(
    "complete",
    [
        pytest.param(
            {
                "status": "FAILED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 1,
                "evidence_upload": "failed",
            },
            id="missing-result",
        ),
        pytest.param(
            {
                "status": "FAILED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 1,
                "result": "not-an-object",
                "evidence_upload": "failed",
            },
            id="malformed-result",
        ),
        pytest.param(
            {
                "status": "FAILED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 1,
                "result": {"status": "success", "pad": "x" * (256 * 1024)},
                "evidence_upload": "failed",
            },
            id="oversize-result",
        ),
        pytest.param(
            {
                "status": "FAILED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 1,
            },
            id="missing-upload-on-direct-lease",
        ),
        pytest.param(
            {
                "status": "FAILED",
                "launch_version": 1,
                "completion_protocol": "docker_v1",
                "exit_code": 1,
                "result": {"status": "failure", "evidence_upload": "uploaded"},
            },
            id="forged-result-upload-without-final-metadata",
        ),
    ],
)
def test_final_upload_failure_without_valid_result_rejects_trap(
    db_session, scope, complete
) -> None:
    from preloop.services.flow_artifacts import (
        EvidenceUnavailableError,
        inspect_evidence,
        load_evidence,
        public_evidence_status,
    )

    trap = put_artifact(
        db_session, **scope, kind="evidence", archive=_evidence_body(b"trap")
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    reported = _apply_private_runner_completion(
        db_session,
        execution,
        scope,
        complete,
        pending_job=DIRECT_EVIDENCE_LEASE,
    )
    assert reported == "FAILED"
    db_session.refresh(execution)
    assert execution.evidence_receipt["status"] == "failed"
    assert public_evidence_status(execution)["status"] == "failed"
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["status"] == "failed"
    with pytest.raises(EvidenceUnavailableError) as failed:
        load_evidence(db_session, account_id=scope["account_id"], execution=execution)
    assert failed.value.code == "failed"
    latest = crud.latest(db_session, **scope, kind="evidence")
    assert latest is not None and latest.id == trap.artifact_id


@pytest.mark.parametrize(
    "forged",
    [["uploaded"], {"status": "uploaded"}, 1, True],
)
def test_non_string_evidence_upload_metadata_is_failed(
    db_session, scope, forged
) -> None:
    from preloop.services.flow_artifacts import inspect_evidence

    put_artifact(db_session, **scope, kind="evidence", archive=_evidence_body(b"trap"))
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    message = {
        "status": "FAILED",
        "launch_version": 1,
        "completion_protocol": "docker_v1",
        "exit_code": 1,
        "result": {"status": "failure"},
        "evidence_upload": forged,
    }
    reported = _apply_private_runner_completion(
        db_session, execution, scope, message, pending_job=DIRECT_EVIDENCE_LEASE
    )
    assert reported == "FAILED"
    db_session.refresh(execution)
    assert execution.evidence_receipt["status"] == "failed"
    inspected = inspect_evidence(
        db_session, account_id=scope["account_id"], execution=execution
    )
    assert inspected["status"] == "failed"


@pytest.mark.asyncio
async def test_failed_receipt_does_not_extract_stale_trap_result(
    db_session, scope, monkeypatch
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    from preloop.config import settings
    from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

    monkeypatch.setattr(settings, "flow_artifact_direct_upload", True)
    put_artifact(
        db_session,
        **scope,
        kind="evidence",
        archive=_evidence_body(b"trap", result=b'{"verdict":"pass"}'),
    )
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    flow = db_session.get(models.Flow, scope["flow_id"])
    orchestrator = object.__new__(FlowExecutionOrchestrator)
    orchestrator.db = db_session
    orchestrator.execution_log = execution
    orchestrator.flow = flow
    orchestrator.execution_logger = Mock()
    orchestrator._evidence_archive = None
    orchestrator._evidence_receipt = None
    orchestrator._evidence_artifact_id = None
    orchestrator._workspace_snapshot = None
    executor = SimpleNamespace(
        evidence_transport_error=None,
        get_evidence_archive=None,
        get_result_artifact=None,
        get_workspace_snapshot=None,
    )
    first = await orchestrator._capture_result_artifact(executor, "job")
    assert first is not None and first["verdict"] == "pass"
    _apply_private_runner_completion(
        db_session,
        execution,
        scope,
        {
            "status": "FAILED",
            "launch_version": 1,
            "completion_protocol": "docker_v1",
            "exit_code": 1,
            "evidence_upload": "failed",
        },
        pending_job=DIRECT_EVIDENCE_LEASE,
    )
    db_session.refresh(execution)
    orchestrator.execution_log = execution
    revived = await orchestrator._capture_result_artifact(executor, "job")
    assert revived is None
    assert orchestrator._evidence_receipt["status"] == "failed"


def test_controller_retention_put_after_failed_run(db_session, scope) -> None:
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "FAILED"
    db_session.commit()
    with pytest.raises(ValueError, match="artifact_execution_closed"):
        put_artifact(
            db_session,
            **scope,
            kind="workspace",
            archive=archive_with("workspace/source"),
        )
    retained = put_artifact(
        db_session,
        **scope,
        kind="workspace",
        archive=archive_with("workspace/source"),
        require_execution_open=False,
    )
    row = crud.get(
        db_session,
        artifact_id=retained.artifact_id,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
    )
    assert row is not None and row.kind == "workspace"
