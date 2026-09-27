# Trigger a flow from CI

GitHub Actions and GitLab CI call the Preloop CLI. Preloop does not dispatch
into your CI APIs. The CLI blocks (when stdin is not a TTY), streams
execution logs to the job, and exits non-zero on FAILED, STOPPED, or TIMEOUT.
The same logs stay visible in the Preloop console.

```yaml
# .github/workflows/preloop-flow.yml
name: Preloop flow
on:
  pull_request:
jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - name: Trigger Preloop flow
        env:
          PRELOOP_TOKEN: ${{ secrets.PRELOOP_TOKEN }}
          PRELOOP_URL: https://preloop.example.com
        run: |
          curl -fsSL https://preloop.ai/install/cli | sh
          export PATH="${HOME}/.local/bin:${PATH}"
          jq '{source: "github", type: "pull_request_opened",
               payload: {pull_request: .pull_request, repository: .repository}}' \
            "$GITHUB_EVENT_PATH" \
            | preloop flow trigger pull-request-reviewer --payload -
```

The request body becomes the flow's trigger event as is. Flow inputs go under
`payload`: the Pull Request Reviewer prompt reads
`trigger_event.payload.object_attributes.*`, which Preloop builds from a
GitHub-shaped `payload.pull_request` (title, body, `html_url`, `user.login`,
`head.ref`, `base.ref`) when `source` is `github`. A pull request placed at the
top level of the body, next to `payload` instead of inside it, renders every
one of those fields empty. `type` is what the prompt sees as the trigger; use
`pull_request_updated` for later pushes so the reviewer can do an incremental
pass.

`PRELOOP_TOKEN` is an account API token. OIDC exchange is not part of this
command yet. Use `--payload -` to pipe a JSON event file from a previous step.
Omit `--wait` in CI; waiting is the default when stdin is not a TTY.

On GitHub-hosted runners the installer writes to `~/.local/bin`, which is
not on PATH. In a single step, export it as above. Across steps, append
that directory to `$GITHUB_PATH` in the install step.

## `runner_pool` flow config

Set `runner_pool` on the flow (create or update) to a runner id, name, or
label. Every execution of that flow then leases to a matching
`preloop runner fg` instead of starting a hosted container.

```json
{ "runner_pool": "local" }
```

`preloop flow trigger --runner` (or a `_runner` field on the trigger payload)
overrides the flow default for that one run. The matching runner must already
be online:

```sh
preloop runner fg --labels local
preloop flow trigger pull-request-reviewer --runner local --wait
```

If no matching runner heartbeats within 15 minutes the execution fails.
There is no fallback onto hosted compute.

To set up a runner on plain Linux or Proxmox, see the
[self-hosted runner quickstart](../runners/quickstart-linux.md).

GitHub Actions users have a packaged version of all of this, including
running the agent on the job's own VM: see
[trigger flows from GitHub Actions](github-actions.md).
