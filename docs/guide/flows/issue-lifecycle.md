# Issue readiness and completion audits

The lifecycle controller connects triage output, authorized implementation pickup,
and an independent audit after a merged PR closes an issue. GitHub is the first
provider adapter. Other providers fail explicitly until an authoritative merge
linkage adapter is installed.

## Configure a project

Clone **Issue Refinement and Readiness** and **Merged Issue Completion Audit**
from the preset catalog. Scope each flow to its tracker/project. The refinement
flow can follow normal triage; its structured result is a proposal, not permission
to implement. The audit flow subscribes to `issue.closed`.

Configure `project.settings.issue_lifecycle` through the existing project settings
API, using the account's actual flow IDs:

```json
{
  "ready_enabled": true,
  "implementation_flow_id": "<implementation-flow-uuid>",
  "audit_flow_id": "<audit-flow-uuid>",
  "create_follow_ups": false
}
```

The implementation flow must select an operator-approved environment profile
with named `test_commands`. Readiness consumes the environment capability
service; it does not define image setup or replace the pre-push verification
gate. Runtime setup still performs its own preflight before running an agent.
Missing profiles, unsupported runner protocols and missing commands block pickup.

The audit flow requires exactly one repository in `git_clone_config.repositories`,
with the scoped `project_id`, cloning enabled and `create_pull_request: false`.
Its MCP tool/server lists must be empty. It starts a fresh conversation, does not
restore an implementation session, and the verified merge SHA is carried in
`payload.sha` for the existing runner checkout path. Configure its environment
profile with the relevant checks and dependency services for the application.

## Refine and authorize

All endpoints below require tenant authentication and existing issue/flow
permissions. Paths are relative to `/api/v1/issues/{issue_uuid}/lifecycle`.

1. `GET /` returns the live issue scope fingerprint and operation history.
2. `POST /refine` stores a `ReadinessContract`, either submitted directly or
   produced by the refinement flow. Include the live `issue_revision`, problem,
   user outcome, constraints, non-goals, ordinary assumptions, materially blocking
   decisions, conflicts, observable criteria with stable IDs, code/test entry
   points, dependency issue numbers, environment profile and command names.
3. Review blockers and the proposal. `POST /ready` with
   `{"issue_revision":"<fingerprint>"}` rechecks the issue, dependencies and
   environment and adds `agent-ready` once under project policy.

Labels are added, not replaced: existing project labels retain their ownership.
The normal label webhook triggers implementation. A durable pickup record binds
that transition to one execution; repeated delivery reuses the same execution.
Follow-up issues never inherit the readiness label or bypass this gate.

The scope fingerprint covers title and description, excluding automation's
comments and labels. GitHub does not offer a conditional label-write API. The
provider rereads scope immediately before labeling, then the controller checks
again afterward and before dispatch. A racing scope edit is recorded as
`needs_reconciliation`; it cannot silently start a new implementation. A major
edit during an existing implementation similarly requires explicit reconciliation
of that original pickup; resubmitting readiness does not launch another run.

## Audit the actual merge

A completed issue state alone is insufficient. The provider loads the latest
closure event, requires a closing commit, and verifies its association with a
merged PR in the same repository. Manual closure without that evidence creates
no audit. Multiple linked merged PRs remain in the evidence envelope; the final
closing merge revision is the checkout and idempotency key.

The independent result maps every stored acceptance criterion to code, test
results and observations. The controller computes `complete`, `gap` or `unknown`.
Missing evidence, unchecked deployment requirements and subsequent issue scope
changes cannot produce a complete verdict. An issue with no stored acceptance
contract remains unknown rather than inventing criteria.

The controller upserts one marked audit comment, including issue and merge
revisions, contributing PRs, evidence, and the execution link. With
`create_follow_ups: true`, it searches existing issues and creates at most three
bounded confirmed defects/enhancements. Existing marked issues, or human-created
issues referencing the source issue and the same acceptance criterion, are reused.
Unknown findings remain in the audit. The original issue is never reopened.

Per-issue PostgreSQL transaction locks serialize decisions, and unique operation
keys survive duplicate webhooks. Remote writes also use deterministic markers:
a retry after provider success and local rollback finds the original issue or
comment. `POST /audit/reconcile` retries pending dispatch or completed-result
publication after transient failures. Results are persisted before the terminal
hook runs, so reconciliation does not ask an agent to generate a second audit.

## Verify deployment separately

Closure cannot establish which revision runs in an environment. Deployment
criteria therefore produce a pending verification requirement and remain unknown.
An authenticated CD/operator caller can subsequently record the deployed revision
and trigger a separate, explicitly approved verification flow:

```json
{
  "deployment_targets": {
    "staging": {
      "flow_id": "<deployment-verification-flow-uuid>",
      "approved_scope": ["Read preferences for the synthetic staging account"]
    }
  }
}
```

Add this to the project lifecycle policy. Configure the dedicated flow with
`agent_config.lifecycle_kind: deployment_audit`, an environment and exact project
checkout, and a prompt that verifies only the supplied approved scope and emits
the same audit-result contract as the merge preset. Its trigger types should be
empty: the authorized endpoint is the trigger.

Call `POST /deployment/verify` with `merge_sha`, configured `target`, exact
`deployed_revision` and a `deployment_evidence` URL identifying the deployment
record. The controller stores the record/scope, creates a separate conversation,
pins checkout to the deployed revision and publishes a separate audit comment.
Repeating the same target/revisions reuses that execution. The deployment record
is an assertion by the authenticated CD/operator caller; it must come from the
actual deployment system. The issue-close flow never probes production.
