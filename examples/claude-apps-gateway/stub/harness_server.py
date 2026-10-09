"""Test-only HTTP services for the Claude apps gateway harness.

One stdlib-only file, three roles selected by ``HARNESS_ROLE``:

* ``model``: a stub Anthropic Messages API. Preloop's AI model points here,
  so no real model provider key or spend is involved.
* ``recorder``: a reverse proxy in front of Preloop. It records the request
  header NAMES (never values) of every request the apps gateway sends to
  Preloop and serves them at ``GET /_harness/requests``.
* ``spare``: the second upstream in ``gateway.yaml``. It counts requests at
  ``GET /_harness/requests`` so ``verify.sh`` can prove the gateway did not
  fail over on a 429.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ROLE = os.environ.get("HARNESS_ROLE", "model")
PORT = int(os.environ.get("HARNESS_PORT", "9000"))
TARGET = os.environ.get("HARNESS_TARGET", "http://preloop:8000")
REPLY_TEXT = os.environ.get("HARNESS_REPLY", "PRELOOP_STUB_OK")
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}

_LOCK = threading.Lock()
_REQUESTS: list[dict[str, Any]] = []


_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _safe_header(name: str, value: str) -> tuple[str, str] | None:
    """Return the header unchanged if it cannot split the response, else None.

    The recorder relays Preloop's response headers. A name that is not an
    RFC 9110 token, or a value with CR, LF or NUL, is dropped instead of
    being written, so a relayed header can never inject another header.
    """
    if not _HEADER_NAME.match(name) or any(ch in value for ch in "\r\n\x00"):
        return None
    return name, value


def _body_model(body: bytes) -> str | None:
    try:
        value = json.loads(body or b"{}").get("model")
    except (ValueError, AttributeError):
        return None
    return str(value) if value is not None else None


def _record(handler: BaseHTTPRequestHandler, body: bytes, status: int) -> None:
    """Keep header names only, plus the two non-secret values the report needs."""
    names = sorted({name.lower() for name in handler.headers.keys()})
    with _LOCK:
        _REQUESTS.append(
            {
                "method": handler.command,
                "path": handler.path,
                "header_names": names,
                "user_agent": handler.headers.get("user-agent"),
                "model": _body_model(body),
                "stream": b'"stream":true' in body.replace(b" ", b""),
                "status": status,
            }
        )


def _message(model: str) -> dict[str, Any]:
    return {
        "id": "msg_harness_stub",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": REPLY_TEXT}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1000,
            "output_tokens": 100,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _sse_events(model: str) -> list[tuple[str, dict[str, Any]]]:
    start = _message(model)
    start["content"] = []
    start["stop_reason"] = None
    start["usage"] = dict(start["usage"], output_tokens=1)
    return [
        ("message_start", {"type": "message_start", "message": start}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": REPLY_TEXT},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 100},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]


class Handler(BaseHTTPRequestHandler):
    """Dispatch on ``ROLE``."""

    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D102
        print(f"[{ROLE}] {self.command} {self.path} " + fmt % args, flush=True)

    def _body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _harness_endpoints(self) -> bool:
        if self.path.startswith("/_harness/requests"):
            if self.command == "DELETE":
                with _LOCK:
                    _REQUESTS.clear()
                self._json(200, {"cleared": True})
            else:
                with _LOCK:
                    self._json(200, {"role": ROLE, "requests": list(_REQUESTS)})
            return True
        if self.path == "/_harness/health":
            self._json(200, {"ok": True, "role": ROLE})
            return True
        return False

    def _handle(self) -> None:
        if self._harness_endpoints():
            return
        body = self._body()
        if ROLE == "recorder":
            self._proxy(body)
            return
        _record(self, body, 200)
        self._model(body)

    def _model(self, body: bytes) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            payload = json.loads(body or b"{}")
        except ValueError:
            payload = {}
        model = str(payload.get("model") or "claude-sonnet-4-5")
        if path.endswith("/count_tokens"):
            self._json(200, {"input_tokens": 1000})
            return
        if path.endswith("/models"):
            self._json(200, {"data": [], "has_more": False})
            return
        if not path.endswith("/messages"):
            self._json(404, {"type": "error", "error": {"type": "not_found_error"}})
            return
        if not payload.get("stream"):
            self._json(200, _message(model))
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for event, data in _sse_events(model):
            chunk = f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()
            self.wfile.write(chunk)
            self.wfile.flush()
        self.close_connection = True

    def _proxy(self, body: bytes) -> None:
        target = urllib.parse.urlparse(TARGET)
        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=300)
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP}
        headers["host"] = target.netloc
        if body:
            headers["content-length"] = str(len(body))
        conn.request(self.command, self.path, body=body or None, headers=headers)
        resp = conn.getresponse()
        _record(self, body, resp.status)
        self.send_response(resp.status)
        for key, value in resp.getheaders():
            safe = _safe_header(key, value)
            if safe is not None and safe[0].lower() not in HOP_BY_HOP:
                self.send_header(*safe)
        self.send_header("connection", "close")
        self.end_headers()
        while True:
            chunk = resp.read1(65536)
            if not chunk:
                break
            self.wfile.write(chunk)
            self.wfile.flush()
        conn.close()
        self.close_connection = True

    do_GET = _handle  # noqa: N815 (BaseHTTPRequestHandler API)
    do_POST = _handle  # noqa: N815
    do_DELETE = _handle  # noqa: N815


if __name__ == "__main__":
    print(f"harness role={ROLE} port={PORT}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
