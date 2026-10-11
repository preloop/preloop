# Personal runners: threat model and controls

A personal runner is the `preloop` CLI running as a service on a developer's own
machine. In 0.17.0 it can run flow jobs on the harnesses installed there (for
example GitHub Copilot CLI on the developer's own seat) and, after explicit
opt-in on the host, accept remote sessions started from the Preloop console.
That makes the laptop reachable from Preloop. This page states what that means,
which controls apply, and what wave 1 does not prevent.

The controls are numbered T1 to T12. Pull requests that implement them cite
these ids and ship a test per id.

## Scope of wave 1 (0.17.0)

- Harness inventory published by the runner (#1480).
- Flows routed to a runner by harness and model (#1481).
- Remote sessions for `copilot_cli` only, started from the web console (#1482
  runner protocol, #1483 HTTP API and console).
- Workspaces: authorized directories on the host, or temporary checkouts of a
  tracker repository with a short-lived clone credential (#1484).
- Install prompt after `preloop login` and the account runner policy (#1479,
  EE #182).

Deferred to 0.18.0: system versus user service split (#1488), runner sharing
to teams (EE #183), MDM enrollment and device credential (#1344 track), more
session harnesses (#1489, #1490).

## Assets and actors

| Asset | Where it lives |
|---|---|
| Source code in authorized directories | Host disk |
| Harness login (Copilot OAuth token, GitHub tokens, keychain items) | Host user session, owned by the harness |
| Runner token | `~/.preloop/` on the host, rotatable with `preloop runner rotate-token` |
| Clone credential for a tracker checkout | Runner process memory, for one clone |
| Session events, approvals, audit | Preloop server |

| Actor | Trust |
|---|---|
| Host user (runner owner) | Trusted for their own machine; decides what is enabled |
| Account owner or admin | May start sessions on members' runners in wave 1 (T2) |
| Other account members | No session access to someone else's runner |
| The model and the harness | Untrusted output; may be steered by prompt injection in code or issues |
| A party holding a stolen Preloop session | Treated as the actor it impersonates; limited by T2 to T12 |

## Controls

### T1. Per-harness opt-in on the host

Remote sessions are off for every harness until the host user runs
`preloop runner sessions enable <harness>` on that machine. The server can
never set `sessions_enabled`; it only reads it from the inventory. A harness can
also be disabled entirely in `~/.preloop/runner.json`
(`"harnesses": {"<id>": {"enabled": false}}`); disabled harnesses are still
reported for compliance but are ignored by routing and sessions.
`preloop runner sessions disable <harness>` revokes at once. Both changes are
audited as `runner.sessions_enabled` and `runner.sessions_disabled` with the
host user as actor. Runner rejects with `harness_not_enabled_for_sessions`.
Implemented by #1482 (command and runner check) and #1480 (inventory field).

Consent text printed by `preloop runner sessions enable copilot_cli`, which then
asks `Enable? [y/N]` (default no):

```text
Allow remote Copilot CLI sessions on this machine?

Your account owner, account admins and you will be able to start GitHub Copilot
CLI sessions here from Preloop, using your Copilot seat and login. Sessions run
as you, only in directories you authorize with `preloop runner dirs add` or in
temporary checkouts. Every tool call goes through Preloop approvals and is
audited. You get a notification when a session starts.

Turn off any time: preloop runner sessions disable copilot_cli
```

### T2. Who may start a session

Wave 1 rule: the runner owner (`registered_by_user_id`) and account owners and
admins. Everyone else gets 403 `not_runner_owner`. The check is the authorizer
action `runner:session_start` (`ACTION_RUNNER_SESSION_START`) so team sharing
(EE #183) plugs in later without changing the endpoint. The admin path is
disclosed to the host user in the T1 consent text and in the T3 notice, which
names the actor. Implemented by #1483.

### T3. Host-visible notice

When the runner accepts a `session_start`, it writes a runner log line and
raises a desktop notification as the logged-in user:
`Preloop: <actor> started a <harness> session in <label>` (Windows toast, macOS
user notification, Linux `notify-send` when present). A failed notification is
logged and does not block the session. Implemented by #1482.

### T4. Workspace containment

A session runs either in an authorized directory or in a temporary checkout.
Authorized directories are listed only on the host (`preloop runner dirs
add|remove|list`); the server sees `{id, label, mode, harnesses}`, never the
path. Paths are resolved with realpath when loaded and again at use; symlinks
and Windows reparse points cannot escape; comparison is case-insensitive on
Windows; the filesystem root, a whole home directory and UNC admin shares are
refused. Unknown or out-of-scope ids fail with `workspace_not_authorized`.
Temporary checkouts live under `~/.preloop/host-workspaces/sessions/<id>` and
are deleted when the session ends. Implemented by #1484.

### T5. Short-lived clone credentials

For a tracker checkout the server mints a credential immediately before
`session_start`: a GitHub App installation token scoped to the one repository
with `contents: read` (at most 60 minutes), or a fresh Bitbucket Cloud OAuth
access token (about 2 hours). Long-lived secrets are never sent to a laptop:
GitHub PAT trackers fail with `checkout_requires_app_or_oauth`, Bitbucket
access tokens and app passwords fail with `checkout_requires_oauth`. The
credential travels only inside the authenticated runner websocket message, is
never stored server-side, never logged, never written to `.git/config` or any
file, is passed to git through an in-memory askpass channel and is zeroed after
the clone. Minting is audited as `runner_session.checkout_credential_minted`
(provider, repository, expiry; never the token). Implemented by #1484.

### T6. Harness credentials are never touched

The runner executes the user's own installed, unmodified harness binary. It
never reads, copies or forwards the harness login. Inventory reports only
`login_state` and `login_source`; for `env` it checks that a variable name is
set and never reads the value. The child environment is the existing host-exec
allowlist. Any code that reads a harness credential store is a review blocker.
Implemented by #1480 (inventory) and #1482 (session launch).

### T7. Mandatory approvals and deny-tool defaults

Remote sessions are a prompt injection target: the model reads code, issues and
tool output it did not write. A remote session starts only when the Preloop
approval hook (`preloop agents onboard "Copilot CLI" --approvals`) is installed
for the host user; otherwise the runner refuses (`copilot_approval_hook_missing`
in the existing host-exec code). Every tool call is evaluated by Preloop
policies and can require a console approval. The default deny-tool list from
the host-exec profile applies, deny always wins over allow, and
`--allow-all-tools` is never used without the hook. Implemented by #1482.

### T8. Copilot specifics

- `--no-remote` is always passed, so the session cannot also be steered through
  GitHub's own remote control.
- `--no-ask-user` and non-interactive `-p` turns; each turn is one process
  resumed with `--resume=<session uuid>`.
- BYOK variables (for example `COPILOT_PROVIDER_BASE_URL`) are stripped by the
  environment allowlist, so a seat session really uses GitHub-hosted models
  under the user's Copilot plan and policies.
- `--assisted-approval` is not used.

Implemented by #1482.

### T9. Limits

Defaults: at most 2 concurrent remote sessions per runner
(`max_concurrent_reached`), idle timeout 30 minutes (`idle_timeout`), a maximum
session duration (`max_duration`), one turn in flight per session (409
`turn_in_progress`), event payloads bounded to 64 KB and redacted by the
existing redaction. Implemented by #1482 and #1483.

### T10. Kill switch and stop

The account kill switch (see [Account kill switch](../guide/account-kill-switch.md))
applies to remote sessions: halting flows or agents ends live sessions with
`killed_by_kill_switch` and rejects new starts. The actor can stop a session
from the console (`graceful` or `kill`); the host user can stop it locally
(`stopped_on_host`). A stop mid-turn kills the harness process. Implemented by
#1483 (server) and #1482 (runner).

### T11. Audit

Every step is an audit event with `resource_type = runner_session` and details
that always carry actor, runner id and name, host, harness, model and workspace
kind and label: `runner_session.start_requested`, `.started`, `.rejected`,
`.turn_sent` (turn id and text length, not the text), `.stop_requested`,
`.ended` (with `end_reason`), `.checkout_credential_minted`, plus
`runner.sessions_enabled`, `runner.sessions_disabled` and
`runner.harness_inventory_changed`. Implemented by #1483 and #1482.

### T12. Revocation and policy

Ways to cut access, from narrowest to widest: stop the session (T10); disable
the harness for sessions on the host (T1); remove an authorized directory;
`preloop runner disable` or `stop` on the host; rotate the runner token
(`preloop runner rotate-token`) or delete the runner in the console; account
kill switch. The account runner policy (`runner_policy`: `optional`,
`required` or `forbidden`) decides whether the CLI asks, informs or stays
silent after login. Implemented by #1479 (login prompt and field) and EE #182
(policy and compliance view).

Consent text shown after `preloop login` when the policy is `optional` (#1479):

```text
Install a Preloop runner on this machine?

The runner starts at login, runs your Preloop flows locally and reports which
coding agents are installed here (names, versions, signed-in state; never your
credentials). Remote sessions stay off until you enable them per agent.

Install and start the runner now? [Y/n]
```

When the policy is `required`, the same text is printed without the question,
followed by the account message. When it is `forbidden`, one line says so.

## What leaves the machine

Sent to Preloop:

- Harness inventory: harness ids, versions, signed-in state, model ids,
  governance state. Never executable paths, argv, environment values or tokens.
- Authorized directory ids, labels, modes and harness lists. Never paths.
- Session events: agent messages, tool calls and results, usage, stderr,
  bounded and redacted (T9).
- Approval requests and decisions, turn summaries, audit events.

Not sent to Preloop: repository content as such. The harness itself sends what
it needs to its own model provider (GitHub for Copilot) under the user's seat,
as it would in a local session; tool results the agent produces can include
file excerpts and are visible in the session events.

## Laptop sleep and offline

- The runner reconnects after sleep or network loss and reports `session_state`
  for every live session, so delivery resumes.
- If the runner stays offline, the server ends its sessions with
  `runner_offline`; turns are not queued for an offline runner. Flows that need
  the harness follow their `harness_fallback` (`queue`, `fallback_server` or
  `fail`).
- Idle sessions end after the idle timeout (T9) whether or not the laptop is
  awake. A Copilot session file survives on disk, but a new turn requires a new
  session.
- Temporary checkouts are deleted when the session ends; if the runner was
  offline, cleanup happens on its next start.

## What wave 1 does not stop

- A user can uninstall or stop the runner, or use a harness on a machine that
  never enrolled. Wave 1 detects this, it does not prevent it: the compliance
  view (EE #182) shows members as `stale` or `never_connected` and lists
  ungoverned harnesses. Prevention belongs to the MDM track (#1181 managed-agent
  deployment recipes, #1186 managed-config bundles).
- An account admin can start a session on a member's runner without asking
  first. The member has consented (T1) and is notified (T3); per-session
  approval by the host user is not in wave 1.
- Anything the harness does outside a Preloop session, and what the harness
  sends to its own model provider.
- A compromised host user account: the runner runs as that user and inherits
  its access.
