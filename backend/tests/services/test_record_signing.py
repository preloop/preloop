"""Signing keys, rotation, and what a signature does and does not verify.

The point of these tests is a verifier that never trusts the code that made
the signature: every check here rebuilds the signed bytes from the payload and
checks them against the raw public key with ``cryptography``, not with our own
helper, wherever the two could hide the same mistake.
"""

import base64
import hashlib
from datetime import UTC, datetime

import pytest
from cryptography.exceptions import InvalidSignature
from sqlalchemy.exc import IntegrityError
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from preloop.cra.evidence_pack import canonical_manifest_json
from preloop.models import models
from preloop.models.models.account_signing_key import AccountSigningKey
from preloop.services import record_signing


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _raw_verify(public_key_b64: str, signature_b64: str, message: bytes) -> None:
    """Verify with cryptography directly, not through our own verifier."""
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    key.verify(base64.b64decode(signature_b64), message)


# --- keys ------------------------------------------------------------------


def test_a_key_is_minted_on_first_use(db_session, account):
    assert record_signing.get_active_key(db_session, account_id=account.id) is None

    key = record_signing.ensure_key(db_session, account_id=account.id, commit=False)

    assert key is not None
    assert key.key_id.startswith("psk_")
    assert key.algorithm == "ed25519"
    assert key.active is True
    assert len(base64.b64decode(key.public_key)) == 32


def test_the_private_key_is_not_stored_in_the_clear(db_session, account):
    key = record_signing.create_key(db_session, account_id=account.id, commit=False)

    material = record_signing.load_material(key)
    raw_private = material.private_key.private_bytes_raw()

    assert base64.b64encode(raw_private).decode() not in key.private_key_encrypted
    assert raw_private not in key.private_key_encrypted.encode()
    # It round trips, so the encryption is real rather than lossy.
    assert material.private_key.public_key().public_bytes_raw() == base64.b64decode(
        key.public_key
    )


def test_the_public_summary_never_carries_private_material(db_session, account):
    key = record_signing.create_key(db_session, account_id=account.id, commit=False)

    summary = record_signing.public_key_summary(key)

    assert set(summary) == {
        "key_id",
        "algorithm",
        "public_key",
        "active",
        "created_at",
        "retired_at",
    }
    assert key.private_key_encrypted not in str(summary)


def test_only_one_key_is_active_at_a_time(db_session, account):
    record_signing.create_key(db_session, account_id=account.id, commit=False)

    # The partial unique index refuses the second one, in the database rather
    # than in a service check that concurrent callers can race past.
    with pytest.raises(IntegrityError):
        record_signing.create_key(db_session, account_id=account.id, commit=False)
        db_session.flush()
    db_session.rollback()


# --- rotation --------------------------------------------------------------


def test_rotation_retires_the_old_key_and_mints_a_new_one(
    db_session, account, test_user
):
    first = record_signing.create_key(db_session, account_id=account.id, commit=False)

    retired, active = record_signing.rotate_key(
        db_session, account_id=account.id, user_id=test_user.id, commit=False
    )

    assert retired is not None and retired.key_id == first.key_id
    assert retired.retired_at is not None
    assert active.key_id != first.key_id
    assert active.retired_at is None
    assert record_signing.get_active_key(db_session, account_id=account.id).key_id == (
        active.key_id
    )


def test_a_signature_from_a_retired_key_still_verifies(db_session, account):
    manifest = {"schema": "test", "value": 1}
    before = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest=manifest,
        commit=False,
    )
    old_key_id = before["key_id"]

    record_signing.rotate_key(db_session, account_id=account.id, commit=False)

    old = record_signing.get_key_by_id(
        db_session, account_id=account.id, key_id=old_key_id
    )
    assert old.retired_at is not None
    assert record_signing.verify_signature_document(
        before,
        public_key=old.public_key,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        digest=record_signing.digest_of(manifest),
    )


def test_the_new_key_cannot_verify_what_the_old_one_signed(db_session, account):
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )

    _, active = record_signing.rotate_key(
        db_session, account_id=account.id, commit=False
    )

    assert not record_signing.verify_signature_document(
        document, public_key=active.public_key
    )


def test_rotation_is_recorded_against_the_user_who_asked(
    db_session, account, test_user
):
    record_signing.create_key(db_session, account_id=account.id, commit=False)

    _, active = record_signing.rotate_key(
        db_session, account_id=account.id, user_id=test_user.id, commit=False
    )

    assert active.rotated_by_user_id == test_user.id


# --- signatures ------------------------------------------------------------


def test_the_signature_verifies_against_the_published_public_key(db_session, account):
    manifest = {"schema": "test", "members": [{"name": "a", "sha256": "00"}]}

    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest=manifest,
        commit=False,
    )

    key = record_signing.get_active_key(db_session, account_id=account.id)
    message = record_signing.signed_bytes(
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        digest=hashlib.sha256(canonical_manifest_json(manifest)).hexdigest(),
        signed_at=document["signed_at"],
    )
    _raw_verify(key.public_key, document["signature"], message)


def test_a_changed_payload_fails_verification(db_session, account):
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test", "value": 1},
        commit=False,
    )
    key = record_signing.get_active_key(db_session, account_id=account.id)

    assert not record_signing.verify_signature_document(
        document,
        public_key=key.public_key,
        digest=record_signing.digest_of({"schema": "test", "value": 2}),
    )


def test_a_signature_cannot_be_replayed_as_another_payload_type(db_session, account):
    manifest = {"schema": "test"}
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_EVIDENCE_PACK,
        manifest=manifest,
        commit=False,
    )
    key = record_signing.get_active_key(db_session, account_id=account.id)

    # The claimed type is inside the signed bytes, so relabelling the document
    # breaks the signature rather than changing what it means.
    forged = dict(document)
    forged["payload_type"] = record_signing.PAYLOAD_PERIOD_EXPORT
    assert not record_signing.verify_signature_document(
        forged, public_key=key.public_key
    )
    with pytest.raises(InvalidSignature):
        _raw_verify(
            key.public_key,
            document["signature"],
            record_signing.signed_bytes(
                payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
                digest=document["digest"],
                signed_at=document["signed_at"],
            ),
        )


def test_the_signing_time_is_covered_not_merely_claimed(db_session, account):
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )
    key = record_signing.get_active_key(db_session, account_id=account.id)

    forged = dict(document)
    forged["signed_at"] = "2020-01-01T00:00:00Z"

    assert not record_signing.verify_signature_document(
        forged, public_key=key.public_key
    )


def test_another_accounts_key_does_not_verify(db_session, account):
    other = models.Account(organization_name="other-co")
    db_session.add(other)
    db_session.flush()
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )
    stranger = record_signing.create_key(db_session, account_id=other.id, commit=False)

    assert not record_signing.verify_signature_document(
        document, public_key=stranger.public_key
    )


def test_a_garbled_signature_is_refused_rather_than_raising(db_session, account):
    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )
    key = record_signing.get_active_key(db_session, account_id=account.id)

    for bad in ["not base64 at all", "", "AAAA"]:
        forged = dict(document)
        forged["signature"] = bad
        assert not record_signing.verify_signature_document(
            forged, public_key=key.public_key
        )


def test_an_unusable_key_degrades_to_no_signature(db_session, account, monkeypatch):
    record_signing.create_key(db_session, account_id=account.id, commit=False)
    monkeypatch.setattr(
        record_signing,
        "decrypt_value",
        lambda value: (_ for _ in ()).throw(ValueError("wrong key")),
    )

    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
        commit=False,
    )

    assert document is None


# --- stored signatures over records ----------------------------------------


def test_an_evidence_pack_signature_is_stored_once_and_reused(db_session, account):
    first = record_signing.sign_evidence_pack(
        db_session,
        account_id=account.id,
        artifact_id="11111111-1111-1111-1111-111111111111",
        execution_id="22222222-2222-2222-2222-222222222222",
        archive_sha256="ab" * 32,
        size_bytes=17,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        commit=False,
    )

    second = record_signing.sign_evidence_pack(
        db_session,
        account_id=account.id,
        artifact_id="11111111-1111-1111-1111-111111111111",
        execution_id="22222222-2222-2222-2222-222222222222",
        archive_sha256="ab" * 32,
        size_bytes=17,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        commit=False,
    )

    assert first["signature"] == second["signature"]
    assert first["signed_at"] == second["signed_at"]


def test_the_stored_payload_is_what_a_downloader_can_rebuild(db_session, account):
    document = record_signing.sign_evidence_pack(
        db_session,
        account_id=account.id,
        artifact_id="11111111-1111-1111-1111-111111111111",
        execution_id="22222222-2222-2222-2222-222222222222",
        archive_sha256="cd" * 32,
        size_bytes=99,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
        commit=False,
    )
    key = record_signing.get_active_key(db_session, account_id=account.id)

    rebuilt = record_signing.evidence_pack_payload(
        account_id=account.id,
        artifact_id="11111111-1111-1111-1111-111111111111",
        execution_id="22222222-2222-2222-2222-222222222222",
        archive_sha256="cd" * 32,
        size_bytes=99,
        created_at=datetime(2026, 4, 1, tzinfo=UTC),
    )

    assert record_signing.digest_of(rebuilt) == document["digest"]
    _raw_verify(
        key.public_key,
        document["signature"],
        record_signing.signed_bytes(
            payload_type=record_signing.PAYLOAD_EVIDENCE_PACK,
            digest=record_signing.digest_of(rebuilt),
            signed_at=document["signed_at"],
        ),
    )


def test_sign_manifest_does_not_commit_the_callers_session(
    db_session, account, monkeypatch
):
    committed: list[bool] = []
    real_commit = db_session.commit

    def capture_commit() -> None:
        committed.append(True)
        real_commit()

    monkeypatch.setattr(db_session, "commit", capture_commit)

    document = record_signing.sign_manifest(
        db_session,
        account_id=account.id,
        payload_type=record_signing.PAYLOAD_PERIOD_EXPORT,
        manifest={"schema": "test"},
    )

    assert document is not None
    assert committed == []


def test_ensure_key_failure_does_not_roll_back_the_caller(
    db_session, account, monkeypatch
):
    marker = AccountSigningKey(
        account_id=account.id,
        key_id="psk_marker_retired",
        algorithm="ed25519",
        public_key="AA",
        private_key_encrypted="nonsense",
        retired_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    db_session.add(marker)
    db_session.flush()

    def boom(*args, **kwargs):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(record_signing, "create_key", boom)

    assert (
        record_signing.ensure_key(db_session, account_id=account.id, commit=False)
        is None
    )
    assert db_session.get(AccountSigningKey, marker.id) is not None


def test_signing_failure_leaves_the_callers_work_intact(db_session, account):
    marker = AccountSigningKey(
        account_id=account.id,
        key_id="psk_marker",
        algorithm="ed25519",
        public_key="AA",
        private_key_encrypted="nonsense",
    )
    db_session.add(marker)
    db_session.flush()

    # An unusable key: signing gives up and the caller's row survives.
    document = record_signing.sign_evidence_pack(
        db_session,
        account_id=account.id,
        artifact_id="33333333-3333-3333-3333-333333333333",
        execution_id=None,
        archive_sha256="ef" * 32,
        commit=False,
    )

    assert document is None
    assert db_session.get(AccountSigningKey, marker.id) is not None
