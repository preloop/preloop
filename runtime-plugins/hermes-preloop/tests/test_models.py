"""Tests for gateway model-list refresh helpers."""

from __future__ import annotations

import json
import sys
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from preloop_hermes_plugin.models import (  # noqa: E402
    fetch_gateway_models,
    gateway_models_url,
    refresh_models,
)


# ---------------------------------------------------------------------------
# gateway_models_url
# ---------------------------------------------------------------------------


def test_gateway_models_url_wss() -> None:
    url = gateway_models_url("wss://app.preloop.ai/api/v1/agents/control/ws")
    assert url == "https://app.preloop.ai/openai/v1/models"


def test_gateway_models_url_ws() -> None:
    url = gateway_models_url("ws://localhost:8000/api/v1/agents/control/ws")
    assert url == "http://localhost:8000/openai/v1/models"


# ---------------------------------------------------------------------------
# fetch_gateway_models (with a local HTTP stub)
# ---------------------------------------------------------------------------


class _StubHandler(BaseHTTPRequestHandler):
    """Tiny handler that returns a canned model list on GET /openai/v1/models."""

    response_body: dict = {}
    response_status: int = 200
    last_auth_header: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        _StubHandler.last_auth_header = self.headers.get("Authorization")
        self.send_response(_StubHandler.response_status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(_StubHandler.response_body).encode())

    def log_message(self, *_args: object) -> None:
        pass  # silence noisy stderr in test output


@pytest.fixture()
def stub_server():
    """Spin up a local HTTP server for the duration of a test."""
    server = HTTPServer(("127.0.0.1", 0), _StubHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, port
    server.shutdown()


def test_fetch_returns_data_array(stub_server) -> None:
    server, port = stub_server
    models = [{"id": "claude-sonnet-4", "object": "model"}]
    _StubHandler.response_body = {"object": "list", "data": models}
    _StubHandler.response_status = 200

    result = fetch_gateway_models(
        f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
        "test-token",
    )
    assert result == models
    assert _StubHandler.last_auth_header == "Bearer test-token"


def test_fetch_falls_back_to_models_array(stub_server) -> None:
    server, port = stub_server
    models = [{"id": "test-model"}]
    _StubHandler.response_body = {"object": "list", "models": models}
    _StubHandler.response_status = 200

    result = fetch_gateway_models(
        f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
        "tok",
    )
    assert result == models


def test_fetch_raises_on_http_error(stub_server) -> None:
    server, port = stub_server
    _StubHandler.response_body = {}
    _StubHandler.response_status = 403

    with pytest.raises(urllib.error.HTTPError):
        fetch_gateway_models(
            f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
            "tok",
        )


# ---------------------------------------------------------------------------
# refresh_models
# ---------------------------------------------------------------------------


def test_refresh_models_returns_list_on_success(stub_server) -> None:
    server, port = stub_server
    _StubHandler.response_body = {
        "data": [{"id": "m1"}, {"id": "m2"}],
    }
    _StubHandler.response_status = 200

    result = refresh_models(
        f"ws://127.0.0.1:{port}/api/v1/agents/control/ws",
        "tok",
    )
    assert len(result) == 2


def test_refresh_models_returns_empty_on_failure() -> None:
    # Point at a port that is definitely not listening.
    result = refresh_models(
        "ws://127.0.0.1:1/api/v1/agents/control/ws",
        "tok",
    )
    assert result == []
