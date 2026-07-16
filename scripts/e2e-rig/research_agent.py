#!/usr/bin/env python3
"""Minimal self-contained research agent driven through the Preloop gateway.

Registers itself as a custom managed agent (POST /api/v1/agents), mints a
durable gateway credential (POST /api/v1/agents/{id}/credentials), then runs
one short research session with plain OpenAI-compatible HTTP calls against
the instance's gateway (/openai/v1). Standard library only — no SDKs.

Usage:
    PRELOOP_USER_TOKEN=<user jwt> python research_agent.py \
        --url https://preloop.example --name "Research Agent (e2e)" \
        [--question "..."] [--state-out state.json]

Exit codes: 0 success, 3 skipped (no model available via the gateway).
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

SKIP_EXIT = 3


def request(
    url: str,
    method: str = "GET",
    token: str | None = None,
    payload: dict | None = None,
    timeout: int = 120,
):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--name", default="Research Agent (e2e)")
    ap.add_argument(
        "--question",
        default="In two sentences: what is an AI agent control plane?",
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
        sys.exit(f"agent registration failed ({status}): {agent}")
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
        sys.exit(f"credential creation failed ({status}): {cred}")
    gw_token = cred["token"]
    credential_id = cred["credential"]["id"]
    print(f"      credential={credential_id} (token withheld from output)")

    if args.state_out:
        with open(args.state_out, "w") as fh:
            json.dump(
                {
                    "agent_id": agent_id,
                    "credential_id": credential_id,
                    "display_name": args.name,
                },
                fh,
                indent=2,
            )

    print("[3/4] listing models available through the gateway ...")
    status, models = request(f"{base}/openai/v1/models", token=gw_token)
    if status != 200:
        sys.exit(f"gateway /models failed ({status}): {models}")
    model_ids = [m["id"] for m in models.get("data", [])]
    print(f"      models: {model_ids or '(none)'}")
    if not model_ids:
        print(
            "SKIP: no model is available via the gateway "
            "(no AI model configured in this account yet)"
        )
        sys.exit(SKIP_EXIT)

    model = model_ids[0]
    print(f"[4/4] research session via gateway model {model!r} ...")
    status, completion = request(
        f"{base}/openai/v1/chat/completions",
        "POST",
        gw_token,
        {
            "model": model,
            "messages": [
                {"role": "system", "content": "You are a concise research assistant."},
                {"role": "user", "content": args.question},
            ],
            "max_tokens": 200,
        },
    )
    if status != 200:
        sys.exit(f"gateway chat completion failed ({status}): {completion}")
    answer = completion["choices"][0]["message"]["content"]
    usage = completion.get("usage", {})
    print("----- agent answer -----")
    print(answer.strip())
    print("------------------------")
    print(f"usage: {usage}")
    print("session complete: traffic flowed through the Preloop gateway.")


if __name__ == "__main__":
    main()
