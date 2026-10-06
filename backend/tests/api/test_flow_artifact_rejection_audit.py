"""A refused artifact PUT leaves an audit row, so diagnosis needs no access log (#1331)."""

from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from preloop.api.endpoints import flow_artifacts
from preloop.models.db.session import get_db_session
from preloop.models.schemas.flow_artifact import ArtifactReference

ACCOUNT = uuid4()
FLOW = uuid4()


def build(
    monkeypatch: pytest.MonkeyPatch, claims: dict[str, Any]
) -> tuple[FastAPI, list[dict[str, Any]], list[str]]:
    app = FastAPI()
    app.include_router(flow_artifacts.router)
    app.dependency_overrides[get_db_session] = object
    app.dependency_overrides[flow_artifacts.artifact_claims] = lambda: claims
    rows: list[dict[str, Any]] = []
    events: list[str] = []

    def log_action(db: Any, **kwargs: Any) -> None:
        events.append("audit")
        rows.append(kwargs)

    monkeypatch.setattr(flow_artifacts.crud_audit_log, "log_action", log_action)
    monkeypatch.setattr(
        flow_artifacts.flow_artifact, "rollback", lambda db: events.append("rollback")
    )
    return app, rows, events


def capability() -> dict[str, Any]:
    return {
        "account_id": ACCOUNT,
        "flow_id": FLOW,
        "thread_id": "t",
        "kind": "workspace",
        "operation": "put",
    }


async def put(app: FastAPI, execution_id: Any, content: bytes = b"x") -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.put(
            f"/flows/executions/{execution_id}/artifacts", content=content
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (403, "artifact_scope_mismatch"),
        (404, "artifact_execution_missing"),
        (409, "artifact_execution_closed"),
    ],
)
async def test_authorization_refusal_is_audited(
    monkeypatch: pytest.MonkeyPatch, status: int, detail: str
) -> None:
    app, rows, events = build(monkeypatch, capability())

    def refuse(*args: Any) -> None:
        raise HTTPException(status, detail)

    monkeypatch.setattr(flow_artifacts, "authorize", refuse)
    execution_id = uuid4()
    response = await put(app, execution_id)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert rows == [
        {
            "account_id": ACCOUNT,
            "action": "flow_artifact_rejected",
            "resource_type": "flow_execution",
            "resource_id": str(execution_id),
            "status": "failure",
            "details": {
                "status_code": status,
                "reason": detail,
                "kind": "workspace",
                "flow_id": str(FLOW),
                "execution_id": str(execution_id),
            },
        }
    ]
    # The upload transaction is released before the row commits alone.
    assert events == ["rollback", "audit"]


@pytest.mark.asyncio
async def test_oversized_stream_is_audited(monkeypatch: pytest.MonkeyPatch) -> None:
    app, rows, _ = build(monkeypatch, capability())
    monkeypatch.setattr(flow_artifacts, "authorize", lambda *args: None)
    monkeypatch.setattr(flow_artifacts.settings, "workspace_snapshot_max_bytes", 4)
    response = await put(app, uuid4(), b"0123456789")
    assert response.status_code == 413
    assert [row["details"]["reason"] for row in rows] == ["artifact_oversized"]
    assert rows[0]["details"]["status_code"] == 413


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("artifact_quota_exceeded", 422),
        ("artifact_execution_closed", 409),
    ],
)
async def test_persistence_refusal_is_audited(
    monkeypatch: pytest.MonkeyPatch, code: str, status: int
) -> None:
    app, rows, _ = build(monkeypatch, capability())
    monkeypatch.setattr(flow_artifacts, "authorize", lambda *args: None)

    def reject(*args: Any, **kwargs: Any) -> None:
        raise ValueError(code)

    monkeypatch.setattr(flow_artifacts, "put_artifact", reject)
    response = await put(app, uuid4())
    assert response.status_code == status
    assert [(r["details"]["status_code"], r["details"]["reason"]) for r in rows] == [
        (status, code)
    ]


@pytest.mark.asyncio
async def test_unexpected_failure_is_audited_as_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rows, _ = build(monkeypatch, capability())
    monkeypatch.setattr(flow_artifacts, "authorize", lambda *args: None)

    def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("storage said: /secret/bucket/key")

    monkeypatch.setattr(flow_artifacts, "put_artifact", explode)
    response = await put(app, uuid4())
    assert response.status_code == 500
    assert rows[0]["details"]["status_code"] == 500
    assert rows[0]["details"]["reason"] == "internal_error"
    assert "secret" not in repr(rows)


@pytest.mark.asyncio
async def test_free_text_detail_is_not_copied(monkeypatch: pytest.MonkeyPatch) -> None:
    app, rows, _ = build(monkeypatch, capability())

    def refuse(*args: Any) -> None:
        raise HTTPException(503, "pool exhausted at /internal/host")

    monkeypatch.setattr(flow_artifacts, "authorize", refuse)
    response = await put(app, uuid4())
    assert response.status_code == 503
    assert rows[0]["details"]["reason"] == "unrecognized"


@pytest.mark.asyncio
async def test_audit_failure_never_replaces_the_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = build(monkeypatch, capability())

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("audit store down")

    monkeypatch.setattr(flow_artifacts.crud_audit_log, "log_action", broken)

    def refuse(*args: Any) -> None:
        raise HTTPException(409, "artifact_execution_closed")

    monkeypatch.setattr(flow_artifacts, "authorize", refuse)
    response = await put(app, uuid4())
    assert response.status_code == 409
    assert response.json() == {"detail": "artifact_execution_closed"}


@pytest.mark.asyncio
async def test_unattributable_request_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rows, _ = build(monkeypatch, {})

    def refuse(*args: Any) -> None:
        raise HTTPException(403, "artifact_scope_mismatch")

    monkeypatch.setattr(flow_artifacts, "authorize", refuse)
    response = await put(app, uuid4())
    assert response.status_code == 403
    assert rows == []


@pytest.mark.asyncio
async def test_accepted_upload_writes_no_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, rows, _ = build(monkeypatch, capability())
    monkeypatch.setattr(flow_artifacts, "authorize", lambda *args: None)

    def persist(db: Any, **kwargs: Any) -> ArtifactReference:
        return ArtifactReference(
            artifact_id=uuid4(),
            execution_id=kwargs["execution_id"],
            manifest_sha256="a" * 64,
        )

    monkeypatch.setattr(flow_artifacts, "put_artifact", persist)
    response = await put(app, uuid4())
    assert response.status_code == 200
    assert rows == []


def test_rejection_reason_reads_the_capability_shape() -> None:
    assert (
        flow_artifacts.rejection_reason(
            {"error": "invalid_artifact_capability", "message": "long text"}
        )
        == "invalid_artifact_capability"
    )
    assert flow_artifacts.rejection_reason(None) == "unrecognized"


def test_rejection_row_commits_on_a_real_session(
    db_session: Any, test_user: Any
) -> None:
    """The row survives the rollback that releases the refused upload."""
    from preloop.models.models.audit_log import AuditLog

    execution_id = uuid4()
    claims = {**capability(), "account_id": test_user.account_id}
    flow_artifacts.record_artifact_rejection(
        db_session, claims, execution_id, 409, "artifact_execution_closed"
    )
    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.action == "flow_artifact_rejected",
            AuditLog.resource_id == str(execution_id),
        )
        .one()
    )
    assert str(row.account_id) == str(test_user.account_id)
    assert row.status == "failure"
    assert row.details["status_code"] == 409
    assert row.details["reason"] == "artifact_execution_closed"
