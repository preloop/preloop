---
status: non-normative
---

# Subagent turns: what reaches the gateway, per harness

> **Status: findings / design note. Not shipped behaviour.** This page records observations or a proposed design. Nothing here is a product capability unless a linked release note says so.

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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

```
1  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b                                       21 tools
2  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b  x-claude-code-agent-id: a1e37403a36fc420c  13 tools
3  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b                                       21 tools
4  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b  x-claude-code-agent-id: a48fe9b1db3fc6abc  13 tools
5  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b                                       21 tools
6  x-claude-code-session-id: ebd4605d-7099-4c54-bd01-747f7a720e1b                                       21 tools
```

A full header diff of a parent turn against a subagent turn in the same run
differs in exactly one header: `x-claude-code-agent-id`, absent on the parent
and present on the child. Everything else, including `anthropic-beta`,
`user-agent`, `x-app` and the session header, is identical.

The second run confirmed the id is per subagent and stable for its lifetime:
one subagent took three turns (a tool call, its result, then its answer) and
sent `x-claude-code-agent-id: a79dad809a7048810` on all three, while the
parent's turns in between carried no such header.

The body level path is no help: `metadata.user_id` carries the JSON string
`{"device_id": "<redacted>", "account_uuid": "", "session_id": "<the same
session uuid>"}` on parent and subagent turns alike, so the fallback Preloop
already reads (`_session_id_from_anthropic_metadata` in
`backend/preloop/services/openai_gateway.py`) sees the parent's identity for
both.

Note also that the subagent's own tool list still contains `Agent`, so a
subagent can spawn its own subagent. The wire carries one flat agent id and no
path, so depth beyond one level is not reconstructible from headers.

Verdict: **yes**, distinguishable, and the parent is exactly the session the
`X-Claude-Code-Session-Id` already names.

## OpenCode 1.18.31

Scenario: one `opencode run` whose first turn answers with a `task` tool call.

Raw (`user-agent: opencode/1.18.31 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14`,
path `POST /v1/chat/completions`):

```
1  x-session-id: ses_f599e0060ffe4gLnO1q69ZcNFw  x-session-affinity: ses_f599e0060ffe4gLnO1q69ZcNFw
2  x-session-id: ses_f599e0060ffe4gLnO1q69ZcNFw  x-session-affinity: ses_f599e0060ffe4gLnO1q69ZcNFw
3  x-session-id: ses_f599dfca1ffe933RP1BLey87Mr  x-session-affinity: ses_f599dfca1ffe933RP1BLey87Mr
   x-parent-session-id: ses_f599e0060ffe4gLnO1q69ZcNFw
4  x-session-id: ses_f599e0060ffe4gLnO1q69ZcNFw  x-session-affinity: ses_f599e0060ffe4gLnO1q69ZcNFw
```

The subagent turn (3) is a first class session with its own id, and the parent
id is sent explicitly. OpenCode's own log line for the spawn shows the same
relationship internally: `created id=ses_f599dfca... parentID=ses_f599e006...
agent=general mode=subagent`. `x-parent-session-id` is absent on root turns.

Verdict: **yes**, distinguishable, with the parent named directly.

## Codex CLI 0.154.0

Codex sends far more identity than it is currently read for. Every request
carries `Session-Id`, `Thread-Id` and `X-Codex-Window-Id`, plus a JSON blob in
`X-Codex-Turn-Metadata`:

```
session-id: 01a0a666-eae6-74a3-83cc-4ec2ffe68a86
thread-id:  01a0a666-eae6-74a3-83cc-4ec2ffe68a86
x-codex-window-id: 01a0a666-eae6-74a3-83cc-4ec2ffe68a86:0
x-codex-turn-metadata: {"installation_id": "<redacted>",
  "session_id": "01a0a666-eae6-...", "thread_id": "01a0a666-eae6-...",
  "agent_name": "/root", "turn_id": "01a0a666-eb03-...",
  "window_id": "01a0a666-eae6-...:0", "window_number": 0,
  "context_window_id": "01a0a664-...", "request_kind": "turn",
  "root_turn_id": "01a0a666-eb03-...", "thread_source": "user", ...}
```

`agent_name` is the interesting one. The tool the model uses to spawn a
subagent is `spawn_agent`, in a namespace tool (`multi_agent_v1`, or
`collaboration` when the `multi_agent_v2` feature is enabled), and its own
description states the naming rule: "If your current task is `/root/task1` and
you spawn_agent with task_name `task_3` the agent will have canonical task name
`/root/task1/task_3`". So the top level agent reports `agent_name: "/root"`,
and a subagent should report a path under it.

That last step could not be confirmed. `codex exec` advertises the spawn tools
in the request but its tool router rejects the call with
`error=unsupported call: spawn_agent` (tried bare, dotted and double
underscore namespaced forms, with and without the `multi_agent_v2` feature
flag), so the handler is not registered in the non interactive entry point of
this build. No subagent turn could be produced, and therefore no subagent
`agent_name` was observed.

What was observed is the other kind of nesting: a Codex run that shells out to
another `codex exec`. The nested process is a clean slate:

```
1  session-id: 01a0a666-eae6-74a3-83cc-4ec2ffe68a86   agent_name: /root   thread_source: user   (outer)
2  session-id: 01a0a666-eb41-7b80-ae61-4b847ea65ab6   agent_name: /root   thread_source: user   (nested process)
3  session-id: 01a0a666-eae6-74a3-83cc-4ec2ffe68a86   agent_name: /root   thread_source: user   (outer, after)
```

New session uuid, new thread uuid, new turn and root turn ids, still `/root`,
and nothing that points at the caller. Only `installation_id` is shared, and
that is per install, not per parent.

Verdict: **partly, unconfirmed**. In process subagents are almost certainly
distinguishable through `agent_name`, and the parent is the path prefix, but
that needs one live capture from an entry point that registers the spawn
handler before anything is built on it. Process level nesting (`codex exec`
inside `codex exec`) is **not** distinguishable: to us it is a second,
unrelated session.

## Gemini CLI 0.35.3

Gemini CLI has in process subagents: the request advertises `generalist` and
`codebase_investigator` as tools, and answering with a `generalist` function
call makes the CLI run it (`[LocalAgentExecutor] Skipping subagent tool
'codebase_investigator' for agent 'generalist' to prevent recursion`).

The subagent's turns reach the endpoint with headers identical to the
parent's apart from `content-length`. A full diff of parent turn against
subagent turn differs only in
`content-length`:

```
POST /v1beta/models/<model>:streamGenerateContent?alt=sse
user-agent: GeminiCLI/0.35.3/<model> (darwin; arm64; terminal)
x-goog-api-client: google-genai-sdk/1.30.0 gl-node/<version>
x-gemini-api-privileged-user-id: <redacted, stable across separate runs>
x-goog-api-key: <credential>
```

There is no session id of any kind, on the header or in the body: the request
body carries only `contents`, `systemInstruction`, `tools` and
`generationConfig`. The only stable identifier,
`x-gemini-api-privileged-user-id`, was identical across two separate CLI
processes, so it is an install identity, the same trap as Codex's
`installation_id` and Claude Code's `device_id`. Keying on it would collapse
every conversation on the machine into one row, which is the bug
`agent_session_headers.py` exists to avoid.

Verdict: **no**. Not only is the subagent turn indistinguishable, the parent
turns are not separable from each other either. Gemini CLI sessions are source
keyed today and this changes nothing.

## The rest of the enrolled products

Claude Desktop, Cursor, Windsurf, VS Code with Copilot, OpenClaw, Hermes and
Aider were not probed. For the note story the answer is the same for all of
them and does not depend on a probe: `_NATIVE_SESSION_HEADERS` has entries for
Codex and OpenCode only, Claude Code is handled by its own header on the
Anthropic ingress, and nothing else sends a per conversation id we read. Their
traffic is source keyed, so there is no per conversation row for a parent to
point at, let alone a parent. Aider has no subagent concept at all.

If one of these later grows a subagent feature, it needs its own entry in this
table before it can participate.

## Proposed mechanism for a parent session id

Per harness, the source of the parent link:

| Harness | Child session key | Parent session id from | Confidence |
| --- | --- | --- | --- |
| Claude Code | `<X-Claude-Code-Session-Id>:<X-Claude-Code-Agent-Id>` | the session row keyed on `X-Claude-Code-Session-Id` alone | observed |
| OpenCode | `X-Session-Id` (unchanged) | `X-Parent-Session-Id` | observed |
| Codex | `<Session-Id>:<slug(agent_name)>` when `agent_name` is not `/root` | the row for the parent path prefix, root being `Session-Id` itself | inferred, needs one capture |
| Gemini CLI | not derivable | not derivable | observed |
| Everything else | not derivable | not derivable | by construction |

Three properties make the composite key workable:

- The existing normaliser accepts it. Client session ids are validated against
  `^[A-Za-z0-9_.:\-]+$` with a 200 character cap
  (`backend/preloop/services/openai_gateway.py`), so `<uuid>:<agent-id>` and
  `ses_...` pass unchanged. Codex's `agent_name` does not: it contains `/`, so
  it would have to be slugified before it is used as part of a key, and that
  slugification must be injective enough not to merge two sibling agents.
- The parent id is always a value we would have keyed a row on anyway, so
  `parent_session_id` can resolve to a real `runtime_session` row rather than
  storing a free text id nobody can join to. Where the parent's own row does
  not exist yet (a subagent's request arriving before the parent's next turn)
  the resolution should create or look up the parent by its source key exactly
  as a parent turn would, which keeps the two paths from racing into two rows.
- The new headers get the same gate as the existing ones. `X-Parent-Session-Id`
  is a generic name that any proxy could stamp, so it must be read only when
  the credential's runtime principal type says OpenCode, and the Claude Code
  agent id only when it says Claude Code, exactly as the module docstring in
  `agent_session_headers.py` argues. The gate fails closed to today's
  behaviour.

Where it would land: a nullable self referencing `parent_session_id` on
`runtime_session` (`backend/preloop/models/models/runtime_session.py`), set
once at session creation and never updated, plus whatever the console needs to
render it. That is a schema change for the capture child to make, not this
page.

Two limits worth stating up front:

- Depth. Claude Code gives one flat agent id with no path, so a subagent of a
  subagent looks exactly like a subagent of the root. The honest reading of
  `parent_session_id` for Claude Code is "the harness conversation this turn
  belongs to", which is correct at depth one and a safe approximation deeper.
  OpenCode's header names the immediate parent and is correct at any depth.
- Process nesting. An agent that launches another CLI process is invisible as
  lineage on every harness probed. Anything that needs to follow that link has
  to get it from execution lineage, not from session identity.

## Fallback when the parent cannot be derived

For Gemini CLI and every unmapped product there is no parent and often no per
conversation row at all. The scope model in the policy child (#637) should
therefore treat lineage as **optional evidence, never a precondition**:

- A missing `parent_session_id` means "lineage unknown", which must resolve to
  **outside my subtree**, not "same subtree". The default scope, an agent may
  note its own children, then denies rather than widens on harnesses that tell
  us nothing.
- Where the delegation epic has execution lineage, that is the authoritative
  parent link and it works on every harness, because Preloop creates those
  records itself. Session lineage is an addition for interactive harness runs
  that have no execution behind them, not a replacement.
- A wider scope stays an explicit per account grant, off by default, so an
  operator on a harness with no lineage can still get agent to agent notes by
  opting in rather than by us guessing a hierarchy.

Concretely: the scope check should be a function of (author identity, target,
grant, optional lineage), and its behaviour with lineage set to `None` must be
in the tests from the first commit, because for Gemini CLI that is
the only shape it will ever see.

## Recommendation on the capture child

Worth doing, narrowed:

1. Do OpenCode and Claude Code now. Both are observed, both are one header,
   and together they cover the two harnesses where subagent use is routine.
   The work is small: a parent aware read in
   `agent_session_headers.py`, a nullable column, and the resolution rule
   above.
2. Do not do Codex in the same change. The mechanism is credible but
   unconfirmed, and `agent_name` needs slugification and a sibling collision
   rule that should be designed against real values, not against a tool
   description. One capture from an entry point that registers the spawn
   handler settles it.
3. Do not block the note write path or the scope model on any of it. With the
   fallback above, phase 1 ships without lineage and gains precision when the
   capture lands.

The thing that would change this recommendation is a decision to key notes on
execution lineage only. In that case session level parents are decoration, and
this becomes a console nicety rather than part of the addressing model.
