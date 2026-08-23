"""Stdio MCP server for the opt-in repo-audit tool family.

Uses only the standard library so it can run inside an agent sandbox after
the package is bootstrapped onto PYTHONPATH. Ordinary agents never start
this process.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Dict, List, Optional

from preloop.security.ci_workflow import ci_workflow_audit
from preloop.security.hygiene import repo_hygiene_walk
from preloop.security.opt_in import REPO_AUDIT_SERVER, REPO_AUDIT_TOOLS
from preloop.security.secret_history import secret_history_scan
from preloop.security.upstream import upstream_divergence

PROTOCOL_VERSION = "2024-11-05"

TOOLS: Dict[str, Dict[str, Any]] = {
    "secret_history_scan": {
        "name": "secret_history_scan",
        "description": (
            "Walk git history for secret-like changes. Emits classifiable "
            "JSON rows {sha, path, subject, term, kind, status}. Never "
            "returns secret values. Never runs git log -p or git show."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to a git work tree",
                },
                "extra_terms": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Additional pickaxe/grep terms (generic)",
                },
                "all_refs": {
                    "type": "boolean",
                    "description": "Search all refs (default true)",
                    "default": True,
                },
                "include_blob_inventory": {
                    "type": "boolean",
                    "default": True,
                },
            },
            "required": ["repo_path"],
        },
    },
    "repo_hygiene_walk": {
        "name": "repo_hygiene_walk",
        "description": (
            "Walk HEAD for junk names, tracked sensitive files, leftover "
            "disabled CI, cert/key metadata (expiry, self-signed, key size), "
            "and binary-string kinds (hostname/URL/high-entropy). Never "
            "returns secret bytes or private keys."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to a git work tree",
                }
            },
            "required": ["repo_path"],
        },
    },
    "ci_workflow_audit": {
        "name": "ci_workflow_audit",
        "description": (
            "Generic CI YAML checks: mutable-tag uses:, pull_request_target, "
            "over-broad or missing permissions. Not a zizmor replacement."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to a git work tree",
                }
            },
            "required": ["repo_path"],
        },
    },
    "upstream_divergence": {
        "name": "upstream_divergence",
        "description": (
            "Compare a local pin to tags advertised by a caller-supplied "
            "upstream remote URL. Do not hardcode a product remote."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo_path": {
                    "type": "string",
                    "description": "Path to a git work tree",
                },
                "upstream_url": {
                    "type": "string",
                    "description": "Upstream git remote URL",
                },
                "pin": {
                    "type": "string",
                    "description": "Commit or ref to compare (default HEAD)",
                },
            },
            "required": ["repo_path", "upstream_url"],
        },
    },
}

_DISPATCH: Dict[str, Callable[..., Dict[str, Any]]] = {
    "secret_history_scan": secret_history_scan,
    "repo_hygiene_walk": repo_hygiene_walk,
    "ci_workflow_audit": ci_workflow_audit,
    "upstream_divergence": upstream_divergence,
}


def _read_message() -> Optional[Dict[str, Any]]:
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        decoded = line.decode("utf-8", errors="replace")
        if ":" not in decoded:
            continue
        key, value = decoded.split(":", 1)
        headers[key.strip().lower()] = value.strip()
    length_s = headers.get("content-length")
    if not length_s:
        return None
    body = sys.stdin.buffer.read(int(length_s))
    if not body:
        return None
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        return None
    return parsed


def _write_message(payload: Dict[str, Any]) -> None:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode("ascii") + raw)
    sys.stdout.buffer.flush()


def _ok(req_id: Any, result: Any) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _err(req_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def _call_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if name not in _DISPATCH:
        raise ValueError(f"unknown tool: {name}")
    fn = _DISPATCH[name]
    # Only pass declared parameters.
    if name == "secret_history_scan":
        return fn(
            repo_path=str(arguments["repo_path"]),
            extra_terms=arguments.get("extra_terms"),
            all_refs=arguments.get("all_refs", True),
            include_blob_inventory=arguments.get("include_blob_inventory", True),
        )
    if name == "repo_hygiene_walk":
        return fn(repo_path=str(arguments["repo_path"]))
    if name == "ci_workflow_audit":
        return fn(repo_path=str(arguments["repo_path"]))
    if name == "upstream_divergence":
        return fn(
            repo_path=str(arguments["repo_path"]),
            upstream_url=str(arguments["upstream_url"]),
            pin=arguments.get("pin"),
        )
    raise ValueError(f"unknown tool: {name}")


def handle_request(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Handle one JSON-RPC MCP request. Notifications return None."""
    method = message.get("method")
    req_id = message.get("id")
    params = message.get("params") or {}
    if req_id is None and method:
        # Notification (e.g. notifications/initialized).
        return None
    if method == "initialize":
        return _ok(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": REPO_AUDIT_SERVER, "version": "1"},
            },
        )
    if method in {"notifications/initialized", "initialized"}:
        return None
    if method == "tools/list":
        return _ok(req_id, {"tools": [TOOLS[name] for name in REPO_AUDIT_TOOLS]})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        try:
            result = _call_tool(name, arguments)
        except Exception as exc:  # noqa: BLE001 — surface as MCP tool error
            return _ok(
                req_id,
                {
                    "content": [{"type": "text", "text": f"error: {exc}"}],
                    "isError": True,
                },
            )
        return _ok(
            req_id,
            {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(result, separators=(",", ":")),
                    }
                ],
                "isError": False,
            },
        )
    if method == "ping":
        return _ok(req_id, {})
    return _err(req_id, -32601, f"method not found: {method}")


def serve() -> None:
    """Run the stdio MCP loop until stdin closes."""
    while True:
        message = _read_message()
        if message is None:
            return
        reply = handle_request(message)
        if reply is not None:
            _write_message(reply)


def main(argv: Optional[List[str]] = None) -> int:
    """Entrypoint for ``python -m preloop.security.mcp_server``."""
    del argv
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
