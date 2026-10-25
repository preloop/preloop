# Issue readiness and completion audits

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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
  "ready_label": "<existing-project-readiness-label>",
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
   environment and adds the explicitly configured existing `ready_label` once
   under project policy. No default label vocabulary is assumed.

Labels are added, not replaced: existing project labels retain their ownership.
The normal label webhook triggers implementation. A durable pickup record binds
that transition to one execution; repeated delivery reuses the same execution.
Follow-up issues never inherit the readiness label or bypass this gate.

When creating a fresh authorized pickup, the controller can attach a stored triage
assessment as `lifecycle_pickup.triage_context`. It loads the packet through
account-scoped records and checks its issue revision, current project policy and
source flow configuration. An applicable packet has status `available`; otherwise
the context is explicitly `unknown` with no packet. Incoming event fields cannot
supply this assessment. A reused execution retains its original context, even if
a newer assessment is later stored. Dispatch retries recheck pickup readiness;
they do not refresh the execution's frozen assessment or claim that its source
flow and catalogue were revalidated. The implementation preset treats the packet
as evidence to recheck against the current issue and checkout, never permission
to dispatch or change scope. The readiness policy and explicit approval above
remain required.

Pickup also checks the issue's current complexity labels against the packet's
recorded family. A changed, removed or conflicting classification makes the
packet inapplicable; unrelated human labels do not invalidate it. This check uses
the fresh issue snapshot without scanning the project's label catalogue.
Catalogue completeness in the packet describes the assessment-time catalogue;
pickup does not claim to have revalidated subsequent repository label definitions.

Triage records both the revision it assessed and the verified revision after its
managed issue update. Pickup uses that resulting full title/body revision. This
mapping permits reuse after a managed assessment or factual title improvement
without excluding human requirements from the readiness fingerprint.

The scope fingerprint covers title and description, excluding automation's
comments and labels. GitHub does not offer a conditional label-write API. The
provider rereads scope immediately before labeling, then the controller checks
again afterward and before dispatch. A racing scope edit is recorded as
`needs_reconciliation`; it cannot silently start a new implementation. If no
execution was created, refine the current revision and explicitly submit `/ready`
again. The controller rechecks scope and blockers and archives the superseded
authorization before replacing it. Stale revisions cannot authorize a pickup.

Once an execution exists, `/ready` returns `pickup_reconciliation_required` and
preserves that execution. Finish or cancel active work through the existing
execution controls; scope edits never retarget a running conversation. Refine
the revised scope, review the proposal, then explicitly call
`POST /pickup/reconcile` with `issue_revision`, `previous_execution_id`, and a
nonempty review `reason`. This requires both issue-edit and flow-execution
permissions. The named previous execution must be terminal, the revision must
be current and changed, and all readiness checks must pass again.

The controller archives the old authorization with its execution link and creates
a distinct authorization for the revised scope. It dispatches one fresh execution
directly because the readiness label may already be present. It never removes
and re-adds labels to manufacture another webhook. Repeating the same request or
receiving duplicate issue events reuses the same authorization/execution, including
a retry after dispatch failure. A missing worker acknowledgment returns durable
`dispatch_pending` plus the execution ID; retrying the same reconciliation request
retries delivery without authorizing a second execution. Old execution prompts and
contracts remain intact.
Same-scope execution retries use the existing execution retry API. The
`/audit/reconcile` endpoint separately recovers audits.

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

Before the audit runs, the controller collects the runtime evidence GitHub
already holds for the merge SHA and puts it in the envelope as
`runtime_evidence`: check runs with their conclusions, workflow-run artifacts,
and the `environment_url` of each successful deployment (a preview or staging
address). Criteria about build output, latency against a baseline, viewport,
layout or visual conformance can then be decided from a check result, a
downloaded artifact or a read-only preview load. The audit cites what it used by
id (`check:<id>`, `artifact:<id>`, `preview:<id>`) in a criterion's `runtime`
list. The controller drops ids it did not supply. A `complete` verdict needs an
observation plus either code and tests or supplied runtime evidence. A source
GitHub refuses (for example, a token without Actions read access) is listed
under `unavailable` and the audit still runs. When the controller downgrades a
verdict, it appends the reason to the criterion.
Collection has its own 3-second deadline, and a source that does not answer
in time is listed as `timed out`. A preview URL that is not public http(s)
(loopback, private, link-local or metadata addresses, other schemes) is refused
before it reaches the prompt. The bundle is a snapshot taken when the audit is
scheduled. Checks, artifacts or deployments that finish later are not in it, and
`/audit/reconcile` reuses it. The Delivery Report applies the same rule when it
reads a stored audit result, so it never counts a citation the controller
dropped.

Each published audit also appends one assessment through the CRUD layer
(`source="audit"`, pinned to the merge SHA or the deployed revision). The
assessment holds the criteria snapshot, the per-criterion evidence and the
runtime bundle. Every criterion complete maps to `covered`, some complete to
`partial`, none to `unverified`, and an empty contract to `no_criteria`.

The controller upserts one marked audit comment, including issue and merge
revisions, contributing PRs, evidence, and the execution link. With
`create_follow_ups: true`, it searches existing issues and creates at most three
bounded confirmed defects/enhancements. Existing marked issues, or human-created
issues referencing the source issue and the same acceptance criterion, are reused.
Unknown findings remain in the audit. The original issue is never reopened.

HTTP handlers and lifecycle hooks run each complete operation in a worker,
including synchronous CRUD calls and lazy ORM reads after commit. Provider I/O
uses that worker's private loop. Flow dispatch returns to the persistent
application loop, so locally dispatched background executions survive the
request or hook finishing.

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


### Bounded audit publication

Audit effects remain serialized per issue because marker lookup followed by
provider creation is not atomic. Marker scans stop as soon as a matching comment
or follow-up is found. Complete scans accept up to 10,000 results, with one extra
page used only to prove exhaustion. Larger responses fail closed before creation.
Publication has an eight-second provider-work deadline and a shared 128-request
budget, including lookups and mutations. Exhaustion rolls back the local operation
and leaves its persisted result available through `/audit/reconcile`; a prior
provider effect is recovered by marker on retry. The controller does not create a
new ticket when it cannot finish the deduplication lookup. Large repositories may
still require a later indexed or independently claimed publication path.
