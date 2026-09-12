"""Provider outage budgets keep fleet-wide availability failures bounded."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from preloop.services import gateway_error_alerts as alerts
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def key(**overrides: object) -> str | None:
    args = dict(
        provider="example-provider",
        upstream_status=500,
        error_class="upstream_error",
        endpoint="https://api.example.com/v1",
        account_id="account-a",
        model_id="model-a",
    )
    return alerts.gateway_outage_key(**(args | overrides))


def test_public_outage_budget_ignores_account_model_status_and_class() -> None:
    first = key()
    for account in range(10):
        for model in range(10):
            assert (
                key(
                    account_id=str(account),
                    model_id=str(model),
                    upstream_status=503,
                    error_class="upstream_overloaded",
                )
                == first
            )
    assert key(provider="another-provider") != first
    assert key(endpoint="https://other.example.com/v1") != first
    assert key(endpoint="https://api.example.com/other-path") != first


def test_endpoint_canonicalization_excludes_credentials_and_default_port() -> None:
    assert (
        key(endpoint="HTTPS://user:secret@API.EXAMPLE.COM.:443/v1/?key=secret#fragment")
        == key()
    )
    assert key(endpoint="https://api.example.com:8443/v1") != key()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://localhost/v1",
        "http://localhost./v1",
        "http://127.0.0.1./v1",
        "http://127.0.0.1/v1",
        "http://[::1]/v1",
        "http://10.0.0.1/v1",
        "https://model.internal/v1",
        "https://model.internal./v1",
        "https://model.local./v1",
        "invalid-endpoint",
        "",
    ],
)
def test_private_and_generic_endpoints_keep_configuration_scope(endpoint: str) -> None:
    first = key(provider="custom", endpoint=endpoint)
    assert key(provider="custom", endpoint=endpoint, account_id="account-b") != first
    assert key(provider="custom", endpoint=endpoint, model_id="model-b") != first


@pytest.mark.parametrize(
    "error_class",
    [
        "upstream_auth",
        "upstream_quota_exhausted",
        "upstream_rate_limited",
    ],
)
def test_account_failures_never_consume_general_outage_budget(error_class: str) -> None:
    assert key(error_class=error_class) is None


def test_unknown_non_http_failures_keep_fine_incident_identity() -> None:
    assert key(upstream_status=None) is None
    assert key(upstream_status=400) is None
    assert key(upstream_status=None, error_class="network") is not None


def test_gateway_fleet_outage_queues_one_alert_and_preserves_error_attribution() -> (
    None
):
    alerts.reset_alert_state_for_tests()
    context = ModelGatewayAuthContext(
        token="test", user=SimpleNamespace(id="user", account_id="account")
    )
    service = OpenAIGatewayService(MagicMock(), context)

    class UpstreamError(Exception):
        status_code = 500

    with patch("preloop.services.openai_gateway.enqueue_gateway_5xx_alert") as enqueue:
        for account in range(10):
            for model in range(10):
                config = SimpleNamespace(
                    account_id=str(account),
                    id=str(model),
                    provider_name="example-provider",
                    model_identifier=f"model-{model}",
                    api_endpoint="https://api.example.com/v1",
                )
                error = service._normalize_upstream_error(
                    "openai", UpstreamError("unavailable"), ai_model=config
                )
                assert error.status_code == 502
                assert error.error_class == "upstream_error"
        assert enqueue.call_count == 1
        assert len(alerts._state) == 1
        config.provider_name = "other-provider"
        service._normalize_upstream_error(
            "openai", UpstreamError("unavailable"), ai_model=config
        )
        assert enqueue.call_count == 2
    alerts.reset_alert_state_for_tests()


def test_broker_outage_still_has_one_local_provider_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    alerts.reset_alert_state_for_tests()
    worker = alerts._SharedAlertWorker()
    monkeypatch.setattr(alerts, "_SHARED_WORKER", worker)
    executor = MagicMock()
    executor.submit.side_effect = lambda deliver: deliver()
    monkeypatch.setattr(alerts, "_ALERT_EXECUTOR", executor)
    monkeypatch.setattr(alerts, "_ALERT_PENDING", threading.BoundedSemaphore(32))
    try:
        with (
            patch("nats.NATS", side_effect=OSError("broker unavailable")) as connect,
            patch("preloop.sync.tasks.notify_admins") as notify,
        ):
            for account in range(100):
                incident = key(account_id=str(account), model_id=str(account))
                send, _ = alerts.reserve_gateway_5xx_alert(
                    "openai", 502, now=0, incident_key=incident
                )
                if send:
                    alerts.enqueue_gateway_5xx_alert(
                        subject="Failure", message="Detail", incident_key=incident
                    )
            assert connect.call_count == notify.call_count == 1
            send, suppressed = alerts.reserve_gateway_5xx_alert(
                "openai", 502, now=300, incident_key=key()
            )
            assert send and suppressed == 99
            alerts.enqueue_gateway_5xx_alert(
                subject="Reminder", message="Detail", incident_key=key()
            )
            assert connect.call_count == notify.call_count == 2
    finally:
        worker.close()
        alerts.reset_alert_state_for_tests()
