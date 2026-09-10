"""Legal hold: actor, reason, what it freezes and what release restores."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.models import models
from preloop.models.crud import flow_artifact as crud_artifact
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.legal_hold import LegalHold
from preloop.services.flow_artifacts import evidence_receipt
from preloop.services.legal_hold import (
    LegalHoldError,
    place_hold,
    release_hold,
)


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


@pytest.fixture
def pack(db_session, test_user):
    """One execution with one evidence pack whose payload expires shortly."""
    flow = models.Flow(
        name=f"hold-{uuid.uuid4().hex[:8]}",
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
        manifest={"sha256": "a" * 64, "size_bytes": 12},
        manifest_sha256="b" * 64,
        ciphertext=b"cipher",
        expires_at=datetime.now(UTC) - timedelta(hours=1),
        availability="available",
    )
    db_session.add(artifact)
    execution.evidence_receipt = {
        "status": "available",
        "artifact_id": str(artifact.id),
        "kind": "evidence",
        "legal_hold": False,
    }
    db_session.add(execution)
    db_session.flush()
    return execution, artifact


# --- the record ------------------------------------------------------------


def test_a_hold_records_the_actor_and_the_reason(db_session, test_user, account, pack):
    execution, _ = pack

    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
        user_id=test_user.id,
    )

    assert outcome.hold.reason == "incident review INC-114"
    assert outcome.hold.placed_by_user_id == test_user.id
    assert outcome.hold.active is True
    assert outcome.flagged["flow_execution"] == 1


def test_a_hold_without_a_real_reason_is_refused(db_session, account, pack):
    execution, _ = pack

    with pytest.raises(LegalHoldError) as excinfo:
        place_hold(
            db_session,
            account_id=account.id,
            resource_type="execution",
            resource_id=str(execution.id),
            reason="asdf",
        )

    assert excinfo.value.code == "reason_required"


def test_a_hold_on_another_accounts_record_is_refused(db_session, account, pack):
    with pytest.raises(LegalHoldError) as excinfo:
        place_hold(
            db_session,
            account_id=uuid.uuid4(),
            resource_type="execution",
            resource_id=str(pack[0].id),
            reason="fishing for someone else's records",
        )

    assert excinfo.value.code == "resource_not_found"


def test_a_hold_on_a_nonexistent_record_is_refused(db_session, account):
    with pytest.raises(LegalHoldError) as excinfo:
        place_hold(
            db_session,
            account_id=account.id,
            resource_type="execution",
            resource_id=str(uuid.uuid4()),
            reason="matter 2026-04 discovery",
        )

    assert excinfo.value.code == "resource_not_found"


def test_holding_the_same_record_twice_is_refused(db_session, account, pack):
    execution, _ = pack
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )

    with pytest.raises(LegalHoldError) as excinfo:
        place_hold(
            db_session,
            account_id=account.id,
            resource_type="execution",
            resource_id=str(execution.id),
            reason="incident review INC-114 again",
        )

    assert excinfo.value.code == "already_held"


def test_a_released_record_can_be_held_again(db_session, account, pack):
    execution, _ = pack
    first = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )
    release_hold(
        db_session,
        account_id=account.id,
        hold_id=first.hold.id,
        reason="review closed, nothing found",
    )

    second = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="reopened as INC-114b",
    )

    assert second.hold.id != first.hold.id
    rows = (
        db_session.execute(select(LegalHold).where(LegalHold.account_id == account.id))
        .scalars()
        .all()
    )
    assert len(rows) == 2


# --- what it freezes -------------------------------------------------------


def test_a_held_pack_survives_the_janitor(db_session, account, pack):
    """The whole point: expiry must not take bytes a hold is protecting."""
    execution, artifact = pack
    artifact_id = artifact.id
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="evidence_pack",
        resource_id=str(artifact_id),
        reason="regulator request 2026-05",
    )

    cleared = crud_artifact.cleanup(db_session, now=datetime.now(UTC))

    row = db_session.get(models.FlowArtifact, artifact_id)
    assert row.ciphertext is not None
    assert row.availability == "available"
    assert cleared == 0


def test_an_unheld_pack_still_expires(db_session, account, pack):
    _, artifact = pack
    artifact_id = artifact.id

    crud_artifact.cleanup(db_session, now=datetime.now(UTC))

    row = db_session.get(models.FlowArtifact, artifact_id)
    assert row.ciphertext is None
    assert row.availability == "expired"


def test_the_receipt_reports_the_hold(db_session, account, pack):
    execution, artifact = pack
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="evidence_pack",
        resource_id=str(artifact.id),
        reason="regulator request 2026-05",
    )
    db_session.refresh(artifact)

    receipt = evidence_receipt(
        status="available",
        execution_id=execution.id,
        transport="direct",
        artifact=artifact,
    )

    assert receipt["legal_hold"] is True
    # Preloop cannot verify a property of the storage layer beneath it.
    assert receipt["object_lock"] is False


def test_the_persisted_receipt_is_stamped_so_polling_agrees(db_session, account, pack):
    execution, artifact = pack

    place_hold(
        db_session,
        account_id=account.id,
        resource_type="evidence_pack",
        resource_id=str(artifact.id),
        reason="regulator request 2026-05",
    )

    db_session.refresh(execution)
    assert execution.evidence_receipt["legal_hold"] is True


def test_an_execution_hold_reaches_its_packs(db_session, account, pack):
    execution, artifact = pack

    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )

    db_session.refresh(artifact)
    assert artifact.legal_hold is True
    assert outcome.flagged["flow_artifact"] == 1


# --- release ---------------------------------------------------------------


def test_release_clears_the_flags_and_keeps_the_record(
    db_session, test_user, account, pack
):
    execution, artifact = pack
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
        user_id=test_user.id,
    )

    released = release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="review closed, nothing found",
        user_id=test_user.id,
    )

    db_session.refresh(execution)
    db_session.refresh(artifact)
    assert execution.legal_hold is False
    assert artifact.legal_hold is False
    assert released.hold.released_at is not None
    assert released.hold.release_reason == "review closed, nothing found"
    assert released.hold.reason == "incident review INC-114"


def test_overlapping_holds_do_not_cancel_each_other(db_session, account, pack):
    """A pack under its own hold stays frozen when the execution hold lifts."""
    execution, artifact = pack
    execution_hold = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="evidence_pack",
        resource_id=str(artifact.id),
        reason="regulator request 2026-05",
    )

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=execution_hold.hold.id,
        reason="review closed, the regulator matter is separate",
    )

    db_session.refresh(artifact)
    db_session.refresh(execution)
    assert artifact.legal_hold is True
    assert execution.legal_hold is False


def test_releasing_twice_is_refused(db_session, account, pack):
    execution, _ = pack
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )
    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="review closed, nothing found",
    )

    with pytest.raises(LegalHoldError) as excinfo:
        release_hold(
            db_session,
            account_id=account.id,
            hold_id=outcome.hold.id,
            reason="review closed, nothing found",
        )

    assert excinfo.value.code == "already_released"


def test_releasing_another_accounts_hold_is_refused(db_session, account, pack):
    execution, _ = pack
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
    )

    with pytest.raises(LegalHoldError) as excinfo:
        release_hold(
            db_session,
            account_id=uuid.uuid4(),
            hold_id=outcome.hold.id,
            reason="lifting somebody else's hold",
        )

    assert excinfo.value.code == "hold_not_found"


# --- audit -----------------------------------------------------------------


def test_placing_and_releasing_are_both_audited(db_session, test_user, account, pack):
    """A hold that can be lifted without a trace proves nothing."""
    execution, _ = pack
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
        user_id=test_user.id,
    )
    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="review closed, nothing found",
        user_id=test_user.id,
    )

    rows = (
        db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.account_id == account.id,
                AuditLog.action.in_(["legal_hold_placed", "legal_hold_released"]),
            )
            .order_by(AuditLog.timestamp)
        )
        .scalars()
        .all()
    )

    assert [row.action for row in rows] == [
        "legal_hold_placed",
        "legal_hold_released",
    ]
    assert all(row.user_id == test_user.id for row in rows)
    assert rows[0].details["reason"] == "incident review INC-114"
    assert rows[0].details["hold_id"] == str(outcome.hold.id)
    assert rows[1].details["placed_reason"] == "incident review INC-114"
