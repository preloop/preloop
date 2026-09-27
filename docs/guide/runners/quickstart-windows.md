# Self-hosted runner quickstart (Windows)

The Preloop CLI **is** the self-hosted runner on Windows too: it registers
with your control plane, holds an outbound WebSocket, leases flow executions,
and runs them locally. On a Windows developer laptop the primary execution
mode is a [host execution profile](#host-execution-profiles-on-windows): the
runner starts a locally installed agent CLI (Cursor, Copilot CLI) under your
own login. Docker execution also works when a Docker engine is available
(Docker Desktop runs the same Linux agent containers), but it is not required
for host profiles.

Shared concepts (flow routing, concurrency, ephemeral CI mode, completion
contract) are documented once in the
[Linux quickstart](quickstart-linux.md); this page covers what is different
on Windows.

## Requirements

- Windows 10 or 11 (x64 or arm64), PowerShell 5.1 or later.
- Outbound HTTPS (443) to your Preloop control plane. Nothing inbound.
- For host execution profiles: the agent CLI installed and signed in as the
  same OS user that runs the runner.
- Optional: Docker Desktop, only if flows should use the Docker harness.

## 1. Install the CLI

```powershell
irm https://preloop.ai/install/cli.ps1 | iex
```

The installer verifies checksums and installs to
`%LOCALAPPDATA%\Preloop\bin\preloop.exe`. Details, version pinning and a
source build are in [Windows CLI install](../../windows-cli.md).

## 2. Authenticate against your control plane

```powershell
$env:PRELOOP_URL = 'https://preloop.example.com'   # your control plane
preloop login --headless                            # prints a URL, paste the code
# CI/service accounts can set an API token instead:
$env:PRELOOP_TOKEN = '<account-api-token>'
```

## 3. Run the runner in the foreground (first test)

```powershell
preloop runner fg --labels laptop --name $env:COMPUTERNAME
```

You should see `Connected. Waiting for jobs.` The runner stores its identity
in `%USERPROFILE%\.preloop\runner.json`; restarts resume the same runner.
Route a flow to it exactly as on Linux
([route a flow to the runner](quickstart-linux.md#4-route-a-flow-to-the-runner)).

## 4. Install as a scheduled task (survives reboots)

```powershell
preloop runner enable     # or: preloop runner install
preloop runner start
preloop runner status
```

`enable` writes a PowerShell launcher to
`%USERPROFILE%\.preloop\runner-task.ps1` and registers the scheduled task
`PreloopRunner` to run it at logon. The task runs **as your user**, not as
SYSTEM, so the agent CLI logins in your profile stay visible to host
execution profiles. Runner output is appended to
`%USERPROFILE%\.preloop\runner.log`.

`preloop runner stop`, `restart` and `disable` (alias `uninstall`) manage the
task with `schtasks`. Creating a logon task may require an elevated
PowerShell prompt depending on the machine's policy; the error from
`schtasks` says so explicitly.

The task starts at logon, not at boot: host execution runs under your
interactive login by design. A machine that must run jobs with nobody logged
in should use a Linux runner or a dedicated service account.

## Host execution profiles on Windows

The profile file is `%USERPROFILE%\.preloop\runner-host-profiles.json` (or
point `PRELOOP_RUNNER_HOST_PROFILES` at an absolute path):

```json
{
  "profiles": [
    {
      "name": "cursor-ask",
      "executable": "cursor-agent",
      "argv": ["--print", "--output-format", "stream-json", "--mode=ask"],
      "workspace_root": "C:\\Users\\jane\\src",
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
completion, injection rejection). Windows specifics:

- **Executable detection.** The runner resolves `cursor-agent` / `agent` and
  `copilot` through `PATH` (respecting `PATHEXT`, so `.exe` and `.cmd` shims
  are found), then through `%APPDATA%\npm`, `%USERPROFILE%\.copilot\bin` and
  `%USERPROFILE%\.local\bin`, trying `.exe`, `.cmd`, `.bat` and `.com`.
- **npm `.cmd` shims are unwrapped.** An npm-installed CLI such as
  `@github/copilot` is a `copilot.cmd` batch shim. Batch files receive their
  command line through `cmd.exe`, which cannot safely carry arbitrary prompt
  text, so the runner reads the shim and runs its Node script with
  `node.exe` directly.
- **Batch scripts fail closed.** If the profile resolves to a bare `.cmd` or
  `.bat` that is not an npm shim, a prompt containing quotes, percent signs
  or newlines is rejected before launch (`host_exec_batch_argument_unsafe`),
  and the whole command line is capped at 8000 characters. Native
  executables are capped at 30000 characters
  (`host_exec_command_too_long`); Windows command lines cannot exceed the
  32767 character CreateProcess limit, so very large prompts need the
  Docker harness on a Linux runner.
- **Workspace.** `workspace_root` is optional. When omitted, job workspaces
  are created under `%USERPROFILE%\.preloop\host-workspaces`, which inherits
  your user profile's ACLs (other non-admin users cannot read it). Each job
  still gets a fresh `.preloop-host-exec\<execution_id>` directory; reuse
  and symlinks are rejected.
- **Environment isolation.** The job does not inherit your full environment.
  It receives a system baseline (`PATH`, `PATHEXT`, `SYSTEMROOT`, `TEMP`,
  `APPDATA`, and similar), the harness's own variables (`CURSOR_*` for
  Cursor; `COPILOT_*`, `GH_*` and `GITHUB_TOKEN` for Copilot, minus the
  BYOK overrides), proxy and TLS variables, and any names the profile lists
  in `"pass_env"`. `PRELOOP_TOKEN` and unrelated secrets in your session
  never reach the job.
- **Halt kills the process tree.** Stop, cancellation and deadline expiry
  use `taskkill /T /F`, so the agent CLI and every descendant (node, tools,
  spawned shells) are terminated, matching the Unix process-group kill.
- **Copilot hooks run under PowerShell.** The Preloop usage hooks written to
  `%USERPROFILE%\.copilot\hooks\preloop.json` (or `%COPILOT_HOME%\hooks`)
  use the `powershell` command form on Windows; no bash is required
  anywhere on this path.

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
| `copilot_not_installed` in the execution log | Install with `npm install -g @github/copilot` as the runner user, or put `copilot.exe` on `PATH`. |
| `copilot_not_logged_in` | Run `copilot` once and `/login` as the runner user, or set `COPILOT_GITHUB_TOKEN` in the profile's `pass_env` source environment. |
| `host_exec_batch_argument_unsafe` | The CLI resolved to a bare batch script. Install the native `.exe`, or install via npm so the shim can be unwrapped to `node.exe`. |
| `schtasks: ... Access is denied` on `runner enable` | Run the command from an elevated PowerShell prompt once. The task itself still runs as your user. |
| A stopped execution leaves processes behind | Report it: halt uses `taskkill /T /F` and should kill the whole tree. `runner.log` records the runner side. |
| Docker flows report `docker is not available` | Host profiles do not need Docker. For the Docker harness, install Docker Desktop and make sure `docker info` succeeds as the runner user. |
