"""Verify the mocked model enforces the credential and response contract."""

from __future__ import annotations

import http.client
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ["PRELOOP_DISABLE_TELEMETRY"] = "true"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fixture import MODEL_KEY  # noqa: E402
from mock_provider import MARKER, Provider  # noqa: E402


def test_model_requires_decrypted_credential_and_returns_known_content() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for credential, status in (("incorrect", 401), (MODEL_KEY, 200)):
            connection = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    body=json.dumps(
                        {"messages": [{"role": "user", "content": "Synthetic"}]}
                    ),
                    headers={
                        "Authorization": f"Bearer {credential}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
                assert response.status == status
                if status == 200:
                    document = json.loads(raw)
                    assert document["choices"][0]["message"]["content"] == MARKER
                    assert document["usage"]["total_tokens"] == 10
            finally:
                connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
