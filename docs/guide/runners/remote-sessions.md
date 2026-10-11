# Remote sessions on a personal runner

A personal runner can host a coding agent session that you (or your account
owner or an account admin) start from Preloop. The session runs on the runner's
machine, as the user the runner runs as, with that user's own installed agent
CLI and its own login. Preloop never sees or stores the agent's credentials.

In 0.17.0 the runner hosts **GitHub Copilot CLI** sessions. Claude Code and
Codex keep their existing Agent Control sidecars
(`preloop agents install-plugin`). More runner-hosted harnesses follow in
0.18.0.

## How a session works

| Harness | Mode | How a turn runs | Verified |
|---|---|---|---|
| `copilot_cli` | resume | One `copilot -p` process per turn. Turn 1 creates the session with `--session-id=<uuid>`; every later turn continues it with `--resume=<uuid>`. | Copilot CLI 1.0.95: resume mode, verified 2026-10-11 |

- One turn runs at a time. A second turn sent while one is running is refused
  with `turn_in_progress`.
- The session uuid is minted by the runner, never by the server, so a session
  can only ever continue a conversation it created.
- Every run passes `--no-remote`, so GitHub's own remote control cannot steer
  the session at the same time.
- Tool permissions come from the host profile named `copilot` in
  `~/.preloop/runner-host-profiles.json` (`allow_tools`, `deny_tools`,
  `allow_all_tools`), exactly as for flows. Without a profile, no tool is
  pre-allowed. The server cannot change them.
- The Preloop approval hook is **required** for every session, whatever the
  profile says. Each tool call is checked by Preloop policies and can wait for
  an approval in the console or in `preloop sessions attach`.
- The agent runs with the host-exec environment allowlist. Variables that
  would move the session off your Copilot seat (`COPILOT_PROVIDER_*`) or grant
  every tool (`COPILOT_ALLOW_ALL`) are removed, and the runner's own Preloop
  credentials never reach the agent.
- A session is shown in **Sessions** like any other governed session: turns,
  agent messages, tool calls, approvals and usage. `preloop sessions attach`
  opens it in command mode: a typed line is the next turn, `/note <text>` sends
  a note.

## Enable sessions on the host

Sessions are off until the host user enables them, per harness, on the machine
itself. The server can never turn them on.

```bash
preloop agents onboard "Copilot CLI" --approvals   # once: the approval hook
preloop runner sessions enable copilot_cli
```

`enable` prints what it allows and asks `Enable? [y/N]`:

```text
Allow remote Copilot CLI sessions on this machine?

Your account owner, account admins and you will be able to start GitHub Copilot
CLI sessions here from Preloop, using your Copilot seat and login. Sessions run
as you, only in directories you authorize with `preloop runner dirs add` or in
temporary checkouts. Every tool call goes through Preloop approvals and is
audited. You get a notification when a session starts.

Turn off any time: preloop runner sessions disable copilot_cli
```

In scripted setups `--yes` accepts the text without the question; without a
terminal and without `--yes` nothing is enabled.

```bash
preloop runner sessions status      # harnesses, limits, live sessions
preloop runner sessions disable copilot_cli
```

`disable` takes effect within seconds on a running runner and ends that
harness's live sessions with `stopped_on_host`. To refuse every remote session
on the machine regardless of the per-harness setting, set
`"sessions": {"enabled": false}` in `~/.preloop/runner.json`.

## Workspaces

A session runs in a directory the host user authorized in
`~/.preloop/runner.json` under `authorized_directories` (managed by
`preloop runner dirs add|remove|list`). The server only ever sends the
directory id and sees its label, never the path. In this release the runner
accepts an authorized directory only when its configured path is absolute, is
already its own real path (no symlink anywhere in it), and is neither the
filesystem root nor the whole home directory. The check runs again before every
turn. Temporary checkouts of tracker repositories come with the workspace
policy work (#1484).

## Limits

Set in `~/.preloop/runner.json`:

```json
{
  "sessions": {
    "max_concurrent": 2,
    "idle_timeout_seconds": 1800,
    "max_duration_seconds": 28800
  }
}
```

| Limit | Default | When reached |
|---|---|---|
| Concurrent sessions | 2 (max 8) | New starts fail with `max_concurrent_reached` |
| Idle timeout | 30 minutes | The session ends with `idle_timeout` |
| Maximum duration | 8 hours | The session ends with `max_duration` |

The server may ask for a shorter idle timeout for one session, never a longer
one.

## What the host user sees

When the runner accepts a session it writes a line to the runner log and raises
a desktop notification as the logged-in user:

```text
Preloop: Jane Doe started a GitHub Copilot CLI session in my-service
```

macOS uses a user notification, Linux uses `notify-send` when it is installed,
Windows uses a toast. A runner installed as a Windows service has no desktop,
so there the log line is the notice. A failed notification never blocks the
session.

## Stopping, restarts and the kill switch

- **Stop from Preloop**: `graceful` lets a running turn finish (up to 30
  seconds) and then ends the session; `kill` ends the agent process tree at
  once. Either way the agent's own session files stay on disk.
- **Runner restart**: sessions are kept on disk (identifiers and timestamps
  only; no prompts, output or credentials) under `~/.preloop/runner-sessions`.
  A runner that comes back within the idle timeout picks them up again and the
  next turn resumes the same Copilot session. A turn that was running when the
  runner stopped is reported as failed with `runner_restarted`.
- **Runner offline**: if the runner stays offline past the idle timeout, the
  server ends the session with `runner_offline`.
- **Account kill switch**: halting flows or tools ends every live session with
  `killed_by_kill_switch` and refuses new ones.

## Rejections

When the runner refuses a start, the session ends with
`runner_rejected:<code>` and the reason is shown with it:

| Code | Meaning |
|---|---|
| `harness_not_enabled_for_sessions` | The host user has not run `preloop runner sessions enable <harness>`, the harness is disabled, or it cannot start (for example the approval hook is missing; the detail names it) |
| `harness_signed_out` | The agent CLI has no login on this machine |
| `max_concurrent_reached` | The runner already hosts its maximum number of sessions |
| `workspace_not_authorized` | The directory is not authorized for this harness on this host |
| `sessions_disabled_on_host` | Remote sessions are turned off for every harness on this machine |

## Audit

Every step is an audit event with resource type `runner_session` naming the
actor, runner, host, harness, model and workspace: `runner_session.start_requested`,
`.started`, `.rejected`, `.turn_sent` (turn id and text length, not the text),
`.stop_requested` and `.ended` (with the end reason). The threat model and
controls (T1 to T12) are tracked in #1485.
