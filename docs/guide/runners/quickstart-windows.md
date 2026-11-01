# Self-hosted runner quickstart (Windows)

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Preloop CLI **is** the self-hosted runner on Windows too: it registers
with your control plane, holds an outbound WebSocket, leases flow executions,
and runs them locally. On a Windows developer laptop the primary execution
mode is a [host execution profile](#host-execution-profiles-on-windows-advanced-override): the
runner starts a locally installed agent CLI (Cursor, Copilot CLI, Claude Code, Codex, OpenCode or DeepSeek CLI) under your
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

## Install during login

On a terminal, `preloop login` asks whether to install a runner as a
background service. The default answer is no. The service is a scheduled
task named `PreloopRunner-<your user name>`, registered for your user
only and running as your user. It starts at logon, connects outbound to
the instance you just signed in to, can run flow executions on this
machine, and reports installed agent harnesses (names, versions, signed
in or not). It does not read or forward harness credentials. Remote
sessions stay off until you enable them per harness.

No elevation is needed: a standard (non-admin) account registers the task
for itself. If a device policy blocks task creation, the install prints an
error that names the policy (see [Task creation blocked by
policy](#task-creation-blocked-by-policy)) and this command, which you can
run later:

```text
preloop runner setup
```

Consent wording (what the runner may see and do):
<https://docs.preloop.ai/security/personal-runners/>.

An account policy of `required` installs without a question and prints
why. `forbidden` prints one line and does not install. Skip the step with
`--no-runner` or `PRELOOP_NO_RUNNER=1`. Headless login, `--token`,
`PRELOOP_TOKEN`, and a shell that is not a terminal never ask and never
install. A service that is already installed is left as it is.

Run the same step later with `preloop runner setup`. A decline is
remembered in `%USERPROFILE%\.preloop\config.yaml`
(`runner_prompt_answered_at`). `preloop login --force` asks again. Remove
the service with `preloop runner disable`.

The runner registers as the machine hostname with labels `personal`,
`os:<os>` and `arch:<arch>`.

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
`%USERPROFILE%\.preloop\runner-task.ps1`, a task definition to
`%USERPROFILE%\.preloop\runner-task.xml`, and registers the scheduled
task `PreloopRunner-<your user name>` from that definition
(`schtasks /Create /XML`). The task runs **as your user** in your
interactive session with least privilege, not as SYSTEM and not elevated,
so the agent CLI logins in your profile stay visible to host execution
profiles and remote sessions. Runner output is appended to
`%USERPROFILE%\.preloop\runner.log`. The launcher pins `HOME` and
`USERPROFILE` to the profile that ran `enable`, so the task always reads
that profile's `.preloop\config.yaml`. Run `enable` again after changing it.

`preloop runner stop`, `restart` and `disable` (alias `uninstall`) manage the
task with `schtasks`. `preloop runner status` prints the context the runner
runs in:

```text
install: task PreloopRunner-jane (Running)
context: logon task PreloopRunner-jane runs as DESKTOP-1\jane in the interactive session, least privilege (no admin rights)
lock: keeps running while the screen is locked
sign-out: ends with the session; the runner reports offline until the next logon
other users: each Windows user gets a separate task and runner identity
```

What the task definition guarantees, and what the `schtasks /SC ONLOGON`
default task of CLI 0.17.0 did not:

- **No elevation.** The logon trigger is scoped to your user and the
  principal is your interactive token at least privilege, which a standard
  user may register. An unscoped "any user" logon trigger needs
  administrator rights, which is why older installs asked for an elevated
  prompt.
- **No time limit.** The default 72 hour execution limit is off
  (`PT0S`); the runner keeps running until sign-out.
- **Battery does not matter.** The task starts and keeps running on
  battery power.
- **Lock keeps the runner online.** Locking the screen or letting the
  machine idle does not end the session; the task has no "stop on idle
  end" condition.
- **Sign-out takes it offline.** Signing out ends your interactive session
  and the runner with it. The runner says goodbye to the control plane on
  the way out, so the console shows it `offline` immediately; if Windows
  kills it first, the runner shows offline after the 45 second heartbeat
  grace. It comes back at your next logon. Fast user switching behaves
  like a lock for your session: the runner keeps running in the
  background while another user is active.
- **Each user has their own runner.** The task name carries the user
  name, so a second user on the same machine runs `preloop runner enable`
  and gets a separate task and a separate runner identity
  (`%USERPROFILE%\.preloop\runner.json` is per user). One user cannot see
  or stop another user's task.
- **Restart on failure.** If the runner process exits with an error, the
  Task Scheduler restarts it up to three times, one minute apart.

A task registered by CLI 0.17.0 under the shared name `PreloopRunner` is
still found by `status`, `stop` and `disable` when it belongs to you.
`status` prints an `upgrade:` line for it; re-run `preloop runner enable`
to replace it with the per-user task (the old one is removed).

The task starts at logon, not at boot: host execution runs under your
interactive login by design. A machine that must run jobs with nobody logged
in should use a Linux runner or a dedicated service account.

### Task creation blocked by policy

When the Task Scheduler refuses the registration, `preloop runner enable`
(and the login prompt) print one of these errors instead of asking for an
elevated prompt:

| Error | Meaning | What to do |
| --- | --- | --- |
| `task_creation_prohibited_by_policy` | The Group Policy "Prohibit New Task Creation" (`HKLM\SOFTWARE\Policies\Microsoft\Windows\Task Scheduler5.0\Task Creation` = 1) is set on this device. | Ask your administrator to exempt the device, or run the runner from a terminal with `preloop runner fg`. |
| `task_creation_denied` | Windows answered "Access is denied" without that policy: a different device policy or a hardened Task Scheduler folder blocks task creation for your account. | An administrator can register the written definition for you: `schtasks /Create /TN "PreloopRunner-<user>" /XML "%USERPROFILE%\.preloop\runner-task.xml" /F`. The task still runs as you. Or use `preloop runner fg`. |
| `task_scheduler_unavailable` | The Task Scheduler service (`Schedule`) is stopped or disabled. | Ask your administrator to enable it, or use `preloop runner fg`. |
| `task_creation_failed` | Any other `schtasks` failure; the output is included. | Report it with the output, use the administrator command above or `preloop runner fg`. |

### What the task sees

The task starts with the environment Windows builds for your account from
the registry (user and machine variables), not with what a PowerShell
profile adds, and the launcher runs `-NoProfile`. In practice:

- `PATH` additions made through System Properties, `setx`, or an
  installer (Node.js, winget, the Preloop installer) are visible.
  Additions made only in `$PROFILE` are not; the runner compensates by
  resolving harness binaries explicitly (next section).
- `COPILOT_GITHUB_TOKEN`, `GH_TOKEN`, `GITHUB_TOKEN`, `COPILOT_HOME` and
  `GH_HOST` are visible when set as user environment variables; the
  runner only checks whether the token variables exist and never reads
  their values.
- `%USERPROFILE%`, `%APPDATA%` and `%LOCALAPPDATA%` point at your
  profile, so the Copilot login record under `%USERPROFILE%\.copilot`
  and the Windows Credential Manager entries of your account are the
  same ones your interactive shell uses. The runner reads only the host
  name from the login record and never opens the credential store.

## Harnesses are detected automatically

The runner reports the agent harnesses installed for the user it runs as
(Copilot CLI, Cursor CLI, Claude Code and others) and generates host
execution profiles named `copilot`, `cursor` and `claude` for Copilot CLI,
Cursor CLI and a signed-in Claude Code, so no profile file is needed. Sign in to the harness as that user (for
Copilot CLI: `copilot login`, or set `COPILOT_GITHUB_TOKEN` for the runner
user), then check what the runner reports:

```powershell
preloop runner inventory --refresh
```

The Copilot login record is read from `%USERPROFILE%\.copilot\config.json`
(or `%COPILOT_HOME%\config.json`); only its host is read. To turn a harness
off, add `"harnesses": {"cursor_cli": {"enabled": false}}` to
`%USERPROFILE%\.preloop\runner.json`. What is checked and what is never
read is described in the
[Linux quickstart](quickstart-linux.md#harnesses-are-detected-automatically).

## Host execution profiles on Windows (advanced override)

A hand-written profile replaces the generated one for the same harness or
name. Use it to pin a working directory, map model aliases or grant tools.

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
[Linux host execution profile section](quickstart-linux.md#host-execution-profiles-advanced-override-private-only)
applies (profile validation, model mapping, prompt delivery, structured
completion, injection rejection). Windows specifics:

- **Executable detection.** The runner resolves `cursor-agent` / `agent` and
  `copilot` through `PATH` (respecting `PATHEXT`, so `.exe` and `.cmd` shims
  are found), then explicitly through the per-user install locations, in
  this order: `%APPDATA%\npm` (npm global), `%LOCALAPPDATA%\Microsoft\WinGet\Links`
  (winget), `%LOCALAPPDATA%\pnpm`, `%LOCALAPPDATA%\Volta\bin`,
  `%USERPROFILE%\.copilot\bin` (Copilot standalone installer),
  `%USERPROFILE%\.local\bin` and `%USERPROFILE%\scoop\shims`, trying
  `.exe`, `.cmd`, `.bat` and `.com`. `node.exe` for shim unwrapping is also
  looked for in `%ProgramFiles%\nodejs` and `%NVM_SYMLINK%`. This is what
  keeps discovery and sessions working under the logon task, whose `PATH`
  lacks anything a shell profile adds.
- **npm `.cmd` shims are unwrapped.** An npm-installed CLI such as
  `@github/copilot` is a `copilot.cmd` batch shim. Batch files receive their
  command line through `cmd.exe`, which cannot safely carry arbitrary prompt
  text, so the runner reads the shim and runs its Node script with
  `node.exe` directly: the `node.exe` beside the shim when there is one
  (nvm-windows, Volta, portable installs), otherwise `node` on `PATH`, with
  any interpreter flags the shim passes (such as `--no-warnings`).
- **Batch scripts fail closed.** If the profile resolves to a bare `.cmd` or
  `.bat` that is not an npm shim, a prompt containing quotes, percent signs,
  newlines or cmd.exe operators (`&`, `|`, `<`, `>`, `^`, `!`) is rejected
  before launch (`host_exec_batch_argument_unsafe`),
  and the whole command line is capped at 8000 characters. Native
  executables are capped at 30000 characters
  (`host_exec_command_too_long`); Windows command lines cannot exceed the
  32767 character CreateProcess limit, so very large prompts need the
  Docker harness on a Linux runner.
- **Executable rules.** A profile `executable` is a command name or a fully
  qualified path (`C:\...` or `\\server\share\...`); relative and
  drive-relative spellings such as `C:copilot.cmd` are rejected. It must
  resolve to a `.exe`, `.cmd`, `.bat` or `.com`; PowerShell scripts
  (`.ps1`) are refused with `host_exec_executable_unsupported`, so point
  the profile at the CLI's `.cmd` shim or `.exe` instead.
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
  The runner pins the job's PID with a process handle first and skips
  taskkill once the runner has reaped the job, so a recycled PID never
  points it at an unrelated process.
- **Copilot hooks run under PowerShell.** The Preloop usage hooks written to
  `%USERPROFILE%\.copilot\hooks\preloop.json` (or `%COPILOT_HOME%\hooks`)
  use the `powershell` command form on Windows; no bash is required
  anywhere on this path. A profile with `"allow_all_tools": true` requires
  the Preloop approval hook under the `powershell` key; a `bash` entry left
  by an older onboarding never runs on Windows and does not count, so
  re-run approval onboarding after upgrading.

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
| `copilot_not_logged_in` | Run `copilot` once and `/login` as the runner user, or set `COPILOT_GITHUB_TOKEN` in the environment the runner (or its scheduled task) starts with; `COPILOT_*`, `GH_*` and `GITHUB_TOKEN` reach Copilot jobs without `pass_env`. |
| `host_exec_batch_argument_unsafe` | The CLI resolved to a bare batch script. Install the native `.exe`, or install via npm so the shim can be unwrapped to `node.exe`. |
| `task_creation_denied` or `task_creation_prohibited_by_policy` on `runner enable` | No elevation is involved; a device policy blocks task creation. See [Task creation blocked by policy](#task-creation-blocked-by-policy). |
| `runner status` shows `upgrade: the task stops the runner after PT72H` | The task was registered by CLI 0.17.0. Re-run `preloop runner enable` to replace it with the per-user task. |
| The runner is offline after signing in on a second account | Each account needs its own `preloop login` and `preloop runner enable`; the task and runner identity are per user. |
| A stopped execution leaves processes behind | Report it: halt uses `taskkill /T /F` and should kill the whole tree. `runner.log` records the runner side. |
| Docker flows report `docker is not available` | Host profiles do not need Docker. For the Docker harness, install Docker Desktop and make sure `docker info` succeeds as the runner user. |


### Codex, OpenCode and DeepSeek native profiles

Use the shared [native profile configuration](quickstart-linux.md#codex-opencode-and-deepseek-native-profiles)
with the Windows `codex`, `opencode` or `dsh` executable installed and signed
in as the runner user. Set an explicit host profile and a private runner
pool; Docker and WSL are not required. Codex also requires its native Windows
sandbox setup. The shared guide describes model maps, Preloop governance,
Windows version guidance and the limits of fixture-based acceptance testing.
