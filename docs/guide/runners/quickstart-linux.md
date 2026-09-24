# Self-hosted runner quickstart (plain Linux / Proxmox)

The Preloop CLI **is** the self-hosted runner. It registers itself with
your Preloop control plane, holds an outbound WebSocket, leases flow
executions for your account, and runs the agent in a local Docker
container — same model as GitHub/GitLab self-hosted runners. No inbound
ports, no Kubernetes.

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

  If `docker info` fails inside the container after this, use a VM —
  the runner refuses jobs when Docker is unavailable and reports
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
TIMEOUT. If no runner in the chosen private pool has a free slot, the job queues
for 15 minutes and then fails. Hosted compute is used only when no
private runner is online, or when the flow or account default is
`server`.

## 5. Install as a service (survives reboots)

```sh
preloop runner enable    # writes a systemd user unit + enables it
preloop runner start
preloop runner status    # service state + last heartbeat + current execution
```

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
Codex/OpenCode default. The default `ghcr.io/openai/codex-universal:latest`
entrypoint is preserved because it initializes language runtimes. Custom
images normally run with `/bin/bash` as the entrypoint. They must provide
Bash, Python 3, Git, Node/npm, writable `/workspace`, a writable home directory,
and the dependencies required by repository setup/tests. The shared bootstrap
installs the configured CLI version. An image whose own entrypoint initializes
its environment and delegates arguments to Bash can opt into
`agent_config.runner.preserve_image_entrypoint: true` (also use this for pinned
or mirrored codex-universal images). Images that cannot execute this bootstrap
fail explicitly; an idle shell cannot be reported as successful work.

## Host execution profiles (opt-in, private only)

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
pool and set `agent_config.host_exec_profile`. Hosted compute and Windows host
profiles are unavailable.

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
environment, API key or session id. Only `cursor-agent` and `agent` executables
are accepted. Local argv cannot override runner-managed workspace, model,
resume or credential controls. The profile should retain `stream-json` output
so the runner can validate structured completion. `force_writes` defaults to false; enable it only for
a profile whose operator intends to permit writes.

Each job creates a fresh directory under
`{workspace_root}/.preloop-host-exec/{execution_id}`. Existing directories and
symlinks are rejected. This controls working-directory placement, not OS
filesystem access: Cursor runs as the runner user with that user's local login,
environment and filesystem permissions. Use a dedicated OS user or VM when
stronger host isolation is needed. Halt, cancellation and deadline expiry clean
up the process group. The tighter profile/flow timeout applies.

Cursor's local configuration, MCP servers and hooks apply. Flow
`allowed_mcp_tools` and server settings are not injected or enforced as a
sandbox on this path. The enforced controls are profile selection, explicit
model mapping, working-directory creation, deadline, cancellation and terminal
result validation. This slice does not add Agent Control, flow governance,
native session continuation or usage ingestion. Runs use the operator's Cursor
plan; no unlimited usage or inferred billing is promised.

Success requires exit zero and a successful Cursor stream-json result; exit
zero alone fails. Remote repository clone/setup, custom commands, workspace
seeds, native CLI session resume and isolated PR publication are rejected.
Isolated publication mode is rejected before execution.
The workspace starts empty. Use the Docker harness for repository
implementation flows that need the managed checkout/test/publication pipeline.

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
| Runner shows offline after IP change | Restart: `preloop runner restart` — registration resumes from `~/.preloop/runner.json`. |

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
