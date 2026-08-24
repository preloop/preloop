"""Codex + MCP canary: full-gateway round-trip guard (incident 2026-08-24).

Two production incidents motivated this suite and neither was catchable by
the existing tests, because none of them put the REAL codex CLI binary in
front of the REAL backend gateway:

1. Codex-runtime MCP tool calls were never routed in production: the codex
   router registers namespace tools only under the (namespace, short-name)
   call shape, while the gateway rendered model tool calls as plain
   ``function_call`` items. Every MCP tool call on the codex runtime died
   with ``unsupported call``.
2. The gateway fix for (1) then regressed codex flows that merely DECLARE
   MCP tools: the first response round-trip broke and agents exited at
   turn 1 without doing anything.

This suite closes that gap deterministically:

* REAL pieces: the full backend HTTP server (``preloop.server``), the
  OpenAI-compatible gateway (``/openai/v1/responses`` including
  ``codex_tool_compat`` translation), the dynamic MCP server
  (``/mcp/v1`` / ``dynamic_fastmcp``), real bearer auth, and the real
  ``codex`` CLI binary as the agent process.
* STUBBED piece: only the UPSTREAM model provider. A scripted
  OpenAI-compatible ``chat/completions`` server plays the model:
  turn 1 = a tool call for the namespaced MCP tool (or a plain text
  message for the regression shape), turn 2 = a final sentinel message.
  No live LLM, no network beyond localhost.

Pass criteria per scenario:

* round trip: codex routes the namespaced call, the MCP server executes
  the builtin tool and returns, the tool output travels back upstream,
  and the run finishes with the completion sentinel.
* turn-1 plain text: with MCP tools declared, a text-only first response
  must pass through untouched and finish with its sentinel (the exact
  shape the regression killed).

The suite skips only when the ``codex`` binary is absent; the CI job
asserts the binary exists before invoking pytest so CI can never skip.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import httpx
import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
# ``INIT_TEST_DATA`` imports ``scripts.init_test_data`` from the repo root.
REPO_ROOT = BACKEND_DIR.parent

CANARY_CALL_ID = "call_canary_1"
SENTINEL_ROUND_TRIP = "CANARY_COMPLETE_TOOL_ROUND_TRIP_OK"
SENTINEL_PLAIN_TEXT = "CANARY_COMPLETE_TURN1_TEXT_OK"
ERROR_NO_TOOLS = "CANARY_ERROR_NO_MCP_SEARCH_TOOL_DECLARED"

# The harmless read-only builtin MCP tool the scripted model calls.
CANARY_TOOL_SHORT_NAME = "search"

CODEX_BIN = shutil.which("codex")

pytestmark = pytest.mark.skipif(
    CODEX_BIN is None,
    reason=(
        "codex CLI binary not found on PATH. The CI job installs it "
        "(npm install -g @openai/codex) and fails if it is missing; this "
        "skip exists only for unrelated local pytest runs."
    ),
)


# ---------------------------------------------------------------------------
# Scripted upstream: the ONLY stubbed component.
# ---------------------------------------------------------------------------


@dataclass
class _UpstreamScript:
    """Mutable state driving and recording the scripted model."""

    mode: str = "tool_call"  # "tool_call" | "plain_text"
    requests: List[Dict[str, Any]] = field(default_factory=list)
    declared_tool_names: List[str] = field(default_factory=list)
    issued_tool_name: Optional[str] = None
    tool_result_content: Optional[str] = None

    def reset(self, mode: str) -> None:
        self.mode = mode
        self.requests.clear()
        self.declared_tool_names.clear()
        self.issued_tool_name = None
        self.tool_result_content = None


def _tool_names(payload: Dict[str, Any]) -> List[str]:
    """Extract declared function-tool names from a chat-completions payload."""
    names: List[str] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if isinstance(function, dict) and function.get("name"):
            names.append(str(function["name"]))
        elif tool.get("name"):
            names.append(str(tool["name"]))
    return names


def _find_canary_tool(names: List[str]) -> Optional[str]:
    """Pick the declared name of the builtin canary tool, however qualified."""
    for name in names:
        if name == CANARY_TOOL_SHORT_NAME or name.endswith(
            f"__{CANARY_TOOL_SHORT_NAME}"
        ):
            return name
    return None


def _tool_result_message(payload: Dict[str, Any]) -> Optional[str]:
    """Return the tool-result content for the canary call, if present."""
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(
                    part.get("text", "") for part in content if isinstance(part, dict)
                )
            return str(content or "")
    return None


def _chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": "chatcmpl-canary",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": "gpt-4o-mini",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _make_handler(script: _UpstreamScript):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _respond(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._respond(
                    404, b'{"error": {"message": "not found"}}', "application/json"
                )
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                payload = {}
            script.requests.append(payload)

            names = _tool_names(payload)
            for name in names:
                if name not in script.declared_tool_names:
                    script.declared_tool_names.append(name)
            tool_result = _tool_result_message(payload)
            if tool_result is not None and script.tool_result_content is None:
                script.tool_result_content = tool_result

            message: Dict[str, Any]
            finish = "stop"
            if script.mode == "tool_call" and tool_result is None:
                target = _find_canary_tool(names)
                if target is None:
                    # Loud, assertable failure: the gateway declared no
                    # usable MCP tool upstream, so the round trip is
                    # impossible. Finishing with a NON-sentinel message
                    # makes the test fail with a precise reason.
                    message = {"role": "assistant", "content": ERROR_NO_TOOLS}
                else:
                    script.issued_tool_name = target
                    message = {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": CANARY_CALL_ID,
                                "type": "function",
                                "function": {
                                    "name": target,
                                    "arguments": json.dumps(
                                        {"query": "codex mcp canary", "limit": 1}
                                    ),
                                },
                            }
                        ],
                    }
                    finish = "tool_calls"
            elif script.mode == "tool_call":
                message = {"role": "assistant", "content": SENTINEL_ROUND_TRIP}
            else:
                message = {"role": "assistant", "content": SENTINEL_PLAIN_TEXT}

            if payload.get("stream"):
                # Mimic the stream shape reasoning models emit through
                # OpenRouter-style upstreams: a role-only delta, reasoning
                # deltas and an empty content delta BEFORE the payload. The
                # staging incident hit exactly such a model; a suspiciously
                # clean stream would under-test the translation layer.
                deltas: List[Dict[str, Any]] = [
                    {"role": "assistant"},
                    {"reasoning": "canary thinking"},
                    {"content": ""},
                ]
                if message.get("tool_calls"):
                    deltas.append({"tool_calls": message["tool_calls"]})
                else:
                    deltas.append({"content": message["content"]})
                events = [_chunk(d) for d in deltas]
                events.append(_chunk({}, finish))
                body = (
                    "".join(f"data: {json.dumps(e)}\n\n" for e in events)
                    + "data: [DONE]\n\n"
                ).encode("utf-8")
                self._respond(200, body, "text/event-stream")
                return

            completion = {
                "id": "chatcmpl-canary",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "gpt-4o-mini",
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": {
                    "prompt_tokens": 7,
                    "completion_tokens": 5,
                    "total_tokens": 12,
                },
            }
            self._respond(
                200, json.dumps(completion).encode("utf-8"), "application/json"
            )

    return _Handler


# ---------------------------------------------------------------------------
# Environment: scripted upstream + REAL backend + REAL codex CLI.
# ---------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass
class _CanaryEnv:
    script: _UpstreamScript
    gateway_base: str
    mcp_url: str
    token: str
    backend_log: Path


@pytest.fixture(scope="module")
def canary_env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_CanaryEnv]:
    if not os.getenv("DATABASE_URL"):
        pytest.skip("DATABASE_URL not set; the canary needs a real database")

    script = _UpstreamScript()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(script))
    upstream_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    backend_port = _free_port()
    log_dir = tmp_path_factory.mktemp("canary")
    backend_log = log_dir / "backend.log"

    env = dict(os.environ)
    env.update(
        {
            # The unit-test conftest exports TESTING=true, which makes the
            # server SKIP the MCP mount and NATS. This canary needs the real
            # production wiring, so force testing mode OFF for the server.
            "TESTING": "false",
            "HOST": "127.0.0.1",
            "PORT": str(backend_port),
            "INIT_DB": "true",
            "INIT_TEST_DATA": "true",
            "PRELOOP_SERVICE_ROLE": "all",
            "PRELOOP_DISABLE_TELEMETRY": "true",
            "SENTRY_DSN": "",
            "PRELOOP_E2E_FAKE_UPSTREAM": f"http://127.0.0.1:{upstream_port}/v1",
            # A placeholder so provider-key guards pass; the scripted
            # upstream ignores auth entirely. Never a real credential.
            "ANTHROPIC_API_KEY": env_placeholder(),
            "PYTHONPATH": os.pathsep.join([str(BACKEND_DIR), str(REPO_ROOT)]),
        }
    )

    log_handle = backend_log.open("wb")
    server_proc = subprocess.Popen(
        [sys.executable, "-m", "preloop.server"],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for_health(f"http://127.0.0.1:{backend_port}", server_proc, backend_log)
        _run_support("tests.e2e_support.seed_gateway_model", env, backend_log)
        token = _run_support(
            "tests.e2e_support.mint_canary_credentials", env, backend_log
        )
        yield _CanaryEnv(
            script=script,
            gateway_base=f"http://127.0.0.1:{backend_port}/openai/v1",
            mcp_url=f"http://127.0.0.1:{backend_port}/mcp/v1",
            token=token,
            backend_log=backend_log,
        )
    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server_proc.kill()
        log_handle.close()
        upstream.shutdown()
        upstream.server_close()


def env_placeholder() -> str:
    """A clearly-fake credential value for guards that only check presence."""
    return "canary-placeholder-not-a-real-key"


def _wait_for_health(
    base: str, proc: subprocess.Popen, log_path: Path, timeout: float = 180.0
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("backend exited during startup:\n" + _tail(log_path))
        try:
            response = httpx.get(f"{base}/api/v1/health", timeout=2.0)
            if response.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(1.0)
    raise RuntimeError("backend never became healthy:\n" + _tail(log_path))


def _run_support(module: str, env: Dict[str, str], log_path: Path) -> str:
    """Run a tests.e2e_support helper as a subprocess; return stdout tail."""
    result = subprocess.run(
        [sys.executable, "-m", module],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{module} failed (rc={result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}\n{_tail(log_path)}"
        )
    lines = [line for line in result.stdout.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _tail(log_path: Path, lines: int = 60) -> str:
    try:
        content = log_path.read_text(errors="replace").splitlines()
    except OSError:
        return "<no backend log>"
    return "\n".join(content[-lines:])


# ---------------------------------------------------------------------------
# The real codex CLI run, configured exactly like production runners.
# ---------------------------------------------------------------------------


def _write_codex_home(home: Path, env_info: _CanaryEnv) -> None:
    """Write config.toml matching ``preloop.agents.codex`` byte for byte in
    shape: provider ``preloop`` speaking the Responses wire API through the
    gateway, plus the ``[mcp_servers.preloop]`` streamable-HTTP entry."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps(
            {"PRELOOP_API_KEY": env_info.token, "OPENAI_API_KEY": env_info.token}
        )
    )
    (home / "config.toml").write_text(
        f"""model_provider = "preloop"
model = "preloop-fake"

rmcp_client = true

[model_providers.preloop]
name = "Preloop"
base_url = "{env_info.gateway_base}"
env_key = "PRELOOP_API_KEY"
wire_api = "responses"

[mcp_servers.preloop]
url = "{env_info.mcp_url}"
bearer_token_env_var = "PRELOOP_API_TOKEN"
tool_timeout_sec = 60
"""
    )


def _run_codex(env_info: _CanaryEnv, tmp_path: Path, prompt: str) -> Tuple[int, str]:
    codex_home = tmp_path / "codex-home"
    workdir = tmp_path / "workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    _write_codex_home(codex_home, env_info)

    env = dict(os.environ)
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            "PRELOOP_API_KEY": env_info.token,
            "OPENAI_API_KEY": env_info.token,
            "PRELOOP_API_TOKEN": env_info.token,
        }
    )
    # Same invocation shape as the production runner script
    # (``preloop.agents.codex``): non-interactive exec, workspace sandbox.
    result = subprocess.run(
        [
            CODEX_BIN,
            "exec",
            "--skip-git-repo-check",
            "--model",
            "preloop-fake",
            "--sandbox",
            "workspace-write",
            "--yolo",
            "-C",
            str(workdir),
            prompt,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


# ---------------------------------------------------------------------------
# Scenario 1: the namespaced MCP tool call must complete a full round trip.
# ---------------------------------------------------------------------------


def test_codex_mcp_tool_round_trip(canary_env: _CanaryEnv, tmp_path: Path) -> None:
    canary_env.script.reset("tool_call")

    returncode, output = _run_codex(
        canary_env,
        tmp_path,
        "canary: use the preloop search tool, then report the result",
    )

    debug = (
        f"\n--- codex rc={returncode} output tail ---\n{output[-4000:]}"
        f"\n--- upstream declared tools ---\n{canary_env.script.declared_tool_names}"
        f"\n--- upstream requests: {len(canary_env.script.requests)} ---"
        f"\n--- backend log tail ---\n{_tail(canary_env.backend_log)}"
    )

    # The gateway must have declared the MCP tool upstream and the scripted
    # model must have been able to call it.
    assert canary_env.script.issued_tool_name is not None, (
        "scripted model found no declared MCP search tool to call" + debug
    )
    # The call must have been routed by codex (not rejected by its router).
    assert "unsupported call" not in output, (
        "codex router rejected the MCP tool call shape the gateway emitted" + debug
    )
    # The tool result must have travelled BACK to the upstream: that is the
    # round-trip proof (codex -> MCP server -> tool output -> next turn).
    assert canary_env.script.tool_result_content is not None, (
        "no tool result ever reached the upstream; the MCP round trip died" + debug
    )
    assert canary_env.script.tool_result_content.strip(), (
        "tool result reached upstream but was empty" + debug
    )
    assert "unsupported call" not in canary_env.script.tool_result_content, (
        "tool result carried a router rejection" + debug
    )
    # Completion confirmation: the final sentinel message came through and
    # codex exited cleanly.
    assert SENTINEL_ROUND_TRIP in output, (
        "run never reached the completion sentinel" + debug
    )
    assert returncode == 0, "codex exec exited non-zero" + debug


# ---------------------------------------------------------------------------
# Scenario 2: turn-1 plain text with MCP tools declared (regression shape).
# ---------------------------------------------------------------------------


def test_codex_turn1_plain_text_passthrough(
    canary_env: _CanaryEnv, tmp_path: Path
) -> None:
    canary_env.script.reset("plain_text")

    returncode, output = _run_codex(
        canary_env,
        tmp_path,
        "canary: reply with a short text answer",
    )

    debug = (
        f"\n--- codex rc={returncode} output tail ---\n{output[-4000:]}"
        f"\n--- upstream declared tools ---\n{canary_env.script.declared_tool_names}"
        f"\n--- upstream requests: {len(canary_env.script.requests)} ---"
        f"\n--- backend log tail ---\n{_tail(canary_env.backend_log)}"
    )

    # The regression shape requires namespace/MCP tools to be DECLARED on
    # the wire; without them the translation layer is inert and this test
    # would prove nothing.
    assert _find_canary_tool(canary_env.script.declared_tool_names), (
        "MCP tools were not declared upstream; scenario precondition failed" + debug
    )
    # The text-only first turn must pass through and complete.
    assert SENTINEL_PLAIN_TEXT in output, (
        "turn-1 plain text response never reached the agent output "
        "(the #289-regression failure shape)" + debug
    )
    assert returncode == 0, "codex exec exited non-zero" + debug
