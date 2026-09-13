# Issue Triage Assistant preset

Triage improves the issue itself and applies its complexity tag. It records
remaining scope, acceptance, evidence, risks and missing decisions in a replaceable
section of the issue body. A developer can pick up the issue without reading the
flow execution output.

The preset ships as `backend/presets/001-issue-triage-assistant.yaml`
(slug `issue-triage-assistant`) and runs for `issue_opened` and `issue_updated`.

## How it updates an issue

1. `get_issue_triage_context` reads fresh provider content and a complete, bounded
   label catalogue for the authorized project. It supplies an expected revision
   and the permitted complexity scheme.
2. The agent reconciles linked PRs and available source evidence with the original
   acceptance. It assesses description quality, readiness, complexity and risk
   independently, and selects an exact label from the returned scheme.
3. `apply_issue_triage` writes the assessment and applies that complexity label.
   It preserves human text outside the managed section and changes labels through
   provider deltas, removing only obsolete siblings in the selected family.
4. The tool reads the provider state back and synchronizes the observed issue
   through CRUD. Its receipt identifies completed operations, conflicts and
   partial failures. `result.json` records that diagnostic receipt.

Existing complexity schemes take precedence. Supported vocabulary includes explicit
complexity, effort, size or difficulty families and unambiguous standalone schemes.
If no scheme exists, the service establishes `complexity:low`, `complexity:medium`
and `complexity:high`. An ambiguous or truncated catalogue is not evidence that no
scheme exists. If complexity cannot be estimated, the issue can still receive its
assessment with the missing information, but the run reports the absent tag as
incomplete. It never invents an estimate to fill a field.

The write tool requires the existing `edit_issues` permission and follows normal
MCP availability and approval policies. Project and tracker identity come from
account-scoped stored records. The preset does not use broad issue mutation tools,
change assignees or dispatch labels, create follow-up issues, or start implementation.
The first provider adapters support GitHub and GitLab. Other providers report an
unsupported operation rather than claiming an update.

The service checks the issue baseline before mutations and verifies the final
state. These are optimistic checks, not atomic provider compare-and-swap. A stale
baseline requires fresh context and re-evaluation. Partial failures retain the
observed provider state and operation receipts; retrying must account for writes
that already succeeded. Human edits during the provider read/write window remain
a documented limitation.

Automatic triage suppresses matching self-generated updates using server-written
receipts, expected edit fields and provider snapshots. Final receipts also match
the observed provider update time; pending write expectations expire. Marker text
alone does not suppress an event. Manual runs and unrelated flows remain eligible,
as do assignment, reopening and later human edits. Rapid human edits can still
enqueue multiple runs; this is not durable per-revision coalescing.

## Manual runs

Use **Run triage** on an issue, or select up to 25 issues on the tracker
issue list and choose **Run triage on selected**. Both call
`POST /api/v1/flows/run-preset` with slug `issue-triage-assistant`.

- Single run: `{ "preset_slug": "issue-triage-assistant", "target": { "kind": "issue", "issue_id": "..." } }`
- Batch: `{ "preset_slug": "issue-triage-assistant", "targets": [ ... ] }` (1–25 issues, duplicate ids dropped, ownership checked before launch, per-item errors)

`confirm_create` is unchanged: a probe does not start a run. Production
runs use `test_mode=false`. Implementer and reviewer run-preset behavior is
unchanged; batch `targets` is triage-only.

For non-Git trackers, the packet keeps the issue key and known URL. It does
not invent a repository, clone URL, default branch, or author from an assignee.

Batch results report each issue separately. If dispatch fails after an execution
was created, its ID, status and link remain in the response with a warning. The
console shows these warnings and run links; inspect an existing run before
retrying. Other valid issues in the batch continue.

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
