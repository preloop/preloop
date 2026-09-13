# Issue Triage Assistant preset

Assesses a new or updated issue, reuses labels that already exist on the
project, and posts a compact triage comment. This first slice is
**proposals only**: it does not rewrite the issue, create follow-up issues,
apply labels, or start implementation.

The preset ships as `backend/presets/001-issue-triage-assistant.yaml`
(slug `issue-triage-assistant`).

## What it does

| Step | Owner |
| --- | --- |
| Match `issue_opened` and `issue_updated` (legacy `issue.opened` clones still match) | flow trigger |
| Ignore Preloop-bot `issue_updated` loops; human title/body edits still run | flow trigger |
| Refresh the issue, inspect linked PRs and nearby project context | agent (`get_issue`, `get_pull_request`, `search_issues`) |
| Propose existing labels; leave missing taxonomy unknown | agent |
| Post one comment marked `<!-- preloop-triage -->` | agent (`add_comment`) |
| Write `/workspace/result.json` | agent |

The agent has no `create_issue` or `update_issue` tool. GitHub's issue
update replaces the full label set, so this slice never applies labels from
the prompt.

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

## Result packet

`result.json` keeps a bounded assessment separate from provider label names:

- `assessment`: existing kind, complexity with rationale/confidence, missing context,
  acceptance, code/test pointers, dependencies and human-review readiness.
- Additive assessment objects: `description_quality`, `implementation_readiness`,
  and `risk`, each with a `value` and `rationale`.
  `complexity_scope` identifies remaining work or a historical umbrella.
- `evidence_baseline`: observed `issue_updated_at`, `checkout_revision`,
  `related_work` and `evidence_limits`. Missing revisions stay null; a PR entry
  records its observed URL/state/revision and the acceptance it covers.
- `observed_labels` / `proposed_labels` (existing names only) / `new_label_proposals`
- `policy_notes`: whether project policy was found; never invent labels

The original `complexity` vocabulary remains `unknown | small | medium | large`.
Small maps to low complexity, large to high. The original `readiness` field remains
`unknown | blocked | ready_for_human_review`; it is not a dispatch signal.
The new fields use these advisory values:

| Field | Values |
| --- | --- |
| `description_quality.value` | `unknown`, `clear`, `needs_improvement` |
| `implementation_readiness.value` | `unknown`, `ready`, `needs_spec`, `blocked`, `in_progress`, `needs_verification` |
| `risk.value` | `unknown`, `low`, `medium`, `high` |
| `complexity_scope` | `remaining_change`, `historical_umbrella`, `unknown` |

These fields do not change the flow schema. Unsupported assessments stay unknown
with an explanation of the missing evidence. New label proposals remain empty.
Existing consumers can continue reading the original fields.

## Evidence and implementation readiness

A clear issue can be complex, risky, already implemented or under active review.
Triage reconciles linked PR state and current source with remaining acceptance
before recommending pickup. A merged PR proves code landed; it does not prove
all acceptance or deployment conditions passed. An acceptance-only tracker needs
verification, even when updating or closing the issue would take little effort.

The generic preset has no checkout by default. Its tools do not enumerate every
project PR or the label catalogue. When those limits prevent confirming remaining
work or overlap, it records the limitation and leaves unsupported assessments unknown.
It does not fabricate code pointers, tests or a missing implementation. Operators
can provide a checkout and scoped evidence appropriate to their own projects.

Readiness describes whether the remaining work is specified and testable.
Complexity describes the implementation effort and interactions; risk describes
the consequence of a wrong change. A missing design, dependency, source baseline
or validation path is recorded directly in the readiness assessment. Good prose
or low complexity alone does not establish readiness. See the
[Preloop repository policy](issue-readiness-policy.md) for one project-specific
mapping of complexity, readiness and risk to labels. End users decide which
implementation flow and model, if any, to use; the presets do not recommend them.

## Implementation freshness

Preset 011 refreshes the original issue even when its trigger packet looks
complete. A continuation uses the controller-bound PR and original criteria,
checks current source and linked PRs, and preserves unpushed work on divergence.
It implements only remaining in-scope behavior, with targeted tests, while retaining
the trusted verifier's required checks. Unknown scope expansion takes the existing
critical-decision path instead of becoming speculative work.

If there is no actionable implementation, active overlapping work or an unresolved
blocker, the agent reports the existing `status: failure` completion outcome with
an advisory `reason_code` (`already_satisfied`, `overlapping_work`, `blocked`, or
`scope_expansion`). It does not manufacture an empty commit or success PR metadata.
The report also adds `evidence_baseline` with observed issue/repository revisions,
related work and remaining acceptance. These are report fields, not new execution
states. The shared legacy publication path also refuses explicit failed or invalid
result artifacts after preserving recovery data, even when the CLI exits zero.
Missing artifacts retain legacy compatibility; a successful report still cannot
bypass the trusted verification gate. A satisfied issue is not automatically closed. A partial implementation
uses a reference to the issue rather than a closing directive.

Changing preset files alone does not deploy or synchronize saved flows. The normal
preset synchronization can propagate uncustomized fields to linked flows; customized
flows retain the update-review path and notifications. Review that behavior before
running synchronization. These changes do not enable isolated publication mode or
automatically assign an issue to an implementation flow.

## Not in this slice

- Atomic managed-label apply
- Durable per-revision coalescing (rapid human edits can still enqueue more than one run)
- Model/harness routing
- Automatic implementation handoff or an `agent-ready` default
