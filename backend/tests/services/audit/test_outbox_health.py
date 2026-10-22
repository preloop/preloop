"""Outbox alerts report durable failures after the writer process is gone."""

from __future__ import annotations

from typing import Any

from unittest.mock import MagicMock

from preloop.models.crud import audit_outbox as crud
from preloop.services.audit import outbox


def test_health_reports_parked_rows_after_restart(
    db_session: Any, test_user: Any, monkeypatch: Any
) -> None:
    row = crud.enqueue(
        db_session,
        account_id=test_user.account_id,
        operation="log_policy_decision",
        payload={"account_id": str(test_user.account_id)},
    )
    db_session.commit()
    crud.record_failure(
        db_session, outbox_id=row.id, error="unavailable", max_attempts=1
    )
    assert crud.oldest_pending_age_seconds(db_session) == 0
    assert crud.parked_count(db_session) == 1
    metrics = []
    monkeypatch.setattr(outbox, "_record_metric", lambda *args: metrics.append(args))
    borrowed = MagicMock(wraps=db_session)
    borrowed.close = lambda: None
    # New relay, no reliance on previous counters.
    relay = outbox.AuditOutboxRelay(
        session_factory=lambda: borrowed, service_getter=MagicMock
    )
    relay._report_lag()
    assert ("parked", 1, {}) in metrics
    assert ("lag", 0.0, {}) in metrics
    crud.requeue_failed(db_session, account_id=test_user.account_id)
    relay._report_lag()
    assert metrics[-1] == ("parked", 0, {})


def test_database_outage_emits_failure_metric(monkeypatch: Any) -> None:
    metrics = []
    monkeypatch.setattr(outbox, "_record_metric", lambda *args: metrics.append(args))
    relay = outbox.AuditOutboxRelay(
        session_factory=MagicMock(), service_getter=MagicMock
    )

    def fail() -> None:
        relay._stop.set()
        raise ConnectionError("database unavailable")

    monkeypatch.setattr(relay, "drain", fail)
    relay._run()
    assert metrics == [("failed", 1, {"reason": "relay_pass"})]


def test_metrics_keep_histogram_and_publish_current_values(monkeypatch: Any) -> None:
    from opentelemetry import metrics

    meter = MagicMock()
    monkeypatch.setattr(metrics, "get_meter", lambda *args: meter)
    for name in (
        "_lag_histogram",
        "_lag_gauge",
        "_parked_gauge",
        "_attempt_failure_counter",
    ):
        monkeypatch.setattr(outbox, name, None)
    monkeypatch.setattr(outbox, "_metrics_failed", False)
    outbox._record_metric("lag", 120, {})
    outbox._record_metric("lag", 0, {})
    outbox._record_metric("parked", 3, {})
    outbox._record_metric("failed", 1, {"reason": "relay_pass"})
    assert (
        meter.create_histogram.call_args.args[0] == "preloop.audit.outbox.lag_seconds"
    )
    assert meter.create_histogram.return_value.record.call_count == 2
    assert [c.args[0] for c in meter.create_gauge.call_args_list] == [
        "preloop.audit.outbox.oldest_pending_age",
        "preloop.audit.outbox.parked",
    ]
    assert [c.args[0] for c in meter.create_gauge.return_value.set.call_args_list] == [
        120,
        0,
        3,
    ]
    meter.create_counter.return_value.add.assert_called_once_with(
        1, {"reason": "relay_pass"}
    )
    outbox.record_admission_failure("notification_admission")
    assert meter.create_counter.return_value.add.call_count == 2
    meter.create_counter.return_value.add.assert_called_with(
        1, {"reason": "notification_admission"}
    )
