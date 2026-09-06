"""Opt-in shipped-image smoke with a local deterministic model HTTP fixture.

Run with PRELOOP_DISABLE_TELEMETRY=true NATIVE_SESSION_IMAGE_SMOKE=1.
No provider credential or production service is used. The mock model asserts
that native resume sends the first turn's seeded context on the second turn.
"""

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from preloop.agents.session_manifest import (
    select_session_files,
    restore_session,
    capture_codex_session_id,
)

OPENCODE_IMAGE = "docker/sandbox-templates@sha256:2611218a51758e564159bb8ef895bdcb9a2c4b143305bbe340161966d7965234"


@pytest.mark.skipif(
    os.environ.get("NATIVE_SESSION_IMAGE_SMOKE") != "1",
    reason="opt-in local Docker image smoke",
)
def test_opencode_native_second_turn_preserves_seeded_fact(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "The seeded fact is apricot."}, None),
                ({}, "stop"),
            ]:
                chunk = {
                    "id": "chatcmpl-local",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    config = {
        "provider": {
            "fixture": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "fixture",
                "options": {
                    "baseURL": f"http://host.docker.internal:{server.server_port}/v1",
                    "apiKey": "local-fixture",
                },
                "models": {"fixture": {"name": "fixture"}},
            }
        },
        "permission": "deny",
        "autoupdate": False,
    }

    def run(home: Path, prompt: str, session_id: str | None = None) -> str:
        home.mkdir(parents=True, exist_ok=True)
        command = [
            "docker",
            "run",
            "--rm",
            "--user",
            "0",
            "--entrypoint",
            "opencode",
            "-v",
            f"{home}:/state",
            "-e",
            "XDG_DATA_HOME=/state",
            "-e",
            "PRELOOP_DISABLE_TELEMETRY=true",
            "-e",
            "OPENCODE_DISABLE_MODELS_FETCH=true",
            "-e",
            "OPENCODE_CONFIG_CONTENT=" + json.dumps(config),
            OPENCODE_IMAGE,
            "run",
            "--model",
            "fixture/fixture",
            "--format",
            "json",
        ]
        if session_id:
            command += ["--session", session_id]
        result = subprocess.run(
            command + [prompt], capture_output=True, text=True, timeout=90, check=False
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    try:
        first = run(
            tmp_path / "first", "Remember the seeded fact apricot and reply once."
        )
        events = [
            json.loads(line) for line in first.splitlines() if line.startswith("{")
        ]
        session_id = next(
            event["sessionID"] for event in events if event.get("sessionID")
        )
        files = select_session_files(
            tmp_path / "first/opencode", "opencode", session_id
        )
        restore_session(files, tmp_path / "second/opencode")
        requests.clear()
        second = run(
            tmp_path / "second",
            "What is the seeded fact from our previous turn?",
            session_id,
        )
        assert session_id in second
        assert requests
        assert "apricot" in json.dumps(requests[0])
        import sqlite3

        with sqlite3.connect(tmp_path / "second/opencode/opencode.db") as db:
            assert list(db.execute("SELECT id FROM session")) == [(session_id,)]
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


CODEX_IMAGE = "ghcr.io/openai/codex-universal@sha256:86f25fd11da9839ae4d75749ae95782f3304d95caab8f7592f92bc2b9ab6e970"


@pytest.mark.skipif(
    os.environ.get("NATIVE_SESSION_IMAGE_SMOKE") != "1",
    reason="opt-in local Docker image smoke",
)
def test_codex_native_second_turn_preserves_seeded_fact(tmp_path: Path) -> None:
    import shlex

    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            item = {
                "id": "msg_fixture",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": "The seeded fact is apricot.",
                        "annotations": [],
                    }
                ],
            }
            response = {
                "id": "resp_fixture",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "fixture",
                "output": [item],
                "usage": {"input_tokens": 10, "output_tokens": 8, "total_tokens": 18},
            }
            events = [
                {
                    "type": "response.created",
                    "response": {**response, "status": "in_progress", "output": []},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress", "content": []},
                },
                {
                    "type": "response.content_part.added",
                    "item_id": "msg_fixture",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_fixture",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": "The seeded fact is apricot.",
                },
                {
                    "type": "response.output_text.done",
                    "item_id": "msg_fixture",
                    "output_index": 0,
                    "content_index": 0,
                    "text": "The seeded fact is apricot.",
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            for index, event in enumerate(events):
                event["sequence_number"] = index
                self.wfile.write(
                    (
                        "event: "
                        + event["type"]
                        + "\ndata: "
                        + json.dumps(event)
                        + "\n\n"
                    ).encode()
                )
            self.wfile.flush()

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def run(home: Path, prompt: str, sid: str | None = None) -> str:
        home.mkdir(parents=True, exist_ok=True)
        arguments = ["codex", "exec"]
        if sid:
            arguments += ["resume", sid]
        arguments += [
            "--skip-git-repo-check",
            "--json",
            "--model",
            "fixture",
            "--yolo",
            "-c",
            'model_provider="fixture"',
            "-c",
            'model_providers.fixture.name="fixture"',
            "-c",
            f'model_providers.fixture.base_url="http://host.docker.internal:{server.server_port}/v1"',
            "-c",
            'model_providers.fixture.wire_api="responses"',
            prompt,
        ]
        shell = "npm install -g @openai/codex@0.153.4 >/dev/null && " + shlex.join(
            arguments
        )
        command = [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{home}:/state",
            "-e",
            "CODEX_HOME=/state",
            "-e",
            "PRELOOP_DISABLE_TELEMETRY=true",
            CODEX_IMAGE,
            "-c",
            shell,
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120, check=False
        )
        assert result.returncode == 0, result.stderr + result.stdout
        return result.stdout

    try:
        first = run(
            tmp_path / "first", "Remember the seeded fact apricot and reply once."
        )
        events = [
            json.loads(line) for line in first.splitlines() if line.startswith("{")
        ]
        sid = next(
            event["thread_id"]
            for event in events
            if event.get("type") == "thread.started"
        )
        assert capture_codex_session_id(tmp_path / "first/sessions") == sid
        files = select_session_files(tmp_path / "first/sessions", "codex", sid)
        restore_session(files, tmp_path / "second/sessions")
        requests.clear()
        second = run(
            tmp_path / "second", "What was the seeded fact in our previous turn?", sid
        )
        assert sid in second
        assert requests and "apricot" in json.dumps(requests[0])
        assert len(list((tmp_path / "second/sessions").rglob("rollout-*.jsonl"))) == 1
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
