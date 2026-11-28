# CI health collector

`ci_health_report` is a builtin MCP tool that reads a repository's CI on the
server and returns one compact JSON report. It also keeps a failure ledger
for the calling flow. A flow calls it first, then spends its model budget
on judgment (what to fix, whether it is worth it) instead of on paging
through the CI API and reading logs.

Supported providers: **GitHub Actions** and **GitLab CI**. Both sit behind
one adapter interface.

## Enable it on a flow

```yaml
allowed_mcp_servers:
  - preloop-mcp
allowed_mcp_tools:
  - name: ci_health_report
  - name: get_flow_state   # optional: read the full ledger
```

The tool works only inside a flow run, and the flow comes from the run's
credential. Repositories must be synced projects of the flow's account on a
GitHub or GitLab tracker. The tool reads them with the tracker's stored
credential, through the same tracker-scope checks as the other code-host
tools. The agent never receives a CI token, and the tool only issues `GET`
requests.

## Arguments

All arguments are optional.

| Argument | Default | Meaning |
| --- | --- | --- |
| `repositories` | the flow's repositories | Up to 8 paths such as `owner/name` or `group/sub/name`. |
| `window_hours` | 24 | Runs created in this window (1-168). |
| `branches` | all | Only runs on these branches. |
| `protected_branches` | default branch | Their failures are read first. |
| `critical_path_jobs` | none | Globs of the jobs that gate a merge (`test*`, `lint`). Without them, a run's gate is whichever job finished last. |
| `max_runs` | 300 | Newest runs read per repository. |
| `max_failed_logs` | 20 | Failed-job logs downloaded in total; other failed jobs get a job-level signature. |
| `max_requests` | 500 | Provider requests per call, across repositories. |
| `rate_limit_floor` | 300 | Stop when the provider reports fewer remaining requests. |
| `report_only` | `auto` | `auto` reports `mode.report_only: true` for the first `report_only_days` (14) after the ledger was created; `on` and `off` force it. |
| `ledger_updates` | none | Up to 50 `{signature_id, status, linked, note}` changes; see below. |

## What it returns

```json
{
  "changed_since_last_run": true,
  "partial": false,
  "mode": {"report_only": true, "reason": "first 14 days of this flow (until 2026-01-15)"},
  "window": {"hours": 24, "start": "...", "end": "..."},
  "repositories": [{
    "repository": "example-org/app", "provider": "github",
    "default_branch": "main", "default_branch_state": "success",
    "new_runs": 4, "failed_logs_read": 3,
    "totals": {"runs": 5, "success_rate": 0.75, "rerun_rate": 0.2,
               "queue_p50_s": 30, "queue_p95_s": 120,
               "run_wall_p50_s": 620, "run_wall_p95_s": 1830,
               "runner_minutes": 76.2},
    "workflows": [{"workflow": "CI", "runs": 4, "success_rate": 0.67, "...": "..."}],
    "jobs": [{"workflow": "CI", "job": "test", "critical": true, "flaky_commits": 1,
              "failures": 1, "reruns": 2, "queue_p95_s": 60,
              "duration_p50_s": 600, "duration_p95_s": 600, "runner_minutes": 40.0}],
    "critical_path": {"slowest_gate": {"job": "test", "runs": 3, "gate_p95_s": 1830}},
    "shard_imbalance": [{"job": "test", "shards": 2, "imbalance_p50": 1.5,
                         "slowest_shard": "test (1/2)"}],
    "flaky_jobs": [{"workflow": "CI", "job": "test", "commits": 1}],
    "failures": [{"job": "frontend", "branch": "main", "url": "...",
                  "signatures": ["5696b9b62bca"],
                  "lines": ["AssertionError: expected ..."]}]
  }],
  "ledger": {"counts": {"open": 9, "fixed": 0, "regressed": 0, "ignored": 0, "quiet": 0},
             "flaky": 2, "top": ["..."], "pull_requests": ["..."],
             "saved": true, "version": 12},
  "previous": {"at": "...", "repositories": {"...": "..."}},
  "requests": {"used": 41, "max": 500, "provider_remaining": 4380, "stopped": null}
}
```

- **`changed_since_last_run`** is false when no run finished since the
  previous call. A flow can stop early on it: no new evidence, nothing new
  to decide.
- **Signatures** name *what* failed, not when, so the same broken test has
  the same 12-character id on every run, branch and commit. The collector
  recognizes:
  - pytest summary and `FAILED`/`ERROR` lines
  - `go test` (`--- FAIL:` with its package, panics)
  - vitest and jest
  - JUnit XML printed into a job log, and GitLab's pipeline test report

  A failed job whose log names no test gets a job-level signature
  (`workflow / job / failed step`). Messages are normalized (numbers, hex
  ids, paths, quoted values) before they count, so they don't split one
  failure into many.
- **Failure lines** are up to 8 short lines per failed job, from the tail of
  the log, with secrets scrubbed.
- **Flaky**: a job that both failed and passed on the same commit (a rerun,
  or two runs of one commit) marks its signatures flaky.
- **Partial**: the request budget, the rate-limit floor or a rate-limit
  answer stopped reading. The report says which (`partial_reason`). The
  next call continues: runs that were not fully read are not marked as
  counted.

## The ledger

The ledger lives in [flow state](flow-state.md) under the reserved key
`preloop.ci_health`. The collector writes it on every call with
compare-and-set. Runs can read it with `get_flow_state`, and owners can reset
it by deleting the key.

Per signature it keeps:
- framework, test and error
- repositories
- first and last seen, and a count
- the last five commits and runs
- the jobs, and the flaky flag
- `status`, `linked` URLs and a `note`

Each run counts once, so overlapping windows never double-count.

| Status | Set by | Meaning |
| --- | --- | --- |
| `open` | collector, agent | Seen recently, not handled. |
| `fixed` | agent | A change addressed it. |
| `regressed` | collector | Seen again after `fixed`. |
| `ignored` | agent | Known and accepted (say why in `note`). |
| `quiet` | collector | Not seen for 7 days; reopens if seen again. |

The agent changes status, links and notes with `ledger_updates`:

```json
{"ledger_updates": [{"signature_id": "5696b9b62bca", "status": "fixed",
                     "linked": "https://github.com/example-org/app/pull/42",
                     "note": "Race on the shared fixture; isolated per test."}]}
```

The ledger also records the pull requests earlier runs of the flow
published (from each execution's `pr_url`). It refreshes their state (open,
merged, closed) through the adapter of the repository they belong to. A
run's `result.json` can carry `change.estimated_runner_minutes_saved_per_week`
and `change.signatures`; both are copied onto the recorded pull request.

The ledger keeps at most 150 signatures (open and regressed first), 100
history points and 60 pull requests, and is trimmed to fit the 256 KB flow
state cap.

## Caching and rate limits

- Completed runs never change: their jobs and log findings are cached in
  the server process, so the flow's next call reads only new runs. A call
  with nothing new costs a handful of list requests.
- One request budget is shared across the call's repositories. The
  collector reads `X-RateLimit-Remaining` (GitHub) and `RateLimit-Remaining`
  (GitLab), stops under the floor, and treats 429 or a rate-limited 403 as a
  stop, not a retry.
- Logs are streamed with a byte cap and only the last 256 KB are parsed,
  which is where test runners print their summaries. GitHub's log redirect
  goes to blob storage; the credential is not forwarded to it.

## With a pre-check

When the flow has a pre-check (the deterministic pre-run stage of #2541)
that started the run,
the URLs of the runs it matched (for example a "new failed runs" check)
are read first. They are listed in `precheck_focus`. The pre-check decides
*whether* a run is worth starting, using a few cheap requests. The collector
does the heavier reading inside the run.

## In the console

The flow page shows a **CI health** card for any flow with a ledger:

- per repository: success rate, queue p95 and the slowest gate, with the
  change over 7 days and a trend line;
- the failures worth a look;
- the value the flow delivered over the last 7 days.

The data is `GET /api/v1/flows/{flow_id}/ci-health?days=N` (`view_flows`).
It is built from the ledger and the flow's executions (tokens, estimated
cost, published pull requests), and calls no provider. See
[Daily CI and QA review](daily-ci-qa-review.md#results-in-the-console).
