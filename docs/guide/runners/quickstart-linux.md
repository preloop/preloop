# Self-hosted runner quickstart (plain Linux / Proxmox)

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The Preloop CLI **is** the self-hosted runner. It registers itself with
your Preloop control plane, holds an outbound WebSocket, leases flow
executions for your account, and runs the agent in a local Docker
container: same model as GitHub/GitLab self-hosted runners. No inbound
ports, no Kubernetes.

Running on a developer laptop instead? The
[Windows quickstart](quickstart-windows.md) and
[macOS quickstart](quickstart-macos.md) cover those platforms, where host
execution profiles are the primary mode.

## Requirements

- Linux x86_64 or arm64 (bare metal, VM, or Proxmox guest).
- Docker Engine (`docker info` must succeed as the runner user).
- Outbound HTTPS (443) to your Preloop control plane. Nothing inbound.
- systemd, if you want the managed service mode (`runner enable`).

### Proxmox notes

- **VM (recommended):** any Linux VM works as-is. Install Docker, done.
- **LXC container:** Docker-in-LXC needs a *privileged* container or an
  unprivileged one with nesting enabled:

  ```sh
  # on the Proxmox host, for container 105
  pct set 105 --features nesting=1,keyctl=1
  pct restart 105
  ```

  If `docker info` fails inside the container after this, use a VM:   the runner refuses jobs when Docker is unavailable and reports
  `docker is not available` back to the execution log.

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
# CI/service accounts can skip login and set an API token instead:
export PRELOOP_TOKEN=<account-api-token>
```

Precedence: `--token`/`--url` flags > `PRELOOP_TOKEN`/`PRELOOP_URL` env >
`~/.preloop/config.yaml` (written by `preloop login`).

## Install during login

On a terminal, `preloop login` asks whether to install a runner as a
background service. The default answer is no. The service is a systemd
user unit. It starts on login, connects outbound to the instance you just
signed in to, can run flow executions on this machine, and reports
installed agent harnesses (names, versions, signed in or not). It does
not read or forward harness credentials. Remote sessions stay off until
you enable them per harness.

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
preloop runner fg --labels local --name $(hostname)
```

Add `--concurrency N` to run more than one execution at a time.

You should see `Runner <name> (<id>) connecting...` then
`Connected. Waiting for jobs.` The runner registers itself on first run
and stores its identity in `~/.preloop/runner.json`; restarts resume the
same runner. If the WebSocket drops (proxy idle timeout, laptop sleep,
control-plane restart), the process reconnects with backoff instead of
exiting; a job already running in Docker keeps going and reports
complete on the new socket. The console Runners page updates online/offline
status over the account websocket without a refresh. Ctrl-C unregisters
cleanly.

## 4. Route a flow to the runner

Private runners are the default. Once this runner is online, a flow with
no `runner_pool` (and no account default of `server`) leases to any
online private runner. Pin a pool only when you want a specific machine
or label, or set `server` to opt into hosted compute:

```json
{ "runner_pool": "local" }
```

The account default is on the console Runners page. Override per-run
from CI / the CLI:

```sh
preloop flow trigger <flow-id-or-name> --runner local --wait
```

When stdin is not a TTY (CI), `flow trigger` waits by default, streams
execution logs to stdout, and exits non-zero on FAILED / STOPPED /
TIMEOUT. If the CI job is cancelled, the CLI stops the execution
before exiting (see [cancelled CI jobs](../flows/ci-trigger.md#cancelled-ci-jobs-stop-the-execution)).
If no runner in the chosen private pool has a free slot, the job queues
for 15 minutes and then fails. Hosted compute is used only when no
private runner is online, or when the flow or account default is
`server`.

## 5. Install as a service (survives reboots)

```sh
preloop runner enable    # writes a systemd user unit + enables it
preloop runner start
preloop runner status    # service state + last heartbeat + current execution
```

The unit pins `HOME` to the home directory of the user who ran `enable`, so
the service always reads that user's `~/.preloop/config.yaml`. Run `enable`
again after changing `HOME`.

`preloop runner stop`, `restart`, and `disable` do what they say.
`preloop runner status` prints `running: <held>/<slots>` and one line per
execution the runner currently holds.

**Headless machines:** the unit is a systemd *user* service, so enable
lingering once or it stops when your SSH session ends:

```sh
sudo loginctl enable-linger $USER
```

The service reads credentials the same way the CLI does; make sure
`~/.preloop/config.yaml` exists (via `preloop login`) for the user that
runs the service, since the unit does not inherit your shell exports.

### Headless hosts (no keychain)

A host with no system credential store (a container or a minimal server)
cannot keep a Copilot login from `copilot login --with-token`: Copilot
reports that login succeeded but the token was not saved, and the runner
then reports Copilot signed out. Start the runner with
`COPILOT_GITHUB_TOKEN` set. The full procedure is in
[Headless hosts (no keychain)](remote-sessions.md#headless-hosts-no-keychain).

### Rotate the token or retire the runner

```sh
preloop runner rotate-token        # new token in runner.json, service restarted
preloop runner disable --delete    # stop the service, then delete the runner
preloop runner disable --delete --force   # also halt executions it still holds
```

`rotate-token` asks the server for a new runner token. The old token is
rejected from that moment, and a runner still connected with it is
disconnected. The new token is written to `~/.preloop/runner.json` and never
printed.

`disable --delete` stops and removes the service, then deletes the runner on
the server and removes `runner.json`. The server refuses while the runner
still holds an execution; `--force` halts those executions the way the kill
switch does and deletes the runner anyway. Flows routed to the runner's
labels fall back to their configured runner pool behaviour.

The console offers the same two actions on the Runners page. Rotating from
the console does not show the new token: run `preloop runner restart` on the
machine and the service reconnects with a fresh one.

The API behind both is `DELETE /api/v1/runners/{runner_id}` (with
`?force=true` to halt held executions) and
`POST /api/v1/runners/{runner_id}/token`. Both need the same permission as
registering a runner.

## Ephemeral (CI) mode: one job, then gone

A CI job is not a machine. It appears, runs one execution, and is deleted,
so a runner registered from inside it must not survive it:

```sh
preloop runner fg --once --ephemeral --labels ci-$GITHUB_RUN_ID
```

`--ephemeral` registers a runner that belongs to this process alone. It
never reads or writes `~/.preloop/runner.json`, so it cannot take over or
overwrite a persistent runner's identity on the same host, and it
unregisters on every exit path: a finished job, Ctrl-C, SIGTERM, SIGHUP
from a dying CI shell. If the job is SIGKILLed, the control plane deletes
the row once its heartbeat lapses (45 seconds) rather than leaving an
offline runner in the console forever. While it is connected, the Runners
page shows an `ephemeral` badge next to its status.

`--once` exits after the first leased execution reaches a terminal state
and prints the execution URL as soon as the job is leased. The process
status is the job's verdict, which is what a CI step needs:

| Exit code | Meaning |
| --- | --- |
| `0` | the execution SUCCEEDED |
| `1` | the execution FAILED, STOPPED or TIMEOUT, or the runner stopped mid-job |
| `2` | no execution was leased within `--wait-for-job` (default 15m) |

`--labels` defaults to `ci-<hostname>-<pid>` under `--ephemeral`, a label
nothing else can match, so a flow triggered with `--runner ci-<...>`
reaches this process and no other. Pass `--labels` yourself when you want
a label the trigger side already knows.

Without `--labels` on the trigger, a flow with no `runner_pool` can lease
to any online private runner, including this one. Pin both sides when a
CI job must run its own work.

Constraints worth knowing before you wire this into a pipeline:

*   **Linux hosts only.** Docker execution on the runner needs Linux; the
    mode itself runs anywhere the CLI does, but jobs will not.
*   **Codex and OpenCode flows only**, the same as any private runner.
*   **The agent image is pulled fresh on every job.** A cold CI host pays
    that download on each run; expect the first minutes of the step to be
    a `docker pull`. A persistent runner amortizes it, an ephemeral one
    cannot.
*   The runner needs `PRELOOP_TOKEN` (or `preloop login`) and a reachable
    control plane, same as a long-lived runner.

The GitHub Actions guide wires this into a workflow:
[trigger flows from GitHub Actions](../flows/github-actions.md).

## How many executions one runner runs

A runner holds 2 executions at once by default. Each one gets its own
workspace, its own log stream and its own halt: stopping one execution
does not disturb the other. Set the number with `--concurrency`, the
`PRELOOP_RUNNER_CONCURRENCY` environment variable (useful for service
units), or the config file:

```yaml
# ~/.preloop/config.yaml
runner:
  concurrency: 4
```

The console Runners page shows `running / slots` per runner and lets an
account owner edit the ceiling, up to 32. The two values are not the same
promise: the stored ceiling is what the account allows, and a runner
process that starts with a lower `--concurrency` lowers it while it is
connected. It cannot raise it, because capacity on someone else's machine
is not the runner's decision. A runner is dispatchable until its slots are
full; `busy` now means no free slot rather than "holds a job".

Pick a number the machine can actually serve. Concurrent agents share CPU,
memory, disk and the Docker daemon, and each one may start containers of
its own. Flows that bind fixed host ports (for example a `docker compose`
file with `ports:`) collide when two of them run together; reach services
by container-network name instead.

## Runner presence

The runner sends a heartbeat every 15 seconds. The control plane turns those
heartbeats into one presence state per runner, and the Runners page and the
runner picker show it. Hover the state to see the last heartbeat time.

| State | Meaning | Gets new work |
| --- | --- | --- |
| `online` | Heartbeats are fresh and a slot is free. | Yes |
| `busy` | Heartbeats are fresh and every slot is taken. | When a slot frees up |
| `idle` | Online, and the host reported no user input for 5 minutes or more. | Yes |
| `asleep` | The runner said it was going to sleep before it disconnected. | No |
| `offline` | No heartbeat for 45 seconds and no sleep signal. | No |

`idle` appears only for runners that report OS idle time; older runners show
`online` instead. A runner that wakes up and reconnects returns to `online` on
its first heartbeat, and the change is pushed to open console tabs without a
reload. The thresholds live in one place on the server
(`backend/preloop/services/runner_presence.py`).

## How runner work counts against your account

A Preloop instance bounds how many executions one account may have admitted
at once on shared hosted compute (`FLOW_EXECUTION_MAX_RUNNING_PER_ACCOUNT`,
default 5). Work assigned to one of your own runners is bounded by that
runner's capacity instead and does not count against the hosted allowance,
so adding runners adds throughput rather than competing with it.

## What the runner executes

Private Docker execution supports **Codex and OpenCode**. Update both the
control plane and CLI together: old or unknown launch protocol versions fail
explicitly. Other harness types require a hosted executor until their private
launch adapter is implemented.

Launches without a configured workspace mount get a writable `/workspace`
tmpfs, so nonroot images such as the default OpenCode image can start without
creating directories under `/`. This temporary workspace uses Docker-host
memory and is discarded with the container. Repository scripts and test
binaries can execute there. For larger or retained workspaces, configure
`agent_config.runner.persist_workspace` or an explicit `/workspace` mount;
the mounted directory must be writable by the image's configured user.
The default tmpfs masks any files a custom image puts in `/workspace`. Package
reusable tools and dependencies elsewhere (for example, `/opt`), and supply
pre-populated working files through an explicit workspace mount.

The control plane builds a versioned launch specification using the same
Codex/OpenCode script and environment builders as hosted execution. The CLI
runs a static Docker bootstrap that launches this script. Repository clone,
setup commands, prompt, model routing, MCP configuration and the existing
post-execution git wrapper therefore run inside the container.

The prompt and (on Kubernetes) the inner agent script are **not** one
environment variable. They arrive as base64 chunks (`PRELOOP_AGENT_PROMPT_*`,
`PRELOOP_INNER_SCRIPT_*`), reassembled to `AGENT_PROMPT_FILE`
(`/tmp/preloop/prompt.txt`) and `/tmp/preloop/agent-script.sh`. `AGENT_PROMPT`
is set only when the prompt is 64 KiB or less. Custom images must not treat
a missing `AGENT_PROMPT` as an empty task. See
[Agent launch payload](../../architecture/flows.md#agent-launch-payload-container-environment).

Scripts and credentials are transient. Persisted leases contain configuration
and execution references; delivery after a queue wait or reconnect regenerates
the model, git and MCP credentials from the execution's stored trigger and
resolved prompt. Changes to the leased flow configuration cause redelivery to
fail so a retry can select the new settings. The process environment carries
secret values rather than Docker command-line arguments. Root on the runner
host can still inspect the container environment. Gateway-enabled runs receive
a scoped flow token; direct-provider runs receive the configured provider key.

A zero exit code is insufficient. The agent must write a nonempty JSON object
to `/workspace/result.json` with a recognized `status` (`success`, `succeeded`,
`pass`, `passed`, or completed-evaluation `fail`) or audit `verdict` (`pass`,
`passed`, `pass_with_findings`, or `fail`). Failure/error and incomplete reports
do not confirm success. The runner removes stale results before launch,
requires exit zero, and sends the bounded report (256 KiB maximum) separately
from ordinary logs. Valid reports are also retained when the process exits
nonzero or reports failure; retaining evidence never promotes a failed run to
success. Malformed, oversized, empty or duplicate result envelopes are rejected.
The API independently checks the completion contract. The versioned vocabulary
and precedence cases live in `backend/tests/fixtures/runner_completion_vocabulary.json`;
both Go and Python tests verify their complete tables against this shared
`docker_v1` contract. Update the fixture and both implementations together.
Workspace source and evidence archives are not uploaded by this protocol.
This is an agent completion report, not independent verification of its tests.

Ordinary output is sent in bounded batches about once per second, with a final
flush before completion. Unsent output stays with the running process across
reconnects (at most 4 MiB / 8192 lines; individual ordinary lines are truncated
to 64 KiB). Exceeding the queue or partial-line bound fails completion because
execution markers may have been lost. Transport is best effort: a disconnect
after a successful socket write but before server persistence can lose that
batch. The runner does not maintain an unbounded replay queue.

When the flow omits `image` / `docker_image`, the control plane uses the hosted
Codex/OpenCode default. The runner keeps the entrypoint of
`ghcr.io/preloop/preloop-codex-universal` and
`ghcr.io/openai/codex-universal` (a tag or a digest), because that entrypoint
initializes language runtimes. Custom images normally run with `/bin/bash` as
the entrypoint. They must provide Bash, Python 3, Git, Node/npm, writable
`/workspace`, a writable home directory, and the dependencies required by
repository setup/tests. The shared bootstrap installs the configured CLI
version. An image whose own entrypoint initializes its environment and
delegates arguments to Bash can opt into
`agent_config.runner.preserve_image_entrypoint: true` (use this for a mirror
under another name). Images that cannot execute this bootstrap fail
explicitly; an idle shell cannot be reported as successful work.

## Harnesses are detected automatically

A runner reports which agent harnesses are installed on its host when it
registers and keeps the report current on its heartbeat. Nothing needs to be
configured: install and sign in to the harness as the user the runner runs as
(`copilot login`, `cursor-agent login`), then start or restart the runner.

```bash
preloop runner inventory            # what this runner reports
preloop runner inventory --refresh  # rebuild now; a running service picks it up within a minute
preloop runner inventory --json
```

The inventory is rebuilt when the runner starts, every 6 hours, after
`preloop runner inventory --refresh`, and within a minute of
`preloop agents onboard` or `preloop agents offboard`. The Runners page in
the console shows each harness with its login state, governance, models and
billing. A runner on an older CLI shows **Inventory unknown** there and keeps
working with hand-written profiles.

For **GitHub Copilot CLI** (`copilot`) and **Cursor CLI** (`cursor-agent`)
the runner also generates a host execution profile named `copilot` or
`cursor`, so a flow can run them without a profile file. Generated profiles
use conservative defaults: no repository checkout, no publication, no forced
writes and no tool grants beyond the harness default. Models listed for the
harness are offered as-is in the profile's model map.

For **Claude Code** (`claude`) the runner generates a profile named
`claude` when the executable is found and the login is verified, either
because `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or
`CLAUDE_CODE_OAUTH_TOKEN` is **set** or because `claude auth status --json`
reports `loggedIn: true` (only that field is kept). Otherwise Claude Code is
reported as presence only and no flow is routed to it. A flow selects it with
agent type `claude_code`, `agent_config.harness` set to `claude_code` and an
optional `agent_config.claude_code_model` (`opus`, `sonnet`, `haiku` or a
model id seen in an earlier run). The runner starts
`claude -p --output-format stream-json --verbose [--model <alias>]` in a fresh
execution directory and writes the prompt to its standard input. Every
argument is runner-owned, so a hand-written Claude profile may set
`model_map`, `workspace_root`, `timeout_seconds` and `pass_env`, but not
`argv` or tool grants. Tool use is gated by the Preloop approval hook: run
`preloop agents onboard "Claude Code" --approvals` as the runner user first,
or the job fails with `claude_approval_hook_missing` before Claude starts.
The flow's Preloop tools are passed with `--mcp-config` from a per-job file
and allowed as `mcp__preloop-flow`. The runner never writes under
`~/.claude`, and the job and its login probe never open a Claude credential
file. (Agent discovery, which the inventory shares with `preloop agents discover`,
still checks whether a Claude credential file or keychain entry exists; that
check alone never makes Claude Code runnable.) The approval hook is found in `~/.claude/settings.json`,
`~/.claude/settings.local.json` (or the same files under
`$CLAUDE_CONFIG_DIR`) or the managed settings file. Claude Code also runs
[remote sessions](remote-sessions.md).

What the runner checks, and what it never reads:

- **Login.** For Copilot CLI: whether `COPILOT_GITHUB_TOKEN`, `GH_TOKEN` or
  `GITHUB_TOKEN` is **set** (the value is never read), otherwise whether
  `~/.copilot/config.json` (or `$COPILOT_HOME/config.json`) has a stored
  login record, of which only the host is read. On a host with no
  keychain, set one of those variables in the runner service environment
  ([Headless hosts (no keychain)](#headless-hosts-no-keychain)). A run
  that fails with "No authentication information found" marks Copilot
  signed out until the next successful run. For Cursor CLI: whether
  `CURSOR_API_KEY` is set, otherwise the result of `cursor-agent status`.
  The runner never opens a credential store and never reads, copies or
  forwards a harness token.
- **Account host.** `COPILOT_GH_HOST` or `GH_HOST`, or the host of the stored
  login (for example `github.com` or `<name>.ghe.com`). Host only.
- **Governance.** The same checks `preloop agents status` uses: Preloop hooks
  and the approval hook installed (`governed`), usage hooks or MCP only
  (`partial`), nothing installed (`ungoverned`).
- **Models.** Copilot CLI has no command that lists models, so the runner
  reports a short list shipped with the CLI (`static`), the `model` set in
  Copilot's config (`configured`) and models the seat's own runs reported
  (`observed`). A Copilot session turn or host execution that includes a
  model id in its `usage` event is remembered for that harness (the id
  only; paths, tokens and environment values are not) and published on the
  next inventory refresh. An id already in the static list is not repeated,
  and the most recently observed id is listed immediately after `auto`.
  The static list is updated in the CLI when Copilot's models change
  (the 1.0.9x list includes `claude-sonnet-5.5` and the older ids); update
  the runner's CLI to pick up a new list, then run
  `preloop runner inventory --refresh`.

Executable paths, arguments, environment values and user names never leave
the host.

### Turning a harness off

To stop a runner offering a harness (it is still reported, marked disabled,
so compliance views see it), add it to `~/.preloop/runner.json`:

```json
{
  "harnesses": {
    "cursor_cli": {"enabled": false}
  }
}
```

Harness ids: `copilot_cli`, `cursor_cli`, `claude_code`, `codex_cli`,
`opencode`, `gemini_cli`, `claude_desktop`, `vscode_copilot`. Restart the
runner or run `preloop runner inventory --refresh` after editing.

## Host execution profiles (advanced override, private only)

The generated `copilot` and `cursor` profiles above are the default path. A
hand-written profile file is the advanced override: use it to pin a working
directory, map team model aliases, grant Copilot tools, or opt in to
checkout and publication. A hand-written profile for the same harness, or
with the name `copilot` or `cursor`, replaces the generated one.

Docker remains the default, including a flow's custom `image` /
`docker_image`. A host execution profile is a separate, explicit
capability: the runner host runs a **fixed local command** instead of
`docker run`. This is not Agent Control, and it is not the
[`preloop cursor`](../cursor-cli.md) operator launcher.

Create `~/.preloop/runner-host-profiles.json` (or point
`PRELOOP_RUNNER_HOST_PROFILES` at an absolute path):

```json
{
  "profiles": [
    {
      "name": "cursor-ask",
      "executable": "cursor-agent",
      "argv": ["--print", "--output-format", "stream-json", "--mode=ask"],
      "workspace_root": "/home/example/src",
      "timeout_seconds": 1800,
      "force_writes": false,
      "model_map": {"team-fast": "sonnet-4.6"}
    }
  ]
}
```

The runner advertises profile names, capabilities and supported requested model
identifiers (at most 64 profiles and 64 models per profile). Executables, argv,
local aliases and credentials stay on the host. Restart `preloop runner fg`
after editing the file. On the flow, choose `cursor`, select a private runner
pool and set `agent_config.host_exec_profile`. Hosted compute never runs host
profiles. Windows and macOS runners support them; the
[Windows quickstart](quickstart-windows.md) and
[macOS quickstart](quickstart-macos.md) list the per-OS executable detection
paths, service install and Windows command-line limits.

An optional local `model_map` maps requested identifiers to Cursor aliases,
for example `"model_map": {"team-fast": "sonnet-4.6"}`. Every nonempty requested
model must match this map. The scheduler selects a runner advertising that
identifier, and the runner passes the mapped alias to Cursor. The legacy
`pass_model` field does not bypass this mapping.
The selected API model's credentials are never delivered to the host. Leave
the requested model empty to use Cursor Auto. Auto is Cursor's own
selector, not a named model such as Grok 4.7. Set the flow's Cursor model
to a Cursor id such as `grok-4.7-high` and map that same id in `model_map`
to pin it. An actual model is
recorded only when Cursor reports it, never inferred from the request.

The lease supplies the prompt as one argument after `--`, plus the profile,
requested model and deadline. It cannot inject an executable, extra argv,
environment, API key or session id. Only `cursor-agent`, `agent` and
`copilot` executables are accepted. Local argv cannot override runner-managed workspace, model,
resume or credential controls. The profile should retain `stream-json` output
so the runner can validate structured completion. `force_writes` defaults to false; enable it only for
a profile whose operator intends to permit writes.

Each job creates a fresh directory under
`{workspace_root}/.preloop-host-exec/{execution_id}`. `workspace_root` is
optional; when omitted, workspaces live under `~/.preloop/host-workspaces`
(mode 0700). Existing directories and symlinks are rejected. This controls
working-directory placement, not OS filesystem access: Cursor runs as the
runner user with that user's local login and filesystem permissions. Use a
dedicated OS user or VM when stronger host isolation is needed. Halt,
cancellation and deadline expiry clean up the process group. The tighter
profile/flow timeout applies.

The job's environment is built from an allowlist, not inherited wholesale: a
per-OS system baseline (`HOME`, `PATH`, locale, proxy and TLS variables), the
harness's own variables (`CURSOR_*` for Cursor; `COPILOT_*`, `GH_*` and
`GITHUB_TOKEN` for Copilot, minus the BYOK overrides), and any names the
profile lists in `"pass_env"` (for example
`"pass_env": ["SSH_AUTH_SOCK"]`). The runner's own `PRELOOP_TOKEN` and
unrelated secrets in the operator's session never reach the job.

Cursor's local configuration, MCP servers and hooks apply. When the flow
allows MCP tools or servers, the runner also adds a `preloop-flow` MCP
server for that run only: a `.cursor/mcp.json` in the run directory (mode
`0600`, removed afterwards) plus `--approve-mcps`. Copilot profiles get the
same server through `--additional-mcp-config`. The server's token is scoped
to the execution, exposes only the flow's allowed tools and is revoked when
the run completes. The enforced controls are profile selection, explicit
model mapping, working-directory creation, deadline, cancellation, the
flow's MCP tool list and terminal result validation. This slice does not
add Agent Control or native session continuation. Runs use the operator's
Cursor plan or Copilot seat; no unlimited usage or inferred billing is
promised.

The runner exports `PRELOOP_FLOW_EXECUTION_ID` and `PRELOOP_FLOW_ID` to the
CLI. The Preloop usage hook forwards the execution id with each record, so
hook-observed sessions and events are linked to the flow execution (only
for host-profile executions of the same account). Records pushed without it
are linked at completion by the CLI session id.

Success requires exit zero and a successful structured result; exit zero
alone fails. Custom commands, clone `setup_commands`, workspace seeds,
native CLI session resume, pull request creation and isolated publication
are rejected before execution.

#### Repository checkout (`allow_checkout`)

A flow with git clone enabled gets a checkout plan with each lease: for
every repository the URL, branch, pinned commit (for a pull request, its
head commit and the refs that reach it), a path relative to the run
directory and a read credential from the flow's tracker. The plan is built
at delivery and never stored in `pending_job`. The runner clones only when
the profile opts in:

```json
{"name": "copilot-review", "executable": "copilot", "allow_checkout": true}
```

A Copilot profile that may also push one repository for a flow that opens a
pull request adds `allow_publish` (see
[Copilot CLI publication](../copilot-cli.md#review-and-implementation-flows)):

```json
{"name": "copilot-publish", "executable": "copilot", "allow_checkout": true, "allow_publish": true}
```

Without `allow_checkout` the run fails with `host_checkout_not_allowed`.
With it, the runner:

- runs `git clone` into the run directory (`workspace` for a trigger
  project, `workspace-1`, `workspace-2` or `workspace/<path>` for configured
  repositories; absolute and `..` paths are refused), then checks out the
  pinned commit, fetching the listed refs when the commit is not on the
  cloned branch;
- passes the credential as an HTTP header scoped to that repository URL,
  with redirects, terminal prompts, askpass and non-HTTP transports off,
  and inherited `GIT_*` overrides removed. The token never appears in the
  remote URL, `.git/config`, argv or the log. A plan that pairs a
  credential with a plain `http` URL is refused unless the host is
  loopback;
- sets the flow's git user name and email in each clone and starts the
  prompt with a short note listing the checkout paths;
- stops the clone on halt or cancellation, and gives the checkout at most
  15 minutes (or the profile timeout, when shorter) on top of the CLI
  run's own deadline. Errors are reported as `host_checkout_failed` or
  `git_not_installed`.

A checked-out repository is untrusted input. The CLI runs as the runner
user in that directory, so repository instructions and tool configuration
reach it. Enable `allow_checkout` only on a profile whose runner user and
tool rules suit the repositories the flow clones, and use a dedicated OS
user or VM when stronger isolation is needed. Host runs do not push
branches or open pull requests unless a Copilot profile also sets
`"allow_publish": true`; see
[Copilot CLI publication](../copilot-cli.md#review-and-implementation-flows).
Otherwise use the Docker harness for flows that publish.

### Running a host flow in an authorized directory

By default every host run gets a fresh execution directory. A flow can
instead work in a directory the runner owner listed with
`preloop runner dirs add` (the same list remote sessions use):

```json
{
  "harness": "claude_code",
  "workspace": {"kind": "authorized_directory", "id": "dir_9f2c", "allow_dirty": false}
}
```

The control plane sends only the id. A run is leased only to a runner that
advertises that directory as `write` for the flow's harness, and the runner
re-checks the entry before the CLI starts: it refuses an unknown id, a
`read_only` entry, a harness the entry does not allow, the filesystem root,
the home directory, and a git work tree with uncommitted changes unless the
flow sets `"allow_dirty": true`. The dirty check runs after the runner
accepts the lease and before the CLI starts, so a slow `git status` never
delays heartbeats. Such a flow cannot also clone repositories or use
`"harness_fallback": "fallback_server"` (the directory does not exist on
hosted compute); the API refuses both when the flow is saved. Per-job files
(the MCP config) stay in the runner's own execution directory, so Cursor and
Devin flows with Preloop tools are refused in an authorized directory: both
read MCP configuration from their working directory. The CLI
runs with the directory's own project configuration, so authorize only
directories whose contents you trust. Two flows that name the same
directory can run at the same time; give them separate directories or limit
the runner's concurrency if that matters.

### Copilot CLI profiles

Set `"executable": "copilot"` to run GitHub Copilot CLI under the runner
user's Copilot login. Choose agent type `copilot` on the flow and set the
optional `copilot_model` to a `model_map` alias. Copilot profiles take
`allow_tools`, `deny_tools` and `allow_all_tools` instead of
`force_writes`; `allow_all_tools` requires the Preloop approval hook. The
completion must carry exactly one Copilot `result` event with exit code 0.
The rest of this section applies unchanged. See
[Copilot CLI](../copilot-cli.md#run-copilot-cli-from-flows-private-runner-host-profile)
for the profile format and named errors, and
[Copilot coverage](../copilot.md) for what this path does and does not
meter.

## Trusted runner options

Private runners are machines you operate. Hosted executors ignore the
`agent_config.runner` block; only `preloop runner` honors it. Every flag
defaults off.

```yaml
agent_config:
  runner:
    mount_docker_socket: true
    persist_workspace: true
    extra_mounts:
      - /var/cache/builds:/cache:ro
    network: preloop-trusted
```

- `mount_docker_socket`: bind `/var/run/docker.sock` into the agent so
  it can start sibling containers (for example `docker compose up`).
- `persist_workspace`: keep `/workspace` on the host at
  `~/.preloop/workspaces/<execution_id>` (mode 0700). A later job whose
  payload includes `resume_from` reuses that directory. Directories
  older than 24 hours that are not the current job are deleted on each
  lease; override the window with `PRELOOP_RUNNER_WORKSPACE_TTL_HOURS`.
- `extra_mounts`: `host:container[:ro]` bind mounts. Host paths must be
  absolute.
- `network`: Docker network to join (`--network`). Created if missing.

Every job also sets `COMPOSE_PROJECT_NAME=preloop-<short execution id>`
so `docker compose up` gets isolated containers, networks, and volumes
per execution. When more than one runner can run at the same time,
compose files should avoid fixed host ports and reach services by
container-network name instead.

Enable these options only on machines you own. Mounting the Docker
socket gives the agent the same privileges as the runner user.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker is not available` in execution log | `docker info` must work as the runner user (add to `docker` group, or fix LXC nesting). |
| `no agent image in payload` | The flow's agent type has no default image and no `image`/`docker_image` was set. |
| Execution FAILED after ~15 min queued | No runner matching `runner_pool` was online; check `preloop runner status` and labels. |
| Service dies after SSH logout | `sudo loginctl enable-linger $USER`. |
| Runner shows offline after IP change | Restart: `preloop runner restart`: registration resumes from `~/.preloop/runner.json`. |

## Runner connection recovery

The runner reconnects when a WebSocket read or write times out. With a server
that supports log acknowledgments, it retains unacknowledged log batches and
replays their stable IDs after reconnecting, so the server stores each line
once. Terminal reports are also retained for reconnect, and execution completion
is committed together with releasing its runner lease. A late report cannot
replace a timeout or cancellation that the server has already recorded.

The pending batches and terminal report are held in the CLI process's memory.
They do not survive stopping or restarting the CLI process. Servers without log
acknowledgment support retain the older best-effort log delivery behavior.
The server records received PR and native-session handoff markers before final
completion, which preserves that metadata if a later terminal report is lost.
A created PR by itself does not mean the agent process has finished or that the
execution succeeded.

## Account runner requirements

See [Account runner requirements](policy.md) for console policy settings,
compliance and member warnings.


### Codex, OpenCode and DeepSeek native profiles

Private runners can execute installed Codex (`codex`), OpenCode (`opencode`)
and DeepSeek CLI (`dsh`) directly on Windows, macOS and Linux without Docker.
This path uses the runner user's existing CLI configuration. It requires an
explicit local host profile and a private runner pool; it does not silently
switch to hosted compute. Existing Docker flows keep their existing behavior
when no host profile is selected.

Onboard Codex through Preloop as the runner user
(`preloop agents onboard "Codex CLI" --approvals`) and keep that
configuration: its `model_provider = "preloop"` sends every model call through
the Preloop gateway with the agent's own key. For each run the runner adds the
execution marker header to that provider with a command-line override (your
`config.toml` is not edited), so the gateway attributes the run's requests to
the flow execution as well as the agent, enforces the flow's run limits, and
the result records `gateway_metered: true` and `gateway_request_count`. Do not
point the runner at a separate `CODEX_HOME` without the Preloop provider: the
run would call the model service directly and end `FAILED` with
`gateway_bypassed`. Claude Code profiles work the same way when Claude Code is
onboarded to the gateway (`ANTHROPIC_BASE_URL` on Preloop); the marker is sent
with `ANTHROPIC_CUSTOM_HEADERS`. A flow that must run a Codex or Claude Code
host CLI unmetered can set `agent_config.allow_unmetered_host_model: true`
(break-glass, recorded on the result as `gateway_bypass_allowed`); read-only
review never allows it.

For example, add these profiles to the local `runner-host-profiles.json`
file (use provider and model names configured in your local CLI):

```json
{
  "profiles": [
    {"name": "local-codex", "executable": "codex", "model_map": {"team-fast": "local-model"}},
    {"name": "local-opencode", "executable": "opencode", "model_map": {"team-fast": "provider/model"}},
    {"name": "local-deepseek", "executable": "dsh", "model_map": {"team-fast": "provider/model"}}
  ]
}
```

These adapters manage their own command arguments; leave `argv` empty.

Use `agent_type: codex`, `opencode` or `deepseek`, respectively, and set
`agent_config.host_exec_profile` to the local profile name. The advertised
harness capabilities are `codex_cli`, `opencode` and `deepseek`. Configure the
fixed executable and model map locally; the runner owns arguments and the
control plane cannot supply an
arbitrary host command. Map requested model identifiers to local CLI aliases
in the profile's `model_map`, and select the alias with `codex_model`,
`opencode_model` or `deepseek_model` in `agent_config`. Unknown requested
models fail closed instead of silently selecting another model.

Onboard the CLI's Preloop governance hooks or plugin before enabling the
profile. Native permissions and Preloop approval policy both apply; installing
a profile does not grant blanket approval.

A command that matches a rule requiring approval does not run and does not
hold the hook open. The execution pauses (`WAITING_FOR_HUMAN`), the runner job
is stopped and its credential released, and a normal approval request naming
the flow, the execution, the command and the rule appears in **Approvals** and
on your notification channels. The window is the flow's
`approval_window_seconds` (or the workflow and account defaults). If it is
approved, the run resumes and that exact command may run once. If it is
refused, cancelled or the window closes, the run ends `FAILED` with
`approval_denied` or `approval_timeout`. While a run is paused, any other
command it tries is denied without a second request. The runner does not need
to stay online while the run is paused. If OpenCode or DeepSeek reports
`host_governance_not_ready`, reinstall or check the Preloop plugin: the runner
withholds the task until plugin activation is acknowledged and terminates the
process if readiness times out. Per-job MCP configuration carries
execution credentials without placing tokens on the command line. A host
workspace uses the runner user's filesystem permissions and is not a sandbox.

DeepSeek requires `dsh` **0.2.0-rc.2** (the version installed by
`preloop agents install-runtime deepseek`); the older 0.1.5 preview does not
implement this JSON/stdin protocol. The per-run profile enables the harness's
native credential and account services so `dsh` resolves its own saved login.
Preloop does not import or copy those credentials. Extra provider environment
variables require an explicit local profile `pass_env` entry; this does not
create or select a hosted provider account.

These three executors initially support fresh flow executions only. Native
resume and pull-request publication are unavailable. Claude Code retains its
existing native behavior. Protocol fixtures and fake executable tests validate
launch and completion behavior without using credentials or paid providers;
those checks are not evidence of live provider acceptance or native execution
on every operating system. Validate your installed CLI version and governance
configuration before using a production profile.

For OpenCode and DeepSeek, each local model alias must include the configured
provider as `provider/model`; DeepSeek rejects managed gateway model settings
on this path. For Codex, see the official
[noninteractive protocol](https://developers.openai.com/codex/noninteractive)
and [native Windows setup](https://developers.openai.com/codex/windows).
The runner selects `workspace-write` sandboxing and an approval policy of
`never` (escalation is denied). On Windows, complete the native sandbox setup
under your operator policy; Windows 11 is recommended and recent Windows 10
is best effort. This mode launches the Windows CLI directly, without WSL.

Native acceptance evidence distinguishes implementation checks from live runs:

| Agent | Linux | macOS | Windows | Live provider acceptance |
| --- | --- | --- | --- | --- |
| Claude Code | Protocol / fake executable coverage | Protocol / fake executable coverage | CI / portable fake executable coverage | Not claimed |
| Codex | Protocol / fake executable coverage | Protocol / fake executable coverage | CI / portable fake executable coverage | Not claimed |
| OpenCode | Protocol / fake executable coverage | Protocol / fake executable coverage | CI / portable fake executable coverage | Not claimed |
| DeepSeek CLI (`dsh`) | Protocol / fake executable coverage | Protocol / fake executable coverage | CI / portable fake executable coverage | Not claimed |

These cells describe available test coverage, not successful native runs on
every OS. A Windows cross-compile checks build compatibility only; a CI job
counts as execution evidence only after its tests have run successfully.

Codex must report the Preloop `PreToolUse` hook as enabled and trusted for the
actual run workspace, with its hooks feature enabled. Open Codex `/hooks` as
the runner user to review and trust the current hook, or use administrator
managed policy. Updating a hook can require renewed trust. The runner checks
effective metadata through `hooks/list` before delivering a task and fails
closed when the API is unavailable; upgrade Codex if the check is unsupported.
It does not grant hook trust or bypass native hook policy.

If an older Codex hook used an unquoted Preloop path containing spaces, rerun
`preloop agents onboard "Codex CLI" --approvals` as the runner user, then
review and trust the updated hook in Codex `/hooks`. Native Windows hooks
quote the executable for `cmd.exe`; no PowerShell invocation prefix is needed.
