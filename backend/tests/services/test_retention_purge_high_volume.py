"""The high-volume sweeps: flow execution logs and account events (EE #46)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from preloop.config import settings
from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.services import retention_purge as purge
from preloop.services.legal_hold import place_hold
from preloop.services.retention_policy import CLASS_EVIDENCE, CLASS_USAGE


@pytest.fixture(autouse=True)
def enabled_purge(monkeypatch):
    """Both switches on; each test turns off what it is about."""
    monkeypatch.setattr(settings, "retention_purge_enabled", True, raising=False)
    monkeypatch.setattr(
        settings, "retention_purge_high_volume_enabled", True, raising=False
    )
    monkeypatch.setattr(settings, "retention_purge_dry_run", False, raising=False)
    monkeypatch.setattr(settings, "retention_purge_window_utc", "", raising=False)


@pytest.fixture
def account(db_session, test_user):
    return db_session.get(models.Account, test_user.account_id)


def _execution(db_session, account_id, *, age_days: int, logs: int = 3):
    flow = models.Flow(
        name=f"flow-{uuid.uuid4().hex[:8]}",
        prompt_template="t",
        agent_type="codex",
        agent_config={},
        account_id=account_id,
    )
    db_session.add(flow)
    db_session.flush()
    stamp = datetime.now(UTC) - timedelta(days=age_days)
    execution = models.FlowExecution(
        flow_id=flow.id,
        status="COMPLETED",
        created_at=stamp.replace(tzinfo=None),
    )
    db_session.add(execution)
    db_session.flush()
    for index in range(logs):
        db_session.add(
            models.FlowExecutionLog(
                execution_id=execution.id,
                timestamp=stamp,
                log_type="log",
                message=f"line {index}",
            )
        )
    db_session.flush()
    return execution


def _log_count(db_session, execution_id) -> int:
    return int(
        db_session.execute(
            select(func.count())
            .select_from(models.FlowExecutionLog)
            .where(models.FlowExecutionLog.execution_id == execution_id)
        ).scalar_one()
    )


def _event(db_session, account_id, *, age_days: int):
    row = models.Event(
        account_id=account_id,
        event_type="page_view",
        timestamp=datetime.now(UTC) - timedelta(days=age_days),
    )
    db_session.add(row)
    db_session.flush()
    return row.id


def _event_exists(db_session, event_id) -> bool:
    return (
        db_session.execute(
            select(models.Event.id).where(models.Event.id == event_id)
        ).scalar_one_or_none()
        is not None
    )


def _run(db_session, account):
    return purge.run_retention_purge(
        db_session, account_ids=[account.id], ignore_window=True
    )


def test_old_execution_logs_go_and_the_execution_summary_stays(db_session, account):
    old = _execution(db_session, account.id, age_days=400)
    recent = _execution(db_session, account.id, age_days=10)
    db_session.commit()

    result = _run(db_session, account)

    assert _log_count(db_session, old.id) == 0
    assert _log_count(db_session, recent.id) == 3
    assert db_session.get(models.FlowExecution, old.id) is not None
    assert result.classes[purge.SWEEP_EXECUTION_LOGS] == 3


def test_execution_logs_follow_the_accounts_evidence_retention(db_session, account):
    execution = _execution(db_session, account.id, age_days=250)
    account.meta_data = {"retention": {CLASS_EVIDENCE: 200}}
    db_session.add(account)
    db_session.commit()

    _run(db_session, account)

    assert _log_count(db_session, execution.id) == 0


def test_a_held_execution_keeps_every_log_line(db_session, account):
    execution = _execution(db_session, account.id, age_days=400)
    db_session.commit()
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="litigation hold for the test",
    )
    assert outcome is not None
    db_session.commit()

    _run(db_session, account)

    assert _log_count(db_session, execution.id) == 3


def test_old_events_go_and_recent_ones_stay(db_session, account):
    old = _event(db_session, account.id, age_days=400)
    recent = _event(db_session, account.id, age_days=10)
    db_session.commit()

    result = _run(db_session, account)

    assert _event_exists(db_session, old) is False
    assert _event_exists(db_session, recent) is True
    assert result.classes[purge.SWEEP_EVENTS] == 1


def test_events_follow_the_accounts_usage_retention(db_session, account):
    row = _event(db_session, account.id, age_days=250)
    account.meta_data = {"retention": {CLASS_USAGE: 200}}
    db_session.add(account)
    db_session.commit()

    _run(db_session, account)

    assert _event_exists(db_session, row) is False


def test_events_never_go_below_the_floor(db_session, account):
    row = _event(db_session, account.id, age_days=150)
    account.meta_data = {"retention": {CLASS_USAGE: 30}}
    db_session.add(account)
    db_session.commit()

    _run(db_session, account)

    assert _event_exists(db_session, row) is True


def test_another_accounts_logs_and_events_are_untouched(db_session, account, test_user):
    from preloop.models.crud import crud_account

    other = crud_account.create(
        db_session, obj_in={"organization_name": "Other Org", "is_active": True}
    )
    execution = _execution(db_session, other.id, age_days=400)
    event = _event(db_session, other.id, age_days=400)
    anonymous = _event(db_session, None, age_days=400)
    db_session.commit()

    _run(db_session, account)

    assert _log_count(db_session, execution.id) == 3
    assert _event_exists(db_session, event) is True
    assert _event_exists(db_session, anonymous) is True


def test_the_sweeps_are_off_unless_separately_enabled(db_session, account, monkeypatch):
    """Turning the purge on before this release must not start these deletes."""
    monkeypatch.setattr(
        settings, "retention_purge_high_volume_enabled", False, raising=False
    )
    execution = _execution(db_session, account.id, age_days=400)
    event = _event(db_session, account.id, age_days=400)
    db_session.commit()

    result = _run(db_session, account)

    assert _log_count(db_session, execution.id) == 3
    assert _event_exists(db_session, event) is True
    assert purge.SWEEP_EVENTS not in result.classes
    assert purge.SWEEP_EXECUTION_LOGS not in result.classes


def test_the_high_volume_switch_alone_does_not_purge(db_session, account, monkeypatch):
    monkeypatch.setattr(settings, "retention_purge_enabled", False, raising=False)
    event = _event(db_session, account.id, age_days=400)
    db_session.commit()

    result = _run(db_session, account)

    assert result.skipped_reason == "disabled"
    assert _event_exists(db_session, event) is True


def test_a_dry_run_counts_and_audits_without_deleting(db_session, account, monkeypatch):
    monkeypatch.setattr(settings, "retention_purge_dry_run", True, raising=False)
    execution = _execution(db_session, account.id, age_days=400)
    event = _event(db_session, account.id, age_days=400)
    db_session.commit()

    result = _run(db_session, account)

    assert result.classes[purge.SWEEP_EXECUTION_LOGS] == 3
    assert result.classes[purge.SWEEP_EVENTS] == 1
    assert _log_count(db_session, execution.id) == 3
    assert _event_exists(db_session, event) is True
    previews = {
        row.resource_id
        for row in db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == purge.AUDIT_ACTION_PREVIEW,
            )
        ).scalars()
    }
    assert {purge.SWEEP_EVENTS, purge.SWEEP_EXECUTION_LOGS} <= previews


def test_each_sweep_writes_a_purge_audit_row(db_session, account):
    _execution(db_session, account.id, age_days=400)
    _event(db_session, account.id, age_days=400)
    db_session.commit()

    _run(db_session, account)

    rows = {
        row.resource_id: row.details
        for row in db_session.execute(
            select(AuditLog).where(
                AuditLog.account_id == account.id,
                AuditLog.action == purge.AUDIT_ACTION_PURGE,
            )
        ).scalars()
    }
    assert rows[purge.SWEEP_EXECUTION_LOGS]["deleted"] == 3
    assert rows[purge.SWEEP_EXECUTION_LOGS]["governed_by"] == CLASS_EVIDENCE
    assert rows[purge.SWEEP_EVENTS]["deleted"] == 1
    assert rows[purge.SWEEP_EVENTS]["governed_by"] == CLASS_USAGE


def test_the_log_sweep_is_bounded_by_the_batch_ceiling(
    db_session, account, monkeypatch
):
    execution = _execution(db_session, account.id, age_days=400, logs=5)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_batch_size", 1, raising=False)
    monkeypatch.setattr(settings, "retention_purge_max_batches", 2, raising=False)

    result = _run(db_session, account)

    assert _log_count(db_session, execution.id) == 3
    assert result.budget_exhausted is True


def test_every_sweep_states_its_hold_coverage():
    """Logs carry the execution hold predicate; events say why they have none."""
    cutoff = datetime.now(UTC)
    rendered = " ".join(
        str(item).lower()
        for item in purge.class_filters(purge.SWEEP_EXECUTION_LOGS, cutoff)
    )
    assert "flow_execution.legal_hold is false" in rendered
    assert purge.SWEEP_EVENTS in purge.HOLD_EXEMPT_CLASSES
    assert purge.SWEEP_EXECUTION_LOGS not in purge.HOLD_EXEMPT_CLASSES


def test_a_purged_log_line_takes_its_search_chunk_with_it(
    db_session, account, monkeypatch
):
    from preloop.models.models.session_search_document import SOURCE_KIND_FLOW_LOG
    from preloop.services import session_search_retention as corpus

    execution = _execution(db_session, account.id, age_days=400, logs=1)
    db_session.commit()
    line = db_session.execute(
        select(models.FlowExecutionLog.id).where(
            models.FlowExecutionLog.execution_id == execution.id
        )
    ).scalar_one()

    calls = []
    original = corpus.crud_session_search_document.delete_for_sources

    def spy(db, **kwargs):
        calls.append(kwargs)
        return original(db, **kwargs)

    monkeypatch.setattr(corpus.crud_session_search_document, "delete_for_sources", spy)
    _run(db_session, account)

    flow_calls = [c for c in calls if c["source_kind"] == SOURCE_KIND_FLOW_LOG]
    assert flow_calls and line in flow_calls[0]["source_ids"]
    assert flow_calls[0]["excluding_held_sessions"] is True
