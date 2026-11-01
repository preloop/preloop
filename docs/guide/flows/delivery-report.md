# Delivery Report preset

**Delivery Report** (`backend/presets/022-delivery-report.yaml`, slug
`delivery-report`) is a scheduled flow with tracker writes off by default.
See what landed, what the
available evidence supports against stated criteria, and which gaps deserve
investigation. It reads tracker and forge data only, so it works whether or
not a Preloop-managed agent did the work.

Connecting a tracker creates one enabled Delivery Report flow for the account.
The first scheduled run is the next weekly slot and covers the prior seven
days. Additional trackers use the same flow. Its deterministic report still
runs when the account has no model configured; in that case the email names
the skipped assessment. Bind a model to the flow to enable its one-time
onboarding assessment. The account policy can turn the report off or change
its cadence and scope. An assessed schedule resolves its enabled, linked
code-host repositories into read-only checkouts when the run starts.

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

On a report page, choose a project in the header and select **Re-run project**.
This starts a manual execution for that project and the report's original
window, with a link to the new execution. The current report policy still
controls scope, assessment budgets and tracker writes; disconnected or
disabled projects cannot be re-run from the report.

The console's **Attention** page lists only report proposals that need a
person: reopen requests, proposed follow-ups, and tracker writes whose
outcome needs checking. Dismissing an attention item silences that reminder
until its pending proposals change; deciding or dismissing a proposal takes
place on the report. Verdicts and evidence gaps alone do not create alerts.

An execution that produced a pull request shows its recorded forge state
and newest assessment for the PR's current revision. A new revision without
an assessment says **Not assessed**. **Unverified** means an evidence gap
and uses the same warning tint as on the report, never a failure colour.

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
  The platform derives the label from the author and the listed files when
  the run leaves it out; a file list the forge reports as incomplete is not
  enough to call a change docs-only.
- **Reopen, not follow-up.** When a closed issue's criterion is shown not
  met (for example its last comment, after the last criteria edit,
  measures it), the issue was closed too early. A follow-up the run
  proposed for that issue becomes a reopen request pinned to the closing
  merge SHA, with the follow-up's and the criterion's evidence. Without a
  known closing merge SHA the follow-up stays. An evidence gap (`met: null`)
  never becomes a reopen request.

Only closed issues with no audit and no unchanged previous assessment, and
PRs with neither a brief nor a summary, go to the model (source `report`).

## Deterministic checks

The run receives `tools/delivery_report_checks.py`, the source of
`backend/preloop/services/delivery_report_checks.py` (standard library
only), and calls it instead of following prose rules:

- `scope <pr.json> --criterion "..."` gives the bounded input for one PR:
  the file list, the added test names, the last `PRELOOP_SUMMARY` block and
  the check conclusion. Review threads are included only when the PR closes
  an issue that states criteria, or a code-scanning alert or a HIGH or
  CRITICAL finding is present. Source diff hunks are included only for
  criteria that no added test name matches, capped at 200 lines per file
  and 400 lines across the PR. Truncation is marked in the scope output.
- `parent-check --repo <clone> --merge <sha> --path <file> --term <name>`
  looks for the removed or changed name in each file at `<merge>^1`. The
  run puts the output on the criterion as `parent_check`. When the platform
  finalizes the report, only an explicit "update X accordingly" criterion
  naming the checked paths and search terms can become met from a matching
  record showing no references at the known closing merge. Other misses
  remain misses. An "accordingly" criterion without usable, matching evidence
  becomes an evidence gap; never rewrite the source criterion to fit a record.

## Measuring prompt changes

`backend/tests/fixtures/delivery_report/eval/` holds the first measured
week of this repository's report: 40 closed issues with the right `met`
value for every criterion, checked by hand (15 covered, 7 partial, 18
unverified), whether a reopen is the right proposal, and 8 merged PRs
without an issue (`no_criteria`; dependency bumps and docs-only changes are
metadata-only). Score a run's `result.json` against it with:

```bash
cd backend && python -m preloop.services.delivery_report_eval result.json
```

Add `--min-accuracy 0.9` to use it as a gate: the command exits 1 when
verdict accuracy is below the value. Without it, it only prints the score.

The score covers verdict accuracy and a confusion table, criterion
agreement, reopen proposals (correct, false, missed) and the shipped rows.
`baseline_spike_assessors.json` is the week's first assessor output: it
scores 37 of 40 verdicts and misses both reopens.

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
| Merge audit CI wait | 30 minutes, maximum 24 hours | account, repository |
| Criteria source | the issue's triage assessment section, else an "Acceptance" heading | account, repository |
| UI screenshot threshold | 40 changed frontend lines | account, repository |
| Tracker writes | create follow-up: off (a proposal in the report); reopen: approval; close: always deny | account |
| Recipients | the account owner, or a sponsoring lead; anyone can opt out | account |
| Per-member updates (`per_member_updates`) | off (`false`); short updates require Enterprise Edition | account |

The per-member updates switch requires the
`delivery_report_member_updates` feature. When `delivery_report_recipients`
is also enabled, it appears in the Recipients section and policy managers
save it through the recipients API. A plugin that enables member updates
without the recipients feature keeps the standalone account-policy switch,
saved with `manage_budgets` through the policy API. A registered
`delivery_report_distributor` service can use the completed report and its
forge engagement facts to send these short updates. Without that extension,
the core distributor sends the standard report email.

### Enterprise recipients

With `delivery_report_recipients` enabled, Account settings shows the
Recipients section to members with `view_policies`. It lists active account
members, their RBAC roles, effective report role, role source, saved delivery
and opt-out. Changing recipients, role overrides or opt-outs requires
`manage_policies`, independently of the `manage_budgets` permission used for
report cadence and budgets. Repository settings do not show this roster.
Each recipient can still change their own email subscription without either
management permission.

Report roles control report delivery; changing one does not grant an RBAC
permission. The account owner has the fixed `owner` report role. Other members
use an explicit report-role override first, then the sponsoring-lead role,
then their RBAC mapping:

| Account RBAC role | Default report role |
|---|---|
| `owner`, `admin` | `owner` |
| `tracker_manager` | `lead` |
| `editor`, `executor`, or no mapped role | `member` |
| `analyst`, `viewer` | `viewer` |

When someone holds several mapped roles, the widest report role wins
(`owner`, then `lead`, `member`, `viewer`). Choose **Use default** to clear
an override and return to that resolution. The sponsoring lead is selected
from the account's active member list.

The designated account owner and sponsoring lead receive the full report.
When member updates are enabled, other `owner` and `lead` report roles also
receive the full report, `member` roles receive a short update, and `viewer`
roles receive no email. A saved opt-out stops all delivery for that person,
including the account owner or sponsoring lead; turning the report off stops
all delivery. The roster's delivery column shows the saved result, so save
draft changes before relying on it.

`GET /api/v1/delivery-report/recipients` returns the current account's
`per_member`, `recipients`, `sponsoring_lead_user_id`, `opted_out_user_ids`
and `members`. `PUT` at the same path accepts those settings plus `roles`,
a map from active member IDs to `owner`, `lead`, `member`, `viewer` or
`null` (clear the override). Send only changed fields: omitted fields remain
unchanged, while `opted_out_user_ids` replaces the saved opt-out list. For
example, this request changes one report role without changing recipients
or opt-outs:

```json
{"roles": {"00000000-0000-4000-8000-000000000002": "viewer"}}
```

The server derives the account from the authenticated membership and rejects
IDs outside that account's active roster. Budget saves preserve the latest
recipient settings, including when the recipients feature is hidden.

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
monthly budget, the hosted credit wallet is empty, or no model is bound. A run that may not
call a model never starts the agent. The platform builds the report from
what the tracker sync and the pull request ledger already stored, so it
still goes out, with one line saying assessments were skipped
(`summary.assessments_skipped_reason`: `budget`, `no_credit`, or `no_model`). An
assessed run gets the effective policy as `payload.report_policy`.

The pull request ledger keeps each PR's title, body and file list, so the
deterministic report describes every change and runs the file-based risk
flags and change brief gaps without a model. Pull request webhooks store
the title, the body and the author's login. Webhooks carry no file list, so
before a deterministic run the platform reads it from the forge for up to
50 merged PRs in the window that have none, once per PR, with the account
owner's access (GitHub and GitLab). Patches are kept only for migration
files, which the missing-downgrade rule reads. Each shipped change carries
`body_summary` (the first prose paragraph of the PR body, outside the brief
and review summary blocks), `files` and `files_changed`.

### Email

When a run finishes, deterministic or assessed, the report is emailed to
its recipient: the account owner, or the sponsoring lead when the policy
names one (the owner when that user is no longer an active member). The
email has the summary, what needs you, the shipped changes with their
summary and files, the line saying assessments were skipped when they were,
and a link to the report page. A run sends once; the outcome is stored on
the result as `distribution` (`sent`, `failed`, `disabled`, `no_recipients`,
`not_configured`). `not_configured` means SMTP credentials are missing.
The `sent_to` and `failed` lists record each recipient's outcome; a `failed`
record may still have a non-empty `sent_to` when only some recipients were reached.

Every email carries a signed opt-out link, also sent as the
`List-Unsubscribe` header. Opening it shows a confirmation button, so a
mail scanner that follows links opts no one out; a POST to it opts out,
which is also what a mail client's one-click unsubscribe sends. Signed in,
the recipient can switch their copy off and on in the Delivery Report card
on Account settings (`GET`/`PUT /api/v1/delivery-report/subscription`).
Opt-outs belong to each person: saving the account policy keeps them.

A repository can change only its windows, its criteria source and whether
it is in the report. Budgets, cadence, scope, tracker writes and recipients
stay on the account, because one run covers every repository and spends
one budget.

The deterministic report generates follow-up proposals from each risk flag,
with its file evidence, except for dependency-bot and docs-only PRs reported
from metadata. Proposals appear in the console, Markdown report and owner email. It also proposes reopening a closed issue when a merge
audit at its closing revision cites a criterion that was not met. The audit
must still match the issue's criteria, and the issue must not have been edited
after the audit. A missing audit, an unpinned revision or a pure evidence gap
does not produce a reopen proposal. These proposals require no model call.

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
When the tracker refuses the write, the proposal is free for another
click. When the outcome is unknown (a timeout or a dropped connection), the
decision is kept as `unconfirmed` and further clicks get 409 `unconfirmed`,
so a retry cannot open a second issue: check the tracker, then dismiss the
proposal. Dismiss also clears a claim left `pending` for 15 minutes.

The run itself writes only where the account allows it:
`create_follow_up: allow` lets it open follow-ups (the platform adds the
label and the report link), `reopen: allow` lets it reopen and comment. A
reopen that also edits the title, description, labels, assignee or priority
is refused, as is any other write from a report run and every close (403).

Every write, refusal and dismissal is in the audit log as
`delivery_report_tracker_write`, with what allowed or refused it
(`allowed_by: click` or the policy rule). For a run's own write the row
records the policy decision (status `allowed` or `denied`), not whether the
tracker applied it.

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
  - `partial`: the evidence shows at least one criterion not met, for
    example the issue's latest measurement misses its target and the
    closing PR adds no newer one.
  - `unverified`: an evidence gap. Nothing shows a criterion unmet, but
    the available evidence cannot decide at least one of them, however
    many others are met. It is not a failure and never triggers a reopen
    by itself.
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
landed on its closing pull request, after the assessment, and its Reopen
button is disabled with the reason. Each proposal offers Open issue or
Reopen and Dismiss (see [Tracker actions](#tracker-actions)); once decided
it shows the decision instead. The server reports which actions it supports
(`actions` on `GET /api/v1/delivery-reports/{id}`), and a server that
reports one as unsupported gets it rendered disabled.

The platform rechecks the agent's verdicts. A verdict that its cited
evidence does not support is downgraded, and a risk flag with no evidence is
dropped. A markdown rendering of the report is stored as
`report_markdown`.


## Local acceptance tests

The [three-case acceptance test](../../testing/delivery-report-three-cases.md)
runs the real deterministic orchestrator against PostgreSQL with fake forge,
tracker and email transports. It exercises one-click reopening and labelled
follow-up creation through the console API, and records the EE boundary for
member relevance.
