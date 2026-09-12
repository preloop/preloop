"""Forward authentic client identity only to the verified Zen Responses endpoint."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Generator
from unittest.mock import MagicMock

import pytest

from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService


def test_gateway_retains_only_bounded_client_identity_headers() -> None:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(token="gateway-token", user=MagicMock()),
        client_identity_headers={
            "user-agent": "actual-client/1.2",
            "X-OpenCode-Client": "cli",
            "x-opencode-request": "request-fixture",
            "x-opencode-session": "session-fixture",
            "x-opencode-project": "project-fixture",
            "authorization": "Bearer never-forward-inbound",
            "cookie": "never-forward-cookie",
            "x-session-id": "do-not-synthesize",
        },
    )
    assert service._client_identity_headers == {
        "User-Agent": "actual-client/1.2",
        "x-opencode-client": "cli",
        "x-opencode-request": "request-fixture",
        "x-opencode-session": "session-fixture",
        "x-opencode-project": "project-fixture",
    }


@pytest.mark.parametrize(
    "value",
    ["", "x" * 513, "bad\r\nheader", "bad\x00header", "bad\x7fheader", "non-ascii-☃"],
)
def test_gateway_discards_unusable_identity_values(value: str) -> None:
    service = OpenAIGatewayService(
        MagicMock(),
        ModelGatewayAuthContext(token="gateway-token", user=MagicMock()),
        client_identity_headers={"User-Agent": value, "x-opencode-client": value},
    )
    assert service._client_identity_headers == {}


@pytest.fixture
def identity_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[SimpleNamespace, None, None]:
    """Keep route/native transport real; stub model storage, policy and accounting."""
    import json
    from uuid import uuid4

    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from preloop.api.endpoints import openai_gateway as endpoint
    from preloop.models import models
    from preloop.services import openai_gateway as gateway
    from preloop.services.model_gateway_errors import ModelGatewayAPIError

    ai_model = models.AIModel(
        id=uuid4(),
        account_id=uuid4(),
        name="Synthetic Responses model",
        provider_name="openai-compatible",
        model_identifier="responses-fixture",
        api_endpoint="https://opencode.ai/zen/v1",
        model_parameters={},
        meta_data={"gateway": {"enabled": True, "responses_api": "native"}},
    )
    captured: list[httpx.Request] = []
    statuses: list[int] = []
    response_payload = {
        "id": "resp_fixture",
        "object": "response",
        "status": "completed",
        "model": "responses-fixture",
        "output": [
            {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Identity fixture complete",
                        "annotations": [],
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
    }

    def upstream(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        status = statuses.pop(0) if statuses else 200
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "error": {
                        "type": "invalid_request_error"
                        if status == 400
                        else "server_error",
                        "message": "Caller is not eligible"
                        if status == 400
                        else "Temporary upstream failure",
                    }
                },
            )
        payload = json.loads(request.content)
        if not payload.get("stream"):
            return httpx.Response(200, json=response_payload)
        message = response_payload["output"][0]
        events = [
            {
                "type": "response.created",
                "response": {**response_payload, "status": "in_progress", "output": []},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**message, "content": [], "status": "in_progress"},
            },
            {
                "type": "response.output_text.delta",
                "item_id": "msg_fixture",
                "output_index": 0,
                "content_index": 0,
                "delta": "Identity fixture complete",
            },
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": response_payload},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join("data: " + json.dumps(event) + "\n\n" for event in events),
        )

    transport = httpx.Client(transport=httpx.MockTransport(upstream))
    monkeypatch.setattr(gateway, "_openai_passthrough_http_client", lambda _: transport)
    monkeypatch.setattr(gateway, "enforce_request_policy", lambda *a, **kw: None)
    monkeypatch.setattr(gateway, "enforce_response_policy", lambda *a, **kw: None)
    monkeypatch.setattr(
        gateway, "wrap_stream_for_response_policy", lambda stream, **kw: stream
    )
    monkeypatch.setattr(gateway.time, "sleep", lambda _: None)
    for name in [
        "_adopt_openai_native_session_id",
        "_reject_if_gateway_halted",
        "_emit_gateway_request_started",
        "_deliver_operator_notes",
        "_capture_tools_meta",
        "release_db_for_wait",
        "_record_gateway_request",
        "_defer_stream_record",
        "flush_deferred_stream_record",
    ]:
        monkeypatch.setattr(OpenAIGatewayService, name, lambda *a, **kw: None)
    monkeypatch.setattr(
        OpenAIGatewayService, "_resolve_requested_model", lambda *a, **kw: ai_model
    )
    monkeypatch.setattr(OpenAIGatewayService, "_check_budget", lambda *a, **kw: None)
    monkeypatch.setattr(
        OpenAIGatewayService,
        "_resolve_openai_passthrough_api_key",
        lambda *a: "upstream-fixture-key",
    )
    monkeypatch.setattr(
        OpenAIGatewayService,
        "_strip_openai_passthrough_tools",
        lambda self, payload: payload,
    )
    monkeypatch.setattr(
        OpenAIGatewayService, "_observe_stream", lambda self, stream, **kw: stream
    )
    auth = ModelGatewayAuthContext(
        token="inbound-fixture-key",
        user=SimpleNamespace(id=uuid4(), account_id=ai_model.account_id),
    )
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/openai/v1")
    app.dependency_overrides[endpoint.get_db_session] = lambda: MagicMock()
    app.dependency_overrides[endpoint.get_model_gateway_auth_context] = lambda: auth
    app.dependency_overrides[endpoint.get_budget_enforcer] = lambda: None

    async def handle_error(request: Request, exc: ModelGatewayAPIError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": {"message": exc.message}}
        )

    app.add_exception_handler(ModelGatewayAPIError, handle_error)
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            model=ai_model,
            captured=captured,
            statuses=statuses,
            auth=auth,
        )
    transport.close()


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize(
    "endpoint", ["https://opencode.ai/zen/v1", "https://opencode.ai/zen/v1/responses"]
)
def test_endpoint_forwards_actual_identity_through_native_retries(
    identity_gateway: SimpleNamespace, stream: bool, endpoint: str
) -> None:
    rig = identity_gateway
    rig.model.api_endpoint = endpoint
    rig.statuses.extend([500, 200])
    response = rig.client.post(
        "/openai/v1/responses",
        json={"model": "gateway-alias", "input": "hello", "stream": stream},
        headers={
            "User-Agent": "opencode/1.18.29 actual-sdk-suffix",
            "x-opencode-client": "cli",
            "x-opencode-request": "request-fixture",
            "x-opencode-session": "session-fixture",
            "x-opencode-project": "project-fixture",
            "Authorization": "Bearer inbound-fixture-key",
            "Cookie": "private=never-forward",
            "x-session-id": "not-opencode-session",
        },
    )
    assert response.status_code == 200, response.text
    assert "Identity fixture complete" in response.text
    assert len(rig.captured) == 2
    for request in rig.captured:
        assert str(request.url) == "https://opencode.ai/zen/v1/responses"
        assert request.headers["user-agent"] == "opencode/1.18.29 actual-sdk-suffix"
        assert request.headers["x-opencode-client"] == "cli"
        assert request.headers["x-opencode-request"] == "request-fixture"
        assert request.headers["x-opencode-session"] == "session-fixture"
        assert request.headers["x-opencode-project"] == "project-fixture"
        assert request.headers["authorization"] == "Bearer upstream-fixture-key"
        assert "cookie" not in request.headers and "x-session-id" not in request.headers


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://other.example/v1",
        "https://opencode.ai.example/zen/v1",
        "http://opencode.ai/zen/v1",
        "https://opencode.ai/zen/v1?mirror=1",
    ],
)
def test_other_destinations_keep_preloop_identity(
    identity_gateway: SimpleNamespace, endpoint: str
) -> None:
    rig = identity_gateway
    rig.model.api_endpoint = endpoint
    response = rig.client.post(
        "/openai/v1/responses",
        json={"model": "gateway-alias", "input": "hello"},
        headers={"User-Agent": "opencode/1.18.29", "x-opencode-client": "cli"},
    )
    assert response.status_code == 200, response.text
    assert rig.captured[0].headers["user-agent"].startswith("Preloop/")
    assert "x-opencode-client" not in rig.captured[0].headers


@pytest.mark.parametrize("stream", [False, True])
def test_non_opencode_identity_is_not_disguised_or_retried_on_rejection(
    identity_gateway: SimpleNamespace, stream: bool
) -> None:
    rig = identity_gateway
    rig.statuses.append(400)
    response = rig.client.post(
        "/openai/v1/responses",
        json={"model": "gateway-alias", "input": "hello", "stream": stream},
        headers={"User-Agent": "another-real-client/2.0"},
    )
    assert response.status_code == 400
    assert len(rig.captured) == 1
    assert rig.captured[0].headers["user-agent"] == "another-real-client/2.0"
    assert not any(name.startswith("x-opencode-") for name in rig.captured[0].headers)


def test_auxiliary_service_does_not_inherit_request_identity(
    identity_gateway: SimpleNamespace,
) -> None:
    rig = identity_gateway
    rig.client.post(
        "/openai/v1/responses",
        json={"model": "gateway-alias", "input": "hello"},
        headers={"User-Agent": "opencode/1.18.29", "x-opencode-client": "cli"},
    )
    auxiliary = OpenAIGatewayService(MagicMock(), rig.auth)
    auxiliary._create_openai_responses_passthrough(
        rig.model, {"model": "gateway-alias", "input": "summary"}
    )
    assert rig.captured[1].headers["user-agent"].startswith("Preloop/")
    assert "x-opencode-client" not in rig.captured[1].headers


@pytest.mark.skipif(
    os.environ.get("OPENCODE_GATEWAY_IDENTITY_SMOKE") != "1",
    reason="opt-in real OpenCode 1.18.29 through gateway to mock upstream",
)
def test_real_opencode_primary_and_title_identity_survives_gateway(
    identity_gateway: SimpleNamespace, tmp_path: Path
) -> None:
    import json
    import shutil
    import subprocess
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from preloop.agents.opencode import OpenCodeAgent

    binary = shutil.which("opencode")
    assert binary
    rig = identity_gateway
    inbound: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            inbound.append(
                {"user_agent": self.headers.get("User-Agent"), "body": json.loads(body)}
            )
            response = rig.client.post(
                self.path, content=body, headers=dict(self.headers)
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", response.headers["content-type"])
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = OpenCodeAgent({})._build_opencode_config(
        "responses-fixture",
        "custom",
        {
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
            "model_api_protocol": "responses",
        },
        600000,
    )
    config["mcp"] = {}
    config["provider"]["preloop"]["options"]["apiKey"] = "inbound-fixture-key"
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OPENCODE_", "OPENAI_", "ANTHROPIC_"))
    }
    env.update(
        {
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "OPENCODE_DISABLE_MODELS_FETCH": "true",
            "OPENCODE_DISABLE_AUTOUPDATE": "true",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            **{
                f"XDG_{key}_HOME": str(tmp_path / key.lower())
                for key in ["CONFIG", "DATA", "CACHE", "STATE"]
            },
        }
    )
    try:
        version = subprocess.run(
            [binary, "--version"], env=env, capture_output=True, text=True, check=True
        )
        assert version.stdout.strip() == "1.18.29"
        result = subprocess.run(
            [
                binary,
                "run",
                "--format",
                "json",
                "Reply with Identity fixture complete. Do not use tools.",
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Identity fixture complete" in result.stdout, result.stdout + result.stderr
    assert len(inbound) >= 2
    assert any(request["body"].get("tools") for request in inbound)
    assert any(not request["body"].get("tools") for request in inbound)
    assert len(rig.captured) == len(inbound)
    for original, forwarded in zip(inbound, rig.captured, strict=False):
        assert original["user_agent"].startswith("opencode/1.18.29")
        assert forwarded.headers["user-agent"] == original["user_agent"]
        assert forwarded.headers["authorization"] == "Bearer upstream-fixture-key"
        assert not any(name.startswith("x-opencode-") for name in forwarded.headers)
        assert str(forwarded.url) == "https://opencode.ai/zen/v1/responses"
    (tmp_path / "identity-wire-summary.json").write_text(
        json.dumps(
            [
                {
                    "path": str(forwarded.url),
                    "caller_user_agent": original["user_agent"],
                    "forwarded_user_agent": forwarded.headers["user-agent"],
                    "tool_count": len(original["body"].get("tools", [])),
                    "stream": original["body"].get("stream", False),
                }
                for original, forwarded in zip(inbound, rig.captured, strict=False)
            ],
            indent=2,
        )
        + "\n"
    )


def test_client_identity_bounds_and_absence_do_not_invent_headers() -> None:
    auth = ModelGatewayAuthContext(token="gateway-token", user=MagicMock())
    service = OpenAIGatewayService(
        MagicMock(),
        auth,
        client_identity_headers={
            "User-Agent": "a" * 512,
            "x-opencode-request": "b" * 256,
            "x-opencode-session": "c" * 257,
        },
    )
    assert service._client_identity_headers == {
        "User-Agent": "a" * 512,
        "x-opencode-request": "b" * 256,
    }
    assert OpenAIGatewayService(MagicMock(), auth)._client_identity_headers == {}
