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
  pull request per run (isolated publication) after the agent exits, so a
  run can never push to an existing pull request, approved or not.
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
agent reads CI through the GitHub credential available in the run (`gh`
or `GITHUB_TOKEN`); without one it writes a failure report saying so.

To run it on your own machine instead of hosted compute, pin the harness
and runner in `agent_config` (see
[Run flows on your machine](../runners/your-machine.md#flows-on-your-machine)):

<!-- example: fragment -->
```json
{
  "harness": "codex_cli",
  "runner_id": "<runner id>",
  "codex_model": "<model alias the runner advertises>",
  "harness_fallback": "fail"
}
```

The harness, runner and model can be changed later with a flow update; the
flow does not have to be recreated. A pinned flow is only leased to a
runner that advertises that harness for flows, so check
`preloop runner inventory` first.
