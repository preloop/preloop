"""OAuth query credentials must never reach access logs or error telemetry."""

import logging

import pytest

from preloop.utils.mcp_oauth_privacy import MCPOAuthLogFilter
from preloop.utils.sentry_filters import (
    sentry_before_send,
    sentry_before_send_transaction,
)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/mcp-servers/oauth/callback",
        "/api/v1/mcp-servers/synthetic-id/oauth/authorize",
    ],
)
def test_callback_access_log_redaction_and_telemetry(path):
    url = path + "?code=synthetic-secret-code&state=synthetic-secret-state"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", url, "1.1", 302),
        None,
    )
    assert MCPOAuthLogFilter().filter(record)
    assert "synthetic-secret" not in record.getMessage()
    assert len(record.args) == 5  # Uvicorn's AccessFormatter still works.
    event = {"request": {"url": "https://console.example.com" + url}}
    assert sentry_before_send(event, {}) is None
    assert sentry_before_send_transaction(event, {}) is None


def test_flow_webhook_secret_is_redacted_from_access_log():
    url = "/api/v1/webhooks/flows/synthetic-flow-id/synthetic-webhook-secret"
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "POST", url, "1.1", 200),
        None,
    )
    assert MCPOAuthLogFilter().filter(record)
    message = record.getMessage()
    assert "synthetic-webhook-secret" not in message
    assert "/api/v1/webhooks/flows/synthetic-flow-id/[redacted]" in message
    assert len(record.args) == 5


def test_flow_webhook_secret_is_scrubbed_from_telemetry():
    path = "/api/v1/webhooks/flows/f-1/synthetic-webhook-secret"
    event = {
        "request": {"url": "https://console.example.com" + path},
        "transaction": path,
        "tags": {"url": "https://console.example.com" + path},
    }
    sent = sentry_before_send(event, {})
    assert sent is not None  # Genuine webhook failures are still reported.
    assert "synthetic-webhook-secret" not in str(sent)
    assert sent["transaction"] == "/api/v1/webhooks/flows/f-1/[redacted]"
    trace = sentry_before_send_transaction({"transaction": path}, {})
    assert trace is not None
    assert "synthetic-webhook-secret" not in str(trace)
