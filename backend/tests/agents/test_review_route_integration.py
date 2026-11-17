"""Native review completion through a real gateway and an offline route replica."""

import json
import os
import shlex
import subprocess
import sys
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.codex_model_catalog import custom_model_catalog
from preloop.services.model_gateway_auth import ModelGatewayAuthContext
from preloop.services.openai_gateway import OpenAIGatewayService
from preloop.services.review_diff import compute_diff
from preloop.services.review_integrity import ObservationCounter, evaluate
from preloop.utils.execve_limits import PROMPT_FILE_PATH

# Minimal offline CLI harness: run actual shell calls from gateway Responses,
# emit the native event shape, and capture the model's final answer unchanged.
HARNESS = r"""import json, os, subprocess, sys, urllib.request
args = sys.argv
schema = json.load(open(args[args.index("--output-schema") + 1]))
out = args[args.index("--output-last-message") + 1]
payload = {"model": "route", "input": [{"role": "user", "content": sys.stdin.read()}],
    "stream": True, "tools": [{"type": "function", "name": "exec_command",
    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}}],
    "text": {"format": {"type": "json_schema", "name": "review", "strict": True, "schema": schema}},
    "include": ["reasoning.encrypted_content"]}
while True:
    request = urllib.request.Request(os.environ["TEST_GATEWAY_URL"], json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "codex_cli_rs/0.153.4"})
    response = urllib.request.urlopen(request).read().decode()
    events = [json.loads(line[6:]) for line in response.splitlines() if line.startswith("data: {")]
    done = next(e["response"] for e in events if e["type"] == "response.completed")
    payload["input"].extend(done["output"])
    calls = [i for i in done["output"] if i["type"] == "function_call"]
    if not calls:
        text = "".join(c["text"] for i in done["output"] if i["type"] == "message" for c in i["content"])
        open(out, "w").write(text)
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
        break
    for call in calls:
        command = json.loads(call["arguments"])["cmd"]
        result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        print(json.dumps({"type": "item.completed", "item": {"type": "command_execution",
            "id": call["call_id"], "command": command, "exit_code": result.returncode,
            "aggregated_output": result.stdout}}), flush=True)
        payload["input"].append({"type": "function_call_output", "call_id": call["call_id"], "output": result.stdout})
"""


def fixture_repository(tmp_path: Path) -> tuple[Path, str, str]:
    """Create two actual revisions with a reviewable defect."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(repo), *args], text=True
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Jane Doe")
    git("config", "user.email", "jane@example.com")
    (repo / "divide.py").write_text("def divide(a, b):\n    return a / b\n")
    (repo / "README.md").write_text("Division helper.\n")
    git("add", ".")
    git("commit", "-qm", "Base")
    base = git("rev-parse", "HEAD")
    (repo / "README.md").write_text("Division returns zero when the divisor is zero.\n")
    git("commit", "-qam", "Document division")
    return repo, base, git("rev-parse", "HEAD")


@pytest.mark.parametrize("harness", ["stub", "pinned"])
def test_route_reaches_a_real_review_after_two_early_answers(
    tmp_path: Path,
    harness: str,
) -> None:
    """Exercise prompt, continuations, wire formatting, tools, replay and gate."""
    binary = os.environ.get("PRELOOP_TEST_CODEX_BINARY")
    if harness == "pinned" and not binary:
        pytest.skip("set PRELOOP_TEST_CODEX_BINARY for the additional pinned CLI run")
    repo, base, head = fixture_repository(tmp_path)
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            key: {"type": "string"}
            for key in ("review", "head_sha", "base_sha", "status")
        },
        "required": [
            "review",
            "head_sha",
            "base_sha",
            "status",
            "success",
            "verdict",
            "findings",
        ],
    }
    schema["properties"].update(
        {
            "success": {"type": "boolean"},
            "verdict": {"type": "string", "enum": ["pass", "fail", "error"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        key: {"type": "string"}
                        for key in (
                            "problem",
                            "evidence",
                            "location",
                            "fix",
                            "severity",
                        )
                    },
                    "required": ["problem", "evidence", "location", "fix", "severity"],
                },
            },
        }
    )
    context = {
        "prompt": f"Review the change. Bound head_sha: {head}; bound base_sha: {base}; base ref: main. Read git diff {base}...{head} and changed files.",
        "agent_config": {"native_result": True},
        "result_schema": schema,
        "git_clone_config": {
            "publication_mode": "read_only",
            "repositories": [{"clone_path": str(repo)}],
        },
        "read_only_head_sha": head,
        "read_only_base_sha": base,
        "read_only_base_ref": "main",
    }
    upstream: list[dict[str, Any]] = []
    received: list[dict[str, Any]] = []
    sessions = 0
    model = SimpleNamespace(
        id="model-fixture",
        provider_name="openrouter",
        model_identifier="deepseek/deepseek-v4-flash-0731",
        api_endpoint=None,
    )
    result = tmp_path / "result.json"
    log = tmp_path / "attempt.log"
    all_log = tmp_path / "output.log"

    def route(
        service: OpenAIGatewayService,
        ai_model: Any,
        *,
        messages: list,
        payload: dict,
        **kwargs: Any,
    ) -> Any:
        nonlocal sessions
        wire = service._build_completion_kwargs(
            ai_model, messages=messages, payload=payload, stream=True, provider="openai"
        )
        upstream.append(wire)
        tool_outputs = [m["content"] for m in messages if m.get("role") == "tool"]
        if not tool_outputs:
            sessions += 1
        if (
            sessions <= 2
            or wire.get("response_format", {}).get("type") == "json_schema"
        ):
            document = {
                "review": "starting",
                "head_sha": head,
                "base_sha": head,
                "status": "success",
                "success": True,
                "verdict": "pass",
                "findings": [],
            }
            return iter(
                [
                    {"choices": [{"delta": {"content": json.dumps(document)}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
            )
        calls = []
        if not tool_outputs:
            assert "response_format" not in wire, service._client_identity_headers
            commands = ["ls", "git rev-parse HEAD"]
        elif not any("diff --git" in str(output) for output in tool_outputs):
            assert wire["response_format"] == {"type": "json_object"}
            assert any(
                "reasoning_content" in m
                for m in wire["messages"]
                if m.get("tool_calls")
            )
            commands = [f"git diff {base}...{head} && cat divide.py"]
        else:
            assert any("return a / b" in str(output) for output in tool_outputs)
            assert any(base in str(m) and head in str(m) for m in messages)
            document = {
                "review": "README.md incorrectly promises zero for a zero divisor. divide.py uses a / b and raises ZeroDivisionError; correct the documentation or implement the promised behavior.",
                "head_sha": head,
                "base_sha": base,
                "status": "success",
                "success": True,
                "verdict": "fail",
                "findings": [
                    {
                        "problem": "The zero-divisor contract contradicts the implementation.",
                        "evidence": "divide.py returns a / b, which raises ZeroDivisionError.",
                        "location": "README.md:1",
                        "severity": "LOW",
                        "fix": "Document the exception or implement the promised zero result.",
                    }
                ],
            }
            return iter(
                [
                    {"choices": [{"delta": {"content": json.dumps(document)}}]},
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
                ]
            )
        for index, command in enumerate(commands):
            calls.append(
                {
                    "index": index,
                    "id": f"call_{len(upstream)}_{index}",
                    "type": "function",
                    "function": {
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": command}),
                    },
                }
            )
        return iter(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "reasoning_content": "Inspect the bound change.",
                                "tool_calls": calls,
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
        )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.append(payload)
            service = OpenAIGatewayService(
                MagicMock(),
                ModelGatewayAuthContext(
                    token="synthetic", user=SimpleNamespace(id="u", account_id="a")
                ),
                client_identity_headers=dict(self.headers),
            )
            data = "".join(service.stream_response(payload)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    with ExitStack() as stack:
        for name in (
            "_check_budget",
            "_emit_gateway_request_started",
            "_record_gateway_request",
            "_defer_stream_record",
            "_finish_stream_generator",
        ):
            stack.enter_context(
                patch.object(OpenAIGatewayService, name, return_value=None)
            )
        stack.enter_context(
            patch.object(
                OpenAIGatewayService, "_resolve_requested_model", return_value=model
            )
        )
        stack.enter_context(
            patch.object(OpenAIGatewayService, "_open_upstream_stream", route)
        )
        stack.enter_context(
            patch(
                "preloop.services.openai_gateway.should_use_responses_passthrough",
                return_value=False,
            )
        )
        secrets = stack.enter_context(
            patch("preloop.services.openai_gateway.get_secret_service")
        )
        secrets.return_value.resolve_ai_model_credentials.return_value = (
            SimpleNamespace(credential_type="api_key", value="synthetic")
        )
        thread.start()
        try:
            bindir = tmp_path / "bin"
            bindir.mkdir()
            cli = bindir / "codex"
            if harness == "stub":
                cli.write_text(f"#!{sys.executable}\n" + HARNESS)
            else:
                assert (
                    subprocess.check_output([binary, "--version"], text=True).strip()
                    == "codex-cli 0.153.4"
                )
                cli.write_text(f'#!/bin/sh\nexec {shlex.quote(binary)} "$@"\n')
            cli.chmod(0o755)
            config = tmp_path / "codex-home"
            config.mkdir()
            (config / "models.json").write_text(
                json.dumps(custom_model_catalog("route", None))
            )
            (config / "config.toml").write_text(
                'model = "route"\nmodel_provider = "fixture"\n'
                f'model_catalog_json = "{config}/models.json"\n'
                '[model_providers.fixture]\nname = "Offline fixture"\nwire_api = "responses"\n'
                f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
                'env_key = "TEST_PROVIDER_KEY"\nrequest_max_retries = 0\nstream_max_retries = 0\n'
            )
            prompt = tmp_path / "prompt"
            prompt.write_text(context["prompt"])
            schema_file = tmp_path / "schema.json"
            schema_file.write_text(json.dumps(schema))
            block = CodexAgent({})._review_inspection_continuation_block(
                context, event_filter="cat", model="route", sandbox_flags="--yolo"
            )
            for original, target in {
                "/workspace/result.json": result,
                "/tmp/preloop-native-result-schema.json": schema_file,
                "/tmp/preloop-agent-attempt.log": log,
                "/tmp/preloop-agent-output.log": all_log,
                "/tmp/preloop-review-continuation.txt": tmp_path / "continuation",
                PROMPT_FILE_PATH: prompt,
            }.items():
                block = block.replace(original, str(target))
            script = (
                f"set -e\nPRELOOP_AS_AGENT=\ncodex exec --json --yolo --skip-git-repo-check --output-schema {schema_file} --output-last-message {result} < {prompt} | tee {log} {all_log}\nCODEX_EXIT_CODE=0\n"
                + block
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                cwd=repo,
                env={
                    "PATH": f"{bindir}:{Path(sys.executable).parent}:"
                    + os.environ["PATH"],
                    "PRELOOP_DISABLE_TELEMETRY": "true",
                    "CODEX_HOME": str(config),
                    "TEST_PROVIDER_KEY": "synthetic",
                    "TEST_GATEWAY_URL": f"http://127.0.0.1:{server.server_port}/v1/responses",
                },
                text=True,
                capture_output=True,
                timeout=60,
            )
            assert proc.returncode == 0, proc.stderr
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    document = json.loads(result.read_text())
    diff = compute_diff(str(repo), base, head)["diff"]
    counter = ObservationCounter(
        binding={"head_sha": head, "base_sha": base}, diff=diff
    )
    for line in all_log.read_text().splitlines():
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        item = event.get("item", {})
        counter.add(
            {
                "observations": [
                    {
                        "source": "native",
                        "kind": "command",
                        "event": event["type"],
                        "item_id": item.get("id"),
                        "value": item.get("command"),
                        "exit_code": item.get("exit_code"),
                    }
                ]
            }
        )
    outcome = evaluate(
        SimpleNamespace(
            git_clone_config=context["git_clone_config"], result_schema=schema
        ),
        result=document,
        binding={"head_sha": head, "base_sha": base},
        diff=diff,
        inspection=counter.counts(),
    )
    assert sessions == 3 and len(upstream) == 5
    assert all(p["text"]["format"]["type"] == "json_schema" for p in received)
    assert document["head_sha"] == head and document["base_sha"] == base
    assert "ZeroDivisionError" in document["review"]
    assert document["findings"][0]["location"] == "README.md:1"
    assert outcome["status"] == "verified", outcome
