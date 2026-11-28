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
