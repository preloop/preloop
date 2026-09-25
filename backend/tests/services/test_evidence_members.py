"""One evidence-pack member, with the same gates as the full download."""

import io
import tarfile
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from preloop.api.auth import get_current_active_user
from preloop.api.endpoints import flows as flow_endpoints
from preloop.cra.evidence_pack import (
    EVIDENCE_MEMBER_READ_MAX_BYTES,
    EvidenceMemberError,
    ensure_pack_manifest,
    normalize_member_path,
    read_evidence_member,
)
from preloop.models import models
from preloop.models.crud import flow_artifact as crud
from preloop.models.db.session import get_db_session
from preloop.services.flow_artifacts import put_artifact


def _pack(*files: tuple[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, data in files:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return ensure_pack_manifest(stream.getvalue())


def test_member_paths_reject_traversal() -> None:
    for path in (
        "../evidence/findings.json",
        "evidence/../../etc/passwd",
        "/etc/passwd",
        "evidence\\findings.json",
        "evidence/./findings.json",
        "",
        "evidence//findings.json",
        "evidence/a\nb.md",
        "evidence/a\rb.md",
        "evidence/n\u00e4me.md",
    ):
        with pytest.raises(EvidenceMemberError) as exc:
            normalize_member_path(path)
        assert exc.value.status_code == 400


def test_unlisted_member_is_refused() -> None:
    archive = _pack(("evidence/findings.json", b'{"findings":[]}'))
    with pytest.raises(EvidenceMemberError) as exc:
        read_evidence_member(archive, "evidence/other.json")
    assert exc.value.status_code == 404
    assert "manifest" in exc.value.detail.lower()


def test_oversize_member_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("preloop.cra.evidence_pack.EVIDENCE_MEMBER_READ_MAX_BYTES", 8)
    archive = _pack(("evidence/report.md", b"# " + b"x" * 40))
    with pytest.raises(EvidenceMemberError) as exc:
        read_evidence_member(archive, "evidence/report.md")
    assert exc.value.status_code == 413
    assert "8 MiB" in exc.value.detail
    assert EVIDENCE_MEMBER_READ_MAX_BYTES == 8 * 1024 * 1024


def test_member_content_types() -> None:
    archive = _pack(
        ("evidence/report.md", b"# Checked\n"),
        ("evidence/findings.json", b'{"findings":[]}'),
        ("evidence/notes.txt", b"plain"),
    )
    body, meta = read_evidence_member(archive, "evidence/report.md")
    assert body == b"# Checked\n"
    assert meta["content_type"] == "text/markdown; charset=utf-8"
    _, findings = read_evidence_member(archive, "evidence/findings.json")
    assert findings["content_type"] == "application/json"
    _, notes = read_evidence_member(archive, "evidence/notes.txt")
    assert notes["content_type"] == "text/plain; charset=utf-8"


@pytest.fixture
def scope(db_session, test_user):
    flow = models.Flow(
        name="Evidence member test",
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


def _client(db_session, user) -> TestClient:
    app = FastAPI()
    app.include_router(flow_endpoints.router)
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[get_current_active_user] = lambda: user
    return TestClient(app)


def _store(db_session, scope, archive: bytes):
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "SUCCEEDED"
    db_session.commit()
    ref = put_artifact(
        db_session,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
        execution_id=scope["execution_id"],
        kind="evidence",
        archive=archive,
        require_execution_open=False,
    )
    return execution, ref


def test_members_require_the_owning_account(db_session, scope, test_user) -> None:
    archive = _pack(("evidence/findings.json", b'{"findings":[]}'))
    execution, _ref = _store(db_session, scope, archive)
    own = _client(db_session, test_user)
    listed = own.get(f"/flows/executions/{execution.id}/evidence/members")
    assert listed.status_code == 200, listed.text
    assert listed.json()["legal_hold"] is False
    paths = [item["path"] for item in listed.json()["members"]]
    assert "evidence/findings.json" in paths

    stranger = MagicMock()
    stranger.account_id = uuid4()
    stranger.id = uuid4()
    denied = _client(db_session, stranger)
    missing = denied.get(f"/flows/executions/{execution.id}/evidence/members")
    assert missing.status_code == 404

    anonymous = FastAPI()
    anonymous.include_router(flow_endpoints.router)
    anonymous.dependency_overrides[get_db_session] = lambda: db_session
    unauthenticated = TestClient(anonymous)
    assert (
        unauthenticated.get(
            f"/flows/executions/{execution.id}/evidence/members"
        ).status_code
        == 401
    )


def test_member_endpoint_reads_one_file(db_session, scope, test_user) -> None:
    archive = _pack(
        ("evidence/report.md", b"# What we checked\n"),
        ("evidence/findings.json", b'{"findings":[{"id":"a"}]}'),
    )
    execution, _ref = _store(db_session, scope, archive)
    client = _client(db_session, test_user)
    response = client.get(
        f"/flows/executions/{execution.id}/evidence/members",
        params={"path": "evidence/findings.json"},
    )
    assert response.status_code == 200, response.text
    assert response.content == b'{"findings":[{"id":"a"}]}'
    assert response.headers["content-type"].startswith("application/json")
    assert response.headers["x-preloop-evidence-integrity"] == "verified"
    assert response.headers["x-preloop-evidence-sha256"]
    assert response.headers["x-preloop-evidence-member"] == "evidence/findings.json"
    walked = client.get(
        f"/flows/executions/{execution.id}/evidence/members",
        params={"path": "evidence/../../etc/passwd"},
    )
    assert walked.status_code == 400


def test_missing_expired_and_failed_packs(db_session, scope, test_user) -> None:
    execution = db_session.get(models.FlowExecution, scope["execution_id"])
    execution.status = "SUCCEEDED"
    execution.evidence_archive = None
    execution.evidence_receipt = {"status": "missing", "transport": "direct"}
    db_session.commit()
    client = _client(db_session, test_user)
    missing = client.get(f"/flows/executions/{execution.id}/evidence/members")
    assert missing.status_code == 404

    archive = _pack(("evidence/findings.json", b"{}"))
    execution, _ref = _store(db_session, scope, archive)
    # A terminal missing receipt short-circuits before the artifact is read.
    # The stored pack is the source of expiry once that receipt is cleared.
    execution.evidence_receipt = None
    row = crud.latest(
        db_session,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
        execution_id=scope["execution_id"],
        kind="evidence",
    )
    row.expires_at = datetime.now(UTC) - timedelta(hours=1)
    row.legal_hold = False
    db_session.commit()
    expired = client.get(f"/flows/executions/{execution.id}/evidence/members")
    assert expired.status_code == 410

    execution.evidence_receipt = {
        "status": "failed",
        "transport": "direct",
        "error": "evidence_upload_failed",
    }
    db_session.commit()
    failed = client.get(f"/flows/executions/{execution.id}/evidence/members")
    assert failed.status_code == 409


def test_held_pack_past_expiry_still_reads(db_session, scope, test_user) -> None:
    archive = _pack(("evidence/report.md", b"# Held\n"))
    execution, _ref = _store(db_session, scope, archive)
    row = crud.latest(
        db_session,
        account_id=scope["account_id"],
        flow_id=scope["flow_id"],
        thread_id=scope["thread_id"],
        execution_id=scope["execution_id"],
        kind="evidence",
    )
    row.expires_at = datetime.now(UTC) - timedelta(days=2)
    row.legal_hold = True
    db_session.commit()
    client = _client(db_session, test_user)
    listed = client.get(f"/flows/executions/{execution.id}/evidence/members")
    assert listed.status_code == 200, listed.text
    assert listed.json()["legal_hold"] is True
    body = client.get(
        f"/flows/executions/{execution.id}/evidence/members",
        params={"path": "evidence/report.md"},
    )
    assert body.status_code == 200, body.text
    assert body.content == b"# Held\n"
    assert body.headers["content-type"].startswith("text/markdown")


def test_endpoint_not_found_without_an_execution(mocker) -> None:
    user = MagicMock()
    user.account_id = uuid4()
    mocker.patch(
        "preloop.api.endpoints.flows.crud_flow_execution"
    ).get.return_value = None
    with pytest.raises(HTTPException) as exc:
        flow_endpoints.get_flow_execution_evidence_members(
            db=MagicMock(),
            execution_id=uuid4(),
            current_user=user,
            path=None,
        )
    assert exc.value.status_code == 404
