"""The purge: bounds, holds, audit rows and the off-peak window."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.services import audit_chain
from preloop.services import retention_purge as purge
from preloop.services.legal_hold import place_hold
from preloop.services.retention_policy import CLASS_AUDIT


@pytest.fixture(autouse=True)
def enabled_purge(monkeypatch):
    """Tests exercise the job itself; the deployment default is off."""
    monkeypatch.setattr(settings, "retention_purge_enabled", True, raising=False)
    monkeypatch.setattr(settings, "retention_purge_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "retention_purge_window_utc", "", raising=False)


def _exists(db_session, model, identifier) -> bool:
    """Row presence read fresh.

    ``Session.get`` would answer from the identity map and raise
    ObjectDeletedError for a row the purge removed under it, which says the
    same thing far less clearly.
    """
    return (
        db_session.execute(
            select(model.id).where(model.id == identifier)
        ).scalar_one_or_none()
        is not None
    )


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _audit_row(db_session, account_id, *, age_days: int, action="permission_check"):
    row = AuditLog(
        account_id=account_id,
        action=action,
        resource_type="tool",
        resource_id="x",
        status="success",
        timestamp=datetime.now(UTC) - timedelta(days=age_days),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _approval(db_session, test_user, *, age_days: int, status: str = "approved"):
    workflow = models.ApprovalWorkflow(
        account_id=test_user.account_id,
        name=f"wf-{uuid.uuid4().hex[:8]}",
        approval_type="manual",
        channel="email",
    )
    db_session.add(workflow)
    db_session.flush()
    config = models.ToolConfiguration(
        account_id=test_user.account_id,
        tool_name="send_payment",
        tool_source="mcp",
        approval_workflow_id=workflow.id,
        is_enabled=True,
        custom_config={},
    )
    db_session.add(config)
    db_session.flush()
    request = models.ApprovalRequest(
        account_id=test_user.account_id,
        tool_configuration_id=config.id,
        approval_workflow_id=workflow.id,
        tool_name="send_payment",
        tool_args={"amount": 1},
        status=status,
        requested_at=datetime.utcnow() - timedelta(days=age_days),
    )
    db_session.add(request)
    db_session.flush()
    return request


def _evidence(db_session, test_user, *, age_days: int):
    flow = models.Flow(
        name=f"flow-{age_days}",
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
    created = datetime.now(UTC) - timedelta(days=age_days)
    artifact = models.FlowArtifact(
        account_id=test_user.account_id,
        flow_id=flow.id,
        execution_id=execution.id,
        thread_id="t",
        kind="evidence",
        manifest={"sha256": "a" * 64, "size_bytes": 10},
        manifest_sha256="b" * 64,
        ciphertext=b"cipher",
        expires_at=created + timedelta(hours=1),
        created_at=created,
        availability="available",
    )
    db_session.add(artifact)
    db_session.flush()
    return execution, artifact


# --- what gets removed -----------------------------------------------------


def test_rows_inside_retention_are_left_alone(db_session, account):
    keep = _audit_row(db_session, account.id, age_days=10)
    db_session.commit()

    result = purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )

    assert result.deleted == 0
    assert _exists(db_session, AuditLog, keep.id) is True


def test_rows_past_retention_are_removed(db_session, account):
    old = _audit_row(db_session, account.id, age_days=400).id
    recent = _audit_row(db_session, account.id, age_days=5).id
    db_session.commit()

    result = purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )

    assert result.classes[CLASS_AUDIT] >= 1
    assert _exists(db_session, AuditLog, old) is False
    assert _exists(db_session, AuditLog, recent) is True


def test_a_shorter_account_retention_removes_more(db_session, account):
    row = _audit_row(db_session, account.id, age_days=200).id
    account.meta_data = {"retention": {CLASS_AUDIT: 183}}
    db_session.add(account)
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, AuditLog, row) is False


# --- holds -----------------------------------------------------------------


def test_a_held_approval_survives_the_purge(db_session, test_user, account):
    held = _approval(db_session, test_user, age_days=400).id
    unheld = _approval(db_session, test_user, age_days=400).id
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="approval",
        resource_id=str(held),
        reason="litigation hold, matter 2026-04",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.ApprovalRequest, held) is True
    assert _exists(db_session, models.ApprovalRequest, unheld) is False


def test_a_held_evidence_pack_survives_the_purge(db_session, test_user, account):
    held = _evidence(db_session, test_user, age_days=500)[1].id
    unheld = _evidence(db_session, test_user, age_days=500)[1].id
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="evidence_pack",
        resource_id=str(held),
        reason="regulator request 2026-05",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.FlowArtifact, held) is True
    assert _exists(db_session, models.FlowArtifact, unheld) is False


def test_an_execution_hold_covers_that_executions_packs(db_session, test_user, account):
    execution, artifact = _evidence(db_session, test_user, age_days=500)
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="incident review INC-114",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.FlowArtifact, artifact.id) is True


def test_a_released_hold_stops_protecting_the_row(db_session, test_user, account):
    approval = _approval(db_session, test_user, age_days=400).id
    db_session.commit()
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="approval",
        resource_id=str(approval),
        reason="litigation hold, matter 2026-04",
        user_id=test_user.id,
    )
    from preloop.services.legal_hold import release_hold

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="matter closed 2026-06",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.ApprovalRequest, approval) is False


# --- what is never purged --------------------------------------------------


def test_a_pending_approval_is_never_purged(db_session, test_user, account):
    """A parked execution is waiting on that row."""
    pending = _approval(db_session, test_user, age_days=900, status="pending")
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.ApprovalRequest, pending.id) is True


def test_a_purged_pack_leaves_the_receipt_explained(db_session, test_user, account):
    execution, artifact = _evidence(db_session, test_user, age_days=500)
    execution.evidence_receipt = {
        "status": "available",
        "artifact_id": str(artifact.id),
        "kind": "evidence",
    }
    db_session.add(execution)
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)
    db_session.expire(execution)

    receipt = db_session.get(models.FlowExecution, execution.id).evidence_receipt
    assert receipt["status"] == "expired"
    assert receipt["error"] == "retention_purged"


# --- bounds ----------------------------------------------------------------


def test_the_pass_stops_at_the_batch_ceiling_and_says_so(
    db_session, account, monkeypatch
):
    for _ in range(5):
        _audit_row(db_session, account.id, age_days=400)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_batch_size", 1, raising=False)
    monkeypatch.setattr(settings, "retention_purge_max_batches", 2, raising=False)

    result = purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )

    assert result.deleted == 2
    assert result.budget_exhausted is True
    remaining = (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == "permission_check",
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining) == 3


def test_the_purge_is_off_unless_the_deployment_enables_it(
    db_session, account, monkeypatch
):
    row = _audit_row(db_session, account.id, age_days=900)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_enabled", False, raising=False)

    result = purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )

    assert result.skipped_reason == "disabled"
    assert _exists(db_session, AuditLog, row.id) is True


def test_a_dry_run_counts_without_deleting(db_session, account, monkeypatch):
    row = _audit_row(db_session, account.id, age_days=900)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", True, raising=False)

    result = purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )

    assert result.deleted >= 1
    assert _exists(db_session, AuditLog, row.id) is True


def test_another_accounts_rows_are_never_touched(db_session, account, test_user):
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    theirs = _audit_row(db_session, other.id, age_days=900)
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, AuditLog, theirs.id) is True


# --- the off-peak window ---------------------------------------------------


def test_outside_the_window_the_pass_does_nothing(db_session, account, monkeypatch):
    row = _audit_row(db_session, account.id, age_days=900)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_window_utc", "1-5", raising=False)

    result = purge.run_retention_purge(
        db_session,
        account_ids=[account.id],
        now=datetime(2026, 5, 5, 14, 0, tzinfo=UTC),
    )

    assert result.skipped_reason == "outside_window"
    assert _exists(db_session, AuditLog, row.id) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1-5", (1, 5)),
        ("22-3", (22, 3)),
        ("", None),
        (None, None),
        ("nonsense", None),
        ("1-99", None),
    ],
)
def test_window_parsing(raw, expected):
    assert purge.parse_window(raw) == expected


@pytest.mark.parametrize(
    "hour,window,inside",
    [
        (3, (1, 5), True),
        (14, (1, 5), False),
        (23, (22, 3), True),
        (2, (22, 3), True),
        (12, (22, 3), False),
        (12, None, True),
    ],
)
def test_window_membership_handles_midnight(hour, window, inside):
    now = datetime(2026, 5, 5, hour, 30, tzinfo=UTC)
    assert purge.in_window(now, window) is inside


# --- audit -----------------------------------------------------------------


def test_the_purge_writes_an_audit_row_with_the_count(db_session, account):
    for _ in range(3):
        _audit_row(db_session, account.id, age_days=400)
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    rows = (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == purge.AUDIT_ACTION_PURGE,
            )
        )
        .scalars()
        .all()
    )
    audit_class = [row for row in rows if row.resource_id == CLASS_AUDIT]
    assert len(audit_class) == 1
    details = audit_class[0].details
    assert details["deleted"] == 3
    assert details["retention_days"] == 365
    assert details["cutoff"]


def test_the_audit_row_the_purge_writes_is_not_purged_by_the_same_pass(
    db_session, account
):
    """The record of a deletion must outlive the deletion it records."""
    _audit_row(db_session, account.id, age_days=400)
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)
    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    rows = (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == purge.AUDIT_ACTION_PURGE,
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


def test_a_dry_run_audits_under_its_own_action(db_session, account, monkeypatch):
    _audit_row(db_session, account.id, age_days=400)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", True, raising=False)

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    rows = (
        db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == purge.AUDIT_ACTION_PREVIEW,
            )
        )
        .scalars()
        .all()
    )
    assert rows


# --- the chain floor -------------------------------------------------------


def test_purging_audit_rows_raises_the_chain_floor(db_session, test_user, account):
    """A retention purge must not read as tampering (#558)."""
    old = [_audit_row(db_session, account.id, age_days=500) for _ in range(3)]
    recent = _audit_row(db_session, account.id, age_days=1)
    audit_chain.seal_account(
        db_session,
        account_id=account.id,
        now=datetime.now(UTC) + timedelta(hours=1),
        lag=timedelta(0),
    )
    db_session.commit()
    highest_purged = max(row.chain_seq for row in old)
    assert recent.chain_seq > highest_purged

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    state = audit_chain.get_state(db_session, account_id=account.id, create=False)
    assert state.pruned_below_seq >= highest_purged
    report = audit_chain.verify_chain(db_session, account_id=account.id)
    assert report["status"] == "ok"
    assert report["start_seq"] > highest_purged


def test_a_purge_that_removes_nothing_leaves_the_floor_where_it_was(
    db_session, test_user, account
):
    _audit_row(db_session, account.id, age_days=1)
    audit_chain.seal_account(
        db_session,
        account_id=account.id,
        now=datetime.now(UTC) + timedelta(hours=1),
        lag=timedelta(0),
    )
    db_session.commit()

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    state = audit_chain.get_state(db_session, account_id=account.id, create=False)
    assert int(state.pruned_below_seq) == 0
