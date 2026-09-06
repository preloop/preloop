# Durable implementation feedback

The Automated Issue Implementation preset can keep a PR moving through review
and CI without leaving an agent container waiting. Each repair gets a new
FlowExecution, its own execution budgets and fresh credentials. The implementation
thread keeps the PR branch and exact native conversation across those turns.
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

In the console, edit a flow and enable **Continue implementation after PR review
or CI failure** under **PR review and CI follow-up**. Enter the reviewer's and
implementer's numeric GitHub or GitLab actor IDs, then choose limits for repair
turns, cumulative estimated cost in USD, lifetime, and feedback debounce. The
initial values match the policy defaults above. Saving an existing flow without
opting in leaves follow-up disabled. Other policy fields set through the API are
preserved when editing these controls. A compatible saved checkpoint is required
for native conversation resume; otherwise an eligible repair reports its cold
handoff explicitly. Turning on the option does not merge a PR.

For an existing PR, open the successful execution that published it and choose
**Set up PR follow-up**. This fetches a read-only preview of the exact PR, branch,
current head and recovery options. Confirm a fresh conversation explicitly if its
saved state cannot be resumed. If the head changes, review the refreshed preview
and confirm again. The action requires enabled flow feedback and deployment
support for saved execution state uploads; unavailable prerequisites are shown in
the preview. A failed network response triggers a status refresh, not an automatic
second adoption request.

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
ruleset workflow/code-scanning gates that require additional evidence fail closed
with a reason. Resolve the blocker or provide the required gate integration;
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

Restore begins in an empty session directory. Missing/expired state produces
`cold_handoff` with a reason, using current issue/PR evidence. Corrupt, mismatched,
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

## Deployment prerequisites

Enable checkpoint uploads only after deploying the transaction-resilience backend
and its EE companion. In particular, the artifact quota lock must use
`FOR NO KEY UPDATE` so checkpoint inserts do not recreate account foreign-key
contention. Apply database migrations before starting the updated API and flow
workers. Merely enabling feedback on a saved flow does not enable artifact upload.

The chart already supports shared `extraEnv` on the API, gateway, execution workers
and scheduler. Its defaults leave `FLOW_ARTIFACT_DIRECT_UPLOAD` disabled. The
optional [native checkpoint overlay](../../../helm/preloop/values-native-checkpoints.yaml)
enables it and retains both workspace and native artifacts for seven days. Copy
and review that file with your installation values; do not enable it merely by
setting an environment variable on the Helm client or CI job.

The example overlay caps compressed uploads at 16 MiB through
`WORKSPACE_SNAPSHOT_MAX_BYTES`, which applies to both artifact kinds. This is below
the chart's default 32 MiB ingress and console proxy body limit. Measure a
representative workspace and native-session archive locally before choosing this
limit: repository history and generated assets can exceed it. For example, a
44.5 MiB compressed checkpoint requires a larger application limit, such as
64 MiB (`67108864` bytes), with `gateway.proxy.bodySize: "80m"` to update both
ingress and console limits. Check for explicit ingress annotation overrides in
the installation values. Oversized archives
fail explicitly; increase application and every proxy limit together only after
checking memory and database capacity. Expanded archives retain their separate
`FLOW_ARTIFACT_EXPANDED_MAX_BYTES` limit (default 2 GiB), and
`FLOW_ARTIFACT_ACCOUNT_QUOTA_BYTES` limits stored account data (default 4 GiB).
Retention consumes that quota, so cleanup and database headroom must cover the
selected retention window. The checkpoint interval defaults to 300 seconds.
Upload buffering, encryption and database driver copies can require several times
the compressed archive size in memory. The expanded-size limit is validated with
streaming reads, but it still bounds restore disk usage and processing work.
Keep upload concurrency bounded during initial validation; increasing a size
limit alone does not establish sufficient memory or storage headroom.

Helm merges maps but **replaces lists**. Merge these entries with any existing
`extraEnv`, preserving database, private-CA and other installation entries. An
additional values file must not silently replace that list. Render the combined
values and verify that the same direct-upload flag reaches the API and execution
workers. Confirm the existing signing/encryption Secret references remain intact;
never print key values in CI logs.

All API and worker replicas must share a stable `SECRET_KEY`, used to sign and
verify scoped upload/download capabilities. Artifacts use the existing encryption
configuration: a stable `SECURITY__ENCRYPTION_KEY` when configured, otherwise the
existing encryption key derived from `SECRET_KEY`. Preserve the installation's
current mode and keys. Switching to a new dedicated key or rotating either key is
not part of enabling checkpoints and can make existing encrypted data unreadable.
If an existing dedicated key comes from a Kubernetes Secret, preserve its
`valueFrom.secretKeyRef` entry in the combined values. A non-empty key check only
proves presence, not continuity with previously stored data.

`PRELOOP_URL` must be reachable from the actual agent execution namespace or
private runner, including DNS, TLS trust and egress to its resolved destination.
The native client uploads directly to
`/api/v1/flows/executions/{execution_id}/artifacts`; MCP access alone does not prove
this route works. An internal ingress may need an explicit existing network-policy
rule for its namespace and HTTPS port. Do not widen agent egress merely to bypass
a failed readiness check. Check upload timeouts as well as size limits.

Use a local or staging acceptance run to verify an encrypted workspace and native
artifact row, then a fresh-container native resume for the same implementation
thread. The two-turn image smoke below proves the harness's selected-session
restore separately from the encrypted HTTP transport. Old executions without an
uploaded checkpoint cannot regain their lost native context by enabling this
setting; they require an explicit cold handoff. Disable future direct uploads by
removing this overlay or setting `FLOW_ARTIFACT_DIRECT_UPLOAD=false` consistently,
while preserving stored artifacts and keys for recovery.

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
