# Automated Issue Implementation preset

Turns a tracker issue into a working change. The agent reads the issue,
implements it, adds tests, runs the project's checks, and commits to the
checkout it was given. Preloop pushes the branch and opens the pull request
after the run.

The preset ships as `backend/presets/011-automated-issue-implementation.yaml`
(slug `automated-issue-implementation`).

## What the agent does and what the flow does

| Step | Owner |
| --- | --- |
| Decide which issues qualify | flow trigger (`trigger_config`) |
| Read the issue, plan, implement, test, lint | agent |
| Commit to the local checkout | agent |
| Write `/workspace/result.json` | agent |
| Verify the final commit against the trusted test profile | flow (runner-controlled verifier) |
| Push the branch and open the pull request | flow (`git_clone_config.create_pull_request`) |

The preset separates implementation from publication. Its MCP toolset is
`get_issue`, `get_pull_request`, `add_comment`, `update_comment`, `ask_user`.
Credential isolation additionally requires the opt-in isolated mode below.
There is no approval tool: gating belongs to your deployment's policies, not
to a preset prompt.

## The verification gate

Asking the agent to test is a request; the gate makes it a contract. The
preset ships with `git_clone_config.verification.mode: "gate"` and a
**trusted test profile**: a versioned list of required checks that travels in
the flow configuration, never inside the target repository, so the agent
cannot narrow required checks or edit the profile inside its own PR. After
the agent exits and commits, the flow re-runs the required checks itself and
only pushes the branch and opens the pull request when they pass on the
exact commit and tree being published.

The profile has three parts:

- `always`: inexpensive hooks required on every change. The shipped profile
  uses two universal git checks (`git diff --check` over the published
  range, and a scan for committed conflict markers).
- `rules`: changed-file patterns that pull in additional required checks
  (migration graph checks, frontend component tests, API contract checks,
  broader suites for shared interfaces). Each rule carries the reason it
  exists, and the reasons are recorded with the verification evidence.
- `unknown_default`: checks required for every changed path without a matching rule. It is never an
  empty list. The shipped profile refuses publication with a message that
  says what to configure: a runnable repository profile is required before
  strict verification is enabled on a live flow.

Customize the profile per repository:

```yaml
git_clone_config:
  verification:
    mode: gate
    gate_budget_seconds: 3600
    profile:
      version: v1
      profile_id: my-repo
      always:
        - id: git-diff-check
          command: 'git diff --check "$PRELOOP_VERIFY_BASE...$PRELOOP_VERIFY_HEAD"'
          reason: whitespace and conflict markers in the published range
          scope: shared
      rules:
        - id: backend
          description: Python changes run the focused backend suite
          path_globs: ["backend/**"]
          commands:
            - id: backend-tests
              command: pytest tests/backend -q
              reason: regression protection for changed code
              scope: backend
              timeout_seconds: 900
        - id: migration
          description: Database migrations must keep one head
          path_globs: ["**/alembic/versions/**"]
          commands:
            - id: alembic-heads
              command: alembic heads
              reason: a branching migration graph breaks upgrades
              scope: migration
      unknown_default:
        - id: fast-tests
          command: pytest -q -x
          reason: unknown impact uses the conservative default
          scope: unknown
```

Check commands run in the repository working tree with
`PRELOOP_DISABLE_TELEMETRY=true` and a `PRELOOP_VERIFY_BASE` /
`PRELOOP_VERIFY_HEAD` range contract, so a check can scope itself to the
published diff. Legacy sandbox checks inherit the agent's setup from
`git_clone_config.setup_commands`. Isolated checks start in fresh runtimes and
inherit neither that setup nor its writable caches. Each trusted command must
find its dependencies in the pinned image or perform bounded setup itself.
A database needed by a check must be available inside that check runtime;
external services are unreachable because verifier network access is denied.
A command that cannot start (exit 126/127), runtime failure or timeout is
`verification_blocked`. A check that runs and exits nonzero is
`verification_failed`; an operator profile can use an explicit prerequisite
check returning 127 to distinguish an unavailable database from test failure.
Both outcomes prevent publication and retain the commit and diagnostics.

What the agent sees and what the runner sees are two different report
fields. The agent's `result.json` `status` says what it *implemented*; the
legacy wrapper records a separate `verification` status from its gate. Its
execution result is labeled `source: sandbox_log`, `authenticated: false`:
log markers and sandbox files are observable diagnostics, not proof of origin.
Isolated publication requires independently authenticated controller or
runner-host verification before it can use these semantics to authorize a push. Anything the agent writes under
`verification` itself is kept as `verification_reported`: a claim, not
evidence. The full evidence (commands, exit codes, per-check logs,
environment digest, profile version, commit and tree) lands in
`/workspace/evidence/verification/` inside the evidence pack, and the
compact verdict is stored on the execution result.

A failed implementation report does not discard useful commits. On the ordinary
post-execution path, commits that pass the configured push gate receive their
configured PR/MR with the failure reason and an execution link. Already-pushed
branches, including resumed branches, receive the PR or an updated failure
notice without another push. Existing PR prose is preserved; the wrapper updates
only its own notice. Automatically added issue references use `Refs` for failed
or incomplete reports. The execution remains failed. This path requires the
agent CLI to reach its post-execution script; it does not change nonzero CLI crash
recovery or isolated controller publication authorization.

A denied publication fails the execution with the commits and evidence kept
recoverable (the workspace snapshot and the `verification_failed` /
`verification_blocked` failure categories tell you which kind of gap it
was). Rejected work goes back to the agent; missing dependencies are an
environment problem, visible as `verification_blocked`, and a runnable
setup (see `setup_commands` in the architecture docs) is how you repair it.

## Choosing which issues qualify

The prompt does not check labels. `trigger_config` does:

```yaml
trigger_event_types:
  - issue_labeled
  - comment_created
trigger_config:
  filter_conditions:
    labels: ["agent-ready"]
```

Every `issue_labeled` event whose labels intersect the list starts a
run. Matching reads label names from the webhook enrichment
(`extract_filter_fields`) and also unwraps GitHub `issue.labels[].name`
and GitLab `labels[].title`, so the example works on raw tracker payloads.
Without a filter, every labeling event qualifies, which is rarely what
you want on a busy repository.

## Resume on pull request comments

`comment_created` is only a trigger when the comment lands on a pull request
this flow opened. The trigger service correlates the comment to the earlier
execution and starts a new run with `{{execution.resume_from}}` set and the
checkout already on the pull request's branch. On that path the agent reads
the pull request with `get_pull_request`, addresses the review comments, and
commits again on the same branch. It never opens a second pull request.

Comments on unrelated issues or pull requests do not start a cold run.

### Durable review and CI continuation

New preset copies enable a durable PR subscription. Feedback uses the recorded
PR binding independently of intake labels; the scheduler coalesces review and CI
feedback into a new execution on the same branch. Existing saved flows opt in
explicitly. Native Codex/OpenCode continuation uses an authorized, isolated
checkpoint and explicit session ID. Missing state has a visible cold-handoff
reason; corrupt or mismatched state fails restore.

See [durable implementation feedback](durable-implementation-feedback.md) for
configuration, provider gates, retention, runner capabilities and local tests.

## Unclear issues

The agent asks a human only when a critical decision is missing and guessing
wrong would waste the run. It uses `ask_user`, which surfaces the question in
the console and in notifications. For anything smaller it picks the most
reasonable option and records the choice under `decisions` in
`/workspace/result.json` (and in its optional closing comment on the issue),
so the reviewer sees what was assumed rather than having to infer it.

## Result contract

```json
{
  "status": "success",
  "summary": "...",
  "changes": ["backend/preloop/foo.py: keeps the retry hint"],
  "tests": ["pytest tests/services: 212 passed"],
  "decisions": ["chose the additive migration, no data rewrite"],
  "skipped": ["full backend suite: needs Postgres, not available"],
  "proposed_checks": ["pytest tests/test_retry.py::test_wrapped_retry_after"],
  "commits": ["550ca71c preserve retry_after_seconds when re-wrapping"],
  "pr_title": "Preserve retry_after_seconds when re-wrapping",
  "pr_body": "Keeps the retry hint on the wrapped error.\n\nCloses #212"
}
```

`status` is `success` when a change was committed, even if some checks could
not run (those belong under `skipped`), and `failure` with a `reason` when the
issue could not be implemented. Checks that cannot run are reported, never
faked. `proposed_checks` is advisory: the agent can suggest checks the
profile should require, but only the profile (owned by the flow, not the
repository) decides what is required, and the gate re-runs those itself
against the final commit before anything is published.

The flow opens the pull request after the agent exits. When `pr_title` and
`pr_body` are present it uses those; otherwise it uses interpolated
`git_clone_config.pull_request_title` / `pull_request_description`, then a
flow-attribution fallback (execution link, and a `**Commits:**` list when
more than one commit landed). New branches are named
`preloop/issue-{number}-{execution[:8]}` when the trigger carries an issue
number.

## Timeout

The preset sets `timeout_seconds: 5400`. Reading an unfamiliar repository,
writing tests, and running a suite regularly outlives the 3600s default, and a
run killed at the finish line loses the commit.

### Inspect a saved publication policy

The flow detail page shows the API's read-only `effective_publication_policy`.
Existing customized flows remain unchanged: an absent or disabled verification
policy is explicitly **No verification gate**. A legacy gate is **Checks in
agent sandbox**, whose log evidence is not a trusted publication attestation.
An isolated policy with a valid profile and pinned image is **Isolated
verification configured**. Missing configuration is shown as blocked with
specific blocker codes.

This is a description of saved configuration, not a pass receipt or deployment
readiness check. Repository selection can still come from the trigger. Tracker
permissions, the supported publication provider, isolation capabilities and
checks on the final commit are validated by each execution. The console never
labels a flow trusted merely because its saved policy requests isolation.

## Isolated publication rollout and repair

Set `git_clone_config.publication_mode: isolated` explicitly and configure
`verification.image` with an immutable `image@sha256:<64 hexadecimal digits>`
toolchain reference. Bind a supported GitHub App installation tracker and
repository-specific trusted checks. The generic preset intentionally refuses
unknown non-documentation changes. Existing saved customized flows are not
silently migrated. Review the effective-policy panel, then enable isolation
for that saved flow after its profile and runtime prerequisites are ready.
GitLab, PAT trackers and unsupported runtime capabilities must not be described
as trusted isolated publication paths.

The controller freezes the Git bundle, confirms agent teardown, selects checks
from its saved profile and executes each check in a fresh credential-free
checkout. Both adapters supply the exact controller-pinned base/head range.
The audit manifest records the base/head/tree and bundle digest, profile ID,
version and content digest, pinned image and runtime. Check records include
selection reasons, commands, exit codes, bounded diagnostic tails and duration.
A scoped write credential is acquired only after successful verification.

The controller can reuse successful evidence within the same executor and
execution when the exact bundle, base, profile content, pinned image and
runtime adapter are unchanged. Reuse is marked on each check. This bounded
in-memory cache contains only checks whose runtime teardown was confirmed;
failures are never cached. Agent-written JSON and writable dependency caches
cannot supply evidence. Changing any bound input runs fresh checks. Pinned
images provide reusable dependency layers without sharing agent-writable state.

An unpublished feedback repair keeps its own workspace and native conversation
as the next repair's source. For a currently reserved durable thread, the
controller may recover the last publication binding from a bounded same-flow,
same-thread execution ancestry. That receipt identifies the existing PR branch;
it does not attest the failed repair or authorize its next commit. Each new
execution must pass its current saved verification policy before publication.
Legacy resumes without a durable binding still require explicit migration.

Local acceptance exercises real Git bundles and Docker runtimes with fake
provider writes in `backend/tests/services/test_publication_worker.py`.
Set `PRELOOP_DISABLE_TELEMETRY=true` and
`PRELOOP_PUBLICATION_DOCKER_IMAGE` to a local toolchain image containing Python 3
and Git, then run that file with pytest. The fixtures cover failing checks,
repair, exact-artifact rejection, narrow selection, missing database setup,
credential exclusion, teardown and successful initial/resumed publication.
Adapter tests cover Kubernetes API contracts; these tests do not establish a
live deployment's network isolation or operator profile readiness.
