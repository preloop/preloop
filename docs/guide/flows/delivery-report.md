# Delivery Report preset

**Delivery Report** (`backend/presets/022-delivery-report.yaml`, slug
`delivery-report`) is a scheduled, read-only flow. See what landed, what the
available evidence supports against stated criteria, and which gaps deserve
investigation. It reads tracker and forge data only, so it works whether or
not a Preloop-managed agent did the work.

It ships disabled. Open **Flows > New flow**, pick **Delivery Report**, add
the repositories in scope to its git clone configuration and enable it.

Pull requests opened by the GitHub Copilot coding agent are recorded too,
with origin `copilot`, and linked to the synced issues they close or name in
their branch, so they show up in the delivery record and fleet history.

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
`search_sessions`, and it reads git history in a read-only clone. It never
pushes or opens pull requests. `create_issue`, `update_issue` and
`add_comment` are listed too, but the MCP endpoints refuse every call from a
report run that the report policy does not allow (see Tracker actions
below), on top of the account's tool policy.

## Report policy

Each account has one report policy, and each repository can override part
of it. Set it on the Account settings page (Delivery Report) and, per
repository, on the tracker's Projects tab. The API is
`GET`/`PUT /api/v1/delivery-report/policy` for the account and
`GET`/`PUT`/`DELETE /api/v1/delivery-report/policy/projects/{project_id}`
for a repository. A save stores only the fields it changes; the
effective policy is the defaults, then the account, then the repository
(`resolve_policy()` in `backend/preloop/services/delivery_report.py`).

| Setting | Default | Who sets it |
|---|---|---|
| Report on | yes; one toggle turns it off | account, repository |
| Cadence | weekly (Monday 07:00 UTC); daily or manual | account |
| Scope | every linked project | account |
| Report mode | `deterministic` (no model call) | account |
| First report assessed | yes, once, under the per-run budget | account |
| Per-run budget | $5, applied as `agent_config.limits.max_usd` | account |
| Monthly budget | $25 | account |
| Drop first when short of budget | risk detail, then assessments beyond 25 changes; the shipped list is never dropped | account |
| Open PR window | 7 days | account, repository |
| Inactive issue window | 14 days | account, repository |
| Criteria source | the issue's triage assessment section, else an "Acceptance" heading | account, repository |
| UI screenshot threshold | 40 changed frontend lines | account, repository |
| Tracker writes | create follow-up: off (a proposal in the report); reopen: approval; close: always deny | account |
| Recipients | the account owner, or a sponsoring lead; anyone can opt out | account |

The deterministic report (shipped, closed, open, the delta and the
deterministic risk flags) costs nothing and is on by default. The first
report is assessed once, capped by the per-run budget, so the account sees
what an assessment adds. After that, runs stay deterministic until the
account sets `report_mode: assessed`, which the API accepts only together
with both budgets. Saving the account policy copies the cadence and the
per-run budget onto the account's Delivery Report flows; a manual cadence
turns the flow's schedule off.

When a run starts, the platform decides whether it may pay for a model pass
(`assessment_decision()`): off, deterministic, assessed, the onboarding
assessment, or skipped because the month's report spend has reached the
monthly budget or the hosted credit wallet is empty. A run that may not
call a model never starts the agent. The platform builds the report from
what the tracker sync and the pull request ledger already stored, so it
still goes out, with one line saying assessments were skipped
(`summary.assessments_skipped_reason`: `budget` or `no_credit`). That
stored data has no PR title, body or file list, so file-based risk flags
and change brief gaps appear only in assessed runs. An assessed run gets
the effective policy as `payload.report_policy`.

A repository can change only its windows, its criteria source and whether
it is in the report. Budgets, cadence, scope, tracker writes and recipients
stay on the account, because one run covers every repository and spends
one budget.

Tracker writes start off. Follow-up issues and reopen requests are proposals
in the report, and opening one is the approval. Automatic creation is a
per-account setting (`create_follow_up: allow`), meant to be enabled once
the proposals have proven precise on that account. The report never closes
an issue: a policy with `close` set to anything but `deny` is refused.

### Tracker actions

A person acts on a proposal from the report page, through
`POST /api/v1/delivery-reports/{id}/actions`:

- `{"kind": "open_issue", "index": N}` opens follow-up N as a tracker issue
  labelled with `follow_up_label` (`preloop:proposed`), with the reason, the
  evidence and a link back to the report. Needs `create_issues`.
- `{"kind": "reopen", "ref": "owner/repo#N"}` reopens the issue and comments
  with the assessed revision, the unmet criteria and the evidence. Needs
  `edit_issues`. A stale request (the issue was edited, or new commits landed
  on its closing pull request, after the assessment) is refused with 409
  `stale`: assess the issue again first.
- `{"kind": "dismiss", "key": "follow_up:N" | "reopen:<ref>", "reason": "..."}`
  records the decision without touching the tracker.
- `{"kind": "close"}` is always refused with 403 `close_denied`.

The click is the approval, so it does not depend on the run's write rules.
Each proposal takes one decision; it is stored on the run under
`proposal_actions` and a second click gets 409 `already_decided`.

The run itself writes only where the account allows it:
`create_follow_up: allow` lets it open follow-ups (the platform adds the
label and the report link), `reopen: allow` lets it reopen and comment. Any
other write from a report run, and every close, is refused with 403.

Every write, refusal and dismissal is in the audit log as
`delivery_report_tracker_write`, with what allowed or refused it
(`allowed_by: click` or the policy rule).

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

## In the console

**Audit > Delivery reports** (`/console/delivery-reports`) lists every run
that stored a report: period, projects, shipped changes, closed issues that
are partial or have an evidence gap, proposals and spend. Open a run for the
full report. Its summary strip shows the linkage, criteria availability and
evidence availability ratios and the closed-issue verdict counts as separate
numbers. "Unverified" is drawn as an evidence gap, never as a failure. Each
section and each project has an anchor, and a change's or an issue's
criteria and evidence expand in place.

A reopen request shows as stale once the issue was edited, or new commits
landed on its closing pull request, after the assessment. The Open issue,
Reopen and Dismiss buttons are shown disabled for now: the server reports
which report actions it supports (`actions` on
`GET /api/v1/delivery-reports/{id}`) and none are implemented yet.

The platform rechecks the agent's verdicts. A verdict that its cited
evidence does not support is downgraded, and a risk flag with no evidence is
dropped. A markdown rendering of the report is stored as
`report_markdown`.
