# Runner end-to-end smoke test (staging)

Internal checklist: verify the self-hosted runner path end-to-end
against staging (`https://review.preloop.ai`) before handing it to a
design partner. Takes ~10 minutes on any Linux box or Proxmox guest
with Docker.

## 0. Prereqs

```sh
docker info                      # must succeed
curl -fsSL https://preloop.ai/install/cli | sh
preloop version
```

## 1. Point the CLI at staging and log in

```sh
export PRELOOP_URL=https://review.preloop.ai
preloop login --headless
preloop auth token >/dev/null && echo auth-ok
```

## 2. Start a foreground runner

```sh
preloop runner fg --labels smoke --name smoke-$(hostname)
```

Expect: `Runner smoke-<host> (<uuid>) connecting...` then
`Connected. Waiting for jobs.`

In a **second shell**:

```sh
export PRELOOP_URL=https://review.preloop.ai
preloop runner status
```

Expect `status: online` and a recent `last_heartbeat` (heartbeats every
15s).

## 3. Lease a real execution onto it

Use any existing staging flow (or create a trivial one) and pin it to
the runner label for this run only:

```sh
preloop flow trigger <flow-id-or-name> --runner smoke --wait
```

Expect, in order:

1. Trigger shell: `Triggered flow <id> (execution <id>, status ...)`.
2. Runner shell: `Leased execution <id>`.
3. Trigger shell streams the container logs, then exits `0` with a
   terminal `SUCCEEDED` status.
4. Console execution view shows the same logs, runner-attributed.

## 4. Halt path

Trigger again without `--wait`, then stop the execution from the
console while it runs. Expect the runner shell to print
`Halt received for <execution-id>` and the execution to end `STOPPED`
(exit code from a killed container must **not** be reported as FAILED).

## 5. Queue-then-fail path (negative test)

Ctrl-C the runner (expect `Unregistering...`), then trigger with
`--runner smoke` again. The execution should sit QUEUED and fail after
15 minutes with no hosted fallback. (Spot-check that it's QUEUED for a
minute or two; you don't need to wait out the full window.)

## 6. Service mode

```sh
preloop runner enable && preloop runner start
sudo loginctl enable-linger $USER          # headless boxes
preloop runner status                       # install: active, status: online
preloop flow trigger <flow-id-or-name> --runner smoke --wait
preloop runner stop && preloop runner disable
```

## 7. CI invocation (what the partner's pipeline will run)

```sh
echo '{"ref":"main"}' | PRELOOP_TOKEN=$(preloop auth token) \
  PRELOOP_URL=https://review.preloop.ai \
  preloop flow trigger <flow-id-or-name> --payload - < /dev/null; echo "exit=$?"
```

Non-TTY stdin means it waits and streams logs by default; `exit=0` on
success, non-zero on FAILED/STOPPED/TIMEOUT.

## Known limitations (set expectations with the partner)

- One job at a time per runner; extra leases are declined while a job
  runs.
- The agent container needs its model credentials via the flow's agent
  config / MCP gateway; the runner passes `PRELOOP_API_TOKEN` +
  `PRELOOP_URL` through, it does not inject provider API keys itself.
- No `git_clone_config` / `custom_commands` execution on the runner
  host yet — those run inside the agent image if it supports them.
- Runner-side workspace caching is not implemented; every job is a
  fresh `docker run --rm`.
