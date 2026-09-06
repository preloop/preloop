"""Disposable PostgreSQL artifact transaction, encryption and isolation tests."""

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
    ref = put_artifact(db_session, **scope, kind="workspace", archive=body, metadata={})
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
        metadata={},
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
        metadata={},
    )
    with pytest.raises(ValueError, match="corrupt"):
        put_artifact(
            db_session, **scope, kind="workspace", archive=b"interrupted", metadata={}
        )
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
