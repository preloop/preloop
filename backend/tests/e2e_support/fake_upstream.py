"""A minimal OpenAI-compatible upstream model server for the T10 E2E harness.

Why this exists
---------------
The Preloop OpenAI gateway routes model traffic through ``litellm`` (see
``preloop.services.openai_gateway._call_litellm``). For a ``provider_name`` of
``openai`` with a custom ``api_base`` (taken from ``AIModel.api_endpoint``),
litellm issues a real HTTP ``POST {api_base}/chat/completions`` to the upstream.

In CI we must NOT depend on a real provider key. Instead of patching litellm
(which only works in-process and not against a *running* server), we point the
seeded ``AIModel.api_endpoint`` at THIS server. It speaks just enough of the
OpenAI chat-completions wire format for the gateway to parse a valid response
and record usage. This keeps product code untouched and works over real HTTP.

Run it standalone:

    source .venv/bin/activate  # or ../.venv/bin/activate
    python -m tests.e2e_support.fake_upstream --port 8081

It listens on ``127.0.0.1:<port>`` and serves:
    GET  /health             -> {"status": "ok"}
    POST /v1/chat/completions-> OpenAI chat.completion JSON (any auth accepted)
    POST /chat/completions    -> same (litellm may or may not append /v1)

Seed the AIModel's ``api_endpoint`` to ``http://127.0.0.1:<port>/v1`` so litellm
hits ``/v1/chat/completions``.
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict


def _completion_body(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Build a deterministic OpenAI chat.completion response.

    Echoes the requested model and a fixed assistant message so the E2E can
    assert a stable shape while the gateway records token usage.
    """
    model = payload.get("model") or "preloop-fake"
    return {
        "id": "chatcmpl-e2e-fake",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "Hello from the Preloop E2E fake upstream.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 9,
            "total_tokens": 20,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    def _send_json(self, status: int, body: Dict[str, Any]) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.rstrip("/") in ("/health", "/v1/health"):
            self._send_json(200, {"status": "ok"})
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {}
        self._send_json(200, _completion_body(payload))

    def log_message(self, *_args: Any) -> None:  # silence per-request logging
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Preloop E2E fake upstream")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()

    server = ThreadingHTTPServer((args.host, args.port), _Handler)
    print(
        f"[fake-upstream] listening on http://{args.host}:{args.port} "
        f"(POST /v1/chat/completions)",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        # Graceful shutdown for manual test rig runs.
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
