# Self-hosted runner quickstart (macOS)

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Preloop CLI **is** the self-hosted runner on macOS: it registers with
your control plane, holds an outbound WebSocket, leases flow executions, and
runs them locally. On a Mac the primary execution mode is a
[host execution profile](#host-execution-profiles-on-macos): the runner
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
no journal). The plist sets a `PATH` that includes `~/.local/bin`,
`/opt/homebrew/bin` and `/usr/local/bin`, because launchd starts agents with
a minimal environment.

`preloop runner stop`, `restart` and `disable` (alias `uninstall`) manage the
agent with `launchctl`. A LaunchAgent runs while you are logged in; a Mac
that must run jobs with nobody logged in should use a Linux runner instead.

## Host execution profiles on macOS

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
[Linux host execution profile section](quickstart-linux.md#host-execution-profiles-opt-in-private-only)
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

- **Cursor profiles** run on the operator's own Cursor plan. Traffic goes to
  Cursor directly, so the run is not gateway metered and this path does not
  ingest usage; the structured result records the model only when Cursor
  reports one.
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
| CLI found in a terminal but not by the launchd runner | The agent CLI is outside the searched locations. Symlink it into `~/.local/bin` or install via Homebrew or npm. |
| Service dies after logout | LaunchAgents run per login session. Use a Linux runner with systemd lingering for always-on machines. |
| Docker flows report `docker is not available` | Host profiles do not need Docker. For the Docker harness, install Docker Desktop and make sure `docker info` succeeds as the runner user. |
