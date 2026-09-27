# Trigger flows from GitHub Actions

There are two ways to run a Preloop flow from a GitHub Actions job, and
the only difference between them is where the agent container runs:

1.  **Hosted** (works today): the job triggers the flow and waits.
    Preloop runs the agent on its own compute. The GitHub job is a
    thin client: it holds a token, streams the execution log, and fails
    on the execution's verdict.
2.  **Runner**: the job additionally starts a one-shot ephemeral Preloop
    runner on its own VM, and pins the execution to it. The agent
    container runs inside the GitHub job. Nothing survives the job.

Pick hosted unless the work needs the job's checkout, its network, or its
secrets on the same machine.

Both are packaged as a composite action in this repository at
`.github/actions/run-flow`. You can also call the CLI by hand; see
[trigger a flow from CI](ci-trigger.md) for the minimal version.

## Token setup

The action needs an **account API token**, not a GitHub token. Create one
in the console (Settings > API tokens), then add it as a repository or
organization secret:

```
Settings > Secrets and variables > Actions > New repository secret
Name:  PRELOOP_TOKEN
Value: <account API token>
```

Pass it as an input. Never inline a token in the workflow file, and never
echo it in a step:

```yaml
with:
  token: ${{ secrets.PRELOOP_TOKEN }}
```

Self-hosted control plane: set `url` to your own address (default
`https://preloop.ai`). OIDC token exchange is not supported yet; the
token is a long-lived secret you rotate like any other.

## Mode 1: hosted

```yaml
name: Preloop review
on:
  pull_request:
    types: [labeled]

permissions:
  contents: read

jobs:
  review:
    if: github.event.label.name == 'preloop-review'
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Build the payload
        run: |
          jq '{source: "github", type: "pull_request_opened",
               payload: {pull_request: .pull_request, repository: .repository}}' \
            "$GITHUB_EVENT_PATH" > payload.json
      - uses: ./.github/actions/run-flow
        id: flow
        with:
          flow: pull-request-reviewer
          payload: payload.json
          token: ${{ secrets.PRELOOP_TOKEN }}
      - run: echo "${{ steps.flow.outputs.execution-url }}"
```

The pull request goes under `payload`, in GitHub's own event shape: see
[trigger a flow from CI](ci-trigger.md) for why a body that puts it at the
top level renders the reviewer prompt empty.

The `payload` input takes either a path to a JSON file (as above) or a
JSON string. The action pipes it through `preloop flow trigger
--payload -`, so nothing lands on a command line where another step
could read it out of the process table. A value that is neither an
existing file nor a JSON object or array fails the step immediately: a
mistyped path is a typo to report, not a payload to send.

The step fails when the execution does. The outputs (`execution-id`,
`execution-url`, `status`) are written before the failure, so a later
`if: always()` step can still comment the link.

## Mode 2: agent on the GitHub runner

```yaml
      - uses: ./.github/actions/run-flow
        with:
          flow: pull-request-reviewer
          payload: payload.json
          token: ${{ secrets.PRELOOP_TOKEN }}
          mode: runner
          # The first release with `runner fg --once --ephemeral`
          # (0.16.0 once published; 0.15.0 does not have it).
          cli-version: '0.16.0'
```

In `runner` mode the action:

1.  starts `preloop runner fg --once --ephemeral --labels ci-<run-id>-<attempt>`
    in the background and waits for it to come online,
2.  triggers the flow with `--runner ci-<run-id>-<attempt>`, so the
    execution reaches that process and no other,
3.  stops the runner on the way out (`if: always()`), which is the
    runner's unregister path.

The runner unregisters on every exit path it can catch. If the job is
cancelled hard, the control plane deletes the ephemeral runner once its
heartbeat lapses (45 seconds) instead of leaving an offline row in the
console. See
[Ephemeral (CI) mode](../runners/quickstart-linux.md#ephemeral-ci-mode-one-job-then-gone).

### Constraints for `runner` mode

*   **Linux hosted runners only.** Private execution needs Docker. The
    action refuses to start on a non-Linux job rather than queueing an
    execution nothing will lease.
*   **Codex and OpenCode flows only**, the same as any private runner.
*   **The agent image is pulled on every job.** A hosted GitHub runner is
    a fresh VM with a cold Docker cache, so each run pays the pull before
    the agent starts. This is the cost you trade for running on the job's
    own machine. (A measured number on `ubuntu-latest` is pending; it
    needs a live run against a control plane.)
*   `cli-version` must be a release that has `runner fg --once
    --ephemeral`, which is the first release after this feature lands
    (`0.16.0` once published). The action checks the installed CLI and
    fails with that message rather than dying on an unknown flag.
*   The default `labels` is unique per run and attempt. Override it only
    when the trigger side already knows a different label, and pass one
    label: the action rejects a comma, because the CLI would register
    several labels while the trigger pins the execution to one.

## Do not review the same pull request twice

If a Preloop flow is already driven by a GitHub webhook (a pull request
reviewer that fires on `pull_request` events), adding a CI trigger for the
same flow reviews every pull request twice: once from the webhook, once
from the job. You pay twice and the author reads two reports.

Pick one of:

*   **A label gate.** Run the workflow only on
    `pull_request: types: [labeled]` with an explicit opt-in label, as in
    the example above, and leave the webhook flow for everything else.
*   **A CLI-only flow.** Give the CI path its own flow whose trigger
    event source is not the webhook that already fires, so the webhook
    cannot start it.

Whichever you pick, state it in the workflow file. The next person to
read it will otherwise add the other one.

## Self-hosted GitHub runners

A self-hosted GitHub runner works in both modes, with one requirement for
`mode: runner`: a working Docker daemon that the runner user can talk to.
The action checks `docker info` and fails early if it cannot.

On a self-hosted GitHub runner you usually do not want `mode: runner` at
all. The machine is already yours, so a persistent Preloop runner
(`preloop runner enable`) keeps the image cache warm between jobs and the
workflow can stay in hosted-style mode with `--runner <label>` pinned on
the flow. See the
[self-hosted runner quickstart](../runners/quickstart-linux.md).

Ephemeral GitHub runners (one VM per job) behave like hosted ones: cold
cache, image pulled every time.

## Action reference

| Input | Default | Meaning |
| --- | --- | --- |
| `flow` | required | Flow id or name |
| `payload` | `''` | JSON object or array, or a path to a file containing one |
| `token` | required | Account API token |
| `url` | `https://preloop.ai` | Control plane |
| `mode` | `hosted` | `hosted` or `runner` |
| `timeout` | `40m` | How long to wait for the execution |
| `labels` | per-run label | One runner label for `mode: runner` (no commas) |
| `cli-version` | pinned | CLI release to install |

| Output | Meaning |
| --- | --- |
| `execution-id` | Id of the triggered execution |
| `execution-url` | Console URL of the execution |
| `status` | `SUCCEEDED`, `FAILED`, `STOPPED`, `TIMEOUT`, `UNKNOWN` |

The action pins the CLI version on purpose. An action that follows
`latest` changes what your pipeline runs without a commit.

GitLab CI is covered by [trigger a flow from CI](ci-trigger.md); the CLI
is the same, only the YAML differs.
