# Remote sessions on a personal runner

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A personal runner can host a coding agent session that you (or your account
owner or an account admin) start from Preloop. The session runs on the runner's
machine, as the user the runner runs as, with that user's own installed agent
CLI and its own login. Preloop never sees or stores the agent's credentials.

The runner hosts **GitHub Copilot CLI**, **Cursor CLI**, **Devin CLI** and
**Claude Code** sessions. Claude Code can also keep using its Agent Control
sidecar, and Codex uses its sidecar only (`preloop agents install-plugin`).

## How a session works

| Harness | Mode | How a turn runs | Verified |
|---|---|---|---|
| `copilot_cli` | resume | One `copilot -p` process per turn. Turn 1 creates the session with `--session-id=<uuid>`; every later turn continues it with `--resume=<uuid>`. | Copilot CLI 1.0.95: resume mode, verified 2026-10-11 |
| `cursor_cli` | resume | The host runs `cursor-agent create-chat`, then `cursor-agent --print --output-format stream-json --resume <chat-id>` for each turn. | Adapter tests with a fake CLI; live session behavior unverified |
| `claude_code` | resume | One `claude -p --output-format stream-json --verbose` process per turn, with the turn text on standard input. Turn 1 creates the session with `--session-id <uuid>`; every later turn continues it with `--resume <uuid>`. | Claude Code 2.1.296 command reference and fake CLI tests; live model turns unverified |
| `devin_cli` | resume | One `devin -p --export <file>` process per turn. The first ATIF export supplies the session id; subsequent turns use `--resume <id>`. | Devin CLI 3000.11.3 command reference and fake CLI tests; live model turns unverified |

Devin requires the `PreToolUse` and `PermissionRequest` Preloop hooks. Its user MCP config is `~/.config/devin/mcp_config.json` (or `$XDG_CONFIG_HOME/devin/mcp_config.json`; `%APPDATA%/devin/mcp_config.json` on Windows). Existing commented config files are supported. Enable sessions with:

```bash
preloop agents onboard Devin --approvals
preloop runner sessions enable devin_cli
preloop runner dirs add /path/to/project --harness devin_cli
```

Devin native flows use the generated `devin` profile or a local named profile whose executable is `devin`. The inventory probes `devin auth status` and `devin models list --format json`, retaining only login state and model ids. Optional `agent_config.devin_model` must be mapped by that profile. Model calls use Cognition directly and are not metered by Preloop. Devin profiles do not accept arbitrary CLI flags, tool grants, managed PR publication, or flow continuation; remote sessions support native resume. Install Devin 3000.3 or later for the dedicated MCP config layout.

Claude Code sessions require the Preloop `PreToolUse` approval hook in the
user (or managed) settings; the runner checks for it before every turn and
does not change Claude's settings or credentials under `~/.claude`. Claude
Code itself keeps the session transcript under `~/.claude/projects`, which
is what lets the next turn resume it. The runner mints the session uuid,
reports the turn's token counts from Claude's `result` event and the model
from its `init` event, and runs
with the host-exec environment allowlist (`ANTHROPIC_*`, `CLAUDE_CODE_*` and
`CLAUDE_CONFIG_DIR` pass; the runner's own Preloop credentials do not). A
host profile named `claude` may map model aliases but cannot add arguments
or tool grants. Enable sessions with:

```bash
preloop agents onboard "Claude Code" --approvals
preloop runner sessions enable claude_code
preloop runner dirs add /path/to/project --harness claude_code
```

Cursor sessions also require the host's `preToolUse`, `beforeShellExecution` and `beforeMCPExecution` Preloop hooks. Enable them explicitly:

```bash
preloop agents onboard Cursor --approvals
preloop runner sessions enable cursor_cli
preloop runner dirs add /path/to/project --harness cursor_cli
```

Choose **New session** on the runner in the console, then **Cursor CLI**. A paused runner refuses new sessions. Native sessions do not require Docker.

For each Cursor turn, the runner revalidates the host-authorized workspace and passes `--trust` to acknowledge Cursor's workspace-trust prompt in headless mode. Tool approvals still use the required Preloop hooks; the runner does not enable `--force` automatically.

- One turn runs at a time. A second turn sent while one is running is refused
  with `turn_in_progress`.
The following tool-grant and environment details describe Copilot sessions.

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
  opens it in command mode: a typed line is the next turn, `a` and `d` decide
  a pending approval (nothing is sent when none is), `/note <text>` sends a
  note, and `/turn <text>` sends that text as a turn. The turn's progress
  (queued, started, finished or failed), the
  agent's messages and tool calls stream live, and attach exits when the
  session ends. The approval hook's calls are attached to the runner session itself,
  so its approvals and notes appear on that session's timeline rather than as
  a second session, even when the hook reaches Preloop before the runner
  reports the Copilot session id.

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

## Headless hosts (no keychain)

On a Linux host with no system credential store (a container or a minimal
server), `copilot login --with-token` can finish with `Login succeeded, but
the token was not saved. Install a system keychain or rerun login and accept
plaintext storage`. The plaintext prompt is interactive, so a non-interactive
setup never stores a login. The runner then reports Copilot signed out and
refuses the session with `harness_signed_out`.

Give the runner `COPILOT_GITHUB_TOKEN` or `GH_TOKEN` in its own environment
(`GITHUB_TOKEN` is accepted the same way). Copilot CLI accepts a GitHub CLI
(`gh`) OAuth token and a fine-grained personal access token with the Copilot
Requests permission (verified with Copilot CLI 1.0.96). The host-exec
allowlist forwards environment names that start with `COPILOT_` or `GH_`,
plus `GITHUB_TOKEN`, to the Copilot process. It still removes
`COPILOT_PROVIDER_*` and `COPILOT_ALLOW_ALL`, and it does not forward the
runner's Preloop credentials.

The inventory reports `login_state: signed_in` when one of those three names
is set. It checks the name only and never reads the value, so the token
never leaves the host. A run that later fails with no authentication
information still marks Copilot signed out until a run succeeds; see the
[Linux quickstart](quickstart-linux.md#harnesses-are-detected-automatically).

`preloop runner enable` writes
`~/.config/systemd/user/preloop-runner.service` and replaces that file on
every later `enable`. The unit sets `HOME` and does not inherit the shell.
Put the token in a drop-in, which `enable` leaves in place.
`systemctl --user edit preloop-runner.service` writes
`~/.config/systemd/user/preloop-runner.service.d/override.conf`; creating
that file directly is the same mechanism:

```text
[Service]
EnvironmentFile=-/home/jane/.preloop/copilot.env
```

`EnvironmentFile=` is systemd's environment-file setting. The leading `-`
lets the service start when the file is absent; Copilot stays signed out
until the variable is set. Create the file as the runner user, one
`NAME=value` line, mode `0600`, under the home directory `enable` pinned
as `HOME`. `umask` applies only when the shell creates the file, so set
the mode again after writing:

```sh
umask 077
cat > ~/.preloop/copilot.env <<'EOF'
COPILOT_GITHUB_TOKEN=replace-with-token
EOF
chmod 600 ~/.preloop/copilot.env
```

Reload the user manager, then restart, so the running process sees it:

```sh
systemctl --user daemon-reload
preloop runner restart
```

A foreground runner needs no drop-in. Export the variable in the shell that
starts `preloop runner fg`.

On a host that has a keychain, `copilot login --device-code` stores the
login for the runner user instead of an environment token. An environment
token is readable by any process of that user.

## Start a session from the console

1. Open **Sessions** and click **New session**, or open **Runners** and click
   **New session** on the runner's row.
2. Pick the runner (when you started from Sessions), the agent and its model.
   Agents that are installed but not ready show why: signed out, disabled, or
   remote sessions not enabled on the host.
3. Pick the workspace:
    - **Authorized directory**: one of the labels the host owner added for
      this agent.
    - **Repository checkout**: a GitHub or Bitbucket Cloud repository from a
      tracker connected to the account. The runner clones it into a temporary
      directory with a short-lived credential that is never written to disk
      (see [Tracker checkouts](#tracker-checkouts)).
4. Optionally type a first message, then click **Start session**.

The console then opens the session view. Operator notes, approvals and the
audit trail work as for any other governed session.

### Who may start a session

The runner owner (the user who registered it) and account admins. Everyone
else gets `403 not_runner_owner`. Team sharing of a runner is an Enterprise
extension.

### Why a start can be refused

| Code | Meaning |
|---|---|
| `runner_offline` | The runner is not connected. |
| `harness_not_enabled_for_sessions` | Remote sessions are off for this agent on the host. |
| `harness_signed_out` | The agent CLI is signed out on the host. On Linux without a keychain, see [Headless hosts (no keychain)](#headless-hosts-no-keychain). |
| `harness_disabled` | The agent is disabled in the runner configuration. |
| `workspace_not_authorized` | The directory is not authorized for this agent. |
| `max_concurrent_reached` | The runner already runs its maximum of remote sessions (2 by default). |
| `checkout_source_not_supported` | The tracker is not GitHub or Bitbucket Cloud. |
| `checkout_requires_app_or_oauth` | A GitHub tracker using a personal access token; checkouts need a GitHub App tracker. |
| `checkout_requires_oauth` | A Bitbucket Cloud tracker using an access token or app password; checkouts need OAuth. |
| `checkout_repository_invalid` | The repository is not of the form `owner/name`. |
| `checkout_provider_unsupported` | The tracker type cannot issue checkout credentials. |
| `checkout_credential_unavailable` | The provider refused, or the GitHub App signing configuration is missing. |
| `checkout_failed` | The runner could not clone the repository. |
| `workspace_dirty` | The runner refused the directory because of uncommitted changes. |
| `sessions_disabled_on_host` | Remote sessions are turned off on this host. |
| `remote_sessions_unavailable` | This server does not run the session service yet. |
| `not_runner_owner` | You are neither the runner owner nor an account admin (403). |

Any other code the runner reports when it rejects a session is passed through
unchanged, and the session ends with `runner_rejected:<code>`. After a start,
`turn_in_progress` means a message is still being answered and
`session_ended` means the session can no longer take messages.

Every start, refusal (including a refused read of a runner's session
options or session list), message and stop is written to the audit log
(`runner_session.*` events) with the actor, runner, host, agent, model and
workspace label. Message text is never stored in the audit log, only its
length.

## API

The console uses the same endpoints the mobile apps will use. All paths are
under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/runners/{runner_id}/session-options` | Agents, authorized directories, checkout sources and limits for this runner |
| POST | `/runners/{runner_id}/sessions` | Start a session: `{"harness","model","workspace","first_prompt","title"}`, answers `202` with `session_id` |
| GET | `/runners/{runner_id}/sessions` | Recent remote sessions on the runner |
| POST | `/runner-sessions/{session_id}/turns` | Send the next message: `{"text"}`; `409 turn_in_progress` while one runs |
| POST | `/runner-sessions/{session_id}/stop` | `{"mode":"graceful"}` or `{"mode":"kill"}` |

Workspaces:

```json
{"kind": "authorized_directory", "id": "dir_9f2c"}
{"kind": "authorized_directory", "id": "dir_9f2c", "path": "pkg/api", "allow_dirty": true}
{"kind": "tracker_checkout", "tracker_id": "<uuid>", "repository": "example/app", "ref": "main"}
```

`path` is optional and relative to the directory. `allow_dirty` is accepted only from the runner owner (`403 not_runner_owner` otherwise).

Refusals use the body `{"detail": {"code": "...", "message": "..."}}`.

A repository checkout's clone credential is sent only once, in the start
message, by the server process that holds the runner's connection at that
moment. It is never stored, so if that send fails the start is delivered
again on the next heartbeat without a credential and the runner ends it with
`checkout_failed`; start the session again.

## Workspaces

A session start names a workspace of one of two kinds. The control plane never
names a path on your machine.

| Kind | What the console sends | What the runner does |
| --- | --- | --- |
| Authorized directory | `{"kind": "authorized_directory", "id": "dir_9f2c"}` (optionally a relative `path` below it) | Opens the directory you listed with `preloop runner dirs add`, after proving the request stays inside it |
| Tracker checkout | `{"kind": "tracker_checkout", "tracker_id": "...", "repository": "owner/name", "ref": "main"}` | Clones the repository into a fresh session directory with a short-lived credential, deletes it when the session ends |

### Authorized directories

Only directories you list on the host can be opened. Manage the list with:

```sh
preloop runner dirs add ~/src/ims --label "IMS" --mode write
preloop runner dirs add ~/src/docs --mode read_only --harness copilot_cli
preloop runner dirs list
preloop runner dirs remove dir_9f2c        # by id or by path
```

`add` resolves the path (symlinks included) and stores the real path with a
generated id in `~/.preloop/runner.json` under `authorized_directories`
(under a lock, so a concurrent token rotation or registration keeps it):

```json
{
  "authorized_directories": [
    {"id": "dir_9f2c", "path": "/home/jane/src/ims", "label": "IMS", "mode": "write", "harnesses": "all"},
    {"id": "dir_aa11", "path": "/home/jane/src/docs", "label": "docs", "mode": "read_only", "harnesses": ["copilot_cli"]}
  ]
}
```

- `mode` is `write` or `read_only`. The mode travels with the resolved
  workspace so the session host can run the harness in a read-only
  configuration; a harness that cannot run read-only is refused for a
  `read_only` directory.
- `harnesses` is `"all"` or a list of harness ids (`copilot_cli`, `cursor_cli`,
  `claude_code`, ...). A session with another harness is refused for that
  directory.
- The control plane sees `id`, `label`, `mode` and `harnesses` only. They are
  sent with the runner's registration and every heartbeat, so a change on the
  host appears in the console within a heartbeat (15 seconds) without a
  restart. Paths never leave the host.

Refused at `dirs add` and again every time an entry is used:

| Entry | Why |
| --- | --- |
| `~`, `$HOME`, `~/x`, globs (`*`, `?`, `[`) | The path must be explicit. Nothing is expanded. |
| `/`, `C:\`, `\\server\share` | A filesystem root would authorize everything. |
| Your home directory, or a directory that contains it | Authorizing the whole home directory authorizes every credential store and configuration file in it. Add project directories below it instead. |
| The Preloop configuration directory (`~/.preloop`, with `runner.json` and `config.yaml`), `~/.ssh`, `~/.gnupg`, `~/.aws`, `~/.azure`, `~/.kube`, `~/.docker`, `~/.terraform.d`, `~/.config/gcloud`, `~/.config/rclone`, `~/.local/share/keyrings`, `~/Library/Keychains`, `~/.config/gh`, `~/.config/github-copilot`, `~/.copilot`, `~/.claude`, `~/.codex`, `~/.cursor`, anything below them, or a directory that contains them | They hold credentials (the runner token, your Preloop sign-in, keys and harness logins). A session must never see them, not even `read_only`. |
| `\\?\...`, `\\.\...`, `\\server\C$\...` | Device namespace paths and UNC administrative shares. |
| A path that is not a directory, or that no longer resolves to the stored real path | The directory was removed, moved, or replaced by a symlink or junction since it was authorized. `dirs list` shows the entry as `not usable` with the reason; remove it and add the current path. |

### Containment

When a session asks for an authorized directory (optionally with a relative
`path` below it), the runner:

1. re-validates the entry (the checks above), and confirms its stored path is
   still its own real path;
2. refuses a relative `path` that is absolute, carries a drive letter or
   backslash, or contains `.` or `..` segments;
3. inspects every component below the directory as named: a reparse point of
   unknown type is refused;
4. resolves the target with realpath and requires the result to be the
   directory or below it. A symlink (or a Windows junction) inside the
   directory that points outside it is refused with
   `workspace_not_authorized`, even though it was reachable from inside.

On Windows the comparison is case-insensitive and 8.3 short names are
expanded; drive-letter and UNC paths are canonicalised. On Linux and macOS
the comparison is exact. The credential directories above are also matched by file
identity, so a different spelling of the same directory on a
case-insensitive volume (the macOS default) is refused too.

### Dirty directories

A directory that is a git work tree with uncommitted changes (including
untracked files that are not ignored) is refused with `workspace_dirty`,
so a session cannot silently build on, or destroy, work you have not
committed. The owner of the runner can start the session anyway with
`allow_dirty`; the control plane only sets it for the owner. A directory
outside any repository has nothing to protect and is never refused for this
reason. If `.git` exists but git is not installed for the runner user, the
check fails closed.

### Tracker checkouts

A tracker checkout clones a repository of one of the account's trackers into
`~/.preloop/host-workspaces/sessions/<session id>/<repository name>` and runs
the harness there. The directory is created fresh for each session (an
existing directory with the same id is never reused) and deleted when the
session ends. A runner restart deletes checkouts of sessions it does not
restore; a restored session keeps its clone.

- `ref` selects a branch or tag (`git clone --single-branch --branch`);
  absent means the provider's default branch.
- The runner derives the clone URL from the provider and `owner/name`
  (`https://github.com/...`, `https://bitbucket.org/...`). The control plane
  cannot supply a URL, so a credential can only ever be presented to that
  provider, and redirects are refused.

#### The clone credential

The control plane mints a credential immediately before it sends the start
message and sends it inside that message only:

| Tracker | Credential | Lifetime |
| --- | --- | --- |
| GitHub, App-authenticated | Installation access token restricted to the one repository with `contents: read` (GitHub adds `metadata: read`) | 60 minutes (GitHub's maximum) |
| Bitbucket Cloud, managed connection (Enterprise) | The OAuth access token of the managed grant; rotated first if it has less than five minutes left | Up to 2 hours (fixed by Bitbucket); the runner discards it right after the clone |

Refused with a named error before any credential exists:

| Tracker | Error | Why |
| --- | --- | --- |
| GitHub with a personal access token | `checkout_requires_app_or_oauth` | A PAT cannot be narrowed to one repository or made short-lived before it reaches a laptop. |
| Bitbucket with an API token, a repository or workspace access token, an app password, or a pasted OAuth token | `checkout_requires_oauth` | Same reason: it is a long-lived secret that cannot be issued per session. |
| GitLab, Jira or any other tracker type | `checkout_provider_unsupported` | GitLab checkouts are planned (#1489). |
| `repository` not of the form `owner/name` | `checkout_repository_invalid` | |
| The provider refused, or the GitHub App signing configuration is missing on the control plane | `checkout_credential_unavailable` | |

On the runner the credential is used through an askpass helper and nothing
else:

- git runs with `GIT_ASKPASS` pointing at the Preloop binary; git asks it for
  the username (answered from an environment variable, `x-access-token` for
  GitHub, `x-token-auth` for Bitbucket) and for the password, which the
  helper reads from a channel the runner serves from memory for the
  duration of the clone: on Linux and macOS a pipe the git process tree
  inherits as fd 3; on Windows a named pipe (`\\.\pipe\preloop-askpass-<random>`)
  whose access control list admits only the runner's own user and that
  rejects remote clients, because Git for Windows passes only the standard
  handles through its intermediate launcher process. The token is never in
  a process environment, on the command line, in the remote URL or in
  `.git/config`.
- The user's global git configuration (`~/.gitconfig`,
  `~/.config/git/config`) is ignored for the clone and `credential.helper`
  is cleared, so a credential helper (keychain, Windows credential manager,
  `store`), a `url.<base>.insteadOf` rewrite, a proxy or a certificate
  override in it can neither see, keep nor redirect the credential. The
  system configuration installed by the administrator stays in force (on
  Windows it selects the TLS backend), and `GIT_SSL_CAINFO` and
  `GIT_SSL_CAPATH` from the runner's environment are kept so a host behind
  an inspecting proxy can still verify the provider certificate.
- Only `https` is allowed and redirects are refused.
- After the clone the runner clears the token from its memory and the pipe.
  The harness then runs in the checkout without any credential: it cannot
  push, and `git remote -v` shows a plain URL.
- The control plane never stores the credential: it is stripped from
  persisted lease payloads at every depth and from the session record, and
  the audit event `runner_session.checkout_credential_minted` records the
  provider, repository and expiry only.

A failed clone is reported as `checkout_failed` with the last git line (with
the token redacted if a provider ever echoed it), and the session directory
is removed.

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
one. The runner reports each session's effective idle timeout, and the server
adopts the host's lower value. The same value is the resume window when the
runner goes away (see [When the laptop sleeps](#when-the-laptop-sleeps)).

## When the laptop sleeps

When the runner hosting a started session (`idle` or `running`) goes
`asleep` (it sent `going_to_sleep`) or `offline` (no heartbeat and no sleep
signal; this is what a lid close looks like on macOS and Windows), the
session moves to `runner_away`:

- It keeps its slot. A turn sent now is accepted and held on the server; it
  goes to the runner when it reconnects. A turn that was running stays as it
  was.
- Pending approvals stay open. The agent that would act on a decision is on
  the sleeping machine, so nothing runs until it wakes.
- The resume window is the session's idle timeout (30 minutes by default,
  or the host's lower value), counted from when the runner went away: the
  sleep signal, or the last heartbeat.
- If the runner reconnects inside the window, the session resumes in the
  same harness session (per-turn adapters resume by session id), the held
  turn is delivered, and the audit log records `runner_session.resumed`.
- After the window the session ends with `runner_away_timeout`, the audit
  log records `runner_session.ended` with that reason, and whoever started
  the session gets a notice in the console.

What clients read while the session is away:

| Where | What it says |
|---|---|
| `GET /api/v1/runners/{runner_id}/sessions` | `state: "runner_away"`, `runner_presence` (`asleep` or `offline`), `away_since`, `resume_until` |
| `GET /api/v1/runtime-sessions/{id}/control` | `mode: "command"`, `reason_code: "runner_away"`, a readable `reason`; a typed line is sent as a held turn |
| `runtime_session_updated` live event | `status: "runner_away"` with `metadata.presence`, `away_since` and `resume_until`; `status: "resumed"` when it is back |

The console shows "Your laptop is asleep or offline" above the message box.
The mobile apps can use the same fields. A session the runner had not
confirmed yet (`requested`, `starting`) or one that is `stopping` does not go
`runner_away`; it ends with `runner_offline` once the runner is offline past
the idle timeout.

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
  runner stopped is reported as failed with `runner_restarted`, and the result
  of the last finished turn is sent again on every reconnect, so a result lost
  in transit never leaves the session waiting.
- **Runner asleep or offline**: a started session waits as `runner_away`
  and ends with `runner_away_timeout` if the runner does not come back
  within the resume window ([When the laptop sleeps](#when-the-laptop-sleeps)).
  A session that never started ends with `runner_offline`.
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
| `workspace_not_authorized` | The directory is not authorized for this harness on this host, a requested path escapes it, or it is `read_only` (no session harness runs read-only yet) |
| `workspace_dirty` | The directory has uncommitted changes and the start did not allow it |
| `checkout_failed` | The tracker checkout could not be cloned |
| `sessions_disabled_on_host` | Remote sessions are turned off for every harness on this machine |

## Audit

Every step is an audit event with resource type `runner_session` naming the
actor, runner, host, harness, model and workspace: `runner_session.start_requested`,
`.started`, `.rejected`, `.turn_sent` (turn id and text length, not the text),
`.stop_requested` and `.ended` (with the end reason). The threat model and
controls (T1 to T12) are tracked in #1485.

## Deferred to 0.18.0

GitLab checkouts (#1489), a per-project checkout cache with TTL and size cap
and write-scoped tokens for pushing from a session (#1478). Flows keep using
the existing host-exec publication for pushes.
