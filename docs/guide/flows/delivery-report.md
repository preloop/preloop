# Delivery Report preset

**Delivery Report** (`backend/presets/022-delivery-report.yaml`, slug
`delivery-report`) is a scheduled, read-only flow. See what landed, what the
available evidence supports against stated criteria, and which gaps deserve
investigation. It reads tracker and forge data only, so it works whether or
not a Preloop-managed agent did the work.

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

## What the run reuses

The report does not pay twice for a judgement that already exists.

- **Merge audits.** When the
  Merged Issue Completion Audit
  (`backend/presets/013-merged-issue-completion-audit.yaml`) has completed for a closed issue at its closing merge SHA, the report takes
  that result as the purpose assessment (source `audit`) and does not assess
  the issue again. The run receives the window's completed audits as
  `previous/merge_audits.json`, and the platform applies them again when it
  finalizes the report, whatever the run wrote. An audit at another revision
  is not used.
- **Previous assessments.** A closed issue whose `updated_at` and closing PR
  head SHA are the same as in the previous report keeps that assessment
  (source `cache`).
- **Change briefs and review summaries.** A PR body may carry a
  `<!-- PRELOOP_BRIEF -->` block (sections What changed, Why, Assumptions,
  Not verified, Rollback, Decisions, closed by `<!-- /PRELOOP_BRIEF -->`)
  and the reviewer's `<!-- PRELOOP_SUMMARY -->` block (with **Not verified**
  and **Must know** lines). The report parses both without a model call. A
  brief is the author's claim, not an assessment. Its "Not verified" bullets
  become evidence gaps on the change. A PR with a brief or a summary gets no
  model risk pass unless a deterministic flag fires.
- **Metadata only.** Dependabot PRs and PRs that only touch docs or the
  README are reported from metadata with no model call (source `metadata`).

Only closed issues with no audit and no unchanged previous assessment, and
PRs with neither a brief nor a summary, go to the model (source `report`).

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
| Report mode | `assessed`; `deterministic` makes no model call |
| Per-run budget | $5, applied as `agent_config.limits.max_usd` |
| Monthly budget | $25; at the limit the run reports without assessments |
| Drop first when short of budget | risk detail, then assessments beyond 25 changes; the shipped list is never dropped |
| Open PR window | 7 days |
| Inactive issue window | 14 days |
| Criteria source | the issue's triage assessment section, else an "Acceptance" heading |
| UI screenshot threshold | 40 changed frontend lines |
| Tracker writes | create follow-up: off (a proposal in the report); reopen: approval; close: always deny |

The deterministic report (shipped, closed, open, the delta and the
deterministic risk flags) costs nothing. The assessed report adds the model
pass under the per-run and monthly budgets. The defaults come from a measured
run on a heavy week. The gateway enforces the per-run budget the same way as
any other [execution limit](flow-execution.md). When assessments are skipped
for budget, the report still goes out and says so in one line
(`summary.assessments_skipped_for_budget`).

Tracker writes start off. Follow-up issues and reopen requests are proposals
in the report, and opening one is the approval. Automatic creation is a
per-account setting (`create_follow_up: allow`), meant to be enabled once
the proposals have proven precise on that account. The report never closes
an issue.

## The report

The report is the run's `result.json`. When the agent exits, the platform
finalizes it:

- **Summary**: how many changes shipped, how many issues closed, how many
  changes carry risk flags and how many items are open past the window.
  Then separate numbers, never one score, all computed from the structured
  report rather than model prose:
  - linkage: shipped changes with a linked issue, of all shipped changes;
  - criteria available: closed issues that state criteria, of all closed
    issues;
  - evidence available: criteria of closed issues with checkable evidence,
    of all those criteria;
  - verdict counts: closed issues that are covered, partial, unverified or
    without criteria.
- **Needs you**: proposed reopens, closed issues with an evidence gap or
  partly covered (with the criteria not shown met), risk flags, and aged
  items.
- **Shipped**: each merged change with its area, linked issues and a purpose
  verdict:
  - `covered`: every criterion is met, each with cited evidence (a file and
    line, a test result or a review comment).
  - `partial`: some criteria are met with evidence.
  - `unverified`: an evidence gap. The available evidence cannot decide
    the criteria. It is not a failure and never triggers a reopen by
    itself.
  - `no_criteria`: the issue states no criteria. Such an issue is never
    reported as `covered`.

  Each verdict names its source: `audit`, `report`, `cache`, `metadata` or
  `none`. Each risk flag cites the evidence that raised it. Deterministic
  flags come first and cost nothing:
  - sensitive path: auth and session code, migrations, billing and
    payments, public API surface and OpenAPI, CI and release workflows,
    secrets handling;
  - hot path without tests, and public API without a version note;
  - UI without a screenshot, only when a style or view file changes or the
    changed frontend lines exceed the threshold;
  - migration without a downgrade, except Alembic merge revisions, whose
    no-op downgrade is by design;
  - incomplete file list, when the forge reports more changed files than
    were read.

  The model adds a risk only where a deterministic rule cannot.
- **Closed issues**: each closed issue with its verdict and source. When the
  issue's latest comment measures a criterion as not met and the closing PR
  adds no newer measurement, the run proposes a reopen that names the
  revision it assessed.
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
