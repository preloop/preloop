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
