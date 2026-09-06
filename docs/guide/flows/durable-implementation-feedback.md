# Durable implementation feedback

The Automated Issue Implementation preset can keep a PR moving through review
and CI without leaving an agent container waiting. Each repair gets a new
FlowExecution, its own execution budgets and fresh credentials. The implementation
thread keeps the PR branch and, when recovery files are available, the exact native
conversation across those turns.
Reviewers remain separate flows and conversations. Merging remains a human action.

## Enable a subscription

New copies of preset 011 enable `agent_config.feedback`. Existing saved flows
keep their configuration until the operator opts in or deliberately applies a
preset update. The issue's `agent-ready` label is an intake condition; feedback
is routed by the recorded account, flow, tracker, numeric provider repository
identity and PR number, without requiring that label on the PR.

```yaml
agent_config:
  feedback:
    enabled: true
    debounce_seconds: 30
    max_turns: 5
    max_cost: 100
    max_no_progress: 2
    max_age_hours: 168
    ci_deadline_seconds: 3600
    repair_early: false
    # Explicit provider actor IDs, not names or comment markers.
    trusted_reviewer_ids: [12345]
    implementer_actor_ids: [67890]
    # Optional project policy; provider rules can add required gates.
    required_checks: ["Backend Tests", "UI Tests"]
    required_approvals: 1
```

Enabling feedback applies to future executions. The server records opt-in when
an execution starts; turning the setting on does not discover and repair an old
PR backlog. An older successful publication requires explicit adoption below.
Turning feedback off pauses future repairs without cancelling a running turn.
Re-enabling keeps consumed turns, cost, no-progress history and the deadline.
Current reviewer trust and budget settings apply on every reconciliation and are
checked again when reserving a repair. Reducing the maximum age can shorten the
original deadline; increasing it never extends an existing subscription.

Use the actual reviewer integration's actor ID in `trusted_reviewer_ids`.
Unlisted bots and the configured implementer actor are ignored. A copied HTML
review marker never grants trust. All comment and CI text is untrusted task data.
Cost is cumulative estimated execution cost in USD, with existing execution
budgets enforced independently. No-progress detection compares the PR head
before and after a repair. The execution result's `continuation` object shows
thread state, consumed turns/cost, pending feedback, head and stop reason.

Publication registers an internal subscription using existing repository
webhooks. No webhook is installed for an individual PR. Registration recovery
and periodic provider reconciliation cover feedback that races with publication
or whose webhook was lost. The sync scheduler publishes `reconcile_flow_feedback`
every 15 seconds; workers reconcile bounded batches. Default feedback debounce
is 30 seconds. Duplicate deliveries and check/workflow notifications do not
create duplicate execution turns.

A PostgreSQL row lease protects each thread. Creating the next PENDING execution
and assigning its feedback receipts is one transaction. If dispatch fails or a
worker crashes, normal execution recovery dispatches the same execution ID.
Feedback that arrives during execution stays pending. The agent runner exits
between turns; CI waiting and stuck-job deadlines belong to the scheduler.

## Provider gates

GitHub reconciliation reads the current PR head, checks, legacy commit statuses,
submitted reviews, unresolved inline review threads and conversation comments.
It incorporates configured required checks, branch protection and effective
ruleset check/review requirements. GitLab reconciliation reads MR notes,
commit/pipeline/job statuses, approvals and blocking discussion state. Both paths
recheck the head after reading gates and stop repairing closed or merged PRs.

Only current-head check failures trigger repairs. Pending or missing required
checks wait until the CI deadline, then report an explicit blocked state.
Current cancellations, startup failures, permission requirements and unknown
outcomes block readiness instead of inviting speculative code edits. Neutral,
skipped and allowed-failure outcomes follow provider semantics. A failed required
check never becomes ready simply because a webhook was missing.

Readiness requires passing checks and review gates on the current head. Provider
permission errors, pagination beyond the bounded reconciliation window, and
ruleset workflow/code-scanning gates that require additional evidence prevent
readiness with a reason. Explicit permission denials on GitHub branch protection
or ruleset discovery still allow repairs to fully read, current-head review and
CI feedback. They never establish that repository requirements passed. Other
provider failures, rate limits or incomplete feedback block repairs as well. Resolve the blocker or provide the required gate integration;
Preloop does not interpret unavailable evidence as approval. The PR remains open
for manual merge.

## Native conversation checkpoints

Native session data is separate from workspace recovery. A versioned manifest
names the harness/version, explicit session ID, implementation thread, file
hashes, size limit and expiry. Codex checkpoints contain the selected rollout and
verified child rollouts. OpenCode 1.2.6 and 1.18.29 use SQLite: a consistent read transaction
exports only that session graph into a new database, without unrelated sessions,
account credentials, share secrets or persisted permission grants. A database
copy followed by deletion is insufficient because unused SQLite pages may retain
foreign data.

The native artifact uses the encrypted artifact service and scoped direct HTTP
capabilities. It is never sent through logs, generic trigger JSON, MCP tools or a
shared account volume. The resolver authorizes account, flow, thread, reserved
current execution and latest prior execution before issuing restore capability.
Providing another issue's execution ID or artifact reference does not authorize
its session. Private runners must advertise native checkpoint support; a runner
without it reports a cold handoff and does not upload its home directory.

Restore begins in an empty session directory. Missing or expired recovery files
stop a durable native resume. Only an explicitly adopted original publication may
start a fresh conversation on its published branch and report `cold_handoff`.
That exception is consumed by the first repair: later repairs need their own
workspace and native checkpoints. Corrupt, mismatched,
unsupported or incompatible existing state produces `resume_failed`. A failed
native CLI resume preserves the checkpoint and does not silently select another
conversation. The execution result records `native_resume`, `cold_handoff` or
`resume_failed`. Native manifests default to seven days; the artifact service's
native retention must cover that window independently of workspace retention.

Codex's CLI is pinned to npm release `0.153.4`, tested against the shipped universal
image. OpenCode is pinned to `1.18.29`, with `agent_config.opencode_cli_version`
as its exact-version override. Its current SQLite event/context tables are scoped
to the selected conversation; account, credential and share tables stay empty.
`agent_config.codex_cli_version` accepts an exact release version for an
intentional upgrade. Upgrade tests must repeat the two-turn image smoke. A
checkpoint from another CLI version is rejected explicitly. OpenCode's image
version and storage schema are validated through the native manifest. Affinity
and completion reminders always use explicit IDs, never latest-session flags.
Wrappers install the selected CLI before one native restore, enter the primary
checkout after setup, and log the actual CLI version and configured image reference.
A configured image tag is not proof of the resolved runtime image digest.

## Adopt one existing publication

First enable feedback on the flow and direct artifact uploads on the API and
execution workers (`FLOW_ARTIFACT_DIRECT_UPLOAD=true`). The worker must reach
`PRELOOP_URL` and share the existing encryption configuration. Retain workspace
and native artifacts for the desired repair window. A native session ID alone is
not sufficient to resume a conversation.

Use the execution detail action, or preview the original successful publishing
execution through `GET /api/v1/flows/executions/{execution_id}/continuation`.
The preview verifies account ownership, the tracker and current PR repository,
branch, URL and head. It also exercises review, CI and repository gate reads with
at most twelve provider requests and a 25-second deadline. It writes no thread
and dispatches no execution. `feedback_readable=false` means feedback read permissions
or provider availability must be fixed before adoption. A permission denial
limited to repository requirement discovery permits adoption and bounded repairs,
with a warning that readiness remains unverified. Other gate blockers
remain visible in `feedback_blocked_reason` and the warnings.

Submit the returned head using
`POST /api/v1/flows/executions/{execution_id}/continuation`:

```json
{
  "recovery_mode": "published_branch_handoff",
  "expected_head_sha": "<head_sha from the preview>",
  "acknowledge_fresh_conversation": true
}
```

Use `native_resume` when it appears in `allowed_recovery_modes`; fresh-conversation
acknowledgement is then unnecessary. `published_branch_handoff` explicitly gives
up unavailable unpublished workspace and native conversation state and starts
from the verified published PR branch. The controller binds that permission to
the selected source execution and its reserved first repair. Trigger payloads
cannot grant the exception. Subsequent turns must restore their own checkpoints.

A changed head, closed PR, disabled flow, missing checkpoint capability or
unreadable provider returns HTTP 409, requiring a new preview. Repeated adoption
returns the existing thread without resetting its counters, deadline or terminal
state. Adoption starts the bounded subscription; it does not immediately create
an agent run. The scheduler first reconciles current trusted feedback and gates.

## Local validation

Unit fixtures cover archive identity, traversal, symlinks, credential isolation,
expiry, scheduler policy and provider outcomes. Set
`FLOW_FEEDBACK_TEST_DATABASE_URL` to a disposable PostgreSQL database for the
lease, crash and concurrent-worker integration tests. The suite never substitutes
the application database for this fixture.

`NATIVE_SESSION_IMAGE_SMOKE=1` enables immutable-image Codex/OpenCode tests with a
local deterministic model HTTP fixture. The first container seeds a fact; a
second container receives only the selected native session, resumes its explicit
ID and sends the remembered fact to the fixture. No provider credentials are
needed. Set `PRELOOP_DISABLE_TELEMETRY=true` for all tests.
