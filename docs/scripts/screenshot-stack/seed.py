"""Seed the local screenshot stack with a quickstart-shaped account.

Runs inside the compose network (it talks to api:8000 and gateway:8000):

    docker run --rm --network preloopshots_default \
      -v "$PWD/docs/scripts/screenshot-stack:/kit:ro" \
      preloop-shots/preloop:local python /kit/seed.py

What it creates, all through the public API:
  * an account through the signup endpoint the register page uses
  * the Example MCP Server, the Support and CFO approval workflows and the
    four quickstart rules on the pay tool (allow <= 100, Support <= 200,
    CFO <= 1000, deny above)
  * two AI models that point at the local stub (no provider key)
  * one API key and four registered agents with enrollment records
  * chat sessions through the model gateway and a few MCP tool calls
"""

import asyncio
import datetime as dt
import random
import uuid

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

API = "http://api:8000"
GW = "http://gateway:8000"
USER = {
    "username": "alex",
    "email": "alex@example.com",
    "password": "local-test-pass-2026",
}
STUB = "http://stub-model:9000/v1"
random.seed(20260929)


def login():
    anon = httpx.Client(base_url=API, timeout=60)
    r = anon.post("/api/v1/auth/register", json=USER)
    print("register", r.status_code)
    r = anon.post(
        "/api/v1/auth/token",
        data={"username": USER["username"], "password": USER["password"]},
    )
    r.raise_for_status()
    return httpx.Client(
        base_url=API,
        headers={"Authorization": "Bearer " + r.json()["access_token"]},
        timeout=120,
    )


def ok(r, what):
    if r.status_code >= 400:
        raise SystemExit("%s: %s %s" % (what, r.status_code, r.text[:300]))
    return r.json()


def seed_policy(c):
    me = ok(c.get("/api/v1/auth/users/me"), "me")
    account = me.get("account_id") or (me.get("account") or {}).get("id")
    server = ok(
        c.post(
            "/api/v1/mcp-servers",
            json={
                "name": "Example MCP Server",
                "url": "http://example-mcp:8001/mcp",
                "transport": "http-streaming",
                "auth_type": "none",
            },
        ),
        "mcp server",
    )
    wf = {}
    for name, desc in [
        ("Support", "Support team signs off on small payments"),
        ("CFO", "Finance signs off on larger payments"),
    ]:
        wf[name] = ok(
            c.post(
                "/api/v1/approval-workflows",
                json={
                    "name": name,
                    "description": desc,
                    "approval_type": "standard",
                    "approvals_required": 1,
                    "timeout_seconds": 3600,
                    "async_approval_enabled": False,
                },
            ),
            "workflow",
        )["id"]
    cfg = ok(
        c.post(
            "/api/v1/tool-configurations",
            json={
                "tool_name": "pay",
                "tool_source": "mcp",
                "mcp_server_id": server["id"],
                "account_id": account,
                "is_enabled": True,
                "justification_mode": "required",
            },
        ),
        "tool config",
    )
    rules = [
        ("allow", "args.amount <= 100", None, "Small payments go through"),
        (
            "require_approval",
            "args.amount <= 200",
            "Support",
            "Support approves up to 200",
        ),
        ("require_approval", "args.amount <= 1000", "CFO", "CFO approves up to 1000"),
        ("deny", None, None, "Anything larger is denied"),
    ]
    for prio, (action, cond, flow, desc) in enumerate(rules, 1):
        body = {"action": action, "priority": prio, "description": desc}
        if cond:
            body["condition_expression"] = cond
        if flow:
            body["approval_workflow_id"] = wf[flow]
        ok(
            c.post(
                "/api/v1/tool-configurations/%s/access-rules" % cfg["id"], json=body
            ),
            "rule",
        )
    for name, ident, default in [
        ("GPT-5.4", "gpt-5.4", True),
        ("GPT-5.4 mini", "gpt-5.4-mini", False),
    ]:
        ok(
            c.post(
                "/api/v1/ai-models",
                json={
                    "name": name,
                    "provider_name": "openai",
                    "model_identifier": ident,
                    "api_endpoint": STUB,
                    "api_key": "sk-local-stub-not-a-real-key",
                    "is_default": default,
                    "meta_data": {
                        "gateway": {
                            "enabled": True,
                            "model_alias": "openai/" + ident,
                            "provider_adapter": "preloop",
                        }
                    },
                },
            ),
            "model",
        )
    ok(
        c.post("/api/v1/auth/api-keys", json={"name": "Claude Code on laptop"}),
        "api key",
    )
    print("policy, models and API key seeded")


AGENTS = [
    (
        "Claude Code (alex-mbp)",
        "claude_code",
        "Refactor the billing module and add tests.",
    ),
    (
        "Codex CLI (ci-runner-2)",
        "codex",
        "Triage the failing integration tests on main.",
    ),
    (
        "OpenClaw (ops-assistant)",
        "openclaw",
        "Summarise yesterday's on-call incidents.",
    ),
    ("Hermes (research)", "hermes", "Compare pricing of three vector databases."),
]
# (agent index, task, turns): one gateway session each.
SESSIONS = [
    (0, "Refactor the billing module and add tests.", 6),
    (0, "Review the open PR for the payments service.", 4),
    (0, "Write the release notes for 0.16.", 3),
    (1, "Triage the failing integration tests on main.", 6),
    (1, "Find the flaky test in the scheduler suite.", 5),
    (1, "Check which dependency updates are safe to merge.", 4),
    (2, "Summarise yesterday's on-call incidents.", 5),
    (2, "Explain the search latency regression since Friday.", 4),
    (3, "Compare pricing of three vector databases.", 6),
    (3, "Check that the orders migration is safe to rerun.", 3),
]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": n,
            "description": d,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    for n, d in [
        ("read_file", "Read a file"),
        ("run_tests", "Run the test suite"),
        ("search_code", "Search the repository"),
        ("web_fetch", "Fetch a URL"),
        ("list_issues", "List tracker issues"),
        ("open_pr", "Open a pull request"),
    ]
]
FOLLOW_UPS = [
    "Run the tests again and show me the failures.",
    "Here is the output of the last command, what does it mean?",
    "Good. Now check the other call sites.",
    "Write that up as a short summary for the team.",
]


def register_agents(c):
    out = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for name, kind, task in AGENTS:
        agent = ok(
            c.post("/api/v1/agents", json={"display_name": name, "agent_kind": kind}),
            "agent",
        )
        aid = agent.get("id") or agent["agent"]["id"]
        cred = ok(
            c.post(
                "/api/v1/agents/%s/credentials" % aid,
                json={"name": "%s key %s" % (name, uuid.uuid4().hex[:6])},
            ),
            "credential",
        )
        token = (
            cred.get("token")
            or cred.get("secret")
            or cred.get("key")
            or (cred.get("credential") or {}).get("token")
        )
        # The record `preloop agents onboard` writes after it wires an agent to
        # the gateway and the MCP proxy; seeded directly so no CLI touches a
        # real agent config.
        ok(
            c.post(
                "/api/v1/agents/%s/enrollments" % aid,
                json={
                    "enrollment_type": "cli",
                    "adapter_key": kind,
                    "status": "validated",
                    "validation_result": {
                        "preloop_server_present": True,
                        "gateway_provider_ok": True,
                        "gateway_base_url_ok": True,
                    },
                    "last_applied_at": now,
                    "last_validated_at": now,
                },
            ),
            "enrollment",
        )
        out.append((name, task, token))
    print("agents", len(out))
    return out


def chat_session(token, task, turns):
    sid = "sess-" + uuid.uuid4().hex[:10]
    g = httpx.Client(
        base_url=GW,
        timeout=120,
        headers={"Authorization": "Bearer " + token, "X-Preloop-Session-Id": sid},
    )
    msgs = [
        {"role": "system", "content": "You are a careful engineering agent."},
        {"role": "user", "content": task},
    ]
    for i in range(turns):
        r = g.post(
            "/openai/v1/chat/completions",
            json={
                "model": random.choice(["openai/gpt-5.4", "openai/gpt-5.4-mini"]),
                "messages": msgs,
                "tools": TOOLS,
            },
        )
        if r.status_code != 200:
            print("gateway", r.status_code, r.text[:200])
            return
        msgs.append(
            {
                "role": "assistant",
                "content": r.json()["choices"][0]["message"].get("content") or "",
            }
        )
        log = "\n".join(
            "line %d: ok tests/test_billing.py::test_case_%d" % (j, j)
            for j in range(40 * (i + 1))
        )
        msgs.append(
            {"role": "user", "content": FOLLOW_UPS[i % len(FOLLOW_UPS)] + "\n\n" + log}
        )


async def mcp_calls(token, calls):
    tr = StreamableHttpTransport(
        API + "/mcp/v1", headers={"Authorization": "Bearer " + token}
    )
    async with Client(tr, timeout=20) as cl:
        names = [t.name for t in await cl.list_tools()]
        for tool, args in calls:
            name = next((n for n in names if n == tool or n.endswith(tool)), None)
            if not name:
                print("missing tool", tool)
                continue
            try:
                res = await asyncio.wait_for(
                    cl.call_tool(name, args, raise_on_error=False), timeout=15
                )
                text = " ".join(getattr(x, "text", "") for x in (res.content or []))
                print(
                    "mcp", name, args.get("amount", ""), text[:100].replace("\n", " ")
                )
            except Exception as exc:  # a pending approval times out here by design
                print("mcp", name, args.get("amount", ""), type(exc).__name__)


async def main():
    c = login()
    seed_policy(c)
    agents = register_agents(c)
    for idx, task, turns in SESSIONS:
        chat_session(agents[idx][2], task, turns)
    print("chat sessions done")
    why = "Paying the contractor invoice for the completed March milestone."
    await mcp_calls(
        agents[0][2],
        [
            (
                "pay",
                {"recipient": "vendor@example.com", "amount": 80, "justification": why},
            ),
            (
                "pay",
                {
                    "recipient": "contractor@example.com",
                    "amount": 5000,
                    "justification": why,
                },
            ),
            (
                "send_email",
                {
                    "recipient": "team@example.com",
                    "subject": "Deploy done",
                    "body": "Release shipped.",
                },
            ),
            ("verify_refund_eligibility", {"order_id": "ORD-1042"}),
        ],
    )
    await mcp_calls(
        agents[2][2],
        [
            (
                "pay",
                {
                    "recipient": "support-refunds@example.com",
                    "amount": 150,
                    "justification": "Refund for a duplicated charge on order ORD-1042.",
                },
            ),
        ],
    )


asyncio.run(main())
