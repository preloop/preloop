# Your machine as a Preloop runner

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

This page is for a developer who signed in with `preloop login` and was asked
whether to install a runner. It explains what that runner is, what it reports,
what it can run, and how to undo all of it. Operators setting up a shared,
server-like runner should start with the
[Linux](quickstart-linux.md), [macOS](quickstart-macos.md) or
[Windows](quickstart-windows.md) quickstart instead.

## The login prompt and what it installs

After an interactive `preloop login`, the CLI asks whether to install a runner
on this machine. The default answer is no. If you answer yes:

- A background service is installed for your user: a launchd agent on macOS,
  a systemd user service on Linux, a logon task for your user on Windows. It
  starts when you log in and needs no administrator rights.
- It connects outbound only, to the instance you signed in to. Nothing listens
  for inbound connections.
- It registers as the machine hostname with the labels `personal`,
  `os:<os>` and `arch:<arch>`.
- It can run flow executions on this machine (see
  [Flows on your machine](#flows-on-your-machine)).
- It reports which agent harnesses are installed (see
  [Inventory](#inventory-what-is-reported-and-what-is-not)).
- Remote sessions stay off until you enable them for a harness.

Setup also asks whether flows on this runner need your approval. The answer is
stored as the runner's flow execution consent:

| Value | Meaning |
|---|---|
| `ask_me` (default) | Each flow execution that would run here first sends you an approval request. It runs only after you approve. |
| `always` | Flows that may use this runner run without asking. |
| `never` | This runner takes no flows. Remote sessions are unaffected. |

Your account's runner policy can change the question. `required` installs
without asking whether to install (you still choose the consent), `forbidden`
does not install. `--no-runner`, `PRELOOP_NO_RUNNER=1`, `preloop login
--headless`, `--token`, `PRELOOP_TOKEN` and a shell without a terminal never
install anything.

Run the same step later, or change your answer:

```bash
preloop runner setup
preloop runner setup --yes --flow-execution-consent=ask_me   # unattended
preloop runner status                                        # service state and last heartbeat
```

A decline is remembered, so the prompt does not come back on every login.
`preloop login --force` asks again. The consent text the prompt links to is in
[Personal runners: threat model and controls](../../security/personal-runners.md).

## Inventory: what is reported and what is not

The runner reports the agent harnesses installed for your user when it starts,
every 6 hours, and within a minute of `preloop runner inventory --refresh` or
`preloop agents onboard` / `offboard`. See it exactly as Preloop sees it:

```bash
preloop runner inventory
preloop runner inventory --json
preloop runner inventory --refresh
```

Reported, per harness: the harness id and version, whether it is enabled on
this runner, whether it is signed in, its governance state (`governed`,
`partial` or `ungoverned`, the same check `preloop agents status` uses), what it
supports (flows, sessions or both), how it is billed (for example `seat`) and
the model ids it lists.

Never reported: executable paths, command lines, environment values, user
names, tokens or any other credential. The login check only tests whether a
login exists (an environment variable is set, or the harness's own status
command says so). The runner never opens a credential store and never reads,
copies or forwards a harness token. Authorized directories are reported by id,
label, mode and harness list, never by path.

To stop the runner offering a harness, mark it disabled in
`~/.preloop/runner.json` as described in
[Turning a harness off](quickstart-linux.md#turning-a-harness-off). It is still
listed, marked disabled.

## Flows on your machine

A flow reaches your runner in one of two ways:

- **By name.** The flow pins your runner (`runner_id`) or names it in
  `runner_pool`. A member's own runner only takes flows that name it; flows
  that name no runner go to runners registered by an account admin.
- **By harness.** The flow sets `agent_config.harness` (for example
  `copilot_cli`, `cursor_cli`, `claude_code` or `devin_cli`) and optionally a
  model. Only runners whose inventory reports that harness as enabled,
  signed in and runnable are eligible. The console calls this
  **Route by harness**; the picker shows how many runners you may use are
  online and signed in, and per model how many list it.

The runner builds the command line itself from a generated host profile
(`copilot`, `cursor`, `claude`, `devin`). The server never sends an executable
or arguments. Hand-written profiles in `~/.preloop/runner-host-profiles.json`
are an advanced override, not a requirement; see
[Host execution profiles](quickstart-linux.md#host-execution-profiles-advanced-override-private-only).

When no eligible runner is available, the flow's `harness_fallback` decides:
`queue` (default, wait up to `harness_queue_timeout_seconds`, then fail with
`queue_timeout`), `fallback_server` (run on hosted compute with
`fallback_model_identifier`, metered by the gateway) or `fail`. The routing
reasons shown on the execution are listed in
[Copilot CLI: when no runner is eligible](../copilot-cli.md#when-no-runner-is-eligible).
Model selection rules for flows are in [Model routing](../flows/model-routing.md).

## Bring your own seat, and how runs are labelled

A harness on your machine runs under your own login and plan. Preloop does not
supply or proxy that seat.

| Harness | Who is billed | What Preloop records |
|---|---|---|
| Copilot CLI | Your Copilot seat (premium requests) | Session id, reported model and premium request count. The execution page shows **Not gateway metered**. |
| Cursor CLI | Your Cursor plan | The model when Cursor reports one. Not gateway metered. |
| Devin CLI | Your Cognition account | Not gateway metered. |
| Claude Code, Codex onboarded through Preloop | The model provider behind the Preloop gateway | Gateway metered per execution (`gateway_metered: true`); flow run limits apply. |

A Claude Code or Codex run that calls its model service directly instead of
through the onboarded gateway configuration fails with `gateway_bypassed`.
Details per platform are in
[What is and is not metered](quickstart-macos.md#what-is-and-is-not-metered).

## Remote sessions

A remote session is a coding agent session that you, your account owner or an
account admin start from Preloop, running on your machine as your user with
your agent CLI and its login. Sessions are off for every harness until you
enable them on the machine itself; the server can never turn them on.

```bash
preloop agents onboard "Copilot CLI" --approvals   # once: the approval hook
preloop runner sessions enable copilot_cli
preloop runner dirs add ~/src/my-project --label my-project
preloop runner sessions status
```

`enable` prints what it allows and asks `Enable? [y/N]`. Sessions run only in
directories you authorize with `preloop runner dirs add` (`--mode read_only` or
`write`, `--harness` to restrict harnesses) or in temporary tracker checkouts
that are deleted when the session ends. Every tool call goes through Preloop
approvals and is audited, and you get a notification when a session starts.

Which harnesses host sessions, and how a turn runs for each (the per-harness
mode table), is in
[Remote sessions on a personal runner](remote-sessions.md#how-a-session-works).
To follow or answer a session from a terminal, see
[Runner-hosted sessions](../sessions-attach.md#runner-hosted-sessions).

## Presence and sleep

The runner sends a heartbeat every 15 seconds. The console shows one state per
runner: `online`, `busy`, `idle` (no user input for 5 minutes or more),
`asleep` (the runner said it was going to sleep) or `offline` (no heartbeat for
45 seconds). Only `online`, `busy` (when a slot frees) and `idle` runners get
new work. See [Runner presence](quickstart-linux.md#runner-presence).

On wake the runner reconnects and returns to `online` on its first heartbeat.

## Undo everything

```bash
preloop runner sessions disable copilot_cli   # per harness; ends its live sessions
preloop runner dirs remove ~/src/my-project   # by id or path
preloop runner disable                        # stop and remove the service
preloop runner disable --delete               # also delete the runner on the server
preloop agents offboard "Copilot CLI"         # restore the agent's local config
```

`preloop runner disable --delete` refuses while the runner still holds an
execution; add `--force` to halt those executions. After `--delete` the
runner's token stops working and its local state is removed. To refuse every
remote session on the machine regardless of per-harness settings, set
`"sessions": {"enabled": false}` in `~/.preloop/runner.json`. An account admin
can also stop everything at once with the
[account kill switch](../account-kill-switch.md).

## FAQ

### Does Preloop see my code?

Not as such. Preloop receives the harness inventory, authorized directory ids
and labels, and, for remote sessions, the session events: agent messages, tool
calls and their results, usage and stderr, bounded and redacted. Tool results
can contain file excerpts, and those are visible in the session events. The
harness itself sends what it needs to its own model provider under your login,
as it would in a local session. The full list is in
[What leaves the machine](../../security/personal-runners.md#what-leaves-the-machine).

### Does this use my Copilot seat?

Yes. Copilot CLI flows and sessions on your machine run under your Copilot
login. Model traffic goes to GitHub and is billed as your seat's premium
requests. Preloop records the session id, model and premium request count, and
labels the run **Not gateway metered**. Preloop never reads or stores your
Copilot token. If you do not want that, keep `copilot_cli` disabled for
sessions (the default), and set flow execution consent to `ask_me` or `never`.

### What happens when my laptop sleeps?

The runner shows as `asleep` or `offline` and gets no new work. A flow routed
by harness follows its `harness_fallback`: it waits (`queue`), runs on hosted
compute (`fallback_server`) or fails (`fail`). Live remote sessions are kept
on disk; if the runner comes back within the idle timeout the next turn resumes
the same session, otherwise the server ends it with `runner_offline`. Turns are
not queued for an offline runner. Temporary checkouts are cleaned up on the
runner's next start.

### How do I turn off remote sessions?

`preloop runner sessions disable <harness>` for one harness. It takes effect
within seconds and ends that harness's live sessions with `stopped_on_host`.
For all harnesses at once, set `"sessions": {"enabled": false}` in
`~/.preloop/runner.json`. Removing the runner with `preloop runner disable`
also ends them.
