# Daily CI and QA review preset

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

The **Daily CI and QA review** preset
(`backend/presets/023-daily-ci-qa-review.yaml`) reviews your repositories'
CI (GitHub Actions or GitLab CI) on a schedule. It keeps a ledger of the
failures it has seen. Once it has a baseline, it fixes the most valuable
CI or test problem in one focused pull request, with tests and measured
before/after numbers, but only when the fix clears a value bar.

## What a run does

1. **Collects, in one tool call.** The agent calls
   [`ci_health_report`](ci-health.md), which reads CI on the server with
   the tracker's stored credential and returns one compact report. The
   report covers, per workflow and job:

   - success and rerun rates
   - queue and duration p50/p95, and runner minutes
   - the critical-path gate
   - shard imbalance and flaky jobs
   - the failed jobs with their failure signatures (pytest, go test,
     vitest, jest, JUnit) and failure lines

   It also returns the flow's failure ledger and the previous run's
   headline numbers. The agent never pages the CI API and never holds a
   CI token.
2. **Stops early when nothing changed.** If no run finished since the
   previous run and no signature regressed, the run writes a three-line
   report and ends without reading the repository or running tests.
3. **Assesses.** The agent estimates the weekly value of each candidate
   from the report: developer minutes and runner minutes saved by fixing
   a flaky or failing signature, shortening the critical-path gate or
   rebalancing shards. It also considers coverage, Playwright or
   performance tests where risk is high. It drops anything an open issue
   or pull request already covers.
4. **Decides.** It changes nothing in report-only mode, when the flow
   already has `max_open_pull_requests` open pull requests, or when no
   candidate clears the value bar. Otherwise it picks the single best
   candidate.
5. **Changes one thing, measured**, with tests, the same before/after
   measurement and the repository's pre-commit hooks.
6. **Updates the ledger** (links to covering issues, "ignored" with a
   reason). A signature the change fixes is listed in
   `change.signatures`. The flow marks it fixed when the pull request
   merges, and "regressed" if it fails again.
7. **Reports**: `/workspace/evidence/ci-qa-report.md` (one page) and
   `/workspace/result.json`. The result carries the metrics, the ranked
   candidates with their estimates, the change with its estimated weekly
   saving, and the pull request title and body.

## Knobs

All knobs live in the schedule payload, which reaches the prompt through
`{{trigger_event.payload}}`.

| Key | Default | Meaning |
| --- | --- | --- |
| `ci_repositories` | flow's repositories | Synced repository paths (`owner/name`, `group/sub/name`), GitHub or GitLab. Repositories other than the flow's own are read, never changed. |
| `ci_window_hours` | `24` | How far back to read CI runs. |
| `critical_path_jobs` | none | Globs of the jobs that gate a merge (`["test*", "lint"]`). Without them, a run's gate is whichever job finished last. |
| `protected_branches` | default branch | Their failures are read first and weigh more. |
| `report_only` | `auto` | `auto`: report only for `report_only_days` after the first run, then changes are allowed. `on` / `off` force it. |
| `report_only_days` | `14` | Length of the report-only period. |
| `value_threshold_dev_minutes_per_week` | `60` | A change must save at least this much developer time per week... |
| `value_threshold_runner_minutes_per_week` | `60` | ...or this many runner minutes per week. |
| `max_open_pull_requests` | `2` | Open pull requests from this flow at which it stops opening new ones. |
| `focus` | none | Free-text hint, for example `flaky e2e` or `shard balance`. |

The per-run budget is `agent_config.limits.max_usd` (10 USD), enforced by
the gateway, and the wall-clock cap is `timeout_seconds` (90 minutes).

## Guardrails

- The agent is read-only towards every remote: no pushes, no pull request
  edits, no comments, no CI reruns, and no direct CI API calls. The
  platform publishes at most one pull request per run, on a new branch,
  after the agent exits, so a run never pushes to an existing pull
  request, approved or not. The pull request body is the
  `pull_request.body` the agent writes in `result.json`, with the
  before/after numbers, the estimated saving and the tests it ran.
- No skip, xfail, quarantine, retry wrapper or raised timeout to make a
  test green, and no weakening of CI gates.
- Performance tests run only against services started in the run's own
  workspace with mocked upstreams; nothing loads staging or production.

## Results in the console

A flow that has run the collector gets a **CI health** card on its page:

- per repository: success rate, queue p95 and the slowest gate, each with
  the change over the last 7 days, runner minutes, and a success-rate
  trend line;
- the failures worth a look: regressed first, then flaky, then by count,
  with their last sighting and the issue or pull request linked to them;
- **value over the last 7 days**:
  - runs (and how many a pre-check skipped)
  - pull requests opened and merged
  - failures fixed
  - the runner minutes per week the merged changes claim to save (an
    estimate from each change's own measurement)
  - tokens and estimated cost, and cost per merged pull request

The same data is `GET /api/v1/flows/{flow_id}/ci-health?days=7`.

## Running it often, cheaply

For a busy repository, an interval schedule (every 2 hours, say) gives
faster feedback. A tick with no new CI result costs a handful of provider
requests and one short model turn, because the collector caches completed
runs and the run stops early. With the flow pre-check stage (#2541), a tick
with nothing new does not start a run at all. For example, run only when
the default branch is red or a CI run failed since the last run:

<!-- example: fragment -->
```yaml
precheck:
  version: 1
  applies_to: [schedule]
  run_if: any
  max_skip_hours: 24
  checks:
    - id: main_red
      type: default_branch_ci
      state: failing
    - id: new_failures
      type: failed_ci_runs
      since: last_run
```

The runs the pre-check matched are read first by the collector. With
`max_skip_hours: 24`, the flow still reviews once a day even when nothing
failed.

## Setting it up

Clone the preset, add the repository to the flow's git clone
configuration (and any other repositories to `ci_repositories`), and set
the schedule's timezone to the team's zone (the schedule is evaluated in
that zone, so 06:00 stays 06:00 across DST). Set `critical_path_jobs` to
the jobs your branch protection requires. Keep the
`{{trigger_event.payload}}` line if you customize the prompt.

Every repository must be synced in the account, on a GitHub or GitLab
tracker whose credential can read CI: Actions read on GitHub, or
`read_api` on GitLab. The collector reports a repository it cannot read
under `unavailable`, and the run says so.

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
with a dedicated minimal-scope token (contents read/write) when you do.
The collector reads CI with the stored tracker credential on the server, so
the agent itself needs no CI access.
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
