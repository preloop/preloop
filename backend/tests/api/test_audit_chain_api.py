"""API surface for the audit chain and the account's signing keys."""

import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.services import audit_chain, record_signing

BASE = "/api/v1/audit/chain"
KEYS = "/api/v1/signing/keys"


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _rows(db_session, account_id, count, *, action="permission_check"):
    when = datetime(2026, 4, 1, tzinfo=UTC)
    for index in range(count):
        db_session.add(
            AuditLog(
                account_id=account_id,
                action=action,
                resource_type="tool",
                resource_id=f"r{index}",
                status="success",
                timestamp=when + timedelta(seconds=index),
                details={"index": index},
            )
        )
    db_session.flush()


def _seal(db_session, account_id):
    return audit_chain.seal_account(
        db_session,
        account_id=account_id,
        batch_size=100,
        max_batches=10,
        now=datetime.now(UTC) + timedelta(hours=1),
        lag=timedelta(0),
    )


# --- status ----------------------------------------------------------------


def test_status_reports_an_empty_chain_without_inventing_one(client):
    response = client.get(f"{BASE}/status")

    assert response.status_code == 200
    body = response.json()
    assert body["head_seq"] == 0
    assert body["head_hash"] == "0" * 64
    assert body["latest_checkpoint"] is None


def test_status_reports_the_head_after_sealing(client, db_session, account):
    _rows(db_session, account.id, 3)
    _seal(db_session, account.id)

    body = client.get(f"{BASE}/status").json()

    assert body["head_seq"] >= 3
    assert body["head_hash"] != "0" * 64
    assert body["sealed_rows"] >= 3


def test_status_counts_rows_that_are_written_but_not_yet_chained(
    client, db_session, account
):
    _rows(db_session, account.id, 2)

    body = client.get(f"{BASE}/status").json()

    # Honest about the lag rather than implying every row is protected.
    assert body["unsealed_rows"] >= 2
    assert body["head_seq"] == 0


# --- verification ----------------------------------------------------------


def test_verify_walks_the_chain_and_calls_it_ok(client, db_session, account):
    _rows(db_session, account.id, 5)
    _seal(db_session, account.id)

    body = client.get(f"{BASE}/verify").json()

    assert body["status"] == "ok"
    assert body["checked_rows"] >= 5
    assert body["first_break"] is None


def test_verify_finds_an_edited_row_and_names_it(client, db_session, account):
    _rows(db_session, account.id, 5)
    _seal(db_session, account.id)
    victim = (
        db_session.query(AuditLog)
        .filter(AuditLog.account_id == account.id, AuditLog.chain_seq == 3)
        .one()
    )
    victim.status = "denied"
    db_session.add(victim)
    db_session.flush()

    body = client.get(f"{BASE}/verify").json()

    assert body["status"] == "broken"
    assert body["first_break"]["kind"] == "row_hash_mismatch"
    assert body["first_break"]["seq"] == 3
    assert body["first_break"]["row_id"] == str(victim.id)


def test_verify_finds_a_deleted_row(client, db_session, account):
    _rows(db_session, account.id, 5)
    _seal(db_session, account.id)
    db_session.query(AuditLog).filter(
        AuditLog.account_id == account.id, AuditLog.chain_seq == 2
    ).delete()
    db_session.flush()

    body = client.get(f"{BASE}/verify").json()

    assert body["status"] == "broken"
    assert body["first_break"]["kind"] == "missing_row"
    assert body["first_break"]["seq"] == 2


def test_a_verification_is_itself_audited(client, db_session, account):
    _rows(db_session, account.id, 2)
    _seal(db_session, account.id)

    client.get(f"{BASE}/verify")

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.account_id == account.id,
            AuditLog.action == "audit_chain_verified",
        )
        .one()
    )
    assert row.details["chain_status"] == "ok"


# --- segment ---------------------------------------------------------------


def test_the_segment_endpoint_hands_back_material_a_client_can_check(
    client, db_session, account
):
    _rows(db_session, account.id, 4)
    _seal(db_session, account.id)

    body = client.get(f"{BASE}/segment", params={"limit": 10}).json()

    assert body["genesis_hash"] == "0" * 64
    assert body["entries"]
    previous = body["genesis_hash"]
    for entry in body["entries"]:
        payload = json.dumps(
            entry["payload"], sort_keys=True, separators=(",", ":"), default=str
        ).encode()
        recomputed = hashlib.sha256(
            body["row_domain"].encode("utf-8") + payload
        ).hexdigest()
        assert entry["prev_hash"] == previous
        assert recomputed == entry["row_hash"]
        previous = entry["row_hash"]


def test_the_segment_endpoint_pages(client, db_session, account):
    _rows(db_session, account.id, 6)
    _seal(db_session, account.id)

    first = client.get(f"{BASE}/segment", params={"limit": 2}).json()
    assert first["has_more"] is True
    last_seq = first["entries"][-1]["seq"]

    second = client.get(
        f"{BASE}/segment", params={"limit": 2, "after_seq": last_seq}
    ).json()

    assert second["entries"][0]["seq"] == last_seq + 1


# --- checkpoints -----------------------------------------------------------


def test_checkpoints_are_listed_with_a_verifiable_signature(
    client, db_session, account, monkeypatch
):
    monkeypatch.setattr(
        "preloop.services.audit_chain.settings.audit_chain_checkpoint_interval", 2
    )
    _rows(db_session, account.id, 4)
    _seal(db_session, account.id)

    body = client.get(f"{BASE}/checkpoints").json()

    assert body
    checkpoint = body[0]
    key = record_signing.get_key_by_id(
        db_session, account_id=account.id, key_id=checkpoint["signing_key_id"]
    )
    assert record_signing.verify_signature_document(
        checkpoint["signature_document"], public_key=key.public_key
    )


# --- keys ------------------------------------------------------------------


def test_the_public_key_is_served_and_the_private_one_is_not(
    client, db_session, account
):
    record_signing.create_key(db_session, account_id=account.id, commit=False)
    db_session.flush()

    body = client.get(KEYS).json()

    assert body["active_key_id"].startswith("psk_")
    assert body["signature_schema"] == "preloop.signature/v1"
    assert body["signed_bytes_format"]
    key = body["keys"][0]
    assert len(base64.b64decode(key["public_key"])) == 32
    assert "private" not in json.dumps(body)


def test_rotation_retires_the_old_key_and_keeps_it_listed(client, db_session, account):
    record_signing.create_key(db_session, account_id=account.id, commit=False)
    db_session.flush()
    before = client.get(KEYS).json()["active_key_id"]

    response = client.post(f"{KEYS}/rotate")

    assert response.status_code == 201
    body = response.json()
    assert body["retired"]["key_id"] == before
    assert body["active"]["key_id"] != before
    listed = client.get(KEYS).json()
    assert {key["key_id"] for key in listed["keys"]} == {
        before,
        body["active"]["key_id"],
    }
    assert listed["active_key_id"] == body["active"]["key_id"]


def test_the_rotated_key_signs_and_the_published_public_key_verifies(
    client, db_session, account
):
    client.post(f"{KEYS}/rotate")
    published = client.get(KEYS).json()
    active = next(key for key in published["keys"] if key["active"])

    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )

    Ed25519PublicKey.from_public_bytes(base64.b64decode(active["public_key"])).verify(
        base64.b64decode(document["signature"]),
        record_signing.signed_bytes(
            payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
            digest=document["digest"],
            signed_at=document["signed_at"],
        ),
    )


def test_a_rotation_is_audited_with_both_key_ids(client, db_session, account):
    record_signing.create_key(db_session, account_id=account.id, commit=False)
    db_session.flush()
    before = client.get(KEYS).json()["active_key_id"]

    body = client.post(f"{KEYS}/rotate").json()

    row = (
        db_session.query(AuditLog)
        .filter(
            AuditLog.account_id == account.id,
            AuditLog.action == "signing_key_rotated",
        )
        .one()
    )
    assert row.details["retired_key_id"] == before
    assert row.details["new_key_id"] == body["active"]["key_id"]


def test_another_accounts_chain_is_not_visible(client, db_session, account):
    other = models.Account(organization_name=f"other-{uuid.uuid4().hex[:6]}")
    db_session.add(other)
    db_session.flush()
    _rows(db_session, other.id, 3)
    _seal(db_session, other.id)

    body = client.get(f"{BASE}/status").json()

    assert body["head_seq"] == 0
    assert client.get(f"{BASE}/segment").json()["entries"] == []
