"""Run the generated wrapper from a Git checkout with a real pinned OpenCode CLI."""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from preloop.agents.opencode import OpenCodeAgent


@pytest.mark.skipif(
    os.environ.get("OPENCODE_WRAPPER_SMOKE") != "1",
    reason="opt-in local Docker test with pinned OpenCode 1.18.29",
)
@pytest.mark.parametrize("alias", ["example-vendor/unknown-fixture-9", "zai/glm-5.3"])
def test_generated_wrapper_loads_custom_provider_from_nested_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, alias: str
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - standard HTTP handler API
            self.send_error(405)

        def do_POST(self) -> None:  # noqa: N802 - standard HTTP handler API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/mcp":
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = (
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                    if body.get("method") == "initialize"
                    else {"tools": []}
                )
                data = json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "FLOW_EXECUTION_SUCCESS"}, None),
                ({}, "stop"),
            ]:
                chunk = {
                    "id": "chatcmpl-fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": alias,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    endpoint = f"http://host.docker.internal:{server.server_port}"
    agent = OpenCodeAgent({})
    # Replace external git cloning only. Config generation, workspace selection,
    # CLI installation/invocation, log filtering and exit handling remain real.
    monkeypatch.setattr(
        agent,
        "_prepare_git_clone_command",
        lambda _: "mkdir -p /workspace/workspace\ngit init -q /workspace/workspace\ncd /workspace",
    )
    context = {
        "prompt": "Reply with FLOW_EXECUTION_SUCCESS and stop. Do not use tools.",
        "opencode_model": alias,
        "model_provider": "preloop",
        "model_gateway_enabled": True,
        "model_gateway_provider": "preloop",
        "model_gateway_url": endpoint,
        "execution_id": "local-fixture",
        "flow_name": "local-fixture",
        "completion_nudge_enabled": False,
        "git_clone_config": {
            "enabled": True,
            "repositories": [{"clone_path": "workspace"}],
        },
    }
    repo_config = tmp_path / "repository-opencode.json"
    repo_config.write_text('{"instructions": []}\n')
    script = tmp_path / "wrapper.sh"
    script.write_text(agent._build_opencode_script(context))
    image = os.environ.get(
        "OPENCODE_WRAPPER_IMAGE",
        "docker/sandbox-templates@sha256:2611218a51758e564159bb8ef895bdcb9a2c4b143305bbe340161966d7965234",
    )
    try:
        result = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--user",
                "0",
                "--entrypoint",
                "/bin/bash",
                "-v",
                f"{script}:/run/preloop-wrapper.sh:ro",
                "-v",
                f"{repo_config}:/workspace/workspace/opencode.json:ro",
                "-e",
                "PRELOOP_DISABLE_TELEMETRY=true",
                "-e",
                "OPENCODE_DISABLE_MODELS_FETCH=true",
                "-e",
                "OPENAI_API_KEY=local-fixture",
                "-e",
                "PRELOOP_API_TOKEN=local-fixture",
                "-e",
                f"PRELOOP_MCP_URL={endpoint}/mcp",
                image,
                "/run/preloop-wrapper.sh",
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        output = result.stdout + result.stderr
        (tmp_path / "wrapper-output.log").write_text(output)
        assert "Agent working directory: /workspace/workspace" in output
        assert requests, output
        assert result.returncode == 0, output
        assert all(request["model"] == alias for request in requests)
        (tmp_path / "model-wire-aliases.json").write_text(
            json.dumps([request["model"] for request in requests])
        )
        assert repo_config.read_text() == '{"instructions": []}\n'
        assert "ProviderModelNotFoundError" not in output
        assert "FLOW_EXECUTION_SUCCESS" in output
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


@pytest.mark.parametrize("confirmation_nudge", [False, True])
def test_generated_config_path_is_inherited_by_every_invocation(
    confirmation_nudge: bool,
) -> None:
    context = {
        "prompt": "fixture",
        "opencode_model": "example-vendor/unknown-fixture-9",
        "model_provider": "preloop",
        "confirmation_nudge": confirmation_nudge,
        "completion_nudge_enabled": True,
        "trigger_event_data": {
            "_resume": {
                "cli_session": {
                    "agent_type": "opencode",
                    "session_id": "ses_fixture1234",
                }
            }
        },
    }
    script = OpenCodeAgent({})._build_opencode_script(context)
    export = "export OPENCODE_CONFIG=/workspace/opencode.json"
    assert script.count(export) == 1
    assert script.index("OPENCODE_CONFIG_EOF\n") < script.index(export)
    assert script.index(export) < script.index("opencode run $OPENCODE_RESUME_ARGS")
    assert script.index(export) < script.index(
        "$PRELOOP_NUDGE_TIMEOUT opencode run --session"
    )
