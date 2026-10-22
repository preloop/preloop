"""Recording deposits, timing updates and isolated mapping reads."""

from typing import Any

from datetime import datetime, timezone
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import crud_account
from tests.api.test_artifact_deposit import _auth, _session, _token, _user_auth

BASE = "/api/v1/runtime-sessions"
TIMING = {
    "started_at": "2026-10-01T00:00:00Z",
    "clock": "agent",
    "duration_ms": 10000,
    "step_offsets": [{"source_step_id": "step-1", "offset_ms": 2000}],
}


def deposit(
    client: Any,
    session: Any,
    headers: Any,
    *,
    kind: Any = "recording",
    timing: Any = TIMING,
) -> Any:
    import base64

    return client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=headers,
        json={
            "name": "capture.webm",
            "kind": kind,
            "media_timing": timing,
            "content": {
                "type": "resource",
                "resource": {
                    "uri": "file:///capture.webm",
                    "mimeType": "video/webm",
                    "blob": base64.b64encode(
                        b"\x1a\x45\xdf\xa3" + b"\x00" * 32
                    ).decode(),
                },
            },
        },
    )


def test_deposit_patch_mapping_and_idempotent_audit(
    client: Any, db_session: Any, test_user: Any
) -> None:
    session = _session(db_session, test_user.account_id, "recording-sync")
    headers = _auth(_token(db_session, test_user, runtime_session_id=session.id))
    result = deposit(client, session, headers)
    assert result.status_code == 201, result.text
    artifact = result.json()
    assert artifact["media_timing"]["step_offsets"] == TIMING["step_offsets"]
    result = client.post(
        f"{BASE}/{session.id}/browser-steps",
        headers=headers,
        json={
            "steps": [
                {
                    "source": "browser_use",
                    "source_step_id": "step-1",
                    "step_index": 1,
                    "action": "click",
                    "occurred_at": "2026-10-01T00:00:01Z",
                    "recorded_at": "1900-01-01T00:00:00Z",
                }
            ]
        },
    )
    assert result.status_code == 200, result.text
    row = db_session.execute(
        select(models.RuntimeSessionActivity).where(
            models.RuntimeSessionActivity.runtime_session_id == session.id,
            models.RuntimeSessionActivity.activity_type == "browser_step",
        )
    ).scalar_one()
    assert (
        datetime.fromisoformat(row.metadata_["recorded_at"]).year
        == datetime.now(timezone.utc).year
    )
    assert row.metadata_["occurred_at"].startswith("2026-10-01")
    result = client.get(f"{BASE}/{session.id}/media-sync", headers=headers)
    assert result.status_code == 200, result.text
    mapping = result.json()["recordings"][0]["mappings"][0]
    assert (mapping["activity_id"], mapping["offset_ms"], mapping["rule"]) == (
        str(row.id),
        2000,
        "exact",
    )
    path = f"{BASE}/{session.id}/artifacts/{artifact['id']}/media-timing"
    changed = {
        **TIMING,
        "step_offsets": [{"source_step_id": "step-1", "offset_ms": 4000}],
    }
    for _ in range(2):
        response = client.patch(path, headers=headers, json=changed)
        assert response.status_code == 200, response.text
        assert response.json()["sha256"] == artifact["sha256"]
    audits = (
        db_session.execute(
            select(models.AuditLog).where(
                models.AuditLog.action == "artifact.timing.updated",
                models.AuditLog.resource_id == artifact["id"],
            )
        )
        .scalars()
        .all()
    )
    assert len(audits) == 1
    assert (
        client.get(f"{BASE}/{session.id}/media-sync", headers=headers).json()[
            "recordings"
        ][0]["mappings"][0]["offset_ms"]
        == 4000
    )


def test_validation_and_plain_old_clients(
    client: Any, db_session: Any, test_user: Any
) -> None:
    session = _session(db_session, test_user.account_id, "recording-validation")
    headers = _auth(_token(db_session, test_user, runtime_session_id=session.id))
    assert deposit(client, session, headers, kind="document").status_code == 422
    assert (
        deposit(
            client, session, headers, timing={**TIMING, "duration_ms": -1}
        ).status_code
        == 422
    )
    result = deposit(client, session, headers, timing=None)
    assert result.status_code == 201, result.text
    assert result.json()["media_timing"] is None
    assert (
        client.get(f"{BASE}/{session.id}/media-sync", headers=headers).json()[
            "recordings"
        ][0]["mappings"]
        == []
    )


def test_auth_binding_permissions_and_account_isolation(
    client: Any, db_session: Any, test_user: Any
) -> None:
    own = _session(db_session, test_user.account_id, "recording-auth")
    other = _session(db_session, test_user.account_id, "recording-other-session")
    headers = _auth(_token(db_session, test_user, runtime_session_id=own.id))
    artifact_id = deposit(client, own, headers).json()["id"]
    url = f"{BASE}/{own.id}/artifacts/{artifact_id}/media-timing"
    assert client.patch(url, json=TIMING).status_code == 401
    assert client.get(f"{BASE}/{own.id}/media-sync").status_code == 401
    bound_other = _auth(_token(db_session, test_user, runtime_session_id=other.id))
    assert client.patch(url, headers=bound_other, json=TIMING).status_code == 403
    assert (
        client.get(f"{BASE}/{own.id}/media-sync", headers=bound_other).status_code
        == 403
    )
    assert (
        client.patch(
            f"{BASE}/{other.id}/artifacts/{artifact_id}/media-timing",
            headers=bound_other,
            json=TIMING,
        ).status_code
        == 404
    )
    account = crud_account.create(
        db_session, obj_in={"organization_name": "Example", "is_active": True}
    )
    foreign = _session(db_session, account.id, "foreign-recording")
    user_headers = _user_auth(test_user)
    assert (
        client.get(f"{BASE}/{foreign.id}/media-sync", headers=user_headers).status_code
        == 404
    )
    assert (
        client.patch(
            f"{BASE}/{foreign.id}/artifacts/{artifact_id}/media-timing",
            headers=user_headers,
            json=TIMING,
        ).status_code
        == 404
    )
    module = "preloop.api.endpoints.runtime_session_artifacts."
    with patch(
        module + "_require_manage_agents", side_effect=HTTPException(403, "denied")
    ):
        assert client.patch(url, headers=user_headers, json=TIMING).status_code == 403
        assert client.patch(url, headers=headers, json=TIMING).status_code == 200
    with patch(
        module + "_require_view_runtime_sessions",
        side_effect=HTTPException(403, "denied"),
    ):
        assert (
            client.get(f"{BASE}/{own.id}/media-sync", headers=user_headers).status_code
            == 403
        )


def test_multipart_timing(client: Any, db_session: Any, test_user: Any) -> None:
    import json

    session = _session(db_session, test_user.account_id, "recording-multipart")
    headers = _auth(_token(db_session, test_user, runtime_session_id=session.id))
    result = client.post(
        f"{BASE}/{session.id}/artifacts",
        headers=headers,
        files={"file": ("capture.gif", b"GIF89a" + b"\0" * 32, "image/gif")},
        data={"metadata": json.dumps({"kind": "screencast", "media_timing": TIMING})},
    )
    assert result.status_code == 201, result.text
    assert result.json()["media_timing"]["duration_ms"] == 10000
