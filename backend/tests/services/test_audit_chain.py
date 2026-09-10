"""The audit hash chain: building it, verifying it, and catching a break."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.models.audit_chain import GENESIS_HASH, AuditChainCheckpoint
from preloop.models.models.audit_log import AuditLog
from preloop.services import audit_chain, record_signing

NO_LAG = timedelta(0)


@pytest.fixture(autouse=True)
def chain_enabled(monkeypatch):
    """The sealer is on by default; make the bounds small and deterministic."""
    monkeypatch.setattr(settings, "audit_chain_enabled", True, raising=False)
    monkeypatch.setattr(settings, "audit_chain_seal_lag_seconds", 0, raising=False)
    monkeypatch.setattr(settings, "audit_chain_checkpoint_interval", 5, raising=False)


@pytest.fixture
def account_id(test_user):
    return test_user.account_id


def _rows(db_session, account_id, count, *, start_minutes: int = 600):
    """Write ``count`` audit rows with strictly increasing timestamps."""
    base = datetime.now(UTC) - timedelta(minutes=start_minutes)
    written = []
    for index in range(count):
        row = AuditLog(
            account_id=account_id,
            action="permission_check",
            resource_type="tool",
            resource_id=f"tool-{index}",
            status="success",
            details={"index": index, "nested": {"b": 2, "a": 1}},
            timestamp=base + timedelta(seconds=index),
        )
        db_session.add(row)
        written.append(row)
    db_session.flush()
    return written


def _seal(db_session, account_id, **kwargs):
    return audit_chain.seal_account(
        db_session, account_id=account_id, lag=NO_LAG, **kwargs
    )


def test_sealing_builds_a_chain_from_genesis(db_session, account_id):
    _rows(db_session, account_id, 4)

    result = _seal(db_session, account_id)

    assert result.sealed == 4
    sealed = (
        db_session.execute(
            select(AuditLog)
            .where(AuditLog.account_id == account_id)
            .order_by(AuditLog.chain_seq)
        )
        .scalars()
        .all()
    )
    assert [row.chain_seq for row in sealed] == [1, 2, 3, 4]
    assert sealed[0].prev_hash == GENESIS_HASH
    for previous, current in zip(sealed, sealed[1:], strict=False):
        assert current.prev_hash == previous.row_hash
    assert all(row.sealed_at is not None for row in sealed)


def test_row_hash_is_the_documented_computation(db_session, account_id):
    row = _rows(db_session, account_id, 1)[0]

    _seal(db_session, account_id)

    payload = audit_chain.canonical_row(row, seq=1, prev_hash=GENESIS_HASH)
    assert row.row_hash == audit_chain.hash_row(payload)
    # Domain separated: the same canonical JSON hashed without the domain
    # prefix must not collide with a row hash.
    import hashlib

    from preloop.cra.evidence_pack import canonical_manifest_json

    assert row.row_hash != hashlib.sha256(canonical_manifest_json(payload)).hexdigest()


def test_a_second_pass_continues_where_the_first_stopped(db_session, account_id):
    _rows(db_session, account_id, 3)
    _seal(db_session, account_id)
    _rows(db_session, account_id, 2, start_minutes=300)

    second = _seal(db_session, account_id)

    assert second.sealed == 2
    report = audit_chain.verify_chain(db_session, account_id=account_id)
    assert report["status"] == audit_chain.STATUS_OK
    assert report["head_seq"] == 5
    assert report["checked_rows"] == 5


def test_sealing_is_bounded_by_batch_size(db_session, account_id):
    _rows(db_session, account_id, 7)

    result = _seal(db_session, account_id, batch_size=3, max_batches=1)

    assert result.sealed == 3
    assert result.more_remaining is True
    assert (
        audit_chain.chain_status(db_session, account_id=account_id)["unsealed_rows"]
        == 4
    )


def test_verify_reports_ok_and_counts_what_it_checked(db_session, account_id):
    _rows(db_session, account_id, 6)
    _seal(db_session, account_id)

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_OK
    assert report["first_break"] is None
    assert report["checked_rows"] == 6
    assert report["unsealed_rows"] == 0


def test_verify_on_an_empty_chain_is_empty_not_broken(db_session, account_id):
    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_EMPTY
    assert report["first_break"] is None


def test_an_edited_row_breaks_at_that_row(db_session, account_id):
    rows = _rows(db_session, account_id, 5)
    _seal(db_session, account_id)
    # The classic tamper: change the outcome of a denied action.
    rows[2].status = "denied"
    db_session.add(rows[2])
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_BROKEN
    assert report["first_break"]["kind"] == audit_chain.BREAK_ROW_HASH
    assert report["first_break"]["seq"] == 3
    assert report["first_break"]["row_id"] == str(rows[2].id)
    # The walk stops at the first break rather than reporting every row after
    # it as broken too, which is what "first break" means.
    assert report["checked_rows"] == 2


def test_a_deleted_row_breaks_as_a_gap(db_session, account_id):
    rows = _rows(db_session, account_id, 5)
    _seal(db_session, account_id)
    db_session.delete(rows[3])
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_BROKEN
    assert report["first_break"]["kind"] == audit_chain.BREAK_MISSING_ROW
    assert report["first_break"]["seq"] == 4


def test_a_rewritten_prev_hash_breaks_the_link(db_session, account_id):
    rows = _rows(db_session, account_id, 4)
    _seal(db_session, account_id)
    # Rehash one row consistently with a forged predecessor: the row hashes
    # to its own stored value, so only the link catches it.
    forged_prev = "f" * 64
    rows[2].prev_hash = forged_prev
    rows[2].row_hash = audit_chain.hash_row(
        audit_chain.canonical_row(rows[2], seq=3, prev_hash=forged_prev)
    )
    db_session.add(rows[2])
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_BROKEN
    assert report["first_break"]["kind"] == audit_chain.BREAK_PREV_HASH
    assert report["first_break"]["seq"] == 3


def test_rows_deleted_from_the_tail_are_reported(db_session, account_id):
    rows = _rows(db_session, account_id, 4)
    _seal(db_session, account_id)
    db_session.delete(rows[3])
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_BROKEN
    assert report["first_break"]["kind"] == audit_chain.BREAK_MISSING_ROW
    assert report["first_break"]["seq"] == 4


def test_a_purged_prefix_is_not_a_break(db_session, account_id):
    rows = _rows(db_session, account_id, 5)
    _seal(db_session, account_id)
    for row in rows[:2]:
        db_session.delete(row)
    audit_chain.note_pruned(
        db_session, account_id=account_id, up_to_seq=2, now=datetime.now(UTC)
    )
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_OK
    assert report["pruned_below_seq"] == 2
    assert report["start_seq"] == 3
    assert report["checked_rows"] == 3


def test_checkpoints_are_written_every_n_rows_and_are_signed(db_session, account_id):
    _rows(db_session, account_id, 11)

    result = _seal(db_session, account_id)

    assert result.checkpoints == 2
    checkpoints = (
        db_session.execute(
            select(AuditChainCheckpoint)
            .where(AuditChainCheckpoint.account_id == account_id)
            .order_by(AuditChainCheckpoint.seq)
        )
        .scalars()
        .all()
    )
    assert [cp.seq for cp in checkpoints] == [5, 10]
    key = record_signing.get_active_key(db_session, account_id=account_id)
    assert key is not None
    for checkpoint in checkpoints:
        assert checkpoint.signing_key_id == key.key_id
        summary = audit_chain.checkpoint_summary(checkpoint)
        document = {
            "algorithm": "ed25519",
            "key_id": checkpoint.signing_key_id,
            "payload_type": audit_chain.CHECKPOINT_SCHEMA,
            "digest": summary["digest"],
            "signed_at": summary["checkpointed_at"],
            "signature": checkpoint.signature,
        }
        assert record_signing.verify_signature_document(
            document, public_key=key.public_key
        )


def test_a_checkpoint_that_no_longer_matches_the_chain_is_a_break(
    db_session, account_id
):
    _rows(db_session, account_id, 5)
    _seal(db_session, account_id)
    checkpoint = db_session.execute(
        select(AuditChainCheckpoint).where(
            AuditChainCheckpoint.account_id == account_id
        )
    ).scalar_one()
    checkpoint.chain_hash = "0" * 63 + "1"
    db_session.add(checkpoint)
    db_session.flush()

    report = audit_chain.verify_chain(db_session, account_id=account_id)

    assert report["status"] == audit_chain.STATUS_BROKEN
    assert report["first_break"]["kind"] == "checkpoint_hash_mismatch"


def test_the_segment_endpoint_material_recomputes_client_side(db_session, account_id):
    _rows(db_session, account_id, 3)
    _seal(db_session, account_id)

    segment = audit_chain.chain_segment(db_session, account_id=account_id)

    assert segment["head_seq"] == 3
    assert len(segment["entries"]) == 3
    previous = segment["genesis_hash"]
    for entry in segment["entries"]:
        assert entry["prev_hash"] == previous
        assert audit_chain.hash_row(entry["payload"]) == entry["row_hash"]
        previous = entry["row_hash"]


def test_the_pass_skips_everything_when_the_chain_is_disabled(
    db_session, account_id, monkeypatch
):
    monkeypatch.setattr(settings, "audit_chain_enabled", False, raising=False)
    _rows(db_session, account_id, 2)

    summary = audit_chain.run_seal_pass(db_session, lag=NO_LAG)

    assert summary.skipped_reason == "disabled"
    assert summary.sealed == 0


def test_the_pass_seals_the_named_account(db_session, account_id):
    _rows(db_session, account_id, 3)

    summary = audit_chain.run_seal_pass(
        db_session, account_ids=[account_id], lag=NO_LAG
    )

    assert summary.sealed == 3
    assert summary.accounts == 1


def test_the_lag_leaves_recent_rows_unsealed(db_session, account_id):
    _rows(db_session, account_id, 2, start_minutes=0)

    result = audit_chain.seal_account(
        db_session, account_id=account_id, lag=timedelta(seconds=300)
    )

    assert result.sealed == 0
    assert (
        audit_chain.chain_status(db_session, account_id=account_id)["unsealed_rows"]
        == 2
    )


def test_status_reports_the_head_and_the_pending_tail(db_session, account_id):
    _rows(db_session, account_id, 5)
    _seal(db_session, account_id, batch_size=2, max_batches=1)

    status = audit_chain.chain_status(db_session, account_id=account_id)

    assert status["head_seq"] == 2
    assert status["sealed_rows"] == 2
    assert status["unsealed_rows"] == 3
    assert status["checkpoint_interval"] == 5
    assert status["head_hash"] != GENESIS_HASH


def test_one_account_chain_ignores_another_accounts_rows(db_session, test_user):
    other = models.Account(organization_name="other-co", is_active=True)
    db_session.add(other)
    db_session.flush()
    _rows(db_session, test_user.account_id, 3)
    _rows(db_session, other.id, 2)

    _seal(db_session, test_user.account_id)

    assert (
        audit_chain.chain_status(db_session, account_id=test_user.account_id)[
            "head_seq"
        ]
        == 3
    )
    assert audit_chain.chain_status(db_session, account_id=other.id)["head_seq"] == 0
    assert (
        audit_chain.chain_status(db_session, account_id=other.id)["unsealed_rows"] == 2
    )
