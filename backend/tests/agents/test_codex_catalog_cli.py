"""Optional offline integration with the exact hosted Codex binary.

Set PRELOOP_TEST_CODEX_BINARY to a local Codex 0.153.4 binary. No provider
credentials or external inference are used; Responses is served on localhost.
"""

import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from preloop.agents.codex_model_catalog import custom_model_catalog


@pytest.mark.parametrize("via_role", [False, True])
@pytest.mark.parametrize("effort", ["none", "low", "xhigh", "max"])
def test_pinned_cli_spawns_custom_model_with_effort(
    tmp_path: Path, effort: str, via_role: bool
) -> None:
    binary = os.environ.get("PRELOOP_TEST_CODEX_BINARY")
    if not binary:
        pytest.skip("requires a local pinned Codex binary")
    version = subprocess.run(
        [binary, "--version"], check=True, capture_output=True, text=True
    )
    assert version.stdout.strip() == "codex-cli 0.153.4"
    requests: list[dict[str, Any]] = []
    child_request = Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            if len(requests) > 1:
                if any(
                    item.get("type") == "function_call_output"
                    for item in body.get("input", [])
                ):
                    child_request.wait(timeout=5)
                else:
                    child_request.set()
            if len(requests) == 1:
                item = {
                    "id": "fc_fixture",
                    "type": "function_call",
                    "call_id": "fixture_spawn",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "status": "completed",
                    "arguments": json.dumps(
                        {
                            "message": "Return fixture complete",
                            **(
                                {"agent_type": "fixture"}
                                if via_role
                                else {"reasoning_effort": effort}
                            ),
                        }
                    ),
                }
            else:
                item = {
                    "id": "msg_fixture",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "fixture complete",
                            "annotations": [],
                        }
                    ],
                }
            response = {
                "id": f"resp_{len(requests)}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "output": [item],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "output": [], "status": "in_progress"},
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            data = "".join(
                "data: " + json.dumps(event) + "\n\n" for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "models.json").write_text(
            json.dumps(custom_model_catalog("example/custom", None))
        )
        (tmp_path / "role.toml").write_text(f'model_reasoning_effort = "{effort}"\n')
        role_config = (
            f'\n[agents.fixture]\ndescription = "Fixture role"\nconfig_file = "{tmp_path}/role.toml"\n'
            if via_role
            else ""
        )
        (tmp_path / "config.toml").write_text(
            'model = "example/custom"\nmodel_provider = "fixture"\n'
            f'model_catalog_json = "{tmp_path}/models.json"\n'
            'tools.update_plan.enabled = false\napproval_policy = "never"\n'
            'sandbox_mode = "read-only"\n[model_providers.fixture]\n'
            'name = "Offline fixture"\nwire_api = "responses"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'env_key = "FIXTURE_API_KEY"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            + role_config
        )
        result = subprocess.run(
            [
                binary,
                "exec",
                "--json",
                "--skip-git-repo-check",
                "Use a subagent for this fixture",
            ],
            cwd=tmp_path,
            env={
                "PATH": os.environ["PATH"],
                "CODEX_HOME": str(tmp_path),
                "FIXTURE_API_KEY": "dummy",
            },
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        outputs = [
            item.get("output", "")
            for request in requests
            for item in request.get("input", [])
            if item.get("type") == "function_call_output"
            and item.get("call_id") == "fixture_spawn"
        ]
        assert any('"agent_id"' in output for output in outputs), outputs
        assert any(
            request.get("reasoning", {}).get("effort") == effort for request in requests
        )
        assert "fallback metadata" not in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
