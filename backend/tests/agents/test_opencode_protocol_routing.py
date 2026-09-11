"""Opt-in real OpenCode wire check against a local, unbilled model fixture."""

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from preloop.agents.opencode import OpenCodeAgent


@pytest.mark.skipif(
    os.environ.get("OPENCODE_PROTOCOL_SMOKE") != "1",
    reason="opt-in local OpenCode 1.18.29 wire test",
)
@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_real_opencode_routes_primary_and_title_with_mixed_inventory(
    tmp_path: Path, protocol: str
) -> None:
    binary = shutil.which("opencode")
    assert binary, "Install OpenCode 1.18.29 for the opt-in smoke test"
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
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
            "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "XDG_STATE_HOME": str(tmp_path / "state"),
        }
    )
    version = subprocess.run(
        [binary, "--version"], env=env, capture_output=True, text=True, check=True
    )
    assert version.stdout.strip() == "1.18.29"
    requests: list[tuple[str, dict[str, Any]]] = []
    alias = "responses-fixture" if protocol == "responses" else "chat-fixture"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            text = "Protocol fixture complete"
            message = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            response = {
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "model": alias,
                "status": "completed",
                "output": [message],
                "usage": {"input_tokens": 10, "output_tokens": 3, "total_tokens": 13},
            }
            self.send_response(200)
            if body.get("stream"):
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                if self.path.endswith("/responses"):
                    events = [
                        {
                            "type": "response.created",
                            "response": {
                                **response,
                                "status": "in_progress",
                                "output": [],
                            },
                        },
                        {
                            "type": "response.output_item.added",
                            "output_index": 0,
                            "item": {**message, "content": [], "status": "in_progress"},
                        },
                        {
                            "type": "response.content_part.added",
                            "item_id": message["id"],
                            "output_index": 0,
                            "content_index": 0,
                            "part": {
                                "type": "output_text",
                                "text": "",
                                "annotations": [],
                            },
                        },
                        {
                            "type": "response.output_text.delta",
                            "item_id": message["id"],
                            "output_index": 0,
                            "content_index": 0,
                            "delta": text,
                        },
                        {
                            "type": "response.output_item.done",
                            "output_index": 0,
                            "item": message,
                        },
                        {"type": "response.completed", "response": response},
                    ]
                    for event in events:
                        self.wfile.write(
                            ("data: " + json.dumps(event) + "\n\n").encode()
                        )
                else:
                    for delta, finish in [
                        ({"role": "assistant", "content": text}, None),
                        ({}, "stop"),
                    ]:
                        chunk = {
                            "id": "chatcmpl_fixture",
                            "object": "chat.completion.chunk",
                            "created": 1,
                            "model": alias,
                            "choices": [
                                {"index": 0, "delta": delta, "finish_reason": finish}
                            ],
                        }
                        self.wfile.write(
                            ("data: " + json.dumps(chunk) + "\n\n").encode()
                        )
                    self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if not self.path.endswith("/responses"):
                    response = {
                        "id": "chatcmpl_fixture",
                        "object": "chat.completion",
                        "created": 1,
                        "model": alias,
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": text},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                self.wfile.write(json.dumps(response).encode())
            self.wfile.flush()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = OpenCodeAgent({})._build_opencode_config(
        alias,
        "custom",
        {
            "model_gateway_enabled": True,
            "model_gateway_provider": "preloop",
            "model_gateway_url": f"http://127.0.0.1:{server.server_port}/openai/v1",
            "model_api_protocol": protocol,
            "authorized_gateway_models": [
                {"alias": "responses-fixture", "api_protocol": "responses"},
                {"alias": "chat-fixture", "api_protocol": "chat_completions"},
            ],
        },
        600000,
    )
    config["mcp"] = {}
    config["provider"]["preloop"]["options"]["apiKey"] = "local-fixture"
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    try:
        result = subprocess.run(
            [
                binary,
                "run",
                "--format",
                "json",
                "Reply with Protocol fixture complete. Do not use tools.",
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
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Protocol fixture complete" in output, output
    assert len(requests) >= 2, (requests, output)  # primary plus title generation
    assert any(not body.get("tools") for _, body in requests), requests
    (tmp_path / "wire-summary.json").write_text(
        json.dumps(
            {
                "version": version.stdout.strip(),
                "protocol": protocol,
                "config": config,
                "requests": [
                    {
                        "path": path,
                        "model": body["model"],
                        "stream": body.get("stream", False),
                        "tool_count": len(body.get("tools", [])),
                    }
                    for path, body in requests
                ],
            },
            indent=2,
        )
        + "\n"
    )
    expected = "/responses" if protocol == "responses" else "/chat/completions"
    assert all(path == "/openai/v1" + expected for path, _ in requests), requests
    assert all(body["model"] == alias for _, body in requests)
    primary = next(body for _, body in requests if body.get("tools"))
    assert primary["stream"] is True
    if protocol == "responses":
        assert "input" in primary and "messages" not in primary
        assert all(
            tool["type"] == "function" and "name" in tool for tool in primary["tools"]
        )
    else:
        assert "messages" in primary and "input" not in primary
        assert all(
            tool["type"] == "function" and "function" in tool
            for tool in primary["tools"]
        )
