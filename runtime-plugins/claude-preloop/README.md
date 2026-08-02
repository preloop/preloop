# @preloop-ai/claude-plugin

Preloop Agent Control sidecar for [Claude Code](https://code.claude.com).
It lets Preloop steer Claude Code sessions (send messages, interrupt) and see
session presence from the Preloop console and mobile apps, using the same
`preloop.agent_control.v1` protocol as the Hermes and OpenClaw runtime
plugins.

Status: prototype (issue preloop/preloop#131).

## Why a sidecar

Hermes and OpenClaw load Preloop's plugin in-process. Claude Code has no
equivalent in-process extension API for message injection: its extension
surface is hooks (child processes) and the Agent SDK (be the host process).
So this package runs as a long-lived sidecar daemon that owns the Agent
Control WebSocket and drives Claude Code through the
[Claude Agent SDK](https://www.npmjs.com/package/@anthropic-ai/claude-agent-sdk).

## What it does

- **Owned sessions (full steering).** Operator messages from the Preloop
  console/mobile start a new Claude Code session, or are pushed into a live
  sidecar-owned session via SDK streaming input, or resume a persisted
  session by id. `interrupt` stops the current turn. Operator text is an
  auditable user turn, never a hidden system prompt.
- **Observed sessions (presence + approvals).** Interactive terminal sessions
  cannot receive injected turns; the sidecar tails
  `~/.claude/projects/**/*.jsonl` and reports presence/telemetry as
  `session_activity` events. Targeting a persisted-but-idle session resumes
  it headlessly. Summaries only; transcripts are not uploaded.
- **Approvals stay on the hook path.** Tool approvals are handled by the
  PreToolUse hook installed by `preloop agents onboard "Claude Code"
  --approvals`. The sidecar advertises `tool_approval` but never reimplements
  it, so stopping the sidecar never ungoverns anything (fail-closed posture
  preserved). Setting sources are loaded for owned sessions so the same hook
  fires there too.

## Honest limitations

- A live interactive TUI session cannot be steered mid-turn. You get
  observe + approve; steering requires the session to be owned or resumed by
  the sidecar once idle.
- `interrupt` on an observed TUI session fails with a clear error rather
  than sending signals to a terminal someone is typing into.
- Node's global `WebSocket` cannot set custom headers, so the bearer token
  is sent as a `token` query parameter on the wss URL. Encrypted in
  transit, but it can appear in server/proxy access logs; production
  hardening should move to header-based auth (e.g. the `ws` package).

## Configuration

The sidecar reads `~/.claude/preloop-control.json` (its own file;
`~/.claude/settings.json` stays reserved for Claude Code's own schema):

```json
{
  "enabled": true,
  "protocol": "preloop.agent_control.v1",
  "runtime": "claude_code",
  "control_ws_url": "wss://app.preloop.ai/api/v1/agents/control/ws",
  "bearer_token": "agt_...",
  "managed_agent_id": "...",
  "runtime_principal_id": "claude-code-...",
  "runtime_principal_name": "Claude Code",
  "workspace_root": "/path/to/default/workspace"
}
```

Optional keys: `permission_mode`, `transcript_dir`, `observer_enabled`,
`observer_poll_ms`, `turn_timeout_ms` (per-turn reply timeout, default 5
minutes; a hung turn is rejected so the sidecar keeps serving commands).

## Usage

```bash
npm install -g @preloop-ai/claude-plugin
preloop-claude-plugin verify            # check the config
preloop-claude-plugin run               # start the sidecar
preloop-claude-plugin run --config /path/to/preloop-control.json
```

## Development

```bash
npm install
npm run build
npm test
```

Tests use Node's built-in test runner with a fake Agent SDK; no network or
Claude Code install is required.
