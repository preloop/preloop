"""Print the Preloop configuration for the warehouse-sim demo.

This script does not touch the database or call any API. It prints, for two
accounts ("Lager Nord" and "Lager Sued"), the REST calls that register the
warehouse-sim MCP server and set its tool policies:

* ``get_transcript`` is allowed only for the account's own site, through a
  ``tool_access_rules`` CEL argument rule (``args.site != '<own>'`` -> deny).
* ``propose_workflow_change`` requires approval and a justification.
* ``create_task`` is allowed.
* ``list_workflows``, ``get_audio`` and ``transcribe_audio`` get the same
  own-site deny rule where they take a site, so nothing crosses sites.

Usage::

    python -m scripts.fixtures.warehouse_sim.seed \
        --mcp-url http://host.docker.internal:8765/mcp
    python -m scripts.fixtures.warehouse_sim.seed --json   # machine-readable

Values in angle brackets (``<server_id>``) come from earlier responses.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Dict, List

ACCOUNTS = (
    {"account": "Lager Nord", "site": "nord"},
    {"account": "Lager Sued", "site": "sued"},
)

SERVER_NAME = "warehouse-sim"
WORKFLOW_NAME = "Site lead"

# Tools that take a ``site`` argument and must stay on the own site.
SITE_SCOPED = ("get_transcript", "list_workflows", "get_audio")


def _step(method: str, path: str, note: str, body: Dict[str, Any] | None = None):
    step: Dict[str, Any] = {"method": method, "path": path, "note": note}
    if body is not None:
        step["json"] = body
    return step


def _tool_config(tool: str, **extra: Any) -> Dict[str, Any]:
    body = {
        "tool_name": tool,
        "tool_source": "mcp",
        "mcp_server_id": "<server_id>",
        "account_id": "<account_id>",
        "is_enabled": True,
    }
    body.update(extra)
    return body


def account_plan(account: str, site: str, mcp_url: str) -> Dict[str, Any]:
    steps: List[Dict[str, Any]] = [
        _step(
            "GET",
            "/api/v1/auth/users/me",
            f"Log in as an admin of '{account}'; note account_id",
        ),
        _step(
            "POST",
            "/api/v1/mcp-servers",
            "Register the fixture server; note server_id",
            {
                "name": SERVER_NAME,
                "url": mcp_url,
                "transport": "http-streaming",
                "auth_type": "none",
            },
        ),
        _step(
            "POST",
            "/api/v1/mcp-servers/<server_id>/scan",
            "Discover the six tools",
        ),
        _step(
            "POST",
            "/api/v1/approval-workflows",
            "Approval workflow for workflow changes; note workflow_id",
            {
                "name": WORKFLOW_NAME,
                "description": f"Site lead of {account} approves workflow changes",
                "approval_type": "standard",
                "approvals_required": 1,
                "timeout_seconds": 3600,
                "async_approval_enabled": False,
            },
        ),
    ]
    for tool in SITE_SCOPED:
        steps.append(
            _step(
                "POST",
                "/api/v1/tool-configurations",
                f"Configure {tool}; note config_id as <{tool}_config_id>",
                _tool_config(tool),
            )
        )
        steps.append(
            _step(
                "POST",
                f"/api/v1/tool-configurations/<{tool}_config_id>/access-rules",
                f"{tool}: deny any site other than {site}",
                {
                    "action": "deny",
                    "condition_expression": f"args.site != '{site}'",
                    "condition_type": "cel",
                    "priority": 1,
                    "description": f"{account} may only read site {site}",
                },
            )
        )
    steps += [
        _step(
            "POST",
            "/api/v1/tool-configurations",
            "Configure propose_workflow_change with a required justification",
            _tool_config("propose_workflow_change", justification_mode="required"),
        ),
        _step(
            "POST",
            "/api/v1/tool-configurations/<propose_workflow_change_config_id>/access-rules",
            "propose_workflow_change: other sites are denied",
            {
                "action": "deny",
                "condition_expression": f"args.site != '{site}'",
                "condition_type": "cel",
                "priority": 1,
                "description": f"{account} may only change site {site}",
            },
        ),
        _step(
            "POST",
            "/api/v1/tool-configurations/<propose_workflow_change_config_id>/access-rules",
            "propose_workflow_change: every own-site change needs approval",
            {
                "action": "require_approval",
                "priority": 2,
                "approval_workflow_id": "<workflow_id>",
                "description": "Workflow changes need the site lead",
            },
        ),
        _step(
            "POST",
            "/api/v1/tool-configurations",
            "Configure create_task: allowed, no rules",
            _tool_config("create_task"),
        ),
        _step(
            "POST",
            "/api/v1/tool-configurations",
            "Configure transcribe_audio: allowed (it takes a clip, not a site)",
            _tool_config("transcribe_audio"),
        ),
    ]
    return {"account": account, "site": site, "steps": steps}


def build_plan(mcp_url: str) -> List[Dict[str, Any]]:
    return [account_plan(a["account"], a["site"], mcp_url) for a in ACCOUNTS]


def render_text(plan: List[Dict[str, Any]], api_base: str) -> str:
    out = [
        "# warehouse-sim seed plan (prints only; nothing was changed)",
        f"# API base: {api_base}   auth: -H 'Authorization: Bearer <token>'",
        "",
    ]
    for acct in plan:
        out.append(f"## Account '{acct['account']}' (site {acct['site']})")
        for i, step in enumerate(acct["steps"], 1):
            out.append(f"{i:>2}. {step['note']}")
            cmd = f"    curl -sX {step['method']} {api_base}{step['path']}"
            if "json" in step:
                cmd += " -H 'Content-Type: application/json' \\\n      -d '"
                cmd += json.dumps(step["json"]) + "'"
            out.append(cmd)
        out.append("")
    return "\n".join(out)


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.fixtures.warehouse_sim.seed"
    )
    parser.add_argument(
        "--mcp-url",
        default="http://host.docker.internal:8765/mcp",
        help="URL the Preloop API uses to reach warehouse-sim",
    )
    parser.add_argument("--api-base", default="http://localhost:8000")
    parser.add_argument("--json", action="store_true", help="print the plan as JSON")
    args = parser.parse_args(argv)
    plan = build_plan(args.mcp_url)
    if args.json:
        print(json.dumps(plan, indent=2))
    else:
        print(render_text(plan, args.api_base.rstrip("/")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
