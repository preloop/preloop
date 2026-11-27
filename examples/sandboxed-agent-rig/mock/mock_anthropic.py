"""A stand-in for the Anthropic API, for the sandboxed agent rig.

It answers the Messages API well enough for the Preloop gateway probe and for
Claude Code itself (JSON and server-sent events), lists models, and rejects
any credential containing the word "stale" with the 401 a dead login gets.
It never talks to a real provider and holds no real credential.

Run: python mock_anthropic.py --port 8080
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

_LOCK = threading.Lock()
_REQUESTS: list[dict[str, Any]] = []

MODELS = [
    "claude-sonnet-4-5",
    "claude-opus-4-5",
    "claude-haiku-4-5",
]


def _credential_kind(headers: Any) -> tuple[str, str]:
    """Return (kind, value) of the credential on a request, never logged."""
    authorization = headers.get("Authorization") or ""
    if authorization.lower().startswith("bearer "):
        return "bearer", authorization[7:].strip()
    api_key = headers.get("x-api-key") or ""
    if api_key:
        return "api_key", api_key.strip()
    return "none", ""


def _message(model: str, text: str) -> dict[str, Any]:
    return {
        "id": "msg_rig_0001",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 3},
    }


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


class Handler(BaseHTTPRequestHandler):
    """Request handler for the mock."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        """Silence the default access log; requests are kept in memory."""

    def _send_json(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("request-id", "req_rig_0001")
        self.end_headers()
        self.wfile.write(payload)

    def _record(self, kind: str, stale: bool) -> None:
        with _LOCK:
            _REQUESTS.append(
                {
                    "method": self.command,
                    "path": self.path.split("?")[0],
                    "credential": kind,
                    "rejected": stale,
                }
            )

    def _reject_if_stale(self) -> bool:
        kind, value = _credential_kind(self.headers)
        stale = "stale" in value
        self._record(kind, stale)
        if kind == "none":
            self._send_json(
                401,
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "missing credential",
                    },
                },
            )
            return True
        if stale:
            self._send_json(
                401,
                {
                    "type": "error",
                    "error": {
                        "type": "authentication_error",
                        "message": "OAuth token has been revoked. "
                        "Please obtain a new token.",
                    },
                },
            )
            return True
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        """Serve health, the request log and the model list."""
        path = self.path.split("?")[0].rstrip("/")
        if path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if path == "/_rig/requests":
            with _LOCK:
                self._send_json(200, {"requests": list(_REQUESTS)})
            return
        if path == "/v1/models":
            if self._reject_if_stale():
                return
            self._send_json(
                200,
                {
                    "data": [
                        {"id": model, "type": "model", "display_name": model}
                        for model in MODELS
                    ],
                    "has_more": False,
                },
            )
            return
        self._send_json(404, {"type": "error", "error": {"type": "not_found_error"}})

    def do_POST(self) -> None:  # noqa: N802 - stdlib naming
        """Serve the Messages API."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        path = self.path.split("?")[0].rstrip("/")
        if path == "/_rig/reset":
            with _LOCK:
                _REQUESTS.clear()
            self._send_json(200, {"status": "reset"})
            return
        if self._reject_if_stale():
            return
        if path == "/v1/messages/count_tokens":
            self._send_json(200, {"input_tokens": 12})
            return
        if path != "/v1/messages":
            self._send_json(
                404, {"type": "error", "error": {"type": "not_found_error"}}
            )
            return
        model = str(body.get("model") or MODELS[0])
        message = _message(model, "ACK")
        if not body.get("stream"):
            self._send_json(200, message)
            return
        start = dict(message, content=[], stop_reason=None)
        start["usage"] = {"input_tokens": 12, "output_tokens": 0}
        chunks = [
            _sse("message_start", {"type": "message_start", "message": start}),
            _sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "ACK"},
                },
            ),
            _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            _sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 3},
                },
            ),
            _sse("message_stop", {"type": "message_stop"}),
        ]
        payload = b"".join(chunks)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("request-id", "req_rig_0001")
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    """Start the mock server."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    server.serve_forever()


if __name__ == "__main__":
    main()
