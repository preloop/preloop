# Self-hosted runner quickstart (macOS)

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Preloop CLI **is** the self-hosted runner on macOS: it registers with
your control plane, holds an outbound WebSocket, leases flow executions, and
runs them locally. On a Mac the primary execution mode is a
[host execution profile](#host-execution-profiles-on-macos-advanced-override): the runner
starts a locally installed agent CLI (Cursor, Copilot CLI) under your own
login. Docker execution also works when Docker Desktop (or another engine)
is installed, but host profiles do not need it.

Shared concepts (flow routing, concurrency, ephemeral CI mode, completion
contract) are documented once in the
[Linux quickstart](quickstart-linux.md); this page covers what is different
on macOS.

## Requirements

- macOS 13 or later (Apple silicon or Intel).
- Outbound HTTPS (443) to your Preloop control plane. Nothing inbound.
- For host execution profiles: the agent CLI installed and signed in as the
  same user that runs the runner.
- Optional: Docker Desktop, only if flows should use the Docker harness.

## Docker capability and job selection

The runner advertises `docker` only when `docker ps --quiet` succeeds within five seconds, both on registration and on heartbeats. A CLI on PATH is insufficient: Docker Desktop must be running and the runner user must be able to connect to its daemon. Container flows are never leased to a runner without that capability. Native Cursor, Copilot, and Devin jobs can still run in managed temporary workspaces or authorized directories.

The LaunchAgent includes `~/.docker/bin` and Docker Desktop's application bin directory on PATH. The runner also checks those locations when launched by an older service definition. macOS has no general installation-time Docker permission grant; check `docker ps` as the runner user and start Docker Desktop if needed. Custom `DOCKER_HOST` or `DOCKER_CONTEXT` settings must also be present in the service environment, not only in your interactive shell.

Installed, enabled and signed-in agents add tags such as `cursor_cli`, `copilot_cli`, `devin_cli`, `codex_cli`, `claude_code` and `opencode`. Presence of a tag does not change how that adapter executes: container adapters still require `docker`. In **Runners**, **Pause runner** stops new work while current jobs finish. **Untagged jobs** is off by default; enable it to accept jobs without a pool tag or native harness/profile selection.

### Upgrading existing runners

The migration also sets **Untagged jobs** to off for existing runners. To keep receiving unpinned flows, enable that toggle in **Runners** on each intended runner after upgrading. Without it, auto routing uses hosted compute, or fails if hosted compute is unavailable. Explicit tags, runner pins, and native harness/profile selections continue to select private runners. Upgrade the runner CLI so it can report Docker availability; older clients that omit it cannot receive container jobs. Native profiles, including Git-based Copilot publication, work without Docker.

## 1. Install the CLI

```sh
curl -fsSL https://preloop.ai/install/cli | sh
# or, from source:
go install github.com/preloop/preloop/cli/cmd/preloop@latest
```

## 2. Authenticate against your control plane

```sh
export PRELOOP_URL=https://preloop.example.com   # your control plane
preloop login --headless                          # prints a URL, paste the code
# CI/service accounts can set an API token instead:
export PRELOOP_TOKEN=<account-api-token>
```

## Install during login

On a terminal, `preloop login` asks whether to install a runner as a
background service. The default answer is no. The service is a launchd
agent in your login session. It starts on login, connects outbound to the
instance you just signed in to, can run flow executions on this machine,
and reports installed agent harnesses (names, versions, signed in or not).
It does not read or forward harness credentials. Remote sessions stay off
until you enable them per harness.

Consent wording (what the runner may see and do):
<https://docs.preloop.ai/security/personal-runners/>.

An account policy of `required` installs without a question and prints
why. `forbidden` prints one line and does not install. Skip the step with
`--no-runner` or `PRELOOP_NO_RUNNER=1`. Headless login, `--token`,
`PRELOOP_TOKEN`, and a shell that is not a terminal never ask and never
install. A service that is already installed is left as it is.

Run the same step later with `preloop runner setup`. A decline is
remembered in `~/.preloop/config.yaml` (`runner_prompt_answered_at`).
`preloop login --force` asks again. Remove the service with
`preloop runner disable`.

The runner registers as the machine hostname with labels `personal`,
`os:<os>` and `arch:<arch>`.

## 3. Run the runner in the foreground (first test)

```sh
preloop runner fg --labels laptop --name $(hostname)
```

You should see `Connected. Waiting for jobs.` The runner stores its identity
in `~/.preloop/runner.json`; restarts resume the same runner. Route a flow to
it exactly as on Linux
([route a flow to the runner](quickstart-linux.md#4-route-a-flow-to-the-runner)).

## 4. Install as a launchd agent (survives reboots)

```sh
preloop runner enable     # or: preloop runner install
preloop runner start
preloop runner status
```

`enable` writes `~/Library/LaunchAgents/ai.preloop.runner.plist` and loads
it. It is a launchd **agent** in your login session, not a system daemon, so
the operator's agent CLI logins and keychain stay visible to host execution
profiles. Runner output is appended to `~/.preloop/runner.log` (launchd has
no journal). The plist sets a `PATH` that includes `~/.local/bin`, `~/.opencode/bin`,
`/opt/homebrew/bin` and `/usr/local/bin`, because launchd starts agents with
a minimal environment. It also pins `HOME` to the home directory of the user
who ran `enable`, so the agent always reads that user's
`~/.preloop/config.yaml`. Run `enable` again after changing `HOME`.

`preloop runner stop`, `restart` and `disable` (alias `uninstall`) manage the
agent with `launchctl`. A LaunchAgent runs while you are logged in; a Mac
that must run jobs with nobody logged in should use a Linux runner instead.

## Harnesses are detected automatically

The runner reports the agent harnesses installed for the user it runs as
(Copilot CLI, Cursor CLI, Claude Code and others) and generates host
execution profiles named `copilot`, `cursor` and `claude` for Copilot CLI,
Cursor CLI and a signed-in Claude Code, so no profile file is needed. Sign in to the harness as that user, then
check what the runner reports:

```bash
preloop runner inventory --refresh
```

Because harness logins live in the user's session (keychain, `~/.copilot`),
run the runner as a LaunchAgent for that user, as installed by
`preloop runner enable`. What is checked and what is never read is described
in the [Linux quickstart](quickstart-linux.md#harnesses-are-detected-automatically).

## Host execution profiles on macOS (advanced override)

A hand-written profile replaces the generated one for the same harness or
name. Use it to pin a working directory, map model aliases or grant tools.

The profile file is `~/.preloop/runner-host-profiles.json` (or point
`PRELOOP_RUNNER_HOST_PROFILES` at an absolute path):

```json
{
  "profiles": [
    {
      "name": "cursor-ask",
      "executable": "cursor-agent",
      "argv": ["--print", "--output-format", "stream-json", "--mode=ask"],
      "workspace_root": "/Users/jane/src",
      "timeout_seconds": 1800,
      "model_map": {"team-fast": "sonnet-4.6"}
    },
    {
      "name": "copilot-review",
      "executable": "copilot",
      "model_map": {"team-default": "claude-sonnet-4.6"},
      "allow_tools": ["shell(git:*)"],
      "deny_tools": ["shell(git push)"]
    }
  ]
}
```

Everything in the
[Linux host execution profile section](quickstart-linux.md#host-execution-profiles-advanced-override-private-only)
applies (profile validation, model mapping, prompt delivery, structured
completion, injection rejection, process-group halt). macOS specifics:

- **Executable detection.** The runner resolves `cursor-agent` / `agent` and
  `copilot` through `PATH`, then through `~/.local/bin`,
  `~/.npm-global/bin`, `~/.copilot/bin`, `~/Library/pnpm`, nvm-managed Node
  installs, `/opt/homebrew/bin` and `/usr/local/bin`. A runner started by
  launchd finds Homebrew- and npm-installed CLIs even though launchd's own
  `PATH` is minimal. The CLI's own directory is put at the front of the
  job's `PATH`, so a CLI installed under nvm, fnm or Volta finds the `node`
  its `#!/usr/bin/env node` line asks for.
  Standalone OpenCode installs in `~/.opencode/bin` are also detected when
  the service PATH omits that directory. After upgrading the CLI, restart
  the runner so its inventory and capability tags use the updated detection;
  an existing LaunchAgent does not need to be reinstalled for this fallback.
- **Workspace.** `workspace_root` is optional. When omitted, job workspaces
  are created under `~/.preloop/host-workspaces` (mode 0700). Each job still
  gets a fresh `.preloop-host-exec/<execution_id>` directory; reuse and
  symlinks are rejected.
- **Environment isolation.** The job does not inherit your full environment.
  It receives a system baseline (`HOME`, `PATH`, `TMPDIR`, locale), the
  harness's own variables (`CURSOR_*` for Cursor; `COPILOT_*`, `GH_*` and
  `GITHUB_TOKEN` for Copilot, minus the BYOK overrides), proxy and TLS
  variables, and any names the profile lists in `"pass_env"`.
  `PRELOOP_TOKEN` and unrelated secrets in your session never reach the job.

## What is and is not metered

- **Codex and Claude Code profiles** onboarded through Preloop
  (`preloop agents onboard "Codex CLI"` / `"Claude Code"` as the runner user)
  send model traffic through the Preloop gateway with the agent's own key.
  The runner adds the execution marker to each request, so usage is metered
  per execution, the flow's run limits apply, and the result records
  `gateway_metered: true`. Keep the onboarded configuration: a CLI that calls
  its model service directly fails the run with `gateway_bypassed`
  (`agent_config.allow_unmetered_host_model: true` is the documented
  break-glass for a flow that must run unmetered; never in read-only review).
- **Cursor profiles** run on the operator's own Cursor plan. Traffic goes to
  Cursor directly, so the run is not gateway metered and this path does not
  ingest usage; the structured result records the model only when Cursor
  reports one. Native flow profiles may need `"argv": ["--trust"]` to
  acknowledge Cursor's workspace trust prompt for new workspaces. Remote
  sessions supply this option after checking host authorization and the
  installed approval hooks; native flow profiles retain the operator's
  explicit trust configuration.
- **Copilot CLI profiles** run under the operator's Copilot seat. Model
  traffic goes to GitHub, billed as the seat's premium requests; the result
  records the session id, the reported model and the premium request count,
  and the execution page shows "Not gateway metered". The Preloop usage
  hooks record the session lifecycle.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `copilot_not_installed` in the execution log | `npm install -g @github/copilot` or `brew install copilot-cli` as the runner user. |
| `copilot_not_logged_in` | Run `copilot` once and `/login` as the runner user, or export `COPILOT_GITHUB_TOKEN` where the runner starts. |
| `claude_approval_hook_missing` | Run `preloop agents onboard "Claude Code" --approvals` as the runner user. |
| `claude_not_logged_in` | Run `claude auth login` as the runner user, then `preloop runner inventory --refresh`. |
| CLI found in a terminal but not by the launchd runner | The agent CLI is outside the searched locations. Symlink it into `~/.local/bin` or install via Homebrew or npm. |
| Service dies after logout | LaunchAgents run per login session. Use a Linux runner with systemd lingering for always-on machines. |
| Docker flows report `docker is not available` | Host profiles do not need Docker. For the Docker harness, install Docker Desktop and make sure `docker ps` succeeds as the runner user. |
