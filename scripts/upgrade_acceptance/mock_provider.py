"""Deterministic local model upstream and API/gateway routing for the rig."""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from fixture import MODEL_KEY

MARKER = "synthetic-upgrade-completion"


class Provider(BaseHTTPRequestHandler):
    """Serve synthetic OpenAI responses without contacting a model provider."""

    def log_message(self, format: str, *args: Any) -> None:
        """Never log request paths, credentials or bodies."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib HTTP handler protocol
        """Return a deterministic model inventory."""
        self.reply(
            {
                "object": "list",
                "data": [
                    {"id": "synthetic-model", "object": "model", "owned_by": "example"}
                ],
            }
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib HTTP handler protocol
        """Return a completion with measurable usage and a known content marker."""
        if self.headers.get("Authorization") != f"Bearer {MODEL_KEY}":
            self.send_error(401)
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        if self.path != "/v1/chat/completions" or body.get("stream"):
            self.send_error(400)
            return
        self.reply(
            {
                "id": "chatcmpl-synthetic",
                "object": "chat.completion",
                "created": 1760000000,
                "model": "synthetic-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": MARKER},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                },
            }
        )

    def reply(self, data: dict[str, Any]) -> None:
        """Write one finite JSON response."""
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Proxy(Provider):
    """Route CLI requests to the separate released API and gateway roles."""

    def forward(self) -> None:
        """Forward only to this Compose project's fixed internal service names."""
        upstream = (
            "gateway" if self.path.startswith(("/openai/", "/anthropic/")) else "api"
        )
        connection = http.client.HTTPConnection(upstream, 8000, timeout=60)
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            headers = {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in {"host", "connection"}
            }
            connection.request(self.command, self.path, body=body, headers=headers)
            response = connection.getresponse()
            content = response.read()
            self.send_response(response.status)
            self.send_header(
                "Content-Type", response.getheader("Content-Type", "application/json")
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
        finally:
            connection.close()

    do_GET = forward  # noqa: N815 - stdlib HTTP handler protocol
    do_POST = forward  # noqa: N815 - stdlib HTTP handler protocol
    do_PUT = forward  # noqa: N815 - stdlib HTTP handler protocol
    do_PATCH = forward  # noqa: N815 - stdlib HTTP handler protocol
    do_DELETE = forward  # noqa: N815 - stdlib HTTP handler protocol


def main() -> None:
    """Run both local listeners until Compose stops the owned container."""
    provider = ThreadingHTTPServer(("0.0.0.0", 9000), Provider)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 9001), Proxy).serve_forever()


if __name__ == "__main__":
    main()
