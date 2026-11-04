"""The high-volume sweeps: flow execution logs and account events (EE #46)."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.sql import visitors
from sqlalchemy.sql.selectable import Select

from preloop.config import settings
from preloop.models import models
from preloop.models.models.audit_log import AuditLog
from preloop.services import retention_purge as purge
from preloop.services.legal_hold import place_hold, release_hold
from preloop.services.retention_policy import CLASS_AUDIT, CLASS_EVIDENCE, CLASS_USAGE


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


def _event(
    db_session: Session,
    account_id: uuid.UUID | None,
    *,
    age_days: int,
    event_data: dict[str, Any] | None = None,
) -> uuid.UUID:
    row = models.Event(
        account_id=account_id,
        event_type="page_view",
        timestamp=datetime.now(UTC) - timedelta(days=age_days),
        event_data=event_data,
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


@pytest.mark.parametrize("dry_run", [False, True])
def test_a_newer_line_of_an_old_execution_survives(
    db_session: Session,
    account: models.Account,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    """The line cutoff applies independently of its execution's age."""
    execution = _execution(db_session, account.id, age_days=400, logs=0)
    old, recent = [
        models.FlowExecutionLog(
            execution_id=execution.id,
            timestamp=datetime.now(UTC) - timedelta(days=age),
            log_type="log",
            message=message,
        )
        for age, message in [(400, "old line"), (10, "recent line")]
    ]
    db_session.add_all([old, recent])
    db_session.flush()
    old_id, recent_id = old.id, recent.id
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", dry_run)

    result = _run(db_session, account)

    remaining = set(
        db_session.scalars(
            select(models.FlowExecutionLog.id).where(
                models.FlowExecutionLog.execution_id == execution.id
            )
        )
    )
    assert result.classes[purge.SWEEP_EXECUTION_LOGS] == 1
    assert remaining == ({old_id, recent_id} if dry_run else {recent_id})


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


@pytest.mark.parametrize("dry_run", [False, True])
def test_events_honor_the_longer_storage_promise(
    db_session: Session,
    account: models.Account,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    account.meta_data = {"retention": {CLASS_USAGE: 200}}
    account.subscription_history_retention_days = 730
    inside = _event(db_session, account.id, age_days=400)
    outside = _event(db_session, account.id, age_days=800)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", dry_run)

    result = _run(db_session, account)

    assert result.classes[purge.SWEEP_EVENTS] == 1
    assert _event_exists(db_session, inside)
    assert _event_exists(db_session, outside) is dry_run


@pytest.mark.parametrize("dry_run", [False, True])
def test_events_honor_unlimited_storage(
    db_session: Session,
    account: models.Account,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    account.meta_data = {"retention": {CLASS_USAGE: 200}}
    account.subscription_history_retention_days = -1
    old = _event(db_session, account.id, age_days=800)
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", dry_run)

    result = _run(db_session, account)

    assert result.classes[purge.SWEEP_EVENTS] == 0
    assert _event_exists(db_session, old)


def test_every_execution_log_subquery_is_account_scoped() -> None:
    """Neither the ownership nor eligibility lookup may scan other tenants."""
    account_id = uuid.uuid4()
    stmt = purge._candidate_ids_stmt(
        purge.SWEEP_EXECUTION_LOGS,
        account_id=account_id,
        cutoff=datetime.now(UTC),
        limit=1000,
    )
    execution_queries = [
        item
        for item in visitors.iterate(stmt)
        if isinstance(item, Select)
        and any(
            column.table is models.FlowExecution.__table__
            for column in item.selected_columns
        )
    ]
    assert execution_queries
    for query in execution_queries:
        sql = str(
            query.compile(
                dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
            )
        )
        assert f"flow.account_id = '{account_id}'" in sql


@pytest.mark.parametrize("dry_run", [False, True])
def test_execution_hold_preserves_its_events_until_released(
    db_session: Session,
    account: models.Account,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    execution = _execution(db_session, account.id, age_days=400)
    held = _event(
        db_session,
        account.id,
        age_days=400,
        event_data={"execution_id": str(execution.id)},
    )
    ordinary = _event(db_session, account.id, age_days=400)
    malformed = _event(
        db_session, account.id, age_days=400, event_data={"execution_id": "not-a-uuid"}
    )
    null_id = _event(
        db_session, account.id, age_days=400, event_data={"execution_id": None}
    )
    db_session.commit()
    outcome = place_hold(
        db_session,
        account_id=account.id,
        resource_type="execution",
        resource_id=str(execution.id),
        reason="preserve execution timeline",
    )
    assert outcome is not None
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", dry_run)

    result = _run(db_session, account)

    assert _event_exists(db_session, held)
    assert result.classes[purge.SWEEP_EVENTS] == 3
    for event_id in (ordinary, malformed, null_id):
        assert _event_exists(db_session, event_id) is dry_run

    release_hold(
        db_session,
        account_id=account.id,
        hold_id=outcome.hold.id,
        reason="timeline hold released",
    )
    db_session.commit()
    monkeypatch.setattr(settings, "retention_purge_dry_run", False)
    _run(db_session, account)
    assert not _event_exists(db_session, held)


@pytest.mark.parametrize("backlogged_class", [CLASS_AUDIT, purge.SWEEP_EXECUTION_LOGS])
def test_a_backlog_cannot_spend_the_events_time_budget(
    db_session: Session,
    account: models.Account,
    monkeypatch: pytest.MonkeyPatch,
    backlogged_class: str,
) -> None:
    """A class that uses its full allowance must leave time for both sweeps."""
    execution = _execution(db_session, account.id, age_days=400)
    event_id = _event(db_session, account.id, age_days=400)
    db_session.commit()
    clock = [0.0]
    monkeypatch.setattr(purge, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = purge.purge_class

    def spend_budget(db: Session, **kwargs: Any) -> purge.ClassResult:
        result = original(db, **kwargs)
        if kwargs["record_class"] == backlogged_class:
            clock[0] = kwargs["deadline"]
            result.more_remaining = True
        return result

    monkeypatch.setattr(purge, "purge_class", spend_budget)
    result = _run(db_session, account)

    assert not _event_exists(db_session, event_id)
    assert _log_count(db_session, execution.id) == 0
    assert result.budget_exhausted
    assert clock[0] <= settings.retention_purge_max_seconds


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
    """Logs and execution events carry account-scoped execution hold checks."""
    cutoff = datetime.now(UTC)
    rendered = " ".join(
        str(item).lower()
        for item in purge.class_filters(
            purge.SWEEP_EXECUTION_LOGS, cutoff, account_id=uuid.uuid4()
        )
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
