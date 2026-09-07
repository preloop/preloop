"""Throttled admin alerting for sustained gateway 5xx failures (#185).

These tests pin the per-process quiet-window contract for gateway 5xx admin
alerts:

  1. The first 5xx for a (provider, status) key notifies immediately.
  2. Repeats inside the window are dropped; the first error after the window
     boundary notifies again and reports how many were suppressed.
  3. Windows are independent per provider and status.
  4. Concurrent same-key reservations permit exactly one notification.
  5. A failing notifier never changes the gateway error and consumes its
     window instead of re-arming the alert.
  6. The quiet window is configurable and falls back to the default.

All timing is fake monotonic time; nothing sleeps and no real alert is sent.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from preloop.services import gateway_error_alerts
from preloop.services.gateway_error_alerts import (
    GATEWAY_ERROR_ALERT_INTERVAL_ENV,
    reserve_gateway_5xx_alert,
    reset_alert_state_for_tests,
)
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


class _FakeClock:
    """Fixed monotonic clock whose value tests can advance between calls."""

    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _FakeHTTPError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _service() -> OpenAIGatewayService:
    auth_context = ModelGatewayAuthContext(
        token="token",
        user=SimpleNamespace(id="user-1", account_id="account-1"),
    )
    return OpenAIGatewayService(MagicMock(), auth_context)


@pytest.fixture(autouse=True)
def _isolate_alert_state():
    """Every test starts and ends with an empty quiet-window table."""
    reset_alert_state_for_tests()
    yield
    reset_alert_state_for_tests()


# ---------------------------------------------------------------------------
# Throttle reservation contract.
# ---------------------------------------------------------------------------


def test_first_error_sends_immediately_and_window_suppresses_repeats():
    send, suppressed = reserve_gateway_5xx_alert("openai", 502, now=100.0)
    assert (send, suppressed) == (True, 0)

    for step in (101.0, 200.0, 399.0):
        assert reserve_gateway_5xx_alert("openai", 502, now=step) == (False, 0)


def test_boundary_allows_one_alert_with_suppressed_count_then_resets():
    assert reserve_gateway_5xx_alert("openai", 502, now=100.0) == (True, 0)
    for _ in range(4):
        assert reserve_gateway_5xx_alert("openai", 502, now=150.0) == (False, 0)

    # The exact window boundary allows one alert carrying the dropped count.
    assert reserve_gateway_5xx_alert("openai", 502, now=400.0) == (True, 4)

    # The new window starts quiet again; one repeat inside it is counted...
    assert reserve_gateway_5xx_alert("openai", 502, now=450.0) == (False, 0)
    # ...and the next boundary reports it, then resets for the window after.
    assert reserve_gateway_5xx_alert("openai", 502, now=700.0) == (True, 1)
    assert reserve_gateway_5xx_alert("openai", 502, now=1000.0) == (True, 0)


def test_different_provider_or_status_has_independent_window():
    assert reserve_gateway_5xx_alert("openai", 502, now=100.0) == (True, 0)
    assert reserve_gateway_5xx_alert("openai", 502, now=101.0) == (False, 0)

    # Other provider, same status: own window.
    assert reserve_gateway_5xx_alert("anthropic", 502, now=102.0) == (True, 0)
    # Same provider, other status: own window.
    assert reserve_gateway_5xx_alert("openai", 503, now=103.0) == (True, 0)
    assert reserve_gateway_5xx_alert("openai", 503, now=104.0) == (False, 0)


def test_non_5xx_status_is_never_reserved_or_counted():
    assert reserve_gateway_5xx_alert("openai", 429, now=100.0) == (False, 0)
    assert reserve_gateway_5xx_alert("openai", 404, now=101.0) == (False, 0)
    # The 4xx traffic above must not have opened or consumed a 5xx window.
    assert reserve_gateway_5xx_alert("openai", 502, now=102.0) == (True, 0)


def test_concurrent_same_key_reservations_permit_one_alert(monkeypatch):
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(gateway_error_alerts, "_clock", clock)

    results: List[tuple] = []
    barrier = threading.Barrier(8)

    def _reserve() -> None:
        barrier.wait()
        results.append(reserve_gateway_5xx_alert("openai", 502))

    threads = [threading.Thread(target=_reserve) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    sends = [result for result in results if result[0]]
    assert len(sends) == 1
    assert sends[0] == (True, 0)
    assert len([result for result in results if not result[0]]) == 7

    # Every suppressed reservation was counted for the boundary report.
    assert reserve_gateway_5xx_alert("openai", 502, now=1300.0) == (True, 7)


# ---------------------------------------------------------------------------
# Interval configuration.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "-5", "0", "nan", "inf", "-inf", "   "],
)
def test_invalid_interval_values_fall_back_to_default(monkeypatch, raw: str):
    monkeypatch.setenv(GATEWAY_ERROR_ALERT_INTERVAL_ENV, raw)
    assert reserve_gateway_5xx_alert("openai", 502, now=0.0) == (True, 0)
    assert reserve_gateway_5xx_alert("openai", 502, now=299.0) == (False, 0)
    # A 300s window proves the default was used despite the invalid value.
    assert reserve_gateway_5xx_alert("openai", 502, now=300.0) == (True, 1)


def test_valid_interval_override_is_used(monkeypatch):
    monkeypatch.setenv(GATEWAY_ERROR_ALERT_INTERVAL_ENV, "60")
    assert reserve_gateway_5xx_alert("openai", 502, now=0.0) == (True, 0)
    assert reserve_gateway_5xx_alert("openai", 502, now=59.0) == (False, 0)
    assert reserve_gateway_5xx_alert("openai", 502, now=60.0) == (True, 1)


# ---------------------------------------------------------------------------
# Gateway integration (_normalize_upstream_error).
# ---------------------------------------------------------------------------


def _normalize(service: OpenAIGatewayService, exc: Exception) -> Any:
    return service._normalize_upstream_error("openai", exc)


def test_gateway_repeated_5xx_sends_once_then_reports_suppressed(monkeypatch):
    """The gateway notifier honors the quiet window and reports drops."""
    service = _service()
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(gateway_error_alerts, "_clock", clock)
    captured: List[dict] = []

    def _capture_notify(*, subject: str, message: str) -> None:
        captured.append({"subject": subject, "message": message})

    exc = _FakeHTTPError("upstream exploded", status_code=502)
    with patch("preloop.sync.tasks.notify_admins", side_effect=_capture_notify):
        first_error = _normalize(service, exc)
        assert first_error.status_code == 502
        assert "upstream exploded" in first_error.message
        assert len(captured) == 1

        # Same key inside the window: no second notification.
        second_error = _normalize(service, exc)
        assert second_error.status_code == 502
        assert len(captured) == 1

        # Exact window boundary: one alert that reports the dropped count.
        clock.value = 1300.0
        boundary_error = _normalize(service, exc)
        assert boundary_error.status_code == 502
        assert len(captured) == 2
        assert "Suppressed 1 similar alert" in captured[1]["message"]

        # Next interval: alert fires again with the count reset.
        clock.value = 1600.0
        _normalize(service, exc)
        assert len(captured) == 3
        assert "Suppressed" not in captured[2]["message"]


def test_gateway_notifier_failure_keeps_error_and_reserves_window(monkeypatch):
    """A broken notifier never changes the response and still consumes the slot."""
    service = _service()
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(gateway_error_alerts, "_clock", clock)
    exc = _FakeHTTPError("upstream exploded", status_code=502)

    with patch(
        "preloop.sync.tasks.notify_admins", side_effect=RuntimeError("SMTP down")
    ) as failing_notify:
        error = _normalize(service, exc)
        assert error.status_code == 502
        assert error.message == "upstream exploded"
        assert failing_notify.call_count == 1

    # The failed notification already consumed the window: no retry storm.
    with patch("preloop.sync.tasks.notify_admins") as healthy_notify:
        _normalize(service, exc)
        healthy_notify.assert_not_called()


def test_gateway_4xx_and_429_do_not_notify_or_reserve(monkeypatch):
    service = _service()
    clock = _FakeClock(1000.0)
    monkeypatch.setattr(gateway_error_alerts, "_clock", clock)

    with patch("preloop.sync.tasks.notify_admins") as mock_notify:
        four_xx = _normalize(
            service, _FakeHTTPError("model not found", status_code=404)
        )
        assert four_xx.status_code == 404
        too_many = _normalize(service, _FakeHTTPError("rate limited", status_code=429))
        assert too_many.status_code == 429
        mock_notify.assert_not_called()

        # 4xx/429 traffic did not consume the 5xx window.
        five_xx = _normalize(
            service, _FakeHTTPError("upstream exploded", status_code=502)
        )
        assert five_xx.status_code == 502
        mock_notify.assert_called_once()


@pytest.fixture(autouse=True)
def _deliver_alerts_inline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep payload/throttle assertions deterministic; delivery has separate tests."""

    def _deliver(**kwargs: str) -> None:
        from preloop.sync.tasks import notify_admins

        notify_admins(**kwargs)

    monkeypatch.setattr(
        "preloop.services.openai_gateway.enqueue_gateway_5xx_alert", _deliver
    )


def test_alert_identifies_protocol_and_actual_upstream() -> None:
    service = _service()
    model = SimpleNamespace(provider_name="deepseek", model_identifier="test-model")
    exc = _FakeHTTPError("provider temporarily unavailable", status_code=503)
    with patch("preloop.sync.tasks.notify_admins") as notify:
        error = service._stream_error("openai", exc, ai_model=model)
    assert error.status_code == 503
    body = notify.call_args.kwargs["message"]
    assert "Gateway protocol: openai" in body
    assert "Upstream provider: deepseek" in body
    assert "Upstream model: test-model" in body
    assert "timeout failure" not in body


def test_alert_delivery_is_nonblocking_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    entered = threading.Event()
    release = threading.Event()
    delivered = []

    def _slow_notify(**kwargs: str) -> None:
        entered.set()
        assert release.wait(5), "test did not release the notifier"
        delivered.append(kwargs)

    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(gateway_error_alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(
        gateway_error_alerts, "_ALERT_PENDING", threading.BoundedSemaphore(1)
    )
    try:
        with patch("preloop.sync.tasks.notify_admins", side_effect=_slow_notify):
            gateway_error_alerts.enqueue_gateway_5xx_alert(
                subject="first", message="failure"
            )
            assert entered.wait(5)
            # The first call returned while its notifier is still blocked;
            # a full queue drops the second alert instead of waiting.
            gateway_error_alerts.enqueue_gateway_5xx_alert(
                subject="second", message="failure"
            )
            assert not delivered
            release.set()
            executor.shutdown(wait=True)
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert [item["subject"] for item in delivered] == ["first"]
    assert gateway_error_alerts._ALERT_PENDING.acquire(blocking=False)


def test_alert_submission_failure_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = MagicMock()
    executor.submit.side_effect = RuntimeError("executor shut down")
    pending = threading.BoundedSemaphore(1)
    monkeypatch.setattr(gateway_error_alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(gateway_error_alerts, "_ALERT_PENDING", pending)
    gateway_error_alerts.enqueue_gateway_5xx_alert(subject="failure", message="detail")
    assert pending.acquire(blocking=False)


def test_alert_delivery_failure_releases_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executor = MagicMock()
    pending = threading.BoundedSemaphore(1)
    monkeypatch.setattr(gateway_error_alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(gateway_error_alerts, "_ALERT_PENDING", pending)
    gateway_error_alerts.enqueue_gateway_5xx_alert(subject="failure", message="detail")
    deliver = executor.submit.call_args.args[0]
    with patch(
        "preloop.sync.tasks.notify_admins", side_effect=RuntimeError("SMTP down")
    ):
        deliver()
    assert pending.acquire(blocking=False)


@pytest.mark.parametrize("streaming", [False, True])
def test_disconnect_is_logged_without_email_or_consuming_alert_window(
    streaming: bool,
) -> None:
    service = _service()
    model = SimpleNamespace(provider_name="deepseek", model_identifier="test-model")

    class MidStreamFallbackError(Exception):
        pass

    exc = MidStreamFallbackError(
        "APIConnectionError: DeepseekException - peer closed connection without "
        "sending complete message body (incomplete chunked read)"
    )
    with (
        patch("preloop.sync.tasks.notify_admins") as notify,
        patch("preloop.services.openai_gateway.logger.warning") as warning,
    ):
        if streaming:
            error = service._stream_error("openai", exc, ai_model=model)
            frame = service._openai_stream_error_event(exc, error)
            assert "upstream_disconnect" in frame
        else:
            error = service._normalize_upstream_error("openai", exc, ai_model=model)
        notify.assert_not_called()
    assert error.status_code == 502
    assert error.error_class == "upstream_disconnect"
    assert warning.call_args.args[1:] == (
        "openai",
        "deepseek",
        "test-model",
        "upstream_disconnect",
    )
    assert reserve_gateway_5xx_alert("openai", 502) == (True, 0)


def test_network_error_remapped_to_stream_disconnect_does_not_email() -> None:
    service = _service()
    with patch("preloop.sync.tasks.notify_admins") as notify:
        error = service._stream_error("openai", ConnectionError("connection reset"))
    assert error.error_class == "upstream_disconnect"
    notify.assert_not_called()


@pytest.mark.parametrize(
    ("renderer", "protocol"),
    [
        ("_openai_stream_error_event", "openai"),
        ("_responses_stream_error_event", "openai"),
        ("_anthropic_stream_error_event", "anthropic"),
    ],
)
def test_direct_stream_error_renderer_preserves_model_and_original_network_class(
    renderer: str, protocol: str
) -> None:
    service = _service()
    model = SimpleNamespace(provider_name="deepseek", model_identifier="test-model")
    with (
        patch("preloop.sync.tasks.notify_admins") as notify,
        patch("preloop.services.openai_gateway.logger.warning") as warning,
    ):
        frame = getattr(service, renderer)(
            ConnectionError("connection reset"), ai_model=model
        )
    assert "upstream_disconnect" in frame
    assert warning.call_args.args[1:] == (protocol, "deepseek", "test-model", "network")
    notify.assert_not_called()
