"""Legal hold: actor, reason, what it freezes and what release restores."""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from preloop.models import models
from preloop.models.crud import flow_artifact as crud_artifact
from preloop.models.crud import runtime_session_artifact as crud_session_artifact
from preloop.models.crud.history_policy import lock_account_for_retention
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


@pytest.fixture
def runtime_session(db_session, test_user):
    """One ended runtime session with an activity row under it."""
    stamp = datetime.now(UTC) - timedelta(days=400)
    session = models.RuntimeSession(
        account_id=test_user.account_id,
        session_source_type="managed_agent",
        session_source_id=f"agent-{uuid.uuid4().hex[:8]}",
        started_at=stamp,
        last_activity_at=stamp,
        ended_at=stamp,
    )
    db_session.add(session)
    db_session.flush()
    db_session.add(
        models.RuntimeSessionActivity(
            account_id=test_user.account_id,
            runtime_session_id=session.id,
            activity_type="tool_call",
            tool_name="search",
            status="success",
            timestamp=stamp,
        )
    )
    db_session.flush()
    return session


@pytest.fixture
def committed_runtime_session(
    db_engine: Engine,
) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    """Account and session committed so independent connections can lock them."""
    stamp = datetime.now(UTC) - timedelta(days=400)
    with Session(db_engine) as seed:
        account = models.Account(organization_name="hold-lock-race")
        seed.add(account)
        seed.flush()
        session = models.RuntimeSession(
            account_id=account.id,
            session_source_type="managed_agent",
            session_source_id=f"agent-{uuid.uuid4().hex[:8]}",
            started_at=stamp,
            last_activity_at=stamp,
            ended_at=stamp,
        )
        seed.add(session)
        seed.commit()
        account_id, session_id = account.id, session.id
    try:
        yield account_id, session_id
    finally:
        with Session(db_engine) as cleanup:
            cleanup.query(LegalHold).filter(
                LegalHold.account_id == str(account_id)
            ).delete()
            cleanup.query(AuditLog).filter(AuditLog.account_id == account_id).delete()
            cleanup.query(models.RuntimeSession).filter(
                models.RuntimeSession.id == session_id
            ).delete()
            cleanup.query(models.Account).filter(
                models.Account.id == account_id
            ).delete()
            cleanup.commit()


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


def test_a_hold_freezes_a_runtime_session(
    db_session, test_user, account, runtime_session
):
    """A session under litigation hold is flagged like every held class."""
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )

    db_session.refresh(runtime_session)
    assert runtime_session.legal_hold is True
    assert outcome.flagged["runtime_session"] == 1
    assert outcome.flagged["runtime_session_artifact"] == 0
    assert outcome.hold.resource_type == "runtime_session"


def test_a_session_hold_flags_its_artifacts_and_release_clears_them(
    db_session, test_user, account, runtime_session
):
    """place_hold and release_hold move the flag on every artifact of the session."""
    first = crud_session_artifact.store(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-1",
        content_type="image/png",
        plaintext=b"unpublished-screenshot-bytes",
        manifest={"step_index": 1},
        commit=False,
    )
    second = crud_session_artifact.store(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        kind="recording",
        source="browser_use",
        source_ref="clip-1",
        content_type="video/webm",
        plaintext=b"unpublished-recording-bytes",
        manifest={"duration_ms": 1000},
        commit=False,
    )
    db_session.commit()

    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )

    db_session.refresh(first)
    db_session.refresh(second)
    assert outcome.flagged["runtime_session_artifact"] == 2
    assert first.legal_hold is True
    assert second.legal_hold is True

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="matter closed 2026-08",
        user_id=test_user.id,
    )

    db_session.refresh(first)
    db_session.refresh(second)
    assert first.legal_hold is False
    assert second.legal_hold is False


def test_the_janitor_expires_an_unheld_artifact_and_leaves_a_held_one(
    db_session, test_user, account, runtime_session
):
    """Expiry clears unheld ciphertext and does not touch a held artifact."""
    past = datetime.now(UTC) - timedelta(hours=2)
    held = crud_session_artifact.store(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-held",
        content_type="image/png",
        plaintext=b"held-screenshot-bytes",
        manifest={"step_index": 1},
        expires_at=past,
        commit=False,
    )
    other = models.RuntimeSession(
        account_id=account.id,
        session_source_type="managed_agent",
        session_source_id=f"agent-{uuid.uuid4().hex[:8]}",
        started_at=past,
        last_activity_at=past,
        ended_at=past,
    )
    db_session.add(other)
    db_session.flush()
    unheld = crud_session_artifact.store(
        db_session,
        account_id=account.id,
        runtime_session_id=other.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-open",
        content_type="image/png",
        plaintext=b"open-screenshot-bytes",
        manifest={"step_index": 1},
        expires_at=past,
        commit=False,
    )
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="regulator request 2026-08",
        user_id=test_user.id,
    )

    cleared = crud_session_artifact.cleanup(db_session, now=datetime.now(UTC))

    db_session.expire_all()
    held_row = db_session.get(models.RuntimeSessionArtifact, held.id)
    unheld_row = db_session.get(models.RuntimeSessionArtifact, unheld.id)
    assert held_row.ciphertext is not None
    assert held_row.availability == "available"
    assert unheld_row.ciphertext is None
    assert unheld_row.availability == "expired"
    assert cleared == 1


def test_an_artifact_stored_on_a_held_session_survives_the_janitor(
    db_session, test_user, account, runtime_session
):
    """A hold already in force covers artifacts written afterwards.

    ``store`` copies the session flag. The janitor also skips the row when
    that copy is missing and the session itself is still held.
    """
    past = datetime.now(UTC) - timedelta(hours=2)
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="incident review still open",
        user_id=test_user.id,
    )
    stored = crud_session_artifact.store(
        db_session,
        account_id=account.id,
        runtime_session_id=runtime_session.id,
        kind="screenshot",
        source="browser_use",
        source_ref="step-after-hold",
        content_type="image/png",
        plaintext=b"held-after-store-bytes",
        manifest={"step_index": 2},
        expires_at=past,
        commit=False,
    )
    db_session.commit()
    db_session.refresh(stored)
    assert stored.legal_hold is True

    crud_session_artifact.cleanup(db_session, now=datetime.now(UTC))
    db_session.expire_all()
    row = db_session.get(models.RuntimeSessionArtifact, stored.id)
    assert row.ciphertext is not None
    assert row.availability == "available"

    row.legal_hold = False
    db_session.add(row)
    db_session.commit()
    crud_session_artifact.cleanup(db_session, now=datetime.now(UTC))
    db_session.expire_all()
    row = db_session.get(models.RuntimeSessionArtifact, stored.id)
    assert row.ciphertext is not None
    assert row.availability == "available"


def test_a_hold_on_another_accounts_session_is_refused(
    db_session, account, runtime_session
):
    with pytest.raises(LegalHoldError) as excinfo:
        place_hold(
            db_session,
            account_id=uuid.uuid4(),
            resource_type="runtime_session",
            resource_id=str(runtime_session.id),
            reason="fishing for someone else's sessions",
        )

    assert excinfo.value.code == "resource_not_found"


def test_releasing_a_session_hold_clears_the_flag(
    db_session, test_user, account, runtime_session
):
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="matter closed 2026-08",
        user_id=test_user.id,
    )

    db_session.refresh(runtime_session)
    assert runtime_session.legal_hold is False


def test_placing_and_releasing_a_session_hold_are_both_audited(
    db_session, test_user, account, runtime_session
):
    """A session hold is audited exactly like the other hold actions."""
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(runtime_session.id),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )
    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="matter closed 2026-08",
        user_id=test_user.id,
    )

    rows = (
        db_session.execute(
            select(AuditLog)
            .where(
                AuditLog.account_id == account.id,
                AuditLog.resource_type == "runtime_session",
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
    assert all(row.resource_id == str(runtime_session.id) for row in rows)
    assert rows[0].details["reason"] == "litigation hold, matter 2026-07"
    assert rows[0].details["flagged"]["runtime_session"] == 1
    assert rows[1].details["placed_reason"] == "litigation hold, matter 2026-07"


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


# --- account lock ----------------------------------------------------------


def test_hold_writes_take_the_purge_account_lock(
    db_engine: Engine, committed_runtime_session: tuple[uuid.UUID, uuid.UUID]
):
    """Hold writes wait on the same account FOR UPDATE the purge uses.

    A concurrent purge then skip_locked-skips that account for the batch
    instead of deleting a row the hold has already selected against.
    """
    account_id, session_id = committed_runtime_session
    with Session(db_engine) as holder, Session(db_engine) as purger:
        place_hold(
            holder,
            account_id=account_id,
            resource_type="runtime_session",
            resource_id=str(session_id),
            reason="litigation hold, matter 2026-07",
            commit=False,
        )
        skipped = lock_account_for_retention(purger, account_id=account_id)
        assert skipped is None
        with Session(db_engine) as competitor:
            competitor.execute(text("SET LOCAL lock_timeout = '250ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                place_hold(
                    competitor,
                    account_id=account_id,
                    resource_type="runtime_session",
                    resource_id=str(session_id),
                    reason="second hold on the same account",
                )
            competitor.rollback()
        holder.rollback()
        locked = lock_account_for_retention(purger, account_id=account_id)
        assert locked is not None
        assert locked.id == account_id
