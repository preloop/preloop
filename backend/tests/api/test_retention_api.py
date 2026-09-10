"""API surface for retention settings, legal holds and the period export."""

import io
import json
import tarfile
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.models.audit_log import AuditLog

BASE = "/api/v1/retention"


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


@pytest.fixture
def pack(db_session, test_user):
    """An execution with one evidence pack, so holds have something to hold."""
    flow = models.Flow(
        name=f"api-{uuid.uuid4().hex[:8]}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=test_user.account_id,
    )
    db_session.add(flow)
    db_session.flush()
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="COMPLETED",
        trigger_event_details={"_session_thread_id": "t"},
    )
    db_session.add(execution)
    db_session.flush()
    artifact = models.FlowArtifact(
        account_id=test_user.account_id,
        flow_id=flow.id,
        execution_id=execution.id,
        thread_id="t",
        kind="evidence",
        manifest={"sha256": "e" * 64, "size_bytes": 32},
        manifest_sha256="f" * 64,
        ciphertext=b"cipher",
        expires_at=datetime.now(UTC) + timedelta(days=1),
        availability="available",
    )
    db_session.add(artifact)
    db_session.flush()
    return execution, artifact


# --- settings --------------------------------------------------------------


def test_settings_report_the_floor_and_the_defaults(client):
    response = client.get(f"{BASE}/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["floor_days"] == 183
    assert body["default_days"] == 365
    classes = {item["record_class"]: item for item in body["classes"]}
    assert set(classes) == {
        "audit",
        "approvals",
        "evidence",
        "runtime_sessions",
        "usage",
    }
    assert classes["audit"]["days"] == 365
    assert classes["audit"]["source"] == "default"
    assert all(item["label"] for item in body["classes"])


def test_settings_say_whether_anything_actually_deletes(client, monkeypatch):
    """Retention nobody enforces is a sentence, and the API admits it."""
    monkeypatch.setattr(settings, "retention_purge_enabled", False, raising=False)

    body = client.get(f"{BASE}/settings").json()

    assert body["purge_enabled"] is False
    assert body["evidence_payload_hours"] == settings.flow_evidence_retention_hours


def test_a_retention_above_the_floor_is_stored(client, db_session, account):
    response = client.put(f"{BASE}/settings", json={"classes": {"audit": 400}})

    assert response.status_code == 200
    classes = {item["record_class"]: item for item in response.json()["classes"]}
    assert classes["audit"]["days"] == 400
    assert classes["audit"]["source"] == "account"
    db_session.refresh(account)
    assert account.meta_data["retention"] == {"audit": 400}


def test_a_retention_below_the_floor_is_refused_with_the_floor_named(client):
    response = client.put(f"{BASE}/settings", json={"classes": {"audit": 30}})

    assert response.status_code == 422
    assert "183" in response.json()["detail"]


def test_an_unknown_record_class_is_refused(client):
    response = client.put(f"{BASE}/settings", json={"classes": {"logs": 400}})

    assert response.status_code == 422


def test_updating_retention_leaves_other_account_metadata_alone(
    client, db_session, account
):
    account.meta_data = {"approval_window_max_seconds": 900}
    db_session.add(account)
    db_session.flush()

    client.put(f"{BASE}/settings", json={"classes": {"audit": 400}})

    db_session.refresh(account)
    assert account.meta_data["approval_window_max_seconds"] == 900
    assert account.meta_data["retention"] == {"audit": 400}


def test_a_retention_change_is_audited_with_the_before_and_after(
    client, db_session, account, test_user
):
    client.put(f"{BASE}/settings", json={"classes": {"audit": 400}})

    row = db_session.execute(
        select(AuditLog).where(
            AuditLog.account_id == account.id,
            AuditLog.action == "retention_settings_updated",
        )
    ).scalar_one()
    assert row.user_id == test_user.id
    assert row.details["changed"]["audit"] == {"from": 365, "to": 400}


def test_clearing_a_class_returns_it_to_the_default(client, db_session, account):
    client.put(f"{BASE}/settings", json={"classes": {"audit": 400}})

    response = client.put(f"{BASE}/settings", json={"classes": {"audit": None}})

    classes = {item["record_class"]: item for item in response.json()["classes"]}
    assert classes["audit"]["days"] == 365
    assert classes["audit"]["source"] == "default"


# --- purge preview ---------------------------------------------------------


def test_purge_preview_counts_without_deleting(client, db_session, account):
    old = AuditLog(
        account_id=account.id,
        action="permission_check",
        status="success",
        timestamp=datetime.now(UTC) - timedelta(days=400),
    )
    db_session.add(old)
    db_session.flush()

    body = client.get(f"{BASE}/purge-preview").json()

    audit = next(row for row in body["classes"] if row["record_class"] == "audit")
    assert audit["purgeable"] >= 1
    assert audit["retention_days"] == 365
    assert db_session.get(AuditLog, old.id) is not None


# --- holds -----------------------------------------------------------------


def test_placing_a_hold_returns_the_record_and_what_it_froze(client, pack):
    execution, _ = pack

    response = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "execution",
            "resource_id": str(execution.id),
            "reason": "incident review INC-114",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["active"] is True
    assert body["reason"] == "incident review INC-114"
    assert body["flagged"]["flow_execution"] == 1
    assert body["flagged"]["flow_artifact"] == 1


def test_a_hold_without_a_real_reason_is_refused(client, pack):
    execution, _ = pack

    response = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "execution",
            "resource_id": str(execution.id),
            "reason": "x",
        },
    )

    assert response.status_code == 422


def test_a_hold_on_an_unknown_record_is_a_404(client):
    response = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "execution",
            "resource_id": str(uuid.uuid4()),
            "reason": "matter 2026-04 discovery",
        },
    )

    assert response.status_code == 404


def test_a_second_hold_on_the_same_record_is_a_conflict(client, pack):
    execution, _ = pack
    payload = {
        "resource_type": "execution",
        "resource_id": str(execution.id),
        "reason": "incident review INC-114",
    }
    client.post(f"{BASE}/holds", json=payload)

    response = client.post(f"{BASE}/holds", json=payload)

    assert response.status_code == 409


def test_an_unknown_resource_type_is_refused(client, pack):
    response = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "invoice",
            "resource_id": str(pack[0].id),
            "reason": "matter 2026-04 discovery",
        },
    )

    assert response.status_code == 422


def test_holds_are_listed_and_released(client, pack):
    execution, _ = pack
    created = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "execution",
            "resource_id": str(execution.id),
            "reason": "incident review INC-114",
        },
    ).json()

    listed = client.get(f"{BASE}/holds").json()
    assert [row["id"] for row in listed] == [created["id"]]

    released = client.post(
        f"{BASE}/holds/{created['id']}/release",
        json={"reason": "review closed, nothing found"},
    )
    assert released.status_code == 200
    assert released.json()["active"] is False

    assert client.get(f"{BASE}/holds").json() == []
    history = client.get(f"{BASE}/holds", params={"active_only": False}).json()
    assert len(history) == 1
    assert history[0]["release_reason"] == "review closed, nothing found"


def test_releasing_an_unknown_hold_is_a_404(client):
    response = client.post(
        f"{BASE}/holds/{uuid.uuid4()}/release",
        json={"reason": "review closed, nothing found"},
    )

    assert response.status_code == 404


def test_releasing_twice_is_a_conflict(client, pack):
    execution, _ = pack
    created = client.post(
        f"{BASE}/holds",
        json={
            "resource_type": "execution",
            "resource_id": str(execution.id),
            "reason": "incident review INC-114",
        },
    ).json()
    body = {"reason": "review closed, nothing found"}
    client.post(f"{BASE}/holds/{created['id']}/release", json=body)

    response = client.post(f"{BASE}/holds/{created['id']}/release", json=body)

    assert response.status_code == 409


# --- period export ---------------------------------------------------------


def test_the_export_streams_a_tar_with_a_manifest(client, db_session, account):
    db_session.add(
        AuditLog(
            account_id=account.id,
            action="permission_check",
            status="success",
            timestamp=datetime(2026, 4, 15, tzinfo=UTC),
        )
    )
    db_session.flush()

    response = client.post(
        f"{BASE}/exports", params={"start": "2026-04-01", "end": "2026-05-01"}
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/gzip"
    assert (
        "preloop-period-export-2026-04-01-to-2026-05-01.tar.gz"
        in (response.headers["content-disposition"])
    )
    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        manifest = json.loads(tar.extractfile("manifest.json").read())
    assert manifest["schema"] == "preloop.retention.period_export_manifest/v1"
    assert manifest["counts"]["audit"] == 1
    assert manifest["members_digest"] == response.headers["x-preloop-members-digest"]


def test_the_response_carries_the_digest_of_the_bytes_served(client):
    import hashlib

    response = client.post(
        f"{BASE}/exports", params={"start": "2026-04-01", "end": "2026-05-01"}
    )

    assert response.headers["x-preloop-archive-sha256"] == (
        hashlib.sha256(response.content).hexdigest()
    )


def test_an_export_is_audited(client, db_session, account, test_user):
    client.post(f"{BASE}/exports", params={"start": "2026-04-01", "end": "2026-05-01"})

    row = db_session.execute(
        select(AuditLog).where(
            AuditLog.account_id == account.id,
            AuditLog.action == "retention_period_export",
        )
    ).scalar_one()
    assert row.user_id == test_user.id
    assert row.details["period_start"] == "2026-04-01T00:00:00Z"
    assert row.details["archive_sha256"]


def test_an_inverted_period_is_refused(client):
    response = client.post(
        f"{BASE}/exports", params={"start": "2026-05-01", "end": "2026-04-01"}
    )

    assert response.status_code == 400


def test_a_period_over_a_year_is_refused(client):
    response = client.post(
        f"{BASE}/exports", params={"start": "2024-01-01", "end": "2026-01-01"}
    )

    assert response.status_code == 400
    assert "366" in response.json()["detail"]


def test_an_unparseable_date_is_refused(client):
    response = client.post(
        f"{BASE}/exports", params={"start": "last april", "end": "2026-05-01"}
    )

    assert response.status_code == 400


def test_a_period_over_the_row_cap_is_refused(client, db_session, account, monkeypatch):
    monkeypatch.setattr(settings, "retention_export_max_rows", 1, raising=False)
    for _ in range(2):
        db_session.add(
            AuditLog(
                account_id=account.id,
                action="permission_check",
                status="success",
                timestamp=datetime(2026, 4, 15, tzinfo=UTC),
            )
        )
    db_session.flush()

    response = client.post(
        f"{BASE}/exports", params={"start": "2026-04-01", "end": "2026-05-01"}
    )

    assert response.status_code == 413
