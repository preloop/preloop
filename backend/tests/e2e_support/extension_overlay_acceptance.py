"""Opt-in AC11 local-stack acceptance with real CLI, PostgreSQL and Claude Code.

protects=rendered drop-in rejects an unapproved plugin and marketplace while the
managed Preloop hook runs; fails_when=any of these observations differs;
no_claim=macOS file reads are redirected into a scratch home using dyld and an
ad-hoc-signed copy of the unchanged vendor executable code. Synthetic identity,
permission verdict and Anthropic stream replace authentication and model calls.
This does not test MDM delivery, vendor-console delivery (11a), or tamper resistance.

Run with PRELOOP_DISABLE_TELEMETRY=true, an isolated local DATABASE_URL containing
extension_inventory, and PYTHONPATH=backend. Pass --preloop and --claude binaries.
"""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import StreamingResponse
import uvicorn

from preloop.api.endpoints import extension_policy, managed_bundle
from tests.e2e_support.extension_server import create_fixture_app


def write_json(path: Path, value: Any) -> None:
    """Write synthetic fixture JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n")


def run(argv: list[str], *, root: Path, env: dict[str, str], expected: int = 0) -> str:
    """Execute locally, retaining only synthetic output on failure."""
    result = subprocess.run(
        argv, cwd=root, env=env, text=True, capture_output=True, timeout=90
    )
    output = (result.stdout + result.stderr).replace(str(root), "$SCRATCH")
    if result.returncode != expected:
        raise AssertionError(
            f"{Path(argv[0]).name} {argv[1:3]} exit {result.returncode}: {output}"
        )
    return output


def main() -> None:
    """Exercise the complete endpoint-file channel without a real provider."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--preloop", type=Path, required=True)
    parser.add_argument("--claude", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("PRELOOP_DISABLE_TELEMETRY") != "true":
        raise RuntimeError("Disable test telemetry first")
    preloop, claude = str(args.preloop.resolve()), str(args.claude.resolve())
    app = create_fixture_app()
    app.include_router(extension_policy.router, prefix="/api/v1")
    app.include_router(managed_bundle.router, prefix="/api/v1")
    hook_calls: list[dict[str, Any]] = []

    @app.post("/api/v1/agents/permission-check")
    async def permission(request: Request) -> dict[str, str]:
        """Record the real Preloop hook; substitute only the server verdict."""
        hook_calls.append(await request.json())
        return {"decision": "allow", "reason": "Synthetic acceptance verdict"}

    @app.post("/v1/messages/count_tokens")
    async def count_tokens() -> dict[str, int]:
        """Keep token counting local."""
        return {"input_tokens": 1}

    @app.post("/v1/messages")
    async def messages(request: Request) -> StreamingResponse:
        """Return one harmless tool call, then text; no external model is called."""
        body = await request.json()
        has_result = any(
            isinstance(part, dict) and part.get("type") == "tool_result"
            for message in body.get("messages", [])
            for part in (
                message.get("content", [])
                if isinstance(message.get("content"), list)
                else []
            )
        )
        content = (
            {"type": "text", "text": "Hook acceptance complete."}
            if has_result
            else {
                "type": "tool_use",
                "id": "toolu_synthetic",
                "name": "Bash",
                "input": {"command": "printf overlay-hook-proof"},
            }
        )
        message = {
            "id": "msg_synthetic",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "claude-sonnet-4-6"),
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
        events = [
            ("message_start", {"type": "message_start", "message": message}),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {**content, "text": ""}
                    if has_result
                    else {**content, "input": {}},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": content["text"]}
                    if has_result
                    else {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(content["input"]),
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {
                        "stop_reason": "end_turn" if has_result else "tool_use",
                        "stop_sequence": None,
                    },
                    "usage": {"output_tokens": 20},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
        return StreamingResponse(
            iter(
                [
                    f"event: {name}\ndata: {json.dumps(value)}\n\n"
                    for name, value in events
                ]
            ),
            media_type="text/event-stream",
        )

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [listener]}, daemon=True
    )
    thread.start()
    try:
        for _ in range(200):
            if server.started:
                break
            time.sleep(0.025)
        assert server.started
        base = f"http://127.0.0.1:{port}"
        with tempfile.TemporaryDirectory(prefix="extension-overlay-") as scratch:
            root = Path(scratch)
            home = root / "home"
            managed = home / "managed"
            drop = managed / "managed-settings.d"
            drop.mkdir(parents=True)
            env = {
                "HOME": str(home),
                "PATH": os.environ["PATH"],
                "PRELOOP_DISABLE_TELEMETRY": "true",
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CONFIG_DIR": str(home / ".claude"),
                "ANTHROPIC_BASE_URL": base,
                "ANTHROPIC_API_KEY": "synthetic-model-key",
                "PRELOOP_TEST_MANAGED_ROOT": str(managed),
            }
            # The signed release blocks dyld injection. Re-sign only this scratch
            # copy; every executable code section remains the vendor's release.
            isolated = root / "claude"
            shutil.copy2(claude, isolated)
            run(
                ["/usr/bin/codesign", "--force", "--sign", "-", str(isolated)],
                root=root,
                env=env,
            )
            resource = run(
                ["/usr/bin/clang", "-print-resource-dir"], root=root, env=env
            ).strip()
            sdk = run(["/usr/bin/xcrun", "--show-sdk-path"], root=root, env=env).strip()
            shim = root / "managed_redirect.dylib"
            run(
                [
                    "/usr/bin/clang",
                    "-dynamiclib",
                    "-nostdinc",
                    "-isystem",
                    sdk + "/usr/include",
                    "-isystem",
                    resource + "/include",
                    "-o",
                    str(shim),
                    str(
                        Path(__file__)
                        .with_name("managed_settings_redirect.c")
                        .resolve()
                    ),
                ],
                root=root,
                env=env,
            )
            env["DYLD_INSERT_LIBRARIES"] = str(shim)
            version = run([str(isolated), "--version"], root=root, env=env).strip()
            market = root / "market"
            write_json(
                market / ".claude-plugin/marketplace.json",
                {
                    "name": "example",
                    "owner": {"name": "Example"},
                    "plugins": [
                        {"name": name, "source": "./" + name}
                        for name in ["unapproved", "approved"]
                    ],
                },
            )
            for name in ["unapproved", "approved"]:
                write_json(
                    market / name / ".claude-plugin/plugin.json",
                    {"name": name, "version": "1.0.0"},
                )
            run(
                [str(isolated), "plugin", "marketplace", "add", str(market)],
                root=root,
                env=env,
            )
            # Control proves the plugin exists and can install without the policy.
            run(
                [str(isolated), "plugin", "install", "unapproved@example"],
                root=root,
                env=env,
            )
            run(
                [str(isolated), "plugin", "uninstall", "unapproved@example"],
                root=root,
                env=env,
            )
            hook_bundle = root / "hook-bundle"
            run(
                [
                    preloop,
                    "agents",
                    "managed-config",
                    "claude-code",
                    "--output",
                    str(hook_bundle),
                    "--platform",
                    "darwin",
                    "--cli-path",
                    preloop,
                ],
                root=root,
                env=env,
            )
            with httpx.Client(
                base_url=base,
                headers={"Authorization": "Bearer synthetic-extension-identity"},
            ) as client:
                client.post(
                    "/api/v1/agents/managed-config/bundles",
                    json=json.loads((hook_bundle / "manifest.json").read_text()),
                ).raise_for_status()
                client.put(
                    "/api/v1/agents/extension-approvals/claude_code/plugin/unapproved@example",
                    json={"approval_state": "unapproved"},
                ).raise_for_status()
                client.put(
                    "/api/v1/agents/extension-policy/claude_code",
                    headers={"If-Match": "0"},
                    json={
                        "options": {
                            "block_unapproved_plugins": True,
                            "restrict_marketplaces": True,
                            "managed_hooks_only": True,
                        },
                        "marketplaces": [
                            {
                                "name": "example",
                                "state": "allowed",
                                "source": {"source": "directory", "path": str(market)},
                            }
                        ],
                    },
                ).raise_for_status()
                response = client.get(
                    "/api/v1/agents/extension-policy/claude_code/managed-settings?platform=darwin"
                )
                response.raise_for_status()
                write_json(root / "response.json", response.json())
            bundle = root / "bundle"
            run(
                [
                    preloop,
                    "agents",
                    "managed-config",
                    "claude-code-extensions",
                    "--from-file",
                    str(root / "response.json"),
                    "--output",
                    str(bundle),
                ],
                root=root,
                env=env,
            )
            assert (
                bundle / "50-preloop-extensions.json"
            ).read_bytes() == response.json()["settings_json"].encode()
            shutil.copyfile(
                bundle / "50-preloop-extensions.json",
                drop / "50-preloop-extensions.json",
            )
            shutil.copyfile(
                hook_bundle / "managed-settings.json", managed / "managed-settings.json"
            )
            denied = run(
                [str(isolated), "plugin", "install", "unapproved@example"],
                root=root,
                env=env,
                expected=1,
            )
            assert "blocked by your organization's policy" in denied, denied
            run(
                [str(isolated), "plugin", "install", "approved@example"],
                root=root,
                env=env,
            )
            available = run(
                [str(isolated), "plugin", "list", "--available", "--json"],
                root=root,
                env=env,
            )
            catalog = json.loads(available)
            assert all(
                item["id"] != "unapproved@example" for item in catalog["installed"]
            ), available
            blocked = run(
                [
                    str(isolated),
                    "plugin",
                    "marketplace",
                    "add",
                    "https://example.com/unapproved.git",
                ],
                root=root,
                env=env,
                expected=1,
            )
            assert (
                "policy" in blocked.lower() or "strictKnownMarketplaces" in blocked
            ), blocked
            write_json(
                home / ".preloop/agents/synthetic/permission_hook.json",
                {
                    "base_url": base,
                    "token": "synthetic-hook-key",
                    "source": "claude_code",
                },
            )
            session = run(
                [
                    str(isolated),
                    "-p",
                    "Run the harmless Bash command once.",
                    "--model",
                    "claude-sonnet-4-6",
                    "--tools",
                    "Bash",
                    "--max-turns",
                    "3",
                    "--output-format",
                    "json",
                ],
                root=root,
                env=env,
            )
            assert any(call.get("tool_name") == "Bash" for call in hook_calls), session
            print(
                json.dumps(
                    {
                        "claude_code_version": version,
                        "plugin_install_refused": True,
                        "unapproved_not_installed": True,
                        "catalog_hiding": "not claimed; --available still lists blocked metadata",
                        "allowed_plugin_installed": True,
                        "marketplace_add_refused": True,
                        "preloop_managed_hook_calls": len(hook_calls),
                        "login_required": False,
                        "vendor_console_11a": "unverified",
                        "isolation": "scratch home, ad-hoc signature, filesystem redirection; not MDM",
                    },
                    indent=2,
                )
            )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


if __name__ == "__main__":
    main()
