"""Record real hosted CLI requests against local Responses and MCP stubs.

Set PRELOOP_TEST_CODEX_BINARY to the exact hosted Codex release. No model
provider, credentials, database, or external tool execution is used.
"""

import asyncio
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from preloop.agents.codex import CodexAgent
from preloop.agents.codex_model_catalog import (
    CODEX_CATALOG_VERSION,
    custom_model_catalog,
)
from preloop.services.dynamic_mcp_server import UserContext
from preloop.services.initialize_mcp import initialize_mcp_with_tools

REVIEW_TOOLS = {
    "get_issue",
    "get_pull_request",
    "update_pull_request",
    "add_comment",
    "update_comment",
}
SHELL = {"exec_command", "write_stdin"}
RESOURCES = {"list_mcp_resources", "list_mcp_resource_templates", "read_mcp_resource"}
OPTIONAL = {
    "request_user_input",
    "view_image",
    "get_goal",
    "create_goal",
    "update_goal",
    "web_search",
}


def flow_catalog(profile: str, approvals: bool) -> list[dict[str, Any]]:
    """Use real registration and flow filtering, with only CRUD reads mocked."""
    server = initialize_mcp_with_tools()
    context = UserContext(
        user_id="fixture-user",
        account_id="fixture-account",
        username="fixture",
        has_tracker=True,
        tracker_types=["github"],
        flow_id="fixture-flow",
        flow_execution_id="fixture-execution",
        allowed_flow_tools=sorted(REVIEW_TOOLS),
        codex_tools=profile,
    )
    server.set_user_context_provider(lambda: context)
    config = MagicMock(
        tool_name="update_pull_request",
        tool_source="builtin",
        managed_agent_id=None,
        approval_workflow_id="fixture-workflow",
        justification_mode=None,
    )
    with (
        patch(
            "preloop.services.dynamic_fastmcp.get_db",
            side_effect=lambda: iter([MagicMock()]),
        ),
        patch(
            "preloop.services.mcp_tool_discovery._get_proxied_tools_sync",
            return_value=[],
        ),
        patch(
            "preloop.services.dynamic_fastmcp.crud_tool_configuration.get_multi_by_account",
            return_value=[config] if approvals else [],
        ),
        patch(
            "preloop.models.crud.crud_account.get", return_value=MagicMock(meta_data={})
        ),
        patch(
            "preloop.models.crud.crud_approval_request.get_multi_by_execution",
            return_value=[],
        ),
        patch(
            "preloop.models.crud.crud_approval_workflow.get_multi_by_account",
            return_value=[object()] if approvals else [],
        ),
        patch(
            "preloop.models.crud.crud_approval_workflow.get_default", return_value=None
        ),
    ):
        catalog = asyncio.run(server.list_tools())
    expected = REVIEW_TOOLS | (
        {"get_approval_status"} if profile == "default" or approvals else set()
    )
    assert {tool.name for tool in catalog} == expected
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
        }
        for tool in catalog
    ]


@pytest.mark.parametrize("model", ["example/custom", "gpt-5.4"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("profile", ["default", "lean"])
def test_recorded_tool_surface(
    tmp_path: Path,
    model: str,
    native: bool,
    profile: str,
) -> None:
    """Assert every turn's actual definitions, including model-specific tools."""
    record_run(tmp_path, model=model, native=native, profile=profile, approvals=False)


@pytest.mark.parametrize("profile", ["default", "lean"])
def test_recorded_approval_surface(tmp_path: Path, profile: str) -> None:
    """Lean must expose replay when an allowed tool has an approval workflow."""
    record_run(
        tmp_path, model="example/custom", native=False, profile=profile, approvals=True
    )


def record_run(
    tmp_path: Path,
    *,
    model: str,
    native: bool,
    profile: str,
    approvals: bool,
) -> None:
    """Run the real adapter configuration and record two deterministic turns."""
    binary = os.environ.get("PRELOOP_TEST_CODEX_BINARY")
    if not binary:
        pytest.skip("set PRELOOP_TEST_CODEX_BINARY to the pinned hosted CLI")
    version = subprocess.run(
        [binary, "--version"], check=True, capture_output=True, text=True
    )
    assert version.stdout.strip() == f"codex-cli {CODEX_CATALOG_VERSION}"
    catalog = [] if native else flow_catalog(profile, approvals)
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            """Keep fixture traffic out of test output."""

        def do_GET(self) -> None:  # noqa: N802 - stdlib interface
            """The fixture uses stateless streamable HTTP, without an SSE GET."""
            self.send_error(405)

        def do_POST(self) -> None:  # noqa: N802 - stdlib interface
            """Serve MCP discovery and a scripted shell call followed by JSON."""
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/mcp":
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                result = (
                    {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fixture", "version": "1"},
                    }
                    if body["method"] == "initialize"
                    else {"tools": catalog}
                )
                data = json.dumps(
                    {"jsonrpc": "2.0", "id": body["id"], "result": result}
                ).encode()
                content_type = "application/json"
            elif self.path == "/v1/responses":
                requests.append(body)
                if len(requests) == 1:
                    item = {
                        "id": "fc_fixture",
                        "type": "function_call",
                        "call_id": "fixture_shell",
                        "name": "exec_command",
                        "status": "completed",
                        "arguments": json.dumps({"cmd": "printf fixture-inspected"}),
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
                                "text": '{"summary":"fixture complete"}',
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
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": item,
                    },
                    {"type": "response.completed", "response": response},
                ]
                data = "".join(
                    "data: " + json.dumps(event) + "\n\n" for event in events
                ).encode()
                content_type = "text/event-stream"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        agent = CodexAgent({})
        block = agent._build_codex_auth_config(
            model,
            "fixture",
            f"http://127.0.0.1:{server.server_port}/v1",
            attach_mcp=not native,
            codex_tools=profile,
        )
        config = block.split("cat > ~/.codex/config.toml << EOF\n")[1].split("\nEOF")[0]
        config = (
            config.replace("$HOME/.codex", str(tmp_path))
            .replace("$PRELOOP_MCP_URL", f"http://127.0.0.1:{server.server_port}/mcp")
            .replace("$MCP_TOOL_TIMEOUT_SEC", "30")
            .replace("$PRELOOP_CODEX_MCP_AUTH", '"Bearer fixture"')
            .replace("$PRELOOP_CODEX_PROVIDER_AUTH", '"Bearer fixture"')
        )
        (tmp_path / "config.toml").write_text(config)
        if metadata := custom_model_catalog(model, None):
            (tmp_path / "preloop-models.json").write_text(json.dumps(metadata))
        subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
        args = [
            binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--yolo",
            "--model",
            model,
        ]
        if native:
            schema = {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            }
            (tmp_path / "schema.json").write_text(json.dumps(schema))
            args += [
                "--output-schema",
                str(tmp_path / "schema.json"),
                "-o",
                str(tmp_path / "result.json"),
            ]
        args.append("Inspect the fixture using the shell and return a JSON summary.")
        environment = {
            "PATH": os.environ["PATH"],
            "HOME": str(tmp_path),
            "CODEX_HOME": str(tmp_path),
            "FIXTURE_API_KEY": "dummy",
            "PRELOOP_API_TOKEN": "fixture",
            "PRELOOP_DISABLE_TELEMETRY": "true",
        }
        result = subprocess.run(
            args,
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stderr
        assert len(requests) == 2
        assert any(
            "fixture-inspected" in str(item.get("output", ""))
            for item in requests[1]["input"]
            if item.get("type") == "function_call_output"
        )
        if native:
            assert json.loads((tmp_path / "result.json").read_text()) == {
                "summary": "fixture complete"
            }
        else:
            resumed = subprocess.run(
                [
                    binary,
                    "exec",
                    "resume",
                    "--last",
                    "--json",
                    "--skip-git-repo-check",
                    "--yolo",
                    "Return the fixture summary again.",
                ],
                cwd=tmp_path,
                env=environment,
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert resumed.returncode == 0, resumed.stderr
            assert len(requests) == 3
        expected = SHELL | (set() if native else RESOURCES)
        if profile == "default":
            expected |= OPTIONAL
        if model == "example/custom":
            if profile == "default":
                expected |= {"multi_agent_v1"}
            if not native:
                expected |= {"mcp__preloop"}
        else:
            expected |= {"apply_patch"}
            if not native or profile == "default":
                expected |= {"tool_search"}
        for request in requests:
            assert {
                tool.get("name", tool["type"]) for tool in request["tools"]
            } == expected
            if "mcp__preloop" in expected:
                namespace = next(
                    tool
                    for tool in request["tools"]
                    if tool.get("name") == "mcp__preloop"
                )
                assert {tool["name"] for tool in namespace["tools"]} == {
                    tool["name"] for tool in catalog
                }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
