# Issue Triage Assistant preset

Triage improves the issue itself and applies its complexity tag. It records
remaining scope, acceptance, evidence, risks and missing decisions in a replaceable
section of the issue body. A developer can pick up the issue without reading the
flow execution output.

The preset ships as `backend/presets/001-issue-triage-assistant.yaml`
(slug `issue-triage-assistant`) and runs for `issue_opened` and `issue_updated`.

## How it updates an issue

1. `get_issue` with `include: ["label_catalog", "revision"]` reads fresh provider
   content and a complete, bounded label catalogue for the authorized project. It
   supplies an expected revision and the permitted complexity scheme.
2. The agent reconciles linked PRs and available source evidence with the original
   acceptance. It assesses description quality, readiness, complexity and risk
   independently, and selects an exact label from the returned scheme.
3. `update_issue` with `expected_revision`, `assessment` and `complexity_label`
   writes the assessment and applies that complexity label. It preserves human text
   outside the managed section and changes labels through provider deltas, removing
   only obsolete siblings in the selected family.
4. The tool reads the provider state back and synchronizes the observed issue
   through CRUD. Its receipt identifies completed operations, conflicts and
   partial failures. A successful controlled run also persists a versioned
   assessment packet in the issue lifecycle ledger. `result.json` records the
   diagnostic receipt; it cannot supply or authorize a persisted packet.

Existing complexity schemes take precedence. Supported vocabulary includes explicit
complexity, effort, size or difficulty families and unambiguous standalone schemes.
If no scheme exists, the service establishes `complexity:low`, `complexity:medium`
and `complexity:high`. An ambiguous or truncated catalogue is not evidence that no
scheme exists. If complexity cannot be estimated, the issue can still receive its
assessment with the missing information, but the run reports the absent tag as
incomplete. It never invents an estimate to fill a field.

Triage has no tools of its own. It is an option on the standard `get_issue` and
`update_issue` tools, so an agent that already has them needs no extra unlock, and
no agent receives a separate triage schema. Passing no `include` leaves `get_issue`
on its existing snapshot-only path, and omitting `expected_revision` and
`assessment` leaves `update_issue` on its existing metadata path. A triage write
must not be mixed with `description`, `status`, `priority`, `assignee`, `labels` or
reactions; such a call is rejected rather than half applied.

The triage write requires the existing `edit_issues` permission and follows normal
MCP availability and approval policies. Account, project, tracker and execution
identity come from authenticated stored records. An execution created for triage
can write only its bound issue, using the assessed revision and unchanged project
policy and flow configuration. Broad `update_issue` metadata writes are rejected
for its credential, even when the MCP request context is absent. Mutating REST
routes also reject triage runtime credentials, so the same key cannot create
follow-up issues, change dispatch labels or launch an implementation through a
second API. Ordinary human, implementer and reviewer credentials retain their
existing behavior. Renaming a flow or explicitly retrying its failed run does not
remove the original execution credential's restrictions.

The first provider adapters support GitHub and GitLab. Other providers report an
unsupported operation before reserving an execution rather than claiming an update.
Triage requires an execution-scoped agent credential. Persistent agent execution
is rejected because its existing agent credential does not provide that scope.

The service checks the issue baseline before mutations and verifies the final
state. These are optimistic checks, not atomic provider compare-and-swap. A stale
baseline requires a new revision assessment. The controller serializes local
claims and applies using the existing tenant/issue advisory-lock key. A dedicated
connection holds the lock across durable intent commits, so a lost provider
response or process failure cannot erase the suppression receipt. External human
edits between the provider read and write remain a documented limitation; the
controller does not claim atomic provider compare-and-swap.

Automatic triage suppresses matching self-generated updates using server-written
receipts, expected edit fields and provider snapshots. Final receipts also match
the observed provider update time; pending write expectations expire. Suppression
keys on the receipt, not on which tools a flow selected, so an event that carries no
trusted receipt stays eligible for every flow. Marker text alone does not suppress
an event. Manual runs and later human edits reach the same durable revision
controller. Different revisions can have separate executions; an older execution
cannot apply its assessment over a newer observed revision.

Automatic triage also skips an issue update whose provider change set touches
neither the title nor the description. GitLab reports an assignee, milestone or
due-date edit as a plain issue update, and triage has nothing new to read in one.
The check needs a provider change set: a delivery without one (Jira, a replayed
payload, a manual run) still passes this relevance filter. Only flows created
from this preset are held
back, so a GitLab assignment still reaches other flows that subscribe to issue
updates. A flow counts as one of those when it records this preset as its
source, or, for flows created before that link existed, when its name is exactly
the preset's name. A hand-built flow renamed to `Issue Triage Assistant`
therefore inherits the same skip; give such a flow a different name if it must
see every update.

## Manual runs

Use **Run triage** on an issue, or select up to 25 issues on the tracker
issue list and choose **Run triage on selected**. Both call
`POST /api/v1/flows/run-preset` with slug `issue-triage-assistant`.

- Single run: `{ "preset_slug": "issue-triage-assistant", "target": { "kind": "issue", "issue_id": "..." } }`
- Batch: `{ "preset_slug": "issue-triage-assistant", "targets": [ ... ] }` (1–25 issues, duplicate ids dropped, ownership checked before launch, per-item errors)

`confirm_create` is unchanged: a probe does not start a run. Production
runs use `test_mode=false`. Implementer and reviewer run-preset behavior is
unchanged; batch `targets` is triage-only.

Unsupported tracker targets report a per-item error. Triage never invents a
repository, clone URL, default branch, or author from an assignee.

Batch results report each issue separately, each with its issue key so a
25-row selection says which issue needs attention. If dispatch fails after an
execution was created, its ID, status and link remain in the response with a
warning. The console shows these warnings and run links; inspect an existing run
before retrying. Other valid issues in the batch continue.

Manual single/batch actions and automatic issue events reserve one execution for
the same current issue revision and effective context. The controller fetches
fresh provider state under the issue lock; a browser or webhook cannot provide
an authoritative revision. The identity includes the saved flow configuration,
project/provider scope and existing `project.settings.issue_lifecycle` policy.
Changes to the selected complexity family also require a fresh assessment.

Repeated requests return `coalesced: true`, the original execution ID, its current
status and its link. Successful completed assessments are replayed without
starting another agent. The controller maps its verified output revision back to
the original assessment, so its own body/tag changes do not create a new run.
New human content reaches a new claim even while an older execution is active.
If the durable lookup fails, triage reports an error instead of starting an
untracked run. Implementer and reviewer actions retain their existing best-effort
active-run reuse.

An unacknowledged dispatch retains its committed `PENDING` execution; retrying
reuses that execution and the existing worker delivery/claim path. Running or
completed executions are not dispatched again. A normal repeated request also
returns a terminal failure so callers can inspect it. An explicit execution retry
can replace a failed, cancelled, stopped, timed-out or aborted attempt under the
same lock, preserving its lineage and archived credential binding. Successful
assessments remain immutable replays.

Triage supports issue single/batch actions and issue events. Matrix and delegated
child runs are rejected because a shared revision claim cannot be reassigned to
another execution tree.

## Recovery and stored context

Before each provider effect, the controller commits its expected provider states
and the bounded original assessment request. If a response is lost after a body
or label update, it can complete that exact request when the current provider
state matches the recorded intent. A recovery cannot replace the assessment or
classification with different text. Explicit retry executions receive the stored
recovery request. If human edits or project policy changed, recovery refuses the
old claim; a fresh request assesses the new context. Partial writes, unavailable
complexity or failed local synchronization never produce an applicable packet.

A successful controlled apply stores a version-1 packet of at most 128 KiB with
the assessment (at most 16,000 characters), selected label and complexity family,
account/project/issue/flow/execution identities, policy/context fingerprints and
both assessed and resulting revisions. The verified result has separate provider,
triage-scope and full-body readiness revisions. Human text outside the managed
section and unrelated labels remain intact. The controller explicitly marks
repository/source and PR coverage as unknown; an agent's prose does not turn those
fields into authenticated coverage evidence.

The existing readiness controller loads this packet from server persistence only
when its resulting full-body revision, current complexity family, account/project
and effective flow/policy context still match. A disabled flow or inapplicable
packet produces explicit unknown context. Caller-supplied packets are discarded.
Readiness still requires its existing reviewed contract, approved environment and
policy-authorized transition before dispatch; the assessment neither creates
that contract nor grants implementation authority. No second scheduler or new
automatic dispatch policy is introduced. Ad hoc human tool calls retain scoped,
serialized issue updates, but do not mint an execution-bound reusable packet.

## Diagnostic result

Success requires an apply receipt of `updated` or `unchanged`, the confirmed
complexity tag and successful local synchronization. An assessment only in output
is not successful triage. Conflicts, missing complexity and partial failures use
`status: error` with a reason and recovery information.

The packet includes `issue_updated`, `applied_complexity_label`, and `application`
with the actual status, operations and cache outcome. An unchanged issue sets
`issue_updated: false`. Existing diagnostic assessment fields remain available:

| Field | Values or content |
| --- | --- |
| `assessment.complexity` | `unknown`, `small`, `medium`, `large`; separate from the provider label name |
| `assessment.readiness` | `unknown`, `blocked`, `ready_for_human_review` |
| `assessment.description_quality` | `unknown`, `clear`, `needs_improvement`, with reasons |
| `assessment.implementation_readiness` | `unknown`, `ready`, `needs_spec`, `blocked`, `in_progress`, `needs_verification`, with reasons |
| `assessment.risk` | `unknown`, `low`, `medium`, `high`, with reasons |
| `assessment.complexity_scope` | `remaining_change`, `historical_umbrella`, `unknown` |
| `evidence_baseline` | Observed issue update time, checkout revision, related work and evidence limits |

These diagnostic fields do not replace the assessment on the issue. Small means
low complexity and large means high, but the applied name follows the project's
scheme. A merged PR proves code landed; it does not prove all acceptance or
deployment conditions passed. An acceptance-only tracker needs verification even
when updating or closing it would take little effort.

The preset has no checkout by default. Its tools can read explicitly linked PRs,
but do not enumerate every project PR. Missing source or provider evidence stays
explicit; triage does not fabricate code pointers, test commands or remaining
implementation. Operators can provide scoped source evidence for their projects.
See the [Preloop repository policy](issue-readiness-policy.md) for the complexity,
readiness and risk labels used in this repository. End users choose any subsequent
implementation flow and model; neither preset recommends them.

## Implementation freshness and publication

Preset 011 refreshes the original issue and uses the controller-bound PR during
continuations. It reconciles current source, acceptance and linked work, preserves
unpushed work on divergence, and implements only remaining in-scope behavior.
Already-satisfied, overlapping or blocked work gets an honest failure report
without a manufactured commit.

Useful commits from a failed implementation remain reviewable. In the inline
publication path, a failed result report does not veto publication: a pushed
branch receives its configured PR/MR with the failure reason and execution link.
Partial work references the issue instead of automatically claiming to close it.
Publishing a PR does not turn a failed execution into a successful one. The
separate configured verifier still controls pre-push checks. This metadata path
does not recover every CLI crash or change isolated publication authorization.

Changing preset files does not synchronize saved flows. The normal synchronization
path can update uncustomized fields on linked flows; customized flows retain their
update-review path. These changes do not enable an automatic implementation handoff.
