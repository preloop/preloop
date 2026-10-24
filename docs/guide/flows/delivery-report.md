# Delivery Report preset

**Delivery Report** (`backend/presets/022-delivery-report.yaml`, slug
`delivery-report`) is a scheduled, read-only flow. It reports what landed in
the connected projects, whether each change did what its issue asked, what it
may have broken, and what is still open. It reads tracker and forge data
only, so it works whether or not a Preloop-managed agent did the work.

It ships disabled. Open **Flows > New flow**, pick **Delivery Report**, add
the repositories in scope to its git clone configuration and enable it.

## Schedule

The default schedule is weekly, Monday 07:00 UTC. For a daily report change
`schedule_config` to `{"type": "daily", "at": "07:00"}`. You can also run
the flow manually at any time. The run covers
`trigger_event.payload.window` (see
[the run's time window](flow-triggers.md#the-runs-time-window)). A manual
run with no window covers the seven days before it starts.

Keep `payload.previous_result_execution_id: last` in the schedule. It makes
the previous report available to the run as `previous/result.json`.

## Tools

The run can call `search_issues`, `get_issue`, `get_pull_request` and
`search_sessions`, and it reads git history in a read-only clone. It cannot
create or update issues, comment, push or open pull requests.

## Report policy

`DeliveryReportPolicy` in `backend/preloop/services/delivery_report.py`
holds the defaults:

| Setting | Default |
|---|---|
| Cadence | weekly (Monday 07:00 UTC); daily or manual |
| Scope | every linked project |
| Per-run ceiling | $5, applied as `agent_config.limits.max_usd` |
| Monthly ceiling | $25 |
| Drop first when short of budget | risk detail, then assessments beyond 25 changes; the shipped list is never dropped |
| Open PR window | 7 days |
| Inactive issue window | 14 days |
| Criteria source | the issue's triage assessment section, else an "Acceptance" heading |
| Tracker writes | create follow-up: allow (labelled `preloop:proposed`); reopen: approval; close: always deny |

The gateway enforces the per-run ceiling the same way as any other
[execution limit](flow-execution.md). The $5 default is provisional and will
be replaced by a value measured on real runs. Tracker writes are not enabled
yet; the policy only records the defaults they will use.

## The report

The report is the run's `result.json`. When the agent exits, the platform
finalizes it:

- **Summary**: how many changes shipped, how many issues closed and how many
  of those are unverified, how many changes carry risk flags, and how many
  items are open past the window.
- **Needs you**: closed-but-unverified issues with the unmet criteria, risk
  flags, and aged items.
- **Shipped**: each merged change with its area, linked issues and a purpose
  verdict:
  - `covered`: every criterion is met, each with cited evidence (a file and
    line, a test result or a review comment).
  - `partial`: some criteria are met with evidence.
  - `unverified`: the run could not check the criteria.
  - `no_criteria`: the issue states no criteria. Such an issue is never
    reported as `covered`.

  Each risk flag also cites the evidence that raised it. The flags are:
  hot path without tests, public API without a version note, UI without a
  screenshot, and migration without a downgrade.
- **Closed issues**: each closed issue with its verdict.
- **Open past the window**: open PRs and inactive issues older than the
  policy windows.
- **Since the previous report**: each open question from the previous
  successful report, marked answered or still open.
- **Open questions**: the questions this run asked plus the ones still open
  from earlier reports. They are saved with the report even when the run
  fails, so they are never dropped.
- **What this run spent**: cost and tokens taken from gateway usage. The
  agent cannot set these numbers. If the gateway could not price the run,
  the cost shows as unknown rather than zero.

The platform rechecks the agent's verdicts. A verdict that its cited
evidence does not support is downgraded, and a risk flag with no evidence is
dropped. A markdown rendering of the report is stored as
`report_markdown`.
