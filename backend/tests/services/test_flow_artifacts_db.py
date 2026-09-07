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


def _evidence_body(payload: bytes = b"findings") -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        data = payload
        info = tarfile.TarInfo("evidence/findings.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
        result = b'{"verdict":"fail"}'
        info = tarfile.TarInfo("result.json")
        info.size = len(result)
        archive.addfile(info, io.BytesIO(result))
    return stream.getvalue()


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
    from preloop.services.flow_artifacts import evidence_receipt

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
    execution.status = run_status
    execution.result = {
        "schema": "preloop.cra.vulnscan/v1",
        "verdict": "fail" if run_status == "FAILED" else "pass",
    }
    execution.evidence_receipt = evidence_receipt(
        status="available",
        execution_id=execution.id,
        transport="direct",
        artifact=row,
    )
    db_session.commit()

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
