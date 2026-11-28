# Daily CI and QA review preset

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The **Daily CI and QA review** preset
(`backend/presets/023-daily-ci-qa-review.yaml`) looks at one repository's
last day of CI every morning, and then does the work: it fixes the most
valuable CI or test problem it found in one focused pull request, with
tests and measured before/after numbers, and writes a short daily report.

## What a run does

1. **Gathers inputs**, read-only and one GitHub API request at a time:
   workflow runs from the last `ci_window_hours` (default 24) with their
   jobs, so it can compute durations, queue waits, failures and reruns;
   flake signatures from failed step names and at most one annotations
   fetch per run (no full log downloads); the coverage report; open CI,
   test, Playwright and performance issues and pull requests, so it does
   not duplicate work in flight; and the previous run's result for the
   trend. It stops fetching when `X-RateLimit-Remaining` drops below 200
   and marks the window partial.
2. **Assesses**: success and rerun rates, p50/p95 queue wait and duration
   per workflow and job, runner minutes, shard imbalance, top flake
   signatures and coverage per package, compared with the previous run.
   Candidates are ranked by time or risk removed per unit of effort.
3. **Changes one thing, measured**: a flaky or order-dependent test fixed
   at its cause, a CI time or runner-usage cut, a shard rebalance, new
   tests where risk is high, a new or repaired Playwright console journey,
   or a local performance test with a baseline. Every change carries
   tests and the same before/after measurement.
4. **Reports**: `/workspace/evidence/ci-qa-report.md` (one page: metrics
   with today's and the previous value, what changed, what was left and
   why) and `/workspace/result.json` with `"status": "success" | "failure"`,
   the metrics, the change and the list of what was left. A day with
   nothing worth changing is still a success; failure means the review
   itself could not be done.

## Guardrails

- The agent is read-only towards every remote: no pushes, no pull request
  edits, no comments, no CI reruns. The platform publishes at most one
  pull request per run, on a new branch, after the agent exits, so a run
  never pushes to an existing pull request, approved or not. The pull
  request body is the `pull_request.body` the agent writes in
  `result.json`, with the before/after numbers and the tests it ran.
- No skip, xfail, quarantine, retry wrapper or raised timeout to make a
  test green, and no weakening of CI gates.
- Performance tests run only against services started in the run's own
  workspace with mocked upstreams; nothing loads staging or production.
- Each run is capped by `agent_config.limits.max_usd` (10 USD by default,
  enforced by the gateway) and `timeout_seconds` (90 minutes).

## Payload

| Key | Default | Meaning |
| --- | --- | --- |
| `previous_result_execution_id` | `last` | Baseline for the trend; see [Baseline from a previous run](repo-review-presets.md#baseline-from-a-previous-run). |
| `ci_window_hours` | `24` | How far back to read CI runs. |
| `ci_repositories` | none | Extra `owner/name` repositories whose CI is read for comparison, never written. |
| `focus` | none | Free-text hint, for example `flaky e2e` or `shard balance`. |

## Setting it up

Clone the preset, add the repository to the flow's git clone
configuration, and set the schedule's timezone to the team's zone (the
schedule is evaluated in that zone, so 06:00 stays 06:00 across DST). The
schedule payload reaches the agent through `{{trigger_event.payload}}` in
the prompt; keep that line if you customize the prompt.

The agent reads CI with the repository's tracker token, which the run
exports as `PRELOOP_GIT_TOKEN_1` (one variable per configured repository).
The prompt restricts it to GET requests. To read another repository's CI
(`ci_repositories`), the tracker token must have read access to Actions
there; without any usable token the run writes a failure report saying so.

The preset uses legacy publication: the platform pushes the agent's commit
to a new branch and opens the pull request. Isolated publication also
requires a repository-specific verification profile in `mode: gate` and a
digest-pinned verification image under `git_clone_config.verification`;
add both before switching `publication_mode` to `isolated`.

To run it on your own machine instead of hosted compute, pin the harness,
runner and host profile in `agent_config` (see
[Run flows on your machine](../runners/your-machine.md#flows-on-your-machine)
and [Host execution profiles](../runners/quickstart-linux.md#host-execution-profiles-advanced-override-private-only)):

<!-- example: fragment -->
```json
{
  "harness": "codex_cli",
  "runner_id": "<runner id>",
  "host_exec_profile": "<local profile name>",
  "codex_model": "<model alias the profile maps>",
  "harness_fallback": "fail"
}
```

A pinned Codex flow can open its pull request: set
`create_pull_request: true` (legacy publication) and add `"allow_publish":
true` next to `"allow_checkout": true` on the runner's host profile. The
runner pushes the managed branch with the repository token Preloop issues
for the run, and Preloop opens the pull request, so the prompt does not need
its own publishing steps. The
harness, runner, model and publication settings can all be changed later
with a flow update; the flow does not have to be recreated. A pinned flow
is only leased to a runner that advertises that harness for flows, so
check `preloop runner inventory` first.

This review reads CI runs and fetches from the repository, so it needs
network. Codex's `workspace-write` sandbox has no network by default: add
`"requires_network": true` to the flow's `agent_config` and
`"allow_network": true` to the host profile. The profile then advertises
`host_network` (shown in `preloop runner inventory`); a flow that requires
network is refused at save and run start when no runner in its pool offers
it, instead of succeeding with nothing reviewed. To let the agent's own
`git fetch` and `git push` reach the flow's repository, also set
`"agent_git_credential": true`: the runner leaves the repository's
credential in the checkout's local `.git/config` as an
`http.<remote>.extraHeader` that git sends only to that remote URL
(redirects refused). It never enters the agent's environment, but the agent
can read the file. The credential is the repository's tracker credential (a
stored personal access token, which may be long-lived, or an App
installation token) with every API scope that credential has, so an agent
that reads it can call the provider's API as that credential. Enable it
only on profiles you trust with that access, and connect the repository
with a dedicated minimal-scope token (contents read/write, and actions
read if the flow reads CI) when you do.
A run whose agent writes `result.json` with `"status": "failure"` ends
FAILED with the agent's summary as the error.

## Deterministic CI collection

Reading CI through the API turn by turn costs tokens and time, and the agent
cannot easily see failing test ids. The repository ships
`scripts/ci_health_collect.py` (standard library only), which reads CI and
writes one compact JSON file for the agent. The preset's prompt does not
call it yet; to adopt it, replace step 1 of your flow's prompt with this
command and have the agent read the output:

<!-- example: fragment -->
```bash
export GH_TOKEN="$PRELOOP_GIT_TOKEN_1"
python3 scripts/ci_health_collect.py collect \
  --github-repo owner/name \
  --window-hours 24 \
  --previous previous/result.json \
  --flow-branch-prefix preloop/<flow name slug>- \
  --out evidence/ci-health.json
```

- **Reads** GitHub Actions runs created since the previous run's window
  end, with a one-hour overlap; run ids already seen are skipped. A run
  still in progress is remembered and re-read by id on later collections
  until it finishes (given up after 24 hours), however long it runs.
  Requests are sequential and GET only. The collector stops, and marks the
  data partial, when `X-RateLimit-Remaining` falls below `--min-remaining`,
  on `Retry-After` or HTTP 429, on a refused token, on a 404 from a listing
  (a wrong repository name or a token without access), or at
  `--max-calls`. It exits 2 when no source could be read. Jobs and logs are
  immutable, so they are cached under `--cache-dir`.
- **Aggregates** per workflow and per job: success rate, reruns, p50/p95
  queue wait and duration, runner minutes by OS, the slowest jobs, and
  shard imbalance. Imbalance is the slowest shard over the mean shard, per
  run, for job names such as `backend (2/4)`.
- **Extracts failure signatures** from the logs of failed jobs, up to
  `--max-logs`. It recognizes the pytest short summary, `go test`, vitest
  and jest output, and JUnit XML (`--junit report.xml`). A job with no
  recognized test failure is keyed by its job and first error annotation.
  Each signature carries its test id, a normalized error class, the jobs,
  commits and run links, and whether the same job also passed on the same
  commit.
- **GitLab CI** (optional, read-only): `--gitlab-project group/name`,
  `--gitlab-api https://<host>/api/v4` and a token in `--gitlab-token-env`
  (default `GITLAB_TOKEN`). Pipelines, jobs, retries and traces are
  normalized to the same aggregates and signatures. Without a token,
  GitLab sources are skipped.

### Flake ledger and value summary

The collector keeps its state under `ci_health_state` in the run's
`result.json`. The next run receives that file as `previous/result.json`
(schedule payload `previous_result_execution_id: last`), so the state
persists with no extra storage. It is bounded to stay well inside the
256 KiB previous-result limit. It holds:

- `ledger`: one entry per signature, with `first_seen`, `last_seen`,
  `failures`, `windows_seen`, `flaky_evidence` (failed and passed on the
  same commit), recent `jobs`, `shas` and `runs`, and `passes_since_seen`.
  The agent owns `status` (`new`, `open`, `tracked`, `resolved`), `issue`,
  `pr` and `note`. A resolved signature that fails again becomes
  `regressed`.
- `trend`: one point per window (success rate, queue p95, runner minutes,
  failures, slowest job).
- `history`: one entry per run (change kind, tokens, cost, early stop).

Once a week the output gains `value_summary`. It covers runs and early
stops, PRs opened and merged, failures fixed, runner minutes saved per week,
tokens and cost, and cost per merged PR. The collector finds the flow's own
pull requests by head branch prefix, with one list request per repository.
Legacy publication names branches `preloop/<flow name slug>-<execution id>`,
so pass that prefix with `--flow-branch-prefix`. Have the prompt put a
`Runner minutes saved per week (estimate): N` line in each PR body; the
collector reads the claim back from merged PRs. Tokens and cost are only as
complete as the runs that recorded them in their history entry.

The output also has `early_stop` and `changed_reasons`. When no job failed,
no signature is new or regressed, no open signature went quiet and no
summary is due, the agent can write a short report and exit.

The agent ends by merging the state and its history entry into its result.
Its ledger edits, written under `ci_health_state.ledger` in `result.json`,
take precedence:

<!-- example: fragment -->
```bash
python3 scripts/ci_health_collect.py finalize \
  --collector evidence/ci-health.json --result result.json \
  --entry '{"change": "none", "early_stop": true}'
```
