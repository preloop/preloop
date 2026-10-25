# Reach your Claude Code sessions from anywhere

A Claude Code session you started this morning is still running. You are not at
that keyboard, and there is no way to see it or say anything to it.

`@preloop-ai/claude-plugin` is the Preloop Agent Control sidecar for
[Claude Code](https://code.claude.com). It connects Claude Code to
[Preloop](https://github.com/preloop/preloop), the open-source AI agent control
plane, so that:

- **You can see which sessions are alive**, from the Preloop console and the
  mobile apps.
- **You can talk to them.** Send a message into a sidecar-owned session, resume
  a persisted one by id, or interrupt the current turn. Operator text lands as
  an auditable user turn, never a hidden system prompt.
- **Approvals keep working without it.** Tool approvals stay on the Claude Code
  hook path, so stopping the sidecar never leaves anything ungoverned.

It uses the same `preloop.agent_control.v1` protocol as the Hermes and OpenClaw
runtime plugins. Apache-2.0.

Status: prototype (issue preloop/preloop#131). Read
[Honest limitations](#honest-limitations) before you rely on it.

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
- **Live reply streaming.** While an owned session answers, the sidecar sends
  its reply text as numbered `session_progress` deltas, merged to at most four
  a second. Talk shows the reply as it is written and reads the stored reply
  when the turn ends. Older plugin versions send no deltas, and Talk falls
  back to refreshing on activity.
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

## Configuration

The sidecar reads `~/.claude/preloop-control.json` (its own file;
`~/.claude/settings.json` stays reserved for Claude Code's own schema).
`preloop agents onboard "Claude Code"` writes the settings nested under a
top-level `"control"` key; a flat file (all keys at the top level) is
accepted too. A file with neither shape yields no usable settings and the
sidecar logs a loud warning instead of idling silently.

```json
{
  "control": {
    "enabled": true,
    "protocol": "preloop.agent_control.v1",
    "runtime": "claude_code",
    "control_ws_url": "wss://app.preloop.ai/api/v1/agents/control/ws",
    "bearer_token": "agt_...",
    "managed_agent_id": "...",
    "runtime_principal_id": "claude-code-...",
    "runtime_principal_name": "Claude Code",
    "workspace_root": "/path/to/default/workspace",
    "workspace_repositories_max": 20,
    "workspace_fetch_timeout_ms": 120000
  }
}
```

Optional keys: `permission_mode`, `transcript_dir`, `observer_enabled`,
`observer_poll_ms`, `turn_timeout_ms` (per-turn reply timeout, default 5
minutes; a hung turn is rejected so the sidecar keeps serving commands; a
flow's `run_limits.max_duration_seconds` or `timeout_seconds` replaces the
default and this key, when set, is the upper bound), `runner_config_path`
(authorized directories file, default `~/.preloop/runner.json`), `claude_executable` (Claude Code binary for owned
sessions; default is the `claude` on PATH when it is newer than the Agent
SDK's bundled Claude Code, else the bundled one),
`workspace_repositories_max` (how many persistent checkouts to keep,
default 20; only clean directories are evicted), and
`workspace_fetch_timeout_ms` (git fetch and clone timeout, default
120000). `workspace_root` is the parent of those checkouts. A persistent
flow with `metadata.workspace.mode` of `persistent_checkout` clones
`<workspace_root>/<repository_slug>` once using the host's git
credentials (the message never carries a token), fetches on that run
and on later runs, then checks the commit out detached. A password in
the clone URL is refused. `ssh://git@host/...` is allowed. A dirty tree
fails the command instead of being reset. A persistent turn that leaves
uncommitted edits with `spawn_worktree: false` fails the next run on that
repository; use a worktree or commit/clean before the next turn. The sidecar records
`preloop.managedcheckout` in git config so a later process still knows
the tree is its own.

A flow with `metadata.workspace.mode` of `authorized_directory` runs in a
directory the host owner listed with `preloop runner dirs add` (by `id`, or
by a `path` that matches a listed entry). Unlisted, `read_only`, home, root,
credential directories, and dirty trees (unless `allow_dirty`) are refused.
A raw `cwd` in the message is ignored. `metadata.model`, when present, sets
the session model.

A turn that ends on an error result (for example an API error) fails the
command with that error text instead of waiting for the flow timeout. A turn
that hits its timeout stops the session, which kills the Claude Code child.
Each turn logs a start line and an end or error line with the session id,
duration, outcome, and the Claude Code binary and version it ran.

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

Owned turns forward bounded assistant and tool progress as `command_progress`
events, correlated to the dispatched command. Tool arguments are excluded.
A final progress line reports the turn duration and measured usage; command
results continue to carry the complete bounded reply and final status.
