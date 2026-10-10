"""Consecutive-failure flow alerts (#1421). No database.

The streak counter, the classifier and the dedup marker are pure functions
over execution stand-ins. The delivery and evaluation tests patch the CRUD
and transport boundaries, so they need no database either: the point is the
streak semantics and the once-per-streak guarantee, not SQL.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from preloop.models.schemas.flow import (
    FlowCreate,
    FlowFailureNotifications,
    FlowNotifications,
)
from preloop.services import flow_failure_alerts as alerts


def _exec(
    status: str,
    *,
    execution_id: Optional[UUID] = None,
    error: Optional[str] = None,
    summary: Optional[str] = None,
    result: Optional[Any] = None,
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=execution_id or uuid4(),
        status=status,
        error_message=error,
        model_output_summary=summary,
        result=result,
        start_time=start,
        end_time=end,
    )


def _flow(
    *,
    name: str = "nightly curation",
    notifications: Optional[dict] = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        name=name,
        notifications=notifications,
    )


# --- Classifier -----------------------------------------------------------


class TestClassifier:
    def test_failed_and_timeout_are_failures(self) -> None:
        assert alerts.classify_execution(_exec("FAILED")) == alerts.CLASSIFY_FAILURE
        assert alerts.classify_execution(_exec("TIMEOUT")) == alerts.CLASSIFY_FAILURE
        assert alerts.classify_execution(_exec("timed_out")) == alerts.CLASSIFY_FAILURE

    def test_success_with_output_is_a_success(self) -> None:
        assert (
            alerts.classify_execution(_exec("SUCCEEDED", summary="done"))
            == alerts.CLASSIFY_SUCCESS
        )
        assert (
            alerts.classify_execution(_exec("SUCCEEDED", result={"pr_url": "x"}))
            == alerts.CLASSIFY_SUCCESS
        )

    def test_success_without_output_is_a_failure(self) -> None:
        assert alerts.classify_execution(_exec("SUCCEEDED")) == alerts.CLASSIFY_FAILURE
        assert (
            alerts.classify_execution(_exec("SUCCEEDED", summary="   "))
            == alerts.CLASSIFY_FAILURE
        )
        assert (
            alerts.classify_execution(_exec("SUCCEEDED", result={}))
            == alerts.CLASSIFY_FAILURE
        )

    def test_operator_stops_are_neutral(self) -> None:
        assert alerts.classify_execution(_exec("STOPPED")) == alerts.CLASSIFY_NEUTRAL
        assert alerts.classify_execution(_exec("CANCELLED")) == alerts.CLASSIFY_NEUTRAL

    def test_live_status_is_neutral(self) -> None:
        assert alerts.classify_execution(_exec("RUNNING")) == alerts.CLASSIFY_NEUTRAL
        assert alerts.classify_execution(_exec("")) == alerts.CLASSIFY_NEUTRAL


# --- Threshold ------------------------------------------------------------


class TestThreshold:
    def test_absent_uses_the_default(self) -> None:
        assert alerts.failure_alert_threshold(None) == 3
        assert alerts.failure_alert_threshold({}) == 3
        assert alerts.failure_alert_threshold({"on_success": {}}) == 3
        assert alerts.failure_alert_threshold({"on_failure": {}}) == 3

    def test_configured_value_wins(self) -> None:
        blob = {"on_failure": {"alert_after_consecutive_failures": 5}}
        assert alerts.failure_alert_threshold(blob) == 5

    def test_pydantic_model_is_read(self) -> None:
        model = FlowNotifications(
            on_failure=FlowFailureNotifications(alert_after_consecutive_failures=4)
        )
        assert alerts.failure_alert_threshold(model) == 4

    def test_invalid_or_out_of_range_is_clamped(self) -> None:
        assert (
            alerts.failure_alert_threshold(
                {"on_failure": {"alert_after_consecutive_failures": 0}}
            )
            == 3
        )
        assert (
            alerts.failure_alert_threshold(
                {"on_failure": {"alert_after_consecutive_failures": "nope"}}
            )
            == 3
        )
        assert (
            alerts.failure_alert_threshold(
                {"on_failure": {"alert_after_consecutive_failures": 10_000}}
            )
            == alerts.MAX_FAILURE_ALERT_THRESHOLD
        )

    def test_schema_rejects_below_one_and_above_ceiling(self) -> None:
        with pytest.raises(ValidationError):
            FlowFailureNotifications(alert_after_consecutive_failures=0)
        with pytest.raises(ValidationError):
            FlowFailureNotifications(alert_after_consecutive_failures=101)

    def test_flow_create_round_trips_the_field(self) -> None:
        flow = FlowCreate(
            name="Legacy flow",
            prompt_template="do the work",
            agent_type="codex",
            agent_config={"sandbox_type": "exec"},
            notifications=FlowNotifications(
                on_failure=FlowFailureNotifications(alert_after_consecutive_failures=2)
            ),
        )
        dumped = flow.model_dump()
        assert (
            dumped["notifications"]["on_failure"]["alert_after_consecutive_failures"]
            == 2
        )


# --- Streak walking -------------------------------------------------------


class TestStreak:
    def test_counts_the_leading_failures(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        streak = alerts.compute_failure_streak(rows)
        assert streak.count == 3
        assert streak.execution_ids == [str(row.id) for row in rows]
        assert streak.streak_start_execution_id == str(rows[-1].id)

    def test_a_success_resets_the_streak(self) -> None:
        rows = [
            _exec("FAILED"),
            _exec("FAILED"),
            _exec("SUCCEEDED", summary="ok"),
            _exec("FAILED"),
            _exec("FAILED"),
            _exec("FAILED"),
        ]
        streak = alerts.compute_failure_streak(rows)
        assert streak.count == 2
        assert streak.execution_ids == [str(rows[0].id), str(rows[1].id)]

    def test_operator_stop_does_not_break_the_streak(self) -> None:
        rows = [_exec("FAILED"), _exec("STOPPED"), _exec("FAILED"), _exec("FAILED")]
        streak = alerts.compute_failure_streak(rows)
        assert streak.count == 3
        assert str(rows[1].id) not in streak.execution_ids

    def test_no_output_success_is_counted_as_a_failure(self) -> None:
        rows = [
            _exec("FAILED"),
            _exec("FAILED"),
            _exec("SUCCEEDED"),  # no summary, no result
        ]
        streak = alerts.compute_failure_streak(rows)
        assert streak.count == 3
        assert str(rows[2].id) in streak.execution_ids

    def test_last_error_is_the_newest_failure(self) -> None:
        rows = [_exec("FAILED", error="newest"), _exec("FAILED", error="older")]
        streak = alerts.compute_failure_streak(rows)
        assert streak.last_error == "newest"

    def test_anchor_inside_the_streak_marks_it_already_alerted(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        streak = alerts.compute_failure_streak(rows, anchor=str(rows[2].id))
        assert streak.already_alerted is True

    def test_anchor_before_a_success_is_a_new_streak(self) -> None:
        old_failure = _exec("FAILED")
        rows = [_exec("FAILED"), _exec("SUCCEEDED", summary="ok"), old_failure]
        streak = alerts.compute_failure_streak(rows, anchor=str(old_failure.id))
        assert streak.already_alerted is False
        assert streak.count == 1

    def test_a_scan_that_exhausts_the_window_with_an_anchor_stays_suppressed(
        self,
    ) -> None:
        rows = [_exec("FAILED") for _ in range(5)]
        streak = alerts.compute_failure_streak(rows, anchor=str(uuid4()), scan_limit=5)
        assert streak.already_alerted is True

    def test_below_threshold_still_reports_the_count(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED")]
        streak = alerts.compute_failure_streak(rows)
        assert streak.count == 2


# --- Message --------------------------------------------------------------


class TestMessage:
    def test_names_the_flow_ids_and_last_error(self) -> None:
        rows = [
            _exec("FAILED", error="boom"),
            _exec("FAILED", error="older"),
            _exec("FAILED", error="oldest"),
        ]
        streak = alerts.compute_failure_streak(rows)
        message = alerts.build_failure_alert_message(
            flow_name="nightly curation", streak=streak, threshold=3
        )
        assert "nightly curation" in message["subject"]
        assert "3 times in a row" in message["subject"]
        for row in rows:
            assert str(row.id) in message["text"]
        assert "boom" in message["text"]
        assert "older" not in message["text"]
        assert "boom" in message["html"]

    def test_missing_error_says_so(self) -> None:
        streak = alerts.FailureStreak(count=3, execution_ids=["a", "b", "c"])
        message = alerts.build_failure_alert_message(
            flow_name="f", streak=streak, threshold=3
        )
        assert "(no error message was recorded)" in message["text"]

    def test_secrets_are_scrubbed_and_html_escaped(self) -> None:
        secret = "sk-proj-" + ("A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0")
        rows = [_exec("FAILED", error=f"failed with {secret} <script>")]
        streak = alerts.compute_failure_streak(rows)
        message = alerts.build_failure_alert_message(
            flow_name="f", streak=streak, threshold=3
        )
        assert secret not in message["text"]
        assert secret not in message["html"]
        assert "<script>" not in message["html"]

    def test_ids_are_capped_at_the_threshold(self) -> None:
        rows = [_exec("FAILED") for _ in range(5)]
        streak = alerts.compute_failure_streak(rows)
        message = alerts.build_failure_alert_message(
            flow_name="f", streak=streak, threshold=3
        )
        assert message["text"].count("- ") == 3
        assert str(rows[4].id) not in message["text"]


# --- Dedup marker ---------------------------------------------------------


class TestClaimMarker:
    def test_marker_anchors_on_the_newest_failure(self) -> None:
        flow_id = uuid4()
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        streak = alerts.compute_failure_streak(rows)
        with patch.object(alerts.crud_audit_log, "log_action") as log:
            alerts._claim_alert(
                MagicMock(),
                account_id=uuid4(),
                flow_id=flow_id,
                streak=streak,
                threshold=3,
            )
        assert log.call_count == 1
        kwargs = log.call_args.kwargs
        assert kwargs["resource_id"] == str(flow_id)
        assert kwargs["details"]["anchor_execution_id"] == str(rows[0].id)
        assert kwargs["details"]["consecutive_failures"] == 3

    def test_latest_anchor_reads_the_marker_details(self) -> None:
        flow_id = uuid4()
        entry = SimpleNamespace(
            resource_id=str(flow_id),
            details={"anchor_execution_id": "exec-anchor"},
        )
        with patch.object(
            alerts.crud_audit_log, "get_by_account", return_value=[entry]
        ) as get:
            anchor = alerts._latest_alert_anchor(
                MagicMock(), account_id=uuid4(), flow_id=flow_id
            )
        assert anchor == "exec-anchor"
        assert get.call_args.kwargs["resource_id"] == str(flow_id)


# --- Evaluation -----------------------------------------------------------


def _evaluate(
    rows: List[SimpleNamespace],
    *,
    anchor: Optional[str] = None,
    notifications: Optional[dict] = None,
    delivery: Optional[dict] = None,
) -> Any:
    flow = _flow(notifications=notifications)
    with (
        patch.object(
            alerts.crud_flow_execution, "get_by_flow", return_value=rows
        ) as get_rows,
        patch.object(alerts, "_latest_alert_anchor", return_value=anchor) as get_anchor,
        patch.object(alerts, "_claim_alert") as claim,
        patch.object(
            alerts,
            "deliver_failure_alert",
            return_value=delivery or {"recipients": 2, "email": 2, "push": 0},
        ) as deliver,
    ):
        outcome = alerts.evaluate_failure_streak(MagicMock(), flow=flow)
    return outcome, get_rows, get_anchor, claim, deliver


class TestEvaluate:
    def test_alert_fires_at_n_and_claims_before_delivery(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        outcome, _rows, _anchor, claim, deliver = _evaluate(rows)
        assert outcome.alerted is True
        assert outcome.emails_sent == 2
        claim.assert_called_once()
        deliver.assert_called_once()

    def test_no_alert_at_n_minus_one(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED")]
        outcome, _rows, _anchor, claim, deliver = _evaluate(rows)
        assert outcome.alerted is False
        assert outcome.skipped_reason == "below_threshold"
        claim.assert_not_called()
        deliver.assert_not_called()

    def test_configured_threshold_two_fires_earlier(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED")]
        outcome, *_ = _evaluate(
            rows,
            notifications={"on_failure": {"alert_after_consecutive_failures": 2}},
        )
        assert outcome.alerted is True

    def test_same_streak_does_not_re_alert(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        outcome, _rows, _anchor, claim, deliver = _evaluate(
            rows, anchor=str(rows[2].id)
        )
        assert outcome.alerted is False
        assert outcome.skipped_reason == "already_alerted"
        claim.assert_not_called()
        deliver.assert_not_called()

    def test_a_success_that_finished_first_does_not_hide_later_failures(
        self,
    ) -> None:
        """Start order is not completion order.

        The success started last and finished first. get_by_flow(start_time)
        would return it first and reset the streak. Completion order is
        F3, F2, F1, then the success, so three failures are due.
        """
        base = datetime(2026, 10, 9, 9, 0, tzinfo=timezone.utc)
        success = _exec(
            "SUCCEEDED",
            summary="ok",
            start=base + timedelta(minutes=30),
            end=base + timedelta(minutes=35),
        )
        failed_late = _exec(
            "FAILED",
            start=base + timedelta(minutes=20),
            end=base + timedelta(minutes=90),
        )
        failed_mid = _exec(
            "FAILED",
            start=base + timedelta(minutes=10),
            end=base + timedelta(minutes=85),
        )
        failed_early = _exec(
            "FAILED",
            start=base,
            end=base + timedelta(minutes=80),
        )
        # Newest start first, which is how get_by_flow orders by default.
        outcome, get_rows, _anchor, claim, deliver = _evaluate(
            [success, failed_late, failed_mid, failed_early]
        )
        assert outcome.alerted is True
        assert outcome.skipped_reason is None
        claim.assert_called_once()
        deliver.assert_called_once()
        get_rows.assert_called_once()
        assert get_rows.call_args.kwargs["order_by_completion"] is True

    def test_a_success_then_a_new_streak_alerts_again(self) -> None:
        old_failure = _exec("FAILED")
        rows = [
            _exec("FAILED"),
            _exec("FAILED"),
            _exec("FAILED"),
            _exec("SUCCEEDED", summary="ok"),
            old_failure,
        ]
        outcome, *_ = _evaluate(rows, anchor=str(old_failure.id))
        assert outcome.alerted is True

    def test_unscoped_flow_is_skipped(self) -> None:
        flow = SimpleNamespace(id=None, account_id=None, name="x", notifications=None)
        outcome = alerts.evaluate_failure_streak(MagicMock(), flow=flow)
        assert outcome.alerted is False
        assert outcome.skipped_reason == "flow_unscoped"

    def test_successful_current_execution_skips_the_scan(self) -> None:
        flow = _flow()
        with (
            patch.object(alerts.crud_flow_execution, "get_by_flow") as get_rows,
            patch.object(alerts, "_latest_alert_anchor") as get_anchor,
        ):
            outcome = alerts.evaluate_failure_streak(
                MagicMock(),
                flow=flow,
                execution=_exec("SUCCEEDED", summary="done"),
            )
        assert outcome.alerted is False
        assert outcome.skipped_reason == "latest_success"
        get_rows.assert_not_called()
        get_anchor.assert_not_called()

    def test_claim_failure_suppresses_delivery(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        flow = _flow()
        with (
            patch.object(alerts.crud_flow_execution, "get_by_flow", return_value=rows),
            patch.object(alerts, "_latest_alert_anchor", return_value=None),
            patch.object(alerts, "_claim_alert", side_effect=RuntimeError("db down")),
            patch.object(alerts, "deliver_failure_alert") as deliver,
        ):
            outcome = alerts.evaluate_failure_streak(MagicMock(), flow=flow)
        assert outcome.alerted is False
        assert outcome.skipped_reason == "claim_failed"
        deliver.assert_not_called()


# --- Delivery -------------------------------------------------------------


class TestDelivery:
    def test_email_failure_is_swallowed(self) -> None:
        owner = SimpleNamespace(id=uuid4(), email="owner@example.com")
        message = {"subject": "s", "text": "t", "html": "h"}
        with (
            patch.object(
                alerts.notification_preferences, "get_by_user", return_value=None
            ),
            patch(
                "preloop.utils.email.send_email", side_effect=RuntimeError("smtp down")
            ),
        ):
            sent = alerts._send_emails(MagicMock(), [owner], message)
        assert sent == 0

    def test_email_respects_preference(self) -> None:
        owner = SimpleNamespace(id=uuid4(), email="owner@example.com")
        prefs = SimpleNamespace(enable_email=False)
        message = {"subject": "s", "text": "t", "html": "h"}
        with (
            patch.object(
                alerts.notification_preferences, "get_by_user", return_value=prefs
            ),
            patch("preloop.utils.email.send_email") as send,
        ):
            sent = alerts._send_emails(MagicMock(), [owner], message)
        assert sent == 0
        send.assert_not_called()

    def test_deliver_returns_counts_and_swallows_push_error(self) -> None:
        flow = _flow()
        streak = alerts.FailureStreak(count=3, execution_ids=["a", "b", "c"])
        with (
            patch(
                "preloop.services.policy_notice_delivery.policy_owners",
                return_value=[SimpleNamespace(id=uuid4(), email="o@example.com")],
            ),
            patch.object(alerts, "_send_emails", return_value=1),
            patch(
                "preloop.services.policy_notice_delivery.send_push_to_owners",
                side_effect=RuntimeError("push down"),
            ),
        ):
            result = alerts.deliver_failure_alert(
                MagicMock(), flow=flow, streak=streak, threshold=3
            )
        assert result == {"recipients": 1, "email": 1, "push": 0}

    def test_recipient_lookup_failure_returns_zero_counts(self) -> None:
        flow = _flow()
        streak = alerts.FailureStreak(count=3, execution_ids=["a"])
        with patch(
            "preloop.services.policy_notice_delivery.policy_owners",
            side_effect=RuntimeError("db down"),
        ):
            result = alerts.deliver_failure_alert(
                MagicMock(), flow=flow, streak=streak, threshold=3
            )
        assert result == {"recipients": 0, "email": 0, "push": 0}

    def test_push_ids_are_capped_for_the_payload(self) -> None:
        """A high threshold must not blow the APNs/FCM 4 KB payload limit."""
        flow = _flow()
        ids = [str(uuid4()) for _ in range(20)]
        streak = alerts.FailureStreak(count=20, execution_ids=ids)
        owner = SimpleNamespace(id=uuid4(), email="o@example.com")
        with (
            patch(
                "preloop.services.policy_notice_delivery.policy_owners",
                return_value=[owner],
            ),
            patch.object(alerts, "_send_emails", return_value=1),
            patch(
                "preloop.services.policy_notice_delivery.send_push_to_owners",
                return_value=1,
            ) as push,
        ):
            alerts.deliver_failure_alert(
                MagicMock(), flow=flow, streak=streak, threshold=100
            )
        data = push.call_args.kwargs["data"]
        assert data["execution_ids"] == ids[: alerts.PUSH_EXECUTION_IDS_LIMIT]
        assert len(data["execution_ids"]) == alerts.PUSH_EXECUTION_IDS_LIMIT


class TestConcurrencyGuard:
    """The read-then-claim sequence is serialized per flow."""

    def test_lock_is_acquired_on_postgres(self) -> None:
        db = MagicMock()
        db.bind.dialect.name = "postgresql"
        alerts._lock_flow_failure_alerts(db, flow_id="flow-1")
        db.execute.assert_called_once()
        sql = str(db.execute.call_args.args[0])
        assert "pg_advisory_xact_lock" in sql
        assert db.execute.call_args.args[1] == {"key": "flow_failure_alert:flow-1"}

    def test_lock_is_skipped_off_postgres(self) -> None:
        db = MagicMock()
        db.bind.dialect.name = "sqlite"
        alerts._lock_flow_failure_alerts(db, flow_id="flow-1")
        db.execute.assert_not_called()

    def test_lock_precedes_the_streak_scan(self) -> None:
        rows = [_exec("FAILED"), _exec("FAILED"), _exec("FAILED")]
        flow = _flow()
        order: List[str] = []

        def record_lock(*args: Any, **kwargs: Any) -> None:
            order.append("lock")

        def record_scan(*args: Any, **kwargs: Any) -> List[Any]:
            order.append("scan")
            return rows

        def record_anchor(*args: Any, **kwargs: Any) -> None:
            order.append("anchor")

        with (
            patch.object(
                alerts,
                "_lock_flow_failure_alerts",
                side_effect=record_lock,
            ),
            patch.object(
                alerts.crud_flow_execution,
                "get_by_flow",
                side_effect=record_scan,
            ),
            patch.object(
                alerts,
                "_latest_alert_anchor",
                side_effect=record_anchor,
            ),
            patch.object(alerts, "_claim_alert"),
            patch.object(
                alerts,
                "deliver_failure_alert",
                return_value={"recipients": 1, "email": 1, "push": 0},
            ),
        ):
            alerts.evaluate_failure_streak(MagicMock(), flow=flow)
        assert order == ["lock", "scan", "anchor"]


class TestOrchestratorWiring:
    """``_notify_failure_streak`` evaluates off the loop on a fresh session."""

    @pytest.mark.asyncio
    async def test_evaluates_off_loop_with_a_fresh_session(self) -> None:
        from preloop.services.flow_orchestrator import FlowExecutionOrchestrator

        flow_id = uuid4()
        account_id = uuid4()
        execution_id = uuid4()
        orchestrator: Any = FlowExecutionOrchestrator.__new__(FlowExecutionOrchestrator)
        orchestrator.flow = SimpleNamespace(
            id=flow_id, account_id=account_id, name="nightly curation"
        )
        orchestrator.execution_log = SimpleNamespace(
            id=execution_id,
            status="FAILED",
            model_output_summary=None,
            result=None,
        )
        orchestrator.db = MagicMock()

        reloaded_flow = SimpleNamespace(
            id=flow_id, account_id=account_id, name="nightly curation"
        )
        outcome = alerts.FailureAlertOutcome(alerted=True, recipients=2)
        fresh_session = MagicMock()

        async def fake_run_db_async(operation: Any) -> Any:
            return operation(fresh_session)

        with (
            patch(
                "preloop.services.flow_orchestrator.crud_flow.get",
                return_value=reloaded_flow,
            ) as get_flow,
            patch(
                "preloop.services.db_executor.run_db_async",
                side_effect=fake_run_db_async,
            ) as run_db_async,
            patch(
                "preloop.services.flow_failure_alerts.evaluate_failure_streak",
                return_value=outcome,
            ) as evaluate,
        ):
            await orchestrator._notify_failure_streak("FAILED")

        run_db_async.assert_called_once()
        get_flow.assert_called_once_with(fresh_session, id=flow_id)
        evaluate.assert_called_once()
        assert evaluate.call_args.args[0] is fresh_session
        assert evaluate.call_args.kwargs["flow"] is reloaded_flow
        assert evaluate.call_args.kwargs["execution"].status == "FAILED"
        # The live session is never handed across the thread boundary.
        assert fresh_session is not orchestrator.db


class TestGenericPushHelper:
    """The push fan-out shared with policy notices is a no-DB boundary."""

    def test_send_push_to_owners_uses_tokens_and_transports(self) -> None:
        from preloop.services import policy_notice_delivery as delivery

        owner: Any = SimpleNamespace(id=uuid4())
        prefs = SimpleNamespace(
            enable_mobile_push=True,
            notify_when_needed=True,
            get_device_tokens=lambda platform=None: (
                ["tok-android-1"] if platform == "android" else []
            ),
        )
        sent: List[dict] = []

        async def fake_android(**kwargs: Any) -> dict:
            sent.append(kwargs)
            return {"success": True}

        with (
            patch.object(
                delivery.notification_preferences, "get_by_user", return_value=prefs
            ),
            patch(
                "preloop.services.push_notifications.get_apns_service",
                return_value=None,
            ),
            patch(
                "preloop.services.push_notifications.is_fcm_configured",
                return_value=True,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.approval_service._send_android_push_transport",
                side_effect=fake_android,
            ),
        ):
            count = delivery.send_push_to_owners(
                MagicMock(),
                [owner],
                title="Flow failed",
                body="x" * 400,
                data={"type": "flow_failure_alert"},
                thread_id="flow-failures",
            )

        assert count == 1
        assert sent[0]["token"] == "tok-android-1"
        assert sent[0]["title"] == "Flow failed"
        assert sent[0]["body"] == "x" * delivery.PUSH_BODY_LIMIT
        assert sent[0]["data"] == {"type": "flow_failure_alert"}

    def test_proxy_body_is_capped_like_apns(self) -> None:
        """The push proxy must not receive the raw body APNs already slices."""
        from preloop.services import policy_notice_delivery as delivery

        owner: Any = SimpleNamespace(id=uuid4())
        prefs = SimpleNamespace(
            enable_mobile_push=True,
            notify_when_needed=True,
            get_device_tokens=lambda platform=None: (
                ["tok-ios-1"] if platform == "ios" else []
            ),
        )
        sent: List[dict] = []

        async def fake_proxy(**kwargs: Any) -> dict:
            sent.append(kwargs)
            return {"success": True}

        with (
            patch.object(
                delivery.notification_preferences, "get_by_user", return_value=prefs
            ),
            patch(
                "preloop.services.push_notifications.get_apns_service",
                return_value=None,
            ),
            patch(
                "preloop.services.push_notifications.is_fcm_configured",
                return_value=False,
            ),
            patch(
                "preloop.services.push_proxy.is_push_proxy_configured",
                return_value=True,
            ),
            patch(
                "preloop.services.push_proxy.send_push_via_proxy",
                side_effect=fake_proxy,
            ),
        ):
            count = delivery.send_push_to_owners(
                MagicMock(),
                [owner],
                title="Flow failed",
                body="y" * 400,
                data={"type": "flow_failure_alert"},
            )

        assert count == 1
        assert sent[0]["body"] == "y" * delivery.PUSH_BODY_LIMIT
