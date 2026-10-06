"""Wire capture: what an openai-compatible upstream receives for reasoning.

Drives the real ``/openai/v1`` routes (Responses native passthrough, the
chat-completions transcode fallback, and chat completions) against a local
HTTP stub, so the assertions are on the JSON bytes LiteLLM and the native
passthrough actually put on the wire. Regression guard for the 2026-10 report
of ``unhashable type: 'dict'`` relayed from a customer-run LiteLLM proxy:
no dict may ever reach a chat-completions upstream as ``reasoning_effort``,
and the Responses ``reasoning`` object may only go to a ``/responses`` path.
"""

from __future__ import annotations
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from preloop.api.endpoints import openai_gateway as endpoint
from preloop.models import models
from preloop.services import openai_gateway as gateway
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.model_gateway_errors import ModelGatewayAPIError
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services import openai_responses_passthrough as pt

FIX = (
    Path(__file__).resolve().parents[1]
    / "fixtures/openai_gateway/codex_responses_request.json"
)
CHAT = {
    "id": "c1",
    "object": "chat.completion",
    "created": 1,
    "model": "m",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}
RESP = {
    "id": "r1",
    "object": "response",
    "status": "completed",
    "model": "m",
    "output": [
        {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "ok", "annotations": []}],
        }
    ],
    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
}


def stub(responses_status):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append({"path": self.path, "body": body})
            if self.path.endswith("/responses"):
                if responses_status != 200:
                    self.send_response(responses_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if body.get("stream"):
                    ev = [
                        {
                            "type": "response.created",
                            "response": {**RESP, "status": "in_progress", "output": []},
                        },
                        {"type": "response.completed", "response": RESP},
                    ]
                    data = "".join(
                        "data: " + json.dumps(e) + "\n\n" for e in ev
                    ).encode()
                    ct = "text/event-stream"
                else:
                    data = json.dumps(RESP).encode()
                    ct = "application/json"
            else:
                if body.get("stream"):
                    c1 = {
                        "id": "c1",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "m",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "ok"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    c2 = {
                        "id": "c1",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "m",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": CHAT["usage"],
                    }
                    data = (
                        "data: "
                        + json.dumps(c1)
                        + "\n\ndata: "
                        + json.dumps(c2)
                        + "\n\ndata: [DONE]\n\n"
                    ).encode()
                    ct = "text/event-stream"
                else:
                    data = json.dumps(CHAT).encode()
                    ct = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", ct)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s, seen


def rig(monkeypatch, provider, api_endpoint, mode):
    ai_model = models.AIModel(
        id=uuid4(),
        account_id=uuid4(),
        name="m",
        provider_name=provider,
        model_identifier="qwen2.5-coder" if provider != "openai" else "gpt-5",
        api_endpoint=api_endpoint,
        model_parameters={},
        meta_data={"gateway": {"enabled": True, "responses_api": mode}},
    )
    for n in ["enforce_request_policy", "enforce_response_policy"]:
        monkeypatch.setattr(gateway, n, lambda *a, **k: None)
    monkeypatch.setattr(gateway, "wrap_stream_for_response_policy", lambda s, **k: s)
    for n in [
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
        monkeypatch.setattr(OpenAIGatewayService, n, lambda *a, **k: None)
    monkeypatch.setattr(
        OpenAIGatewayService, "_resolve_requested_model", lambda *a, **k: ai_model
    )
    monkeypatch.setattr(OpenAIGatewayService, "_check_budget", lambda *a, **k: None)
    monkeypatch.setattr(
        OpenAIGatewayService, "_resolve_openai_passthrough_api_key", lambda *a: "k"
    )
    monkeypatch.setattr(
        gateway.get_secret_service(),
        "resolve_ai_model_credentials",
        lambda *a, **k: SimpleNamespace(credential_type="api_key", value="k"),
        raising=False,
    )
    auth = ModelGatewayAuthContext(
        token="t", user=SimpleNamespace(id=uuid4(), account_id=ai_model.account_id)
    )
    app = FastAPI()
    app.include_router(endpoint.router, prefix="/openai/v1")
    app.dependency_overrides[endpoint.get_db_session] = lambda: MagicMock()
    app.dependency_overrides[endpoint.get_model_gateway_auth_context] = lambda: auth
    app.dependency_overrides[endpoint.get_budget_enforcer] = lambda: None

    async def he(r: Request, e: ModelGatewayAPIError):
        return JSONResponse(
            status_code=e.status_code, content={"error": {"message": e.message}}
        )

    app.add_exception_handler(ModelGatewayAPIError, he)
    return TestClient(app)


def codex():
    d = json.loads(FIX.read_text())
    d.update(
        {
            "instructions": "You are Codex.",
            "reasoning": {"effort": "medium", "summary": "auto"},
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "store": False,
            "stream": True,
            "parallel_tool_calls": False,
            "prompt_cache_key": "pck-1",
            "model": "gateway-alias",
        }
    )
    return d


CASES = [
    ("oc_responses_native", "openai-compatible", "responses", 200, "native"),
    ("oc_responses_auto_404_fallback", "openai-compatible", "responses", 404, "auto"),
    ("oc_responses_transcode", "openai-compatible", "responses", 200, "transcode"),
    ("oc_chat", "openai-compatible", "chat", 200, "auto"),
    ("openai_responses_native", "openai", "responses", 200, "native"),
    ("openai_responses_transcode", "openai", "responses", 200, "transcode"),
    ("openai_chat", "openai", "chat", 200, "auto"),
]
EXPECTED_PATHS = {
    "oc_responses_native": ["/v1/responses"],
    "oc_responses_auto_404_fallback": ["/v1/responses", "/v1/chat/completions"],
    "oc_responses_transcode": ["/v1/chat/completions"],
    "oc_chat": ["/v1/chat/completions"],
    "openai_responses_native": ["/v1/responses"],
    "openai_responses_transcode": ["/v1/chat/completions"],
    "openai_chat": ["/v1/chat/completions"],
}


@pytest.mark.parametrize("name,provider,kind,rstatus,mode", CASES)
def test_openai_compatible_reasoning_wire_body(
    monkeypatch, name, provider, kind, rstatus, mode
):
    pt.reset_capability_cache()
    s, seen = stub(rstatus)
    try:
        c = rig(monkeypatch, provider, f"http://127.0.0.1:{s.server_port}/v1", mode)
        if kind == "responses":
            r = c.post("/openai/v1/responses", json=codex())
        else:
            r = c.post(
                "/openai/v1/chat/completions",
                json={
                    "model": "gateway-alias",
                    "messages": [{"role": "user", "content": "hi"}],
                    "reasoning_effort": "medium",
                    "stream": True,
                },
            )
        status = r.status_code
        text = r.text[:500]
    finally:
        s.shutdown()
    assert status == 200, text
    assert seen, text
    for request in seen:
        wire = request["body"]
        assert not isinstance(wire.get("reasoning_effort"), dict)
        for key in ("think", "thinking", "extra_body"):
            assert key not in wire
        if request["path"].endswith("/chat/completions"):
            assert "reasoning" not in wire
            assert "reasoning_effort" not in wire
        else:
            assert request["path"].endswith("/responses")
            assert wire["reasoning"] == {"effort": "medium", "summary": "auto"}
    paths = [request["path"] for request in seen]
    assert paths == EXPECTED_PATHS[name]
