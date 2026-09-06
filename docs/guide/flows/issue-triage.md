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
| Read the issue and nearby project context | agent (`get_issue`, `search_issues`) |
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

- `assessment`: kind, complexity with rationale/confidence, missing context, acceptance, code/test pointers, dependencies, readiness
- `observed_labels` / `proposed_labels` (existing names only) / `new_label_proposals`
- `policy_notes`: whether project policy was found; never invent labels

## Not in this slice

- Atomic managed-label apply
- Durable per-revision coalescing (rapid human edits can still enqueue more than one run)
- Model/harness routing
- Automatic implementation handoff or an `agent-ready` default
