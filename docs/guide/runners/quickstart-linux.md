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
same runner. Ctrl-C unregisters cleanly.

## 4. Route a flow to the runner

Set `runner_pool` on the flow (id, name, or label) in the console or
API. Every execution of that flow then leases to a matching runner
instead of hosted compute:

```json
{ "runner_pool": "local" }
```

Or override per-run from CI / the CLI:

```sh
preloop flow trigger <flow-id-or-name> --runner local --wait
```

When stdin is not a TTY (CI), `flow trigger` waits by default, streams
execution logs to stdout, and exits non-zero on FAILED / STOPPED /
TIMEOUT. If no matching runner is online, the job queues for 15 minutes
and then fails — there is no silent fallback onto hosted compute.

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

Each leased job runs the flow's agent image via
`docker run --rm -e ... <image>` with the same environment contract as
hosted executions: `EXECUTION_ID`, `FLOW_ID`, `AGENT_PROMPT`,
`AGENT_CONFIG`, `AI_MODEL`, `AI_MODEL_PROVIDER`, `PRELOOP_API_TOKEN`,
and `PRELOOP_URL`. Values are passed through the process environment,
not argv, so they don't appear in `ps`. Container stdout/stderr is
shipped back and shows up in the console execution view.

`PRELOOP_API_TOKEN` is a **flow-execution token**, not the partner
account's long-lived key. It is scoped to that execution (`mcp:read` /
`mcp:write`), expires in about two hours, and is deactivated when the
execution completes, fails, or is halted. Root on the runner host can
still `docker inspect` the running container and read it for that
window. `AGENT_CONFIG` is the flow's agent settings (image, type); the
runner strips credential-shaped keys before injecting it. Model and MCP
credentials stay on the control plane and are used through that token.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `docker is not available` in execution log | `docker info` must work as the runner user (add to `docker` group, or fix LXC nesting). |
| `no agent image in payload` | The flow's agent config has no `image`/`docker_image`; check the flow's agent settings. |
| Execution FAILED after ~15 min queued | No runner matching `runner_pool` was online; check `preloop runner status` and labels. |
| Service dies after SSH logout | `sudo loginctl enable-linger $USER`. |
| Runner shows offline after IP change | Restart: `preloop runner restart` — registration resumes from `~/.preloop/runner.json`. |
