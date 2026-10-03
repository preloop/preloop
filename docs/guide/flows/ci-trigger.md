# Trigger a flow from CI

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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
               payload: {pull_request: .pull_request, repository: .repository},
               ci: {provider: "github-actions",
                    run_url: "https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}"}}' \
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

## CI provenance

A CI-dispatched run keeps its real event label (for example
"Pull Request Updated") instead of showing "Manual Test Run", and the console
renders a "via <provider>" hint that links back to the CI run. Opt in by adding
a top-level `ci` block to the trigger body:

```json
{
  "source": "github",
  "type": "pull_request_opened",
  "payload": {"pull_request": {}, "repository": {}},
  "ci": {
    "provider": "github-actions",
    "run_url": "https://github.com/acme/backend/actions/runs/1234567890"
  }
}
```

`provider` is required; `run_url` is optional and only needs to be an
`http(s)` URL (the console refuses anything else). The console maps these
`provider` values to a human-readable label:

| `provider` | label |
| --- | --- |
| `github-actions` | GitHub Actions |
| `gitlab`, `gitlab-ci`, `gitlab-ci-cd` | GitLab CI |
| `jenkins` | Jenkins |
| `circleci`, `circle-ci` | CircleCI |
| `buildkite` | Buildkite |
| `bitbucket`, `bitbucket-pipelines` | Bitbucket Pipelines |
| `azure-pipelines` | Azure Pipelines |
| `azure-devops` | Azure DevOps |
| `teamcity` | TeamCity |
| `travis`, `travis-ci` | Travis CI |

An unknown `provider` still renders: its value is title-cased
(`my-internal-bot` becomes "My Internal Bot"). A run a person or webhook
started carries no `ci` block, so it shows no hint.

`PRELOOP_TOKEN` is an account API token. OIDC exchange is not part of this
command yet. Use `--payload -` to pipe a JSON event file from a previous step.
Omit `--wait` in CI; waiting is the default when stdin is not a TTY.

On GitHub-hosted runners the installer writes to `~/.local/bin`, which is
not on PATH. In a single step, export it as above. Across steps, append
that directory to `$GITHUB_PATH` in the install step.

## Cancelled CI jobs stop the execution

When the job is cancelled (a newer push, a closed pull request, or the
cancel button), the runner sends the CLI SIGINT or SIGTERM. In CI the CLI
then stops the execution on the server, prints its id and final status, and
exits 130 (SIGINT) or 143 (SIGTERM). A run that finished just before the
signal is reported, not stopped. This is `--stop-on-interrupt`, on by
default when stdin is not a TTY. In a terminal it is off, so Ctrl-C only
ends the CLI and the run continues; pass `--stop-on-interrupt` to stop it.

Pair it with a concurrency group so a new push cancels the older job:

```yaml
concurrency:
  group: preloop-${{ github.event.pull_request.number }}
  cancel-in-progress: true
```

On GitLab, mark the job `interruptible: true` and enable auto-cancel of
redundant pipelines. The stop only happens if the runner signals the job
before killing it; a job killed outright leaves the execution running.

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
