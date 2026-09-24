"""The purge: bounds, holds, audit rows and the off-peak window."""

import inspect
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from preloop.config import settings
from preloop.models import models
from preloop.models.crud import runtime_session_artifact as crud_session_artifact
from preloop.models.models.audit_log import AuditLog
from preloop.models.models.base import Base
from preloop.services import audit_chain
from preloop.services import retention_purge as purge
from preloop.services import session_search_retention
from preloop.services.legal_hold import place_hold
from preloop.services.retention_policy import (
    CLASS_AUDIT,
    CLASS_RUNTIME_SESSIONS,
    RECORD_CLASSES,
)


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


def _runtime_session(db_session, test_user, *, age_days: int, activities: int = 2):
    """One ended session with activity rows, all dated ``age_days`` ago."""
    stamp = datetime.now(UTC) - timedelta(days=age_days)
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
    for index in range(activities):
        db_session.add(
            models.RuntimeSessionActivity(
                account_id=test_user.account_id,
                runtime_session_id=session.id,
                activity_type="tool_call",
                tool_name=f"tool-{index}",
                status="success",
                timestamp=stamp,
            )
        )
    db_session.flush()
    return session


def _session_count(db_session, account_id) -> int:
    return len(
        db_session.execute(
            select(models.RuntimeSession.id).where(
                models.RuntimeSession.account_id == account_id
            )
        )
        .scalars()
        .all()
    )


def _activity_count(db_session, session_id) -> int:
    return len(
        db_session.execute(
            select(models.RuntimeSessionActivity.id).where(
                models.RuntimeSessionActivity.runtime_session_id == session_id
            )
        )
        .scalars()
        .all()
    )


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


def test_a_held_execution_keeps_its_legacy_evidence_archive(
    db_session, test_user, account
):
    """The legacy-column drop is not a RECORD_CLASS, but a hold still stops it."""
    held, _held_pack = _evidence(db_session, test_user, age_days=500)
    unheld, _unheld_pack = _evidence(db_session, test_user, age_days=500)
    aged = datetime.now(UTC) - timedelta(days=500)
    held.created_at = aged
    unheld.created_at = aged
    held.evidence_archive = b"held-archive"
    unheld.evidence_archive = b"unheld-archive"
    db_session.add_all([held, unheld])
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(held.id),
        reason="incident review INC-114",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)
    db_session.expire_all()

    held_archive = db_session.get(models.FlowExecution, held.id).evidence_archive
    unheld_archive = db_session.get(models.FlowExecution, unheld.id).evidence_archive
    assert bytes(held_archive or b"") == b"held-archive"
    assert unheld_archive is None


def test_a_held_runtime_session_and_its_activity_survive_the_purge(
    db_session, test_user, account
):
    """A session told to be preserved keeps its activity rows too (#650)."""
    held = _runtime_session(db_session, test_user, age_days=500, activities=3).id
    unheld = _runtime_session(db_session, test_user, age_days=500, activities=2).id
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(held),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )
    before_sessions = _session_count(db_session, account.id)
    before_activity = _activity_count(db_session, held)
    assert (before_sessions, before_activity) == (2, 3)

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _session_count(db_session, account.id) == 1
    assert _exists(db_session, models.RuntimeSession, held) is True
    assert _exists(db_session, models.RuntimeSession, unheld) is False
    assert _activity_count(db_session, held) == before_activity
    assert _activity_count(db_session, unheld) == 0


def _session_artifact(
    db_session,
    test_user,
    session,
    *,
    source_ref: str,
    expires_at: datetime | None = None,
):
    """One screenshot on ``session``. ``session`` may be a row or an id."""
    session_id = session.id if hasattr(session, "id") else session
    return crud_session_artifact.store(
        db_session,
        account_id=test_user.account_id,
        runtime_session_id=session_id,
        kind="screenshot",
        source="browser_use",
        source_ref=source_ref,
        content_type="image/png",
        plaintext=b"unpublished-screenshot-bytes",
        manifest={"step_index": 1},
        expires_at=expires_at,
        commit=False,
    )


def _artifact_rows(db_session, session_id) -> list:
    return list(
        db_session.execute(
            select(models.RuntimeSessionArtifact).where(
                models.RuntimeSessionArtifact.runtime_session_id == session_id
            )
        )
        .scalars()
        .all()
    )


def test_an_unheld_session_purge_names_and_removes_its_artifacts(
    db_session, test_user, account
):
    """Cascade takes the artifacts, and the class result says how many."""
    session = _runtime_session(db_session, test_user, age_days=500, activities=1)
    session_id = session.id
    _session_artifact(db_session, test_user, session, source_ref="step-1")
    _session_artifact(db_session, test_user, session, source_ref="step-2")
    db_session.commit()

    result = purge.purge_class(
        db_session,
        account=account,
        record_class=CLASS_RUNTIME_SESSIONS,
        now=datetime.now(UTC),
        batch_size=100,
        max_batches=5,
        dry_run=False,
    )

    assert result.deleted == 1
    assert result.as_details()["runtime_session_artifact"] == 2
    assert _exists(db_session, models.RuntimeSession, session_id) is False
    assert _artifact_rows(db_session, session_id) == []
    assert session_search_retention.orphan_session_artifact_count(db_session) == 0
    session_search_retention.assert_no_orphan_chunks(
        db_session, context="a session purge that cascades artifacts"
    )


def test_a_held_session_keeps_artifact_bytes_past_expiry(
    db_session, test_user, account
):
    """A hold blocks the purge and the janitor, including past expires_at."""
    session = _runtime_session(db_session, test_user, age_days=500, activities=1)
    past = datetime.now(UTC) - timedelta(hours=2)
    artifact = _session_artifact(
        db_session,
        test_user,
        session,
        source_ref="step-held",
        expires_at=past,
    )
    db_session.commit()
    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(session.id),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )

    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)
    crud_session_artifact.cleanup(db_session, now=datetime.now(UTC))
    db_session.expire_all()

    assert _exists(db_session, models.RuntimeSession, session.id) is True
    row = db_session.get(models.RuntimeSessionArtifact, artifact.id)
    assert row is not None
    assert row.legal_hold is True
    assert row.ciphertext is not None
    assert row.availability == "available"


def test_releasing_a_session_hold_makes_it_purgeable_again(
    db_session, test_user, account
):
    session = _runtime_session(db_session, test_user, age_days=500, activities=2).id
    db_session.commit()
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(session),
        reason="litigation hold, matter 2026-07",
        user_id=test_user.id,
    )
    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)
    assert _exists(db_session, models.RuntimeSession, session) is True

    from preloop.services.legal_hold import release_hold

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="matter closed 2026-08",
        user_id=test_user.id,
    )
    purge.run_retention_purge(db_session, account_ids=[account.id], ignore_window=True)

    assert _exists(db_session, models.RuntimeSession, session) is False
    assert _activity_count(db_session, session) == 0


def test_a_held_session_is_not_counted_as_purgeable(db_session, test_user, account):
    """The dry-run preview must not promise to delete a frozen session."""
    session = _runtime_session(db_session, test_user, age_days=500, activities=1).id
    db_session.commit()
    cutoff = datetime.now(UTC) - timedelta(days=365)
    assert (
        purge.count_purgeable(
            db_session,
            account_id=account.id,
            record_class=CLASS_RUNTIME_SESSIONS,
            cutoff=cutoff,
        )
        == 1
    )

    place_hold(
        db_session,
        account_id=account.id,
        resource_type="runtime_session",
        resource_id=str(session),
        reason="regulator request 2026-08",
        user_id=test_user.id,
    )

    assert (
        purge.count_purgeable(
            db_session,
            account_id=account.id,
            record_class=CLASS_RUNTIME_SESSIONS,
            cutoff=cutoff,
        )
        == 0
    )


# --- the invariant ---------------------------------------------------------


def test_every_purgeable_class_carries_a_hold_predicate():
    """No class may delete by cutoff alone while its model can be held.

    This is the test that would have caught #650: runtime sessions were
    purgeable with no hold check because nothing compared the classes.
    """
    cutoff = datetime.now(UTC)
    for record_class in RECORD_CLASSES:
        model = purge._CLASS_MODELS[record_class]
        rendered = [
            str(item).lower()
            for item in purge.class_filters(cutoff=cutoff, record_class=record_class)
        ]
        if record_class in purge.HOLD_EXEMPT_CLASSES:
            assert not hasattr(model, "legal_hold")
            continue
        assert any(
            f"{model.__tablename__}.legal_hold is false" in text for text in rendered
        ), f"{record_class} is purged without a legal hold predicate"
    for model in purge.HOLD_SIDE_MODELS:
        rendered = [str(item).lower() for item in purge._hold_filters_for_model(model)]
        assert any(
            f"{model.__tablename__}.legal_hold is false" in text for text in rendered
        ), f"{model.__name__} is written by the purge without a legal hold predicate"
    covered = set(purge._CLASS_MODELS.values()) | set(purge.HOLD_SIDE_MODELS)
    for model in Base.registry.mappers:
        cls = model.class_
        if "legal_hold" not in cls.__table__.columns:
            continue
        assert cls in covered, (
            f"{cls.__name__} has a legal_hold column and is not a purge class "
            "or a hold side model"
        )
    assert models.RuntimeSessionArtifact in purge.HOLD_SIDE_MODELS


def test_legacy_evidence_drop_routes_through_the_hold_helper():
    """The FlowExecution UPDATE must unpack the helper, not repeat the flag."""
    source = inspect.getsource(purge._drop_legacy_evidence_columns)
    assert "_hold_filters_for_model(FlowExecution)" in source
    assert "legal_hold.is_(False)" not in source


def test_hold_filters_always_returns_a_list_of_clauses():
    """Exempt and held classes both yield a sequence, never mixed tuple lengths."""
    for record_class in RECORD_CLASSES:
        clauses = purge._hold_filters(record_class)
        assert isinstance(clauses, list)
        if record_class in purge.HOLD_EXEMPT_CLASSES:
            assert clauses == []
        else:
            assert len(clauses) == 1
    for model in purge.HOLD_SIDE_MODELS:
        clauses = purge._hold_filters_for_model(model)
        assert isinstance(clauses, list)
        assert len(clauses) == 1


def test_a_class_purged_without_a_hold_check_has_to_say_why():
    """An exemption is a written decision, not a forgotten predicate."""
    assert set(purge.HOLD_EXEMPT_CLASSES) <= set(RECORD_CLASSES)
    for record_class, reason in purge.HOLD_EXEMPT_CLASSES.items():
        assert len(reason.strip()) >= 20
        assert not hasattr(purge._CLASS_MODELS[record_class], "legal_hold")


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
