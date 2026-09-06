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
TIMEOUT. If a chosen private pool has no idle runner, the job queues
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

**Headless machines:** the unit is a systemd *user* service, so enable
lingering once or it stops when your SSH session ends:

```sh
sudo loginctl enable-linger $USER
```

The service reads credentials the same way the CLI does; make sure
`~/.preloop/config.yaml` exists (via `preloop login`) for the user that
runs the service, since the unit does not inherit your shell exports.

## What the runner executes

Private Docker execution supports **Codex and OpenCode**. Update both the
control plane and CLI together: old or unknown launch protocol versions fail
explicitly. Other harness types require a hosted executor until their private
launch adapter is implemented.

The control plane builds a versioned launch specification using the same
Codex/OpenCode script and environment builders as hosted execution. The CLI
runs a static Docker bootstrap that launches this script. Repository clone,
setup commands, prompt, model routing, MCP configuration and the existing
post-execution git wrapper therefore run inside the container.

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
from ordinary logs. The API independently checks the completion contract.
Workspace source and evidence archives are not uploaded by this protocol.
This is an agent completion report, not independent verification of its tests.

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
