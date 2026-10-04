"""Per-account opt-in for storing raw audio artifacts (#1102)."""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.plugins import account_hooks
from preloop.plugins.account_hooks import Decision
from preloop.services import artifact_deposit, audio_storage
from preloop.services.workspace_snapshot_cleanup import cleanup_workspace_artifacts

from tests.api.test_artifact_deposit import _auth, _session, _token

SETTINGS = "/api/v1/account/session-artifacts/settings"
OGG = b"OggS\x00\x02" + b"\x00" * 64


def _audio_body():
    return {
        "name": "shift-2.ogg",
        "labels": {"site": "nord", "shift": "2"},
        "content": {
            "type": "audio",
            "data": base64.b64encode(OGG).decode(),
            "mimeType": "audio/ogg",
        },
    }


def _deposit(client, db_session, test_user):
    session = _session(db_session, test_user.account_id, f"audio-{uuid.uuid4().hex}")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    response = client.post(
        f"/api/v1/runtime-sessions/{session.id}/artifacts",
        headers=_auth(token),
        json=_audio_body(),
    )
    return session, response


@pytest.fixture(autouse=True)
def _no_authorizer():
    yield
    account_hooks.register_authorizer(None)


def test_default_is_off_and_refuses_audio(client, db_session, test_user):
    body = client.get(SETTINGS).json()
    _session_row, response = _deposit(client, db_session, test_user)

    assert body["audio_storage_enabled"] is False
    assert body["audio_retention_days"] == audio_storage.DEFAULT_AUDIO_RETENTION_DAYS
    assert response.status_code == 409
    assert response.json()["detail"] == "artifact_audio_storage_disabled"


def test_admin_enables_and_audio_is_stored_and_audited(client, db_session, test_user):
    changed = client.put(
        SETTINGS, json={"audio_storage_enabled": True, "audio_retention_days": 7}
    )
    _session_row, response = _deposit(client, db_session, test_user)

    assert changed.status_code == 200, changed.text
    assert changed.json()["audio_storage_enabled"] is True
    assert changed.json()["audio_retention_days"] == 7
    assert changed.json()["updated_by_user_id"] == str(test_user.id)
    assert changed.json()["updated_at"]
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "audio"
    row = db_session.scalars(
        select(AuditLog).where(
            AuditLog.account_id == test_user.account_id,
            AuditLog.action == audio_storage.AUDIT_ACTION,
        )
    ).one()
    assert row.user_id == test_user.id
    assert row.timestamp is not None
    assert row.details["changed"] == {
        "audio_storage_enabled": {"from": False, "to": True},
        "audio_retention_days": {"from": 30, "to": 7},
    }


def test_non_admin_cannot_change_the_setting(client, db_session, test_user):
    account_hooks.register_authorizer(
        lambda ctx, action, resource: Decision("deny", reason="not an admin")
        if action == "manage_policies"
        else Decision("allow")
    )

    response = client.put(SETTINGS, json={"audio_storage_enabled": True})

    assert response.status_code == 403
    assert client.get(SETTINGS).json()["audio_storage_enabled"] is False
    assert (
        db_session.scalars(
            select(AuditLog).where(AuditLog.action == audio_storage.AUDIT_ACTION)
        ).first()
        is None
    )


@pytest.mark.parametrize("days", [0, 100000])
def test_retention_outside_one_day_to_session_retention_is_refused(client, days):
    response = client.put(SETTINGS, json={"audio_retention_days": days})

    assert response.status_code == 422


def test_turning_it_off_refuses_audio_again(client, db_session, test_user):
    client.put(SETTINGS, json={"audio_storage_enabled": True})
    client.put(SETTINGS, json={"audio_storage_enabled": False})

    _session_row, response = _deposit(client, db_session, test_user)

    assert response.status_code == 409


def test_other_metadata_is_left_alone(client, db_session, test_user):
    account = db_session.get(models.Account, test_user.account_id)
    account.meta_data = {"retention": {"audit": 400}, "other": 1}
    db_session.commit()

    client.put(SETTINGS, json={"audio_storage_enabled": True})

    db_session.refresh(account)
    assert account.meta_data["other"] == 1
    assert account.meta_data["retention"] == {"audit": 400}
    assert account.meta_data["artifacts"]["audio_storage_enabled"] is True


@pytest.mark.asyncio
async def test_janitor_expires_old_audio_and_byte_route_says_expired(
    client, db_session, test_user
):
    client.put(
        SETTINGS, json={"audio_storage_enabled": True, "audio_retention_days": 3}
    )
    session, old = _deposit(client, db_session, test_user)
    fresh = client.post(
        f"/api/v1/runtime-sessions/{session.id}/artifacts",
        headers=_auth(_token(db_session, test_user, runtime_session_id=session.id)),
        json={**_audio_body(), "name": "fresh.ogg"},
    )
    old_id = uuid.UUID(old.json()["id"])
    fresh_id = uuid.UUID(fresh.json()["id"])
    db_session.execute(
        update(models.RuntimeSessionArtifact)
        .where(models.RuntimeSessionArtifact.id == old_id)
        .values(created_at=datetime.now(UTC) - timedelta(days=4))
    )
    db_session.commit()

    await cleanup_workspace_artifacts(db_session)

    db_session.expire_all()
    gone = db_session.get(models.RuntimeSessionArtifact, old_id)
    kept = db_session.get(models.RuntimeSessionArtifact, fresh_id)
    assert gone is not None
    assert (gone.availability, gone.ciphertext) == ("expired", None)
    assert kept.availability == "available"
    byte_url = f"/api/v1/runtime-sessions/{session.id}/artifacts/{old_id}"
    response = client.get(byte_url)
    assert response.status_code == 410
    assert response.json() == {"availability": "expired"}
    assert (
        client.get(
            f"/api/v1/runtime-sessions/{session.id}/artifacts/{fresh_id}"
        ).content
        == OGG
    )


def test_gate_reads_the_account_setting():
    class _Account:
        meta_data = {"artifacts": {"audio_storage_enabled": True}}

    assert artifact_deposit.audio_storage_enabled(_Account()) is True
    _Account.meta_data = {"artifacts": {"audio_storage_enabled": "yes"}}
    assert artifact_deposit.audio_storage_enabled(_Account()) is False
    assert artifact_deposit.audio_storage_enabled(None) is False


def _audit_rows(db_session, account_id):
    db_session.expire_all()
    return db_session.scalars(
        select(AuditLog).where(
            AuditLog.account_id == account_id,
            AuditLog.action == audio_storage.AUDIT_ACTION,
        )
    ).all()


def test_audit_records_only_changed_fields_and_no_op_writes_nothing(
    client, db_session, test_user
):
    client.put(SETTINGS, json={"audio_storage_enabled": True})
    stamped = client.get(SETTINGS).json()["updated_at"]

    same = client.put(SETTINGS, json={"audio_storage_enabled": True})
    empty = client.put(SETTINGS, json={})
    days = client.put(
        SETTINGS, json={"audio_storage_enabled": True, "audio_retention_days": 9}
    )

    assert same.status_code == empty.status_code == days.status_code == 200
    assert same.json()["updated_at"] == empty.json()["updated_at"] == stamped
    rows = _audit_rows(db_session, test_user.account_id)
    assert [r.details["changed"] for r in rows] == [
        {"audio_storage_enabled": {"from": False, "to": True}},
        {"audio_retention_days": {"from": 30, "to": 9}},
    ]


def test_audio_relabelled_as_another_kind_is_refused_while_off(
    client, db_session, test_user
):
    session = _session(db_session, test_user.account_id, f"audio-{uuid.uuid4().hex}")
    token = _token(db_session, test_user, runtime_session_id=session.id)
    body = {
        "name": "shift-2.ogg",
        "kind": "generated_file",
        "content": {
            "type": "resource",
            "resource": {
                "uri": "file:///shift-2.ogg",
                "mimeType": "audio/ogg",
                "blob": base64.b64encode(OGG).decode(),
            },
        },
    }
    url = f"/api/v1/runtime-sessions/{session.id}/artifacts"

    refused = client.post(url, headers=_auth(token), json=body)
    client.put(SETTINGS, json={"audio_storage_enabled": True})
    stored = client.post(url, headers=_auth(token), json=body)

    assert refused.status_code == 409
    assert refused.json()["detail"] == "artifact_audio_storage_disabled"
    assert stored.status_code == 201, stored.text


@pytest.mark.asyncio
@pytest.mark.parametrize("hold", ["artifact", "session"])
async def test_janitor_keeps_held_audio(client, db_session, test_user, hold):
    client.put(
        SETTINGS, json={"audio_storage_enabled": True, "audio_retention_days": 3}
    )
    session, old = _deposit(client, db_session, test_user)
    artifact_id = uuid.UUID(old.json()["id"])
    values = {"created_at": datetime.now(UTC) - timedelta(days=4)}
    if hold == "artifact":
        values["legal_hold"] = True
    db_session.execute(
        update(models.RuntimeSessionArtifact)
        .where(models.RuntimeSessionArtifact.id == artifact_id)
        .values(**values)
    )
    if hold == "session":
        db_session.execute(
            update(models.RuntimeSession)
            .where(models.RuntimeSession.id == session.id)
            .values(legal_hold=True)
        )
    db_session.commit()

    assert audio_storage.expire_audio(db_session, now=datetime.now(UTC)) == 0

    db_session.expire_all()
    kept = db_session.get(models.RuntimeSessionArtifact, artifact_id)
    assert kept.availability == "available"
    assert kept.ciphertext is not None
