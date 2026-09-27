---
status: non-normative
---

# Subagent turns: what reaches the gateway, per harness

> **Status: findings / design note. Not shipped behaviour.** This page records observations or a proposed design. Nothing here is a product capability unless a linked release note says so.

When a coding harness runs a subagent, does the subagent's model traffic look
different from its parent's by the time it reaches Preloop? This page records
what was observed, harness by harness, and proposes how a parent session id
would be captured where one is derivable.

It is a findings page, not a design. Nothing here has been implemented.

## Why it matters

Preloop derives a per-run session id from the request the agent sends:
`X-Preloop-Session-Id` first, then a vendor header gated on the credential's
runtime principal type, then a body level conversation id, then nothing
(`backend/preloop/services/agent_session_headers.py`, precedence in the module
docstring, reading in `native_session_id_from_headers`). None of that has a
notion of a parent. Without one, an agent to agent note addressed to "the agent
that ran that subagent" has no row to land on, and a scope model that means
"my own children" has nothing to key on.

## Method

Each harness was pointed at a local endpoint on `127.0.0.1` that speaks just
enough of the relevant wire protocol to complete a turn, and that writes every
inbound request line, header and body to a file. The endpoint answers the first
turn with a tool call that makes the harness spawn a subagent, and answers
everything after that with a short text reply, so one run produces a parent
turn, the subagent's turns, and the parent's follow up turn.

No traffic left the machine and no Preloop instance was involved: the point is
what the harness puts on the wire, which is the same whether the endpoint is a
fake or the gateway. Runs used a scratch `HOME`, a scratch working directory
and a placeholder api key. Identifiers below are from those throwaway runs;
install scoped identifiers are redacted.

Observed on 2026-09-15.

## Summary

| Harness | Version | Subagent turn distinguishable | Parent derivable | From |
| --- | --- | --- | --- | --- |
| Claude Code | 2.1.268 | Yes | Yes | `X-Claude-Code-Agent-Id` present only on subagent turns, alongside the parent's `X-Claude-Code-Session-Id` |
| OpenCode | 1.18.31 | Yes | Yes | `X-Parent-Session-Id`, sent next to the child's own `X-Session-Id` |
| Codex CLI | 0.154.0 | Partly, unconfirmed | Probably, unconfirmed | `agent_name` inside `X-Codex-Turn-Metadata` (a canonical task path); the in process spawn could not be exercised, see below |
| Gemini CLI | 0.35.3 | No | No | Subagent turns are identical to parent turns apart from `content-length`; no conversation id on the wire at all |
| Claude Desktop, Cursor, Windsurf, VS Code, OpenClaw, Hermes, Aider | not probed | No | No | No native session header is read for these today, so turns are already source keyed |

## Claude Code 2.1.268

Scenario: one `-p` run whose first turn answers with an `Agent` tool call
(the subagent tool is named `Agent` in this version, not `Task`), twice in one
run, then a second run where the subagent itself takes three turns.

Raw, one run with two subagents (`user-agent: claude-cli/2.1.268 (external, sdk-cli)`,
path `POST /v1/messages?beta=true`):