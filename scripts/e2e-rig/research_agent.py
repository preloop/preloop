#!/usr/bin/env python3
"""Minimal self-contained research agent driven through the Preloop gateway.

Registers itself as a custom managed agent (POST /api/v1/agents), mints a
durable gateway credential (POST /api/v1/agents/{id}/credentials), then runs
one short research session with plain OpenAI-compatible HTTP calls against
the instance's gateway (/openai/v1). Standard library only — no SDKs.

Usage:
    PRELOOP_USER_TOKEN=<user jwt> python research_agent.py \
        --url https://preloop.example --name "Research Agent (e2e)" \
        [--question "..."] [--state-out state.json] [--wasteful]

``--wasteful`` runs a deliberately inefficient multi-turn session instead of
the single question: it advertises ten MCP-style tools of which only two are
ever invoked, carries fat tool outputs whose bulky JSON fields (embeddings,
raw HTML, crawl metadata) the conversation never references, repeats one tool
output verbatim, and re-sends the ever-growing history every turn. Every
pattern maps to a measurable waste signal in the optimizer's context
analyzers (unused tool schemas, oversized output fields, duplicate outputs,
compressible homogeneous arrays, repeated uncached prefix) so an optimize
run on the resulting session yields real, applicable suggestions.

Exit codes: 0 success, 3 skipped (no model available via the gateway).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request

SKIP_EXIT = 3

# --- output redaction -------------------------------------------------------
# Error responses can echo request context (Authorization header material,
# submitted payloads). Never print a raw body on failure — always mask
# token-like substrings and truncate. Kept in sync with lib/riglib.redact
# (this script is deliberately standalone, stdlib-only).
_REDACT_PATTERNS = [
    # Bearer credentials, wherever they appear.
    re.compile(r"(?i)\bbearer\b[ \t]*[A-Za-z0-9._~+/=-]{4,}"),
    # JWTs (three dot-separated base64url segments starting with eyJ).
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b"),
    # Long opaque token-ish strings.
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),
]
# Values of sensitive JSON keys (FastAPI validation errors echo the input).
_SENSITIVE_JSON_KEY = re.compile(
    r'(?i)("(?:password|token|access_token|refresh_token|secret|api_key|'
    r'authorization)"\s*:\s*")[^"]*(")'
)


def redact(body: object, limit: int = 400) -> str:
    """Return a failure-safe excerpt of a response body: token-like
    substrings and sensitive JSON values masked, length capped."""
    text = body if isinstance(body, str) else json.dumps(body)
    text = _SENSITIVE_JSON_KEY.sub(r"\1***\2", text)
    for pattern in _REDACT_PATTERNS:
        text = pattern.sub("***", text)
    if len(text) > limit:
        text = f"{text[:limit]}... [{len(text) - limit} chars truncated]"
    return text


def request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
    timeout: int = 120,
    extra_headers: dict[str, str] | None = None,
):
    headers = {"Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # TLS verification stays ON; certifi (optional) covers Pythons that ship
    # without a usable system CA bundle for urllib.
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, body


# --- wasteful session fixtures ---------------------------------------------
# Every constant below maps to a measurable waste signal in the optimizer's
# deterministic context analyzers (backend/preloop/services/context_analysis.py):
#   * ten advertised MCP-style tools, two invoked  -> unused tool schemas
#     (scope-tools suggestion; savings scale with resend count)
#   * >500-char top-level JSON fields the chat never references (embeddings,
#     raw HTML, crawl metadata)                    -> oversized output fields
#     (filter-tool-output suggestion + replay filtered_output_fields)
#   * one tool output repeated verbatim            -> duplicate output tokens
#     (dedupe-tool-outputs suggestion)
#   * a single deliberate system-prompt tweak mid-session
#                                                  -> cache-breaking event
#     (stabilize-prefix suggestion)
#   * the full history re-sent every turn          -> high prompt share
#     (trim-context / cap_tool_results)

_WASTEFUL_SYSTEM_PROMPT = (
    "You are a market research assistant compiling a competitive brief on "
    "AI agent cost governance platforms. Ground every answer in the research "
    "notes already present in this conversation. Answer in at most four "
    "sentences. Never call tools yourself; the harness executes tools and "
    "pastes their results into the conversation."
)

# Ten MCP-style (server__tool) tools; only the first two are ever invoked.
_WASTEFUL_TOOLS: list[dict] = [
    {
        "server": "websearch",
        "tool": "search",
        "description": (
            "Full-text web search. Returns ranked organic results with "
            "snippets, source metadata, a relevance-scored embedding of the "
            "aggregated result set, and the raw HTML of the first page for "
            "downstream parsers."
        ),
        "params": {
            "query": ("string", "Search query string"),
            "max_results": ("integer", "Maximum number of results (1-25)"),
            "recency_days": ("integer", "Restrict to pages newer than N days"),
            "safe_mode": ("boolean", "Filter unsafe content"),
        },
        "used": True,
    },
    {
        "server": "docs",
        "tool": "fetch_page",
        "description": (
            "Fetch and extract a documentation or marketing page. Returns "
            "cleaned article text plus crawl diagnostics, a content "
            "embedding, and the raw pre-extraction HTML."
        ),
        "params": {
            "url": ("string", "Absolute URL to fetch"),
            "render_js": ("boolean", "Render client-side JavaScript first"),
            "max_bytes": ("integer", "Truncate the fetched body at N bytes"),
        },
        "used": True,
    },
    {
        "server": "vector",
        "tool": "embed_text",
        "description": (
            "Embed arbitrary text with the account's default embedding model "
            "and return the dense vector plus token accounting."
        ),
        "params": {
            "text": ("string", "Text to embed"),
            "model": ("string", "Embedding model identifier"),
        },
        "used": False,
    },
    {
        "server": "vector",
        "tool": "similarity_search",
        "description": (
            "K-nearest-neighbour search over the account vector store. "
            "Returns matched chunks with scores and source document ids."
        ),
        "params": {
            "query_vector": ("array", "Query embedding vector"),
            "top_k": ("integer", "Number of neighbours to return"),
            "namespace": ("string", "Vector namespace to search"),
        },
        "used": False,
    },
    {
        "server": "crm",
        "tool": "lookup_contact",
        "description": (
            "Look up a CRM contact by email or company domain. Returns "
            "profile, deal history, and engagement timeline."
        ),
        "params": {
            "email": ("string", "Contact email address"),
            "domain": ("string", "Company domain to match"),
        },
        "used": False,
    },
    {
        "server": "github",
        "tool": "list_issues",
        "description": (
            "List repository issues with labels, assignees, and reaction "
            "counts. Supports state and label filtering."
        ),
        "params": {
            "repo": ("string", "owner/name repository slug"),
            "state": ("string", "open, closed, or all"),
            "labels": ("array", "Label names to filter by"),
        },
        "used": False,
    },
    {
        "server": "slack",
        "tool": "post_message",
        "description": (
            "Post a message to a Slack channel as the research bot, with "
            "optional thread broadcast and unfurl control."
        ),
        "params": {
            "channel": ("string", "Channel id or #name"),
            "text": ("string", "Message text (mrkdwn)"),
            "thread_ts": ("string", "Thread timestamp to reply into"),
        },
        "used": False,
    },
    {
        "server": "calendar",
        "tool": "list_events",
        "description": (
            "List upcoming calendar events for the research team, including "
            "attendees, rooms, and conferencing links."
        ),
        "params": {
            "calendar_id": ("string", "Calendar identifier"),
            "days_ahead": ("integer", "Look-ahead window in days"),
        },
        "used": False,
    },
    {
        "server": "tickets",
        "tool": "create_ticket",
        "description": (
            "Create a tracker ticket with title, body, priority, and "
            "component routing; returns the created ticket key."
        ),
        "params": {
            "title": ("string", "Ticket title"),
            "body": ("string", "Ticket body (markdown)"),
            "priority": ("string", "P0-P4 priority band"),
        },
        "used": False,
    },
    {
        "server": "weather",
        "tool": "current_conditions",
        "description": (
            "Current weather conditions for a location, including "
            "temperature, wind, and a 6-hour precipitation outlook."
        ),
        "params": {
            "location": ("string", "City name or lat,lon"),
            "units": ("string", "metric or imperial"),
        },
        "used": False,
    },
]


def _wasteful_tool_definitions() -> list[dict]:
    """OpenAI chat-completions tool definitions for the ten fixture tools."""
    definitions = []
    for spec in _WASTEFUL_TOOLS:
        properties = {
            name: {"type": kind, "description": desc}
            for name, (kind, desc) in spec["params"].items()
        }
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": f"{spec['server']}__{spec['tool']}",
                    "description": spec["description"],
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": list(spec["params"])[:1],
                    },
                },
            }
        )
    return definitions


def _fake_embedding(seed: int, dims: int = 256) -> list[float]:
    """Deterministic pseudo-embedding (bulky, never referenced downstream)."""
    import random

    rng = random.Random(seed)
    return [round(rng.uniform(-1.0, 1.0), 6) for _ in range(dims)]


def _fake_raw_html(topic: str, blocks: int = 24) -> str:
    """Deterministic bulky HTML blob a text-only conversation never reads."""
    return "".join(
        f'<div class="serp-item" data-rank="{i}"><h3 class="serp-title">'
        f'{topic} insight {i}</h3><p class="serp-snippet">Vendor briefing '
        f"note {i}: governance, budgets, and audit trails for autonomous "
        f'agents. <a href="https://example.com/{topic.replace(" ", "-")}/{i}">'
        f"read more</a></p></div>"
        for i in range(1, blocks + 1)
    )


def _websearch_result_json() -> str:
    """Fat websearch__search output: useful snippets + never-read bulk.

    The useful field (``results``) deliberately stays under the analyzer's
    500-char oversized-field threshold so only the genuinely-unread bulk
    (``embedding``, ``raw_html``) is flagged as droppable.
    """
    results = [
        {
            "title": name,
            "url": f"https://example.com/v/{name.lower().replace(' ', '-')}",
            "snippet": f"{name}: {pitch}",
        }
        for name, pitch in [
            ("Preloop", "agent control plane, budgets"),
            ("AgentLedger", "cost attribution, replay"),
            ("FleetGov", "enterprise governance"),
            ("PromptMeter", "token analytics"),
        ]
    ]
    return json.dumps(
        {
            "query": "AI agent cost governance platforms 2026",
            "results": results,
            "embedding": _fake_embedding(seed=11),
            "raw_html": _fake_raw_html("agent cost governance"),
            "request_meta": {
                "engine": "metasearch-v3",
                "latency_ms": 412,
                "region": "us-central",
                "cache": {"hit": False, "ttl_s": 900, "layer": "edge"},
                "billing": {"credits_charged": 3, "plan": "scale"},
                "trace": [
                    {"stage": "fanout", "ms": 118},
                    {"stage": "rank", "ms": 201},
                    {"stage": "annotate", "ms": 93},
                ],
            },
        }
    )


def _docs_fetch_result_json() -> str:
    """Fat docs__fetch_page output: pricing text + never-read bulk."""
    return json.dumps(
        {
            "url": "https://example.com/vendors/agentledger/pricing",
            "title": "AgentLedger pricing",
            "content": (
                "AgentLedger prices per governed agent: Starter $49/agent/mo "
                "(5 agents min), Growth $99/agent/mo with budget guardrails "
                "and SSO, Enterprise custom with replay verification, "
                "on-prem gateway, and 24/7 support. Metered overage at "
                "$0.90 per million gateway tokens. Annual commit discounts "
                "reach 20%. A 14-day trial includes all Growth features."
            ),
            "embedding": _fake_embedding(seed=23),
            "crawl_details": {
                "fetched_at": "2026-07-18T00:00:00Z",
                "renderer": "chromium-headless",
                "status_code": 200,
                "redirects": [
                    {"from": "http://example.com/pricing", "status": 301},
                    {"from": "https://example.com/pricing", "status": 302},
                ],
                "timing": {
                    "dns_ms": 12,
                    "connect_ms": 38,
                    "tls_ms": 41,
                    "ttfb_ms": 187,
                    "download_ms": 96,
                    "render_ms": 1244,
                },
                "resources": [
                    {"kind": "script", "count": 23, "bytes": 481223},
                    {"kind": "css", "count": 6, "bytes": 88213},
                    {"kind": "img", "count": 31, "bytes": 1922144},
                    {"kind": "font", "count": 4, "bytes": 240031},
                ],
                "headers": {
                    "content-type": "text/html; charset=utf-8",
                    "cache-control": "max-age=600, public",
                    "x-served-by": "cache-fra-1289",
                    "x-crawler-quota-remaining": "4977",
                },
            },
            "raw_html": _fake_raw_html("agentledger pricing", blocks=18),
        }
    )


def _wasteful_script() -> list[dict]:
    """Scripted turns: each entry appends messages, then asks the model.

    ``pre`` messages are fabricated tool traffic (assistant tool_calls plus
    their fat results) appended to the history before the user question of
    that turn. ``break_prefix`` tweaks the system prompt once, deliberately
    breaking the shared request prefix mid-session (a cache-breaking event).
    """
    websearch_json = _websearch_result_json()
    return [
        {
            "user": (
                "We are evaluating the AI agent cost governance market. "
                "In three sentences, what does this product category do?"
            )
        },
        {
            "pre": [
                _tool_call_msg(
                    "call_websearch_001",
                    "websearch__search",
                    {
                        "query": "AI agent cost governance platforms 2026",
                        "max_results": 8,
                    },
                ),
                _tool_result_msg("call_websearch_001", websearch_json),
            ],
            "user": (
                "From the search results above, list the main vendors, one line each."
            ),
        },
        {
            "user": (
                "Which of those vendors appear to target enterprises "
                "rather than individual developers?"
            )
        },
        {
            "pre": [
                _tool_call_msg(
                    "call_docs_001",
                    "docs__fetch_page",
                    {
                        "url": "https://example.com/vendors/agentledger/pricing",
                        "render_js": True,
                    },
                ),
                _tool_result_msg("call_docs_001", _docs_fetch_result_json()),
            ],
            "user": (
                "Summarize AgentLedger's pricing tiers from the fetched "
                "page in two sentences."
            ),
        },
        {
            "break_prefix": True,
            "user": (
                "How would these products position themselves against a "
                "DIY monitoring stack built on raw provider dashboards?"
            ),
        },
        {
            # The second search "returns" byte-identical results: a verbatim
            # duplicate tool output the dedupe analyzer must catch.
            "pre": [
                _tool_call_msg(
                    "call_websearch_002",
                    "websearch__search",
                    {
                        "query": "AI agent cost governance platforms 2026",
                        "max_results": 8,
                    },
                ),
                _tool_result_msg("call_websearch_002", websearch_json),
            ],
            "user": (
                "The second search returned the same results. Anything "
                "genuinely new in them? One sentence."
            ),
        },
        {
            "user": (
                "What are the top three risks a buyer should weigh before "
                "adopting one of these platforms?"
            )
        },
        {
            "user": (
                "Write the final competitive brief: exactly three bullets, "
                "one sentence each."
            )
        },
    ]


def _tool_call_msg(call_id: str, tool_name: str, arguments: dict) -> dict:
    """A fabricated assistant message that 'invoked' one fixture tool."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": tool_name,
                    "arguments": json.dumps(arguments),
                },
            }
        ],
    }


def _tool_result_msg(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def run_wasteful_session(
    base: str, gw_token: str, model_ids: list[str], client_session_id: str
) -> None:
    """Drive the scripted multi-turn wasteful session through the gateway.

    The harness owns the conversation: fabricated tool traffic is appended to
    the history and the model is asked to reason over it with
    ``tool_choice: "none"`` so the set of invoked tools stays deterministic
    (exactly two of the ten advertised) regardless of model behaviour.

    Every call carries the same ``X-Preloop-Session-Id`` header so the
    gateway groups all turns into ONE runtime session, distinct from any
    other session on the same credential.
    """
    session_header = {"X-Preloop-Session-Id": client_session_id}
    tools = _wasteful_tool_definitions()
    script = _wasteful_script()
    messages: list[dict] = [{"role": "system", "content": _WASTEFUL_SYSTEM_PROMPT}]
    model: str | None = None
    total_prompt = total_completion = 0
    for turn_no, turn in enumerate(script, start=1):
        if turn.get("break_prefix"):
            # Deliberate mid-session system-prompt tweak: breaks the shared
            # request prefix once (measurable cache-breaking event).
            messages[0] = {
                "role": "system",
                "content": _WASTEFUL_SYSTEM_PROMPT
                + " Reminder: keep every answer terse.",
            }
        for pre in turn.get("pre", []):
            messages.append(pre)
        messages.append({"role": "user", "content": turn["user"]})

        payload = {
            "model": model or model_ids[0],
            "messages": messages,
            "tools": tools,
            "tool_choice": "none",
            "max_tokens": 260,
        }
        if model is None:
            # First turn: same listing-order fallback as the simple session —
            # principal-bound models correctly refuse third-party callers.
            completion = None
            for candidate in model_ids:
                payload["model"] = candidate
                print(f"[turn {turn_no}/{len(script)}] trying model {candidate!r} ...")
                status, completion = request(
                    f"{base}/openai/v1/chat/completions",
                    "POST",
                    gw_token,
                    payload,
                    extra_headers=session_header,
                )
                if status == 200:
                    model = candidate
                    break
                print(
                    f"      model {candidate!r} unusable ({status}): "
                    f"{redact(completion)}"
                )
            if model is None:
                sys.exit(
                    "wasteful session: no listed gateway model is usable "
                    f"(last: {redact(completion)})"
                )
        else:
            print(f"[turn {turn_no}/{len(script)}] model {model!r} ...")
            status, completion = request(
                f"{base}/openai/v1/chat/completions",
                "POST",
                gw_token,
                payload,
                extra_headers=session_header,
            )
            if status != 200:
                sys.exit(
                    f"wasteful session turn {turn_no} failed ({status}): "
                    f"{redact(completion)}"
                )
        message = completion["choices"][0]["message"]
        answer = (message.get("content") or "").strip()
        usage = completion.get("usage", {})
        total_prompt += int(usage.get("prompt_tokens") or 0)
        total_completion += int(usage.get("completion_tokens") or 0)
        messages.append({"role": "assistant", "content": answer})
        excerpt = answer.replace("\n", " ")[:110]
        print(f"      a: {excerpt}{'...' if len(answer) > 110 else ''}")
        print(
            f"      usage: prompt={usage.get('prompt_tokens')} "
            f"completion={usage.get('completion_tokens')} "
            f"(session so far: {total_prompt + total_completion})"
        )
    advertised = len(tools)
    used = sum(1 for spec in _WASTEFUL_TOOLS if spec["used"])
    print("----- wasteful session complete -----")
    print(
        f"turns: {len(script)}  advertised tools: {advertised}  "
        f"invoked tools: {used}  duplicate outputs: 1  prefix breaks: 1"
    )
    print(
        f"tokens through the gateway: prompt={total_prompt} "
        f"completion={total_completion} total={total_prompt + total_completion}"
    )
    print(
        "this session intentionally wastes context; run Optimize on it to "
        "surface the savings."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--name", default="Research Agent (e2e)")
    ap.add_argument(
        "--question",
        default="In two sentences: what is an AI agent control plane?",
    )
    ap.add_argument(
        "--wasteful",
        action="store_true",
        help="run the scripted multi-turn wasteful session instead of the "
        "single question (see module docstring)",
    )
    ap.add_argument("--state-out", default=None)
    args = ap.parse_args()
    base = args.url.rstrip("/")

    user_token = os.environ.get("PRELOOP_USER_TOKEN")
    if not user_token:
        sys.exit("PRELOOP_USER_TOKEN env var is required")

    print(f"[1/4] registering custom agent {args.name!r} ...")
    status, agent = request(
        f"{base}/api/v1/agents",
        "POST",
        user_token,
        {"display_name": args.name, "description": "e2e rig research agent"},
    )
    if status not in (200, 201):
        sys.exit(f"agent registration failed ({status}): {redact(agent)}")
    agent_id = agent["id"]
    print(f"      agent_id={agent_id}")

    print("[2/4] minting a gateway credential ...")
    status, cred = request(
        f"{base}/api/v1/agents/{agent_id}/credentials",
        "POST",
        user_token,
        {"name": "e2e-rig-session", "expires_in_days": 1},
    )
    if status not in (200, 201):
        sys.exit(f"credential creation failed ({status}): {redact(cred)}")
    gw_token = cred["token"]
    credential_id = cred["credential"]["id"]
    print(f"      credential={credential_id} (token withheld from output)")

    # One client session id per invocation: the gateway groups every call
    # carrying the same X-Preloop-Session-Id header into one runtime session.
    import secrets

    client_session_id = f"e2e-wasteful-{secrets.token_hex(4)}"

    if args.state_out:
        with open(args.state_out, "w") as fh:
            json.dump(
                {
                    "agent_id": agent_id,
                    "credential_id": credential_id,
                    "display_name": args.name,
                    "client_session_id": (client_session_id if args.wasteful else None),
                },
                fh,
                indent=2,
            )

    print("[3/4] listing models available through the gateway ...")
    status, models = request(f"{base}/openai/v1/models", token=gw_token)
    if status != 200:
        sys.exit(f"gateway /models failed ({status}): {redact(models)}")
    model_ids = [m["id"] for m in models.get("data", [])]
    print(f"      models: {model_ids or '(none)'}")
    if not model_ids:
        print(
            "SKIP: no model is available via the gateway "
            "(no AI model configured in this account yet)"
        )
        sys.exit(SKIP_EXIT)

    if args.wasteful:
        run_wasteful_session(base, gw_token, model_ids, client_session_id)
        return

    # Try models in listing order: some listed models are backed by
    # credentials bound to a specific agent principal (e.g. a subscription-
    # OAuth model imported from Claude Code) and correctly refuse third-party
    # callers with a 400. A usable BYOK-API-key model may appear later in
    # the list, so fall through until one answers.
    completion = None
    model = None
    for candidate in model_ids:
        print(f"[4/4] research session via gateway model {candidate!r} ...")
        status, completion = request(
            f"{base}/openai/v1/chat/completions",
            "POST",
            gw_token,
            {
                "model": candidate,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a concise research assistant.",
                    },
                    {"role": "user", "content": args.question},
                ],
                "max_tokens": 200,
            },
        )
        if status == 200:
            model = candidate
            break
        print(f"      model {candidate!r} unusable ({status}): {redact(completion)}")
    if model is None:
        sys.exit(
            "gateway chat completion failed on every listed model "
            f"(last: {redact(completion)})"
        )
    answer = completion["choices"][0]["message"]["content"]
    usage = completion.get("usage", {})
    print("----- agent answer -----")
    print(answer.strip())
    print("------------------------")
    print(f"usage: {usage}")
    print("session complete: traffic flowed through the Preloop gateway.")


if __name__ == "__main__":
    main()
