# Reach your Codex CLI sessions from anywhere

A Codex CLI session you started this morning is still running. You are not at
that keyboard, and there is no way to see it or say anything to it.

`@preloop-ai/codex-plugin` is the Preloop Agent Control sidecar for Codex CLI.
It connects Codex to [Preloop](https://github.com/preloop/preloop), the
open-source AI agent control plane, so that:

- **You can see which sessions are alive**, from the Preloop console and the
  mobile apps.
- **You can talk to them.** Send a message that starts a thread in
  `workspace_root`, continue an owned thread, resume a persisted thread by
  id, or interrupt the current turn. Operator text lands as an auditable user
  turn, never a hidden system prompt.
- **Approvals keep working without it.** Tool approvals stay on the Codex
  hook path, so stopping the sidecar never leaves anything ungoverned.

It uses the same `preloop.agent_control.v1` protocol as the Claude Code
sidecar. Apache-2.0.

Status: prototype (issue preloop/preloop#830). CLI onboarding that installs
this package is a separate change.

## Why a sidecar

Hermes and OpenClaw load Preloop's plugin in-process. Codex CLI has no
equivalent in-process extension API for message injection, so this package
runs as a long-lived sidecar daemon that owns the Agent Control WebSocket
and drives Codex through
[`@openai/codex-sdk`](https://www.npmjs.com/package/@openai/codex-sdk).

The session layer is behind a `CodexClient` interface (`startThread`,
`resumeThread`, `run` with an `AbortSignal`). A future `codex app-server`
driver can replace the SDK without changing the WebSocket protocol.

## What it does

- **Owned threads (full steering).** `send_message` with `start_new_session`
  calls `startThread({ workingDirectory })`, where the directory is `cwd`,
  else `workspace_root`, else the home directory. A follow-up that names an
  owned thread calls `thread.run`. An unknown `target_session_id` or
  `metadata.session_id` calls `resumeThread`. `interrupt` aborts the
  in-flight run with an `AbortController` and answers `command_result` with
  a stopped marker (`status: "stopped"`). The SDK `finalResponse` is
  `reply_text`. `usage` is included in result metadata when the SDK
  provides it.
- **Observed sessions (presence).** The sidecar tails
  `~/.codex/sessions/**/*.jsonl` and reports `session_activity` (session id,
  cwd, last role, tail-window turn count, mtime). Summaries only; transcripts
  are not uploaded. The parser understands Codex rollout records
  (`session_meta`, `event_msg` / `task_started`, `response_item`,
  `turn_context`). A file it cannot parse still counts as presence via mtime.
- **Approvals stay on the hook path (fail-closed).** Tool approvals are
  handled by the hook installed at `~/.codex/hooks.json` by
  `preloop agents onboard "Codex CLI" --approvals`. The sidecar advertises
  `tool_approval` but does not reimplement it, and it does **not** set
  `approvalPolicy` to `never`. Stopping the sidecar never ungoverns
  anything. Auth and model routing are inherited from `~/.codex/config.toml`
  and `~/.codex/auth.json` written by enrollment. This package does not
  read or write either file.

## Honest limitations

- `interrupt` only aborts a turn the sidecar itself started. An interactive
  terminal session is observe-only; interrupting it fails with a clear
  error rather than signalling a terminal someone is typing into.
- Turn count in `session_activity` is the number of `task_started` events
  in the tail window (at most 64KB), not a full-file count.

## Configuration

The sidecar reads `~/.codex/preloop-control.json` (its own file;
`~/.codex/config.toml` stays reserved for Codex). Keys match the Claude
sidecar's control file, plus the Codex-specific keys below. A nested
`"control"` block and a flat file are both accepted.

```json
{
  "control": {
    "enabled": true,
    "protocol": "preloop.agent_control.v1",
    "runtime": "codex",
    "control_ws_url": "wss://app.preloop.ai/api/v1/agents/control/ws",
    "bearer_token": "agt_...",
    "managed_agent_id": "...",
    "runtime_principal_id": "codex-...",
    "runtime_principal_name": "Codex CLI",
    "workspace_root": "/path/to/default/workspace",
    "codex_model": "optional-model-id",
    "codex_sandbox_mode": "workspace-write",
    "codex_path": ""
  }
}
```

Optional keys shared with the Claude sidecar: `permission_mode` (accepted
for schema compatibility; not applied to Codex), `transcript_dir` (default
`~/.codex/sessions`), `observer_enabled`, `observer_poll_ms`,
`turn_timeout_ms` (default 5 minutes).

Codex-specific keys:

| Key | Default | Meaning |
| --- | --- | --- |
| `codex_model` | unset | Passed as `model` to `startThread` / `resumeThread`. |
| `codex_sandbox_mode` | `workspace-write` | Also allows `read-only`. `danger-full-access` is refused unless `codex_allow_full_access` is `true`. |
| `codex_allow_full_access` | unset | Explicit opt-in for `danger-full-access`. |
| `codex_path` | unset | Override of the `codex` binary the SDK spawns (`codexPathOverride`). |

`verify` exits 0 for a valid file and non-zero with a message naming the
problem otherwise (missing required key, wrong `runtime`, or a refused
sandbox mode).

## Usage

```bash
npm install -g @preloop-ai/codex-plugin
preloop-codex-plugin verify            # check the config
preloop-codex-plugin run               # start the sidecar
preloop-codex-plugin run --config /path/to/preloop-control.json
```

## Development

```bash
npm install
npm run build
npm test
```

Tests use Node's built-in test runner with a fake Codex client. No network
and no Codex install are required.
