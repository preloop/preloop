# Native restricted CI setup

Restricted CI credentials are available in OSS for an account's human owner.
They identify a stable machine principal bound to one project and one approved
hosted flow. The flow is either dedicated to that project, as described next,
or a [shared read-only review flow](#shared-read-only-review-flows) that an
administrator has opened to several projects of the account; either way the
identity reaches its own project only. They are not account keys with scope strings. Existing account keys
remain broader; adding a scope or renaming one never converts it. There is no
OIDC or federation exchange in this version.

## Prepare a dedicated hosted flow

Use an existing GitHub or GitLab tracker and synced project, then create a hosted
review flow as a human administrator through the usual Flows API or console.
The project needs a provider repository identifier and slug. The flow must be
enabled, use `runner_pool: "server"`, and trigger on exactly that project. The
one exception is a
[read-only review on a private runner](#read-only-review-on-a-private-runner),
which names its private pool instead.

A complete `POST /api/v1/flows` body for a dedicated read-only review flow.
`name`, `prompt_template` and `agent_config` are required (the API answers 422
without them). Replace the placeholders with your own IDs:

<!-- example: flow-create -->
```json
{
  "name": "Repository review",
  "prompt_template": "Review the checked-out pull request. Do not edit files. Write result.json with a review string.",
  "ai_model_id": "<model id>",
  "agent_type": "codex",
  "agent_config": {"limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
  "runner_pool": "server",
  "allowed_mcp_tools": [],
  "allowed_mcp_servers": [],
  "trigger_project_ids": ["<project id>"],
  "validate_result": true,
  "result_schema": {
    "type": "object",
    "required": ["review"],
    "properties": {"review": {"type": "string"}}
  },
  "git_clone_config": {
    "enabled": true,
    "publication_mode": "read_only",
    "repositories": [{"project_id": "<project id>", "tracker_id": "<GitHub App tracker id>"}]
  }
}
```

Set `ai_model_id` when you create the flow. A flow saved without a model is
accepted and then runs on the account's default model, which may not be the
model, key or budget you meant for reviews. To attach a model to an existing
flow, or change it, send only that field to `PUT /api/v1/flows/{flow_id}`
(`GET /api/v1/ai-models` lists the IDs):

<!-- example: flow-update -->
```json
{"ai_model_id": "<model id>"}
```

`GET /api/v1/ai-models/{model_id}/used-by` then lists the flow.

To bind an existing flow instead, use the human `PUT /api/v1/flows/{flow_id}`
endpoint to set the dedicated clone binding. Replace the synthetic IDs below
with your disposable resources:

<!-- example: flow-update -->
```json
{
  "runner_pool": "server",
  "trigger_project_ids": ["00000000-0000-4000-8000-000000000001"],
  "git_clone_config": {
    "enabled": true,
    "repositories": [{
      "project_id": "00000000-0000-4000-8000-000000000001",
      "tracker_id": "00000000-0000-4000-8000-000000000002"
    }]
  }
}
```

Do not supply an arbitrary repository URL, a second repository or another
runner pool.
The server verifies the actual project, tracker and flow rather than trusting
the selector. If the repository identity changes, provision a new principal;
its stored project/flow/repository snapshot is immutable. Configure the flow
to return a structured result with a `review` string when using the publication
example below.

## Model eligibility

The dedicated hosted flow must use a model its execution credential can invoke:
an API-key or hosted model (Codex also requires Responses support), or a Codex
or Claude subscription model whose owner turned on **Use from my flows** and
also owns this flow. The native read-only review on the server pool works
with such a subscription model; usage is attributed to the review execution.
Any other subscription selection is refused with HTTP 422 before an execution
is created: selecting its registration does not attach its owning agent to the
CI execution. Active credentials alone do not establish eligibility, and
`supports_server_side_generation` is not an executor compatibility check.
Alternatively, run the read-only review on a private runner Codex host profile;
there the host's own Codex login serves the model and no subscription
registration needs to be shared with flows.
See [subscription credentials and execution identity](model-routing.md#subscription-credentials-and-execution-identity).

## Read-only native GitHub App review

For a review that must clone source without provider write credentials, set
`git_clone_config.publication_mode` to `"read_only"` on the dedicated flow above.
Select `agent_type: codex`. Two executors are supported:

- **Hosted:** `runner_pool: "server"`, the ephemeral hosted Codex executor. The
  model must be a server-usable API-key or hosted model, or a subscription
  model whose owner turned on **Use from my flows** and owns this flow (see
  [Model eligibility](#model-eligibility)).
- **Private runner:** a named private runner pool plus
  `agent_config.host_exec_profile` naming a Codex host profile. Codex runs on
  the runner host with that host's own Codex login, so a ChatGPT (or other
  Codex) subscription can serve the review. See
  [Read-only review on a private runner](#read-only-review-on-a-private-runner).

The default `openhands` executor is rejected because its checkout does not
implement this capability. Persistent execution, harness routing
(`agent_config.harness`), `runner_pool: "auto"` or an unset pool, an authorized
host directory (`agent_config.workspace`) and a host profile on the server pool
are rejected at provisioning, admission, lease issuance and launch.
Keep the repository's existing owning GitHub App tracker ID. A different tracker,
arbitrary URL, or unapproved repository is rejected. Shared flows require the
explicit project-selection mode below. The flow must be
enabled for preview and provisioning; enabling a flow does not issue a CI key or
trigger a review.

Only the CI identity can start a read-only review. A trigger from a person,
from the console, a session or an account key, is refused with HTTP 409
before any execution is created; the message names the route
(`POST /api/v1/flows/{flow_id}/trigger` with the CI key) and its two body
fields, `pr_number` and `head_sha`. The console shows **Run now** disabled on
such a flow with the same explanation.

Codex native structured results are on by default in this mode when the flow
has a result schema (see
[Native Codex structured results](repo-review-presets.md#native-codex-structured-results));
set `agent_config.native_result: false` to opt out.

Each review execution runs under the per-execution ceilings shown as **Run
limits** on the flow page: the flow's own `agent_config.limits`, otherwise an
instance default when the operator set `PRELOOP_DEFAULT_MAX_TOTAL_TOKENS`,
`PRELOOP_DEFAULT_MAX_TURNS` or `PRELOOP_DEFAULT_MAX_USD`, otherwise none.
There is no built-in default, so set explicit limits on review flows, for
example `{"limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd":
25}}` for a PR review (large reviews use several million tokens). A run
stopped by the flow's own ceiling fails with `budget_exceeded`, one stopped by
an instance default with `default_limit_reached`.

This mode requires an App installation and the control plane's configured App
signing key. At execution startup, Preloop rechecks the principal and repository
binding and requests one repository with `contents: read` (plus GitHub's implicit
`metadata: read`). An unsupported credential, failed issuance, or broader returned
scope fails the execution; it never falls back to the tracker's normal token.
The checkout is pinned to the server-admitted PR head SHA. The principal snapshots
this mode: changing between legacy and read-only invalidates the old identity's
binding, so provision a new principal instead of silently changing its capability.

**This initial mode does not allow MCP tools or MCP servers.** Set
`allowed_mcp_tools: []` and `allowed_mcp_servers: []`. Native shell helpers may read
source and write local `result.json`; the local filesystem remains writable.
Remote MCP finalizers are unsupported. Runtime credentials cannot call MCP tools
or general account REST routes, even when legacy scope enforcement is disabled.
The normal execution-bound evidence upload capability still accepts only its own
execution and operation. Results remain agent-authored, not an attested command
history or custom diff/challenge proof.

Omit verification, publication approval, report publication, follow-up filing and
backport configuration; `create_pull_request` must be false. Completion persists
the result and emits the existing principal-filtered signed callback. Neither
native post-execution commands nor the trusted publisher push a branch or create
a PR, and Preloop does not post commit statuses in this mode. Use the separately
authorized publisher to verify the owned result and publish it. An agent-authored
success or publication bundle does not grant publication authority.

Workspace/session restoration and continuation are unsupported. A recovered
controller may monitor the existing execution, but cannot restore an old workspace
or broaden its credentials. The read lease expires at GitHub's deadline (normally
one hour); terminal cleanup revokes the lease when the issuing controller still
holds it. A lost controller relies on provider expiry. There is no automatic clone
credential refresh. Operator-supplied images, setup commands and workspace inputs
must not inject independent provider secrets; this mode narrows the credentials
Preloop supplies, not credentials supplied separately by an administrator.

### Trigger, startup receipt and result

Start a review with `POST /api/v1/flows/{flow_id}/trigger` and the CI
credential. The body is exactly `{"pr_number": 7, "head_sha": "<sha>"}`
(`head_sha` is the full lowercase 40 or 64 character SHA). Any other field,
such as a diff, base SHA, challenge or template data, is rejected with 422; it
is never silently ignored. Preloop reads the open pull request from the
provider, requires its current head to equal `head_sha`, builds the pull
request event server-side and starts one production execution
(`test_mode: false`). The flow webhook does not accept CI credentials. Bind
diffs, base revisions or challenges through the flow's own configured review
inputs and your own validator instead.

`GET /api/v1/flows/executions/{id}` (the status projection) and `GET /api/v1/flows/executions/{id}/result` include
`startup_receipt`, written by the controller, never by the model:

<!-- example: fragment -->
```json
{
  "version": 1,
  "executor": "hosted",
  "commit": "<admitted head SHA>",
  "checkout_verified": true,
  "setup_completed": true,
  "agent_started_at": "2030-01-01T00:00:00+00:00",
  "lease_revoked": true,
  "agent_cli": {"name": "codex", "version": "0.153.4"}
}
```

On the hosted executor the container's startup chain fetches the admitted
commit, checks out exactly that commit, verifies `HEAD` equals it and runs
setup commands. Any failure exits before the agent starts. Its last step
prints a startup line naming the admitted commit; the controller records the
receipt only when that line is seen before the agent start marker, and
withdraws it (`checkout_verified` and `setup_completed` false) when a setup
failure is reported. `lease_revoked` is null until terminal cleanup, then
records whether GitHub confirmed revocation of every read lease. The receipt
is null when startup did not complete. A private runner review reports its
runner receipt in the same shape with `executor: "private_runner"` and its
`sandbox`. `agent_cli` names the CLI and the version the container printed
(`codex --version`) before the agent started; it is null when no version line
was seen before the start marker, and always null for a private runner, whose
runner does not report it yet. Fields are only ever added to `version: 1`, so
ignore keys you do not know. The receipt describes the controller's observation of the
container's own output; setup commands run repository code inside that same
container, so treat it as the platform's assertion, not an independent
attestation. The completion callback body is unchanged; fetch `GET /api/v1/flows/executions/{id}` or
`/result` for the receipt.

The restricted `/result` returns `result`, the captured `result.json`
(at most 256 KiB at capture) without controller-private members: keys that
start with `_` or name credentials, headers, prompts, logs, environment or
runtime configuration, at any depth. `result_withheld_paths` lists the JSON
Pointers of withheld members; an empty list means `result` is the complete
captured document. `schema_validation` has `valid`, `error_count` and up to
20 `errors` with their `path`; error messages are included only when nothing
was withheld, since a message can quote a value. It is null when the flow
sets no result schema. Name your result fields so that none is withheld.

Human-owned historical executions retain their human ownership. These CI
permissions do not authorize reading or publishing another execution. Historic
result recovery/publication uses the explicit export grant described below.
GitLab, PAT and alternate-tracker clone bindings are not
supported by this mode. Local tests use a synthetic provider; they do not certify
an installation's live GitHub permissions.

## Read-only review on a private runner

A read-only review can run on your own machine with the Codex CLI already
logged in there (for example with a ChatGPT subscription). Preloop never sees
that login; it sends the runner the prompt, a model alias and a short-lived
read-only clone credential, and the runner starts Codex locally.

What the runner enforces for this mode:

- **Clone:** exactly one repository at the admitted pull request head SHA, with
  the delivered GitHub App lease (`contents: read` on that one repository). The
  host's own git configuration is not used for the checkout: system and global
  git config and credential helpers are disabled, so a missing or rejected lease
  fails the run instead of falling back to your own git login. After checkout
  the runner verifies `HEAD` equals the admitted SHA.
- **Lease:** the runner revokes the lease as soon as the checkout is verified,
  before Codex starts. The control plane revokes it again at completion, and
  GitHub's expiry (normally one hour) is the fallback.
- **Codex:** `codex exec --sandbox read-only` with `approval_policy="never"`. No
  MCP server, no publication, no session resume and no setup commands.
- **Receipt:** the completion carries a runner receipt (admitted commit,
  `read-only` sandbox, whether the lease was revoked). A successful completion
  without a matching receipt is recorded as `FAILED`. The control plane stores
  it on the result as `host_read_only_review`.

Runners that do not advertise the `host_read_only_review` profile capability
(older CLIs, or a Codex profile without `allow_checkout`) are never assigned
this work; the run waits in the queue for a capable runner.

### Step by step

1. **On the runner host**, install a Preloop CLI release that includes
   this capability and the Codex CLI, as the OS user that will run the runner.
2. Onboard Codex through Preloop as that user, with the approval hook:
   `preloop agents onboard "Codex CLI" --approvals`. Onboarding writes
   `model_provider = "preloop"` to Codex's `config.toml`, so every model call
   goes through the Preloop gateway with the agent's own key. Then open
   `codex`, run `/hooks` and trust the Preloop `PreToolUse` hook. The runner
   checks the hook is enabled and trusted before every run.

    Keep this onboarded configuration. Do not run the runner with a separate
    `CODEX_HOME` that lacks the Preloop model provider: Codex would then call
    its model service directly, the run would not be governed or metered, and
    it ends `FAILED` with `gateway_bypassed`.
3. Add a Codex profile with checkout enabled to
   `~/.preloop/runner-host-profiles.json`:

    <!-- example: fragment -->
    ```json
    {
      "profiles": [
        {
          "name": "codex-review",
          "executable": "codex",
          "allow_checkout": true,
          "timeout_seconds": 1200,
          "model_map": {"review-model": "<a model your Codex login offers>"}
        }
      ]
    }
    ```

    `model_map` maps the model the flow selects (its `model_identifier`) to the
    Preloop model alias your onboarded Codex uses (`preloop models list`). If
    the flow selects no model, omit `model_map` and Codex uses the model
    onboarding configured.

4. Start the runner in a named pool, for example
   `preloop runner fg --labels review-hosts` (or install it as a service). Run
   `preloop runner inventory --refresh` after editing profiles. In **Runners**
   the profile should list the `host_read_only_review` capability.
5. **In Preloop**, set the dedicated review flow to (with
   `PUT /api/v1/flows/{flow_id}`):

    <!-- example: flow-update -->
    ```json
    {
      "agent_type": "codex",
      "runner_pool": "review-hosts",
      "agent_config": {"host_exec_profile": "codex-review"},
      "allowed_mcp_tools": [],
      "allowed_mcp_servers": [],
      "trigger_project_ids": ["00000000-0000-4000-8000-000000000001"],
      "git_clone_config": {
        "enabled": true,
        "publication_mode": "read_only",
        "repositories": [{
          "project_id": "00000000-0000-4000-8000-000000000001",
          "tracker_id": "00000000-0000-4000-8000-000000000002"
        }]
      }
    }
    ```

    The model may be any registration, including an agent-bound subscription
    model: on a host profile it only names the alias the runner maps. Codex
    calls the gateway with the onboarded agent's own key, so that agent must be
    allowed to use the model. Give the flow a prompt that asks for a review of
    the checked-out change and does not ask Codex to edit files (the sandbox
    would refuse).
6. Provision the CI identity as below (**Settings → CI identities** lists the
   flow; the preview shows your private pool), add the completion subscription,
   and trigger reviews exactly as for the hosted executor.

Usage and run limits: the runner sends each run's gateway requests with an
execution marker (`X-Preloop-Execution`, signed by the control plane at
delivery and added to Codex's Preloop provider for that run only; your
`config.toml` is not edited). The gateway attributes those requests to the
flow execution as well as the agent, so the execution page shows the run's
requests, tokens and cost, and the token, turn and USD ceilings on the flow
page (including the defaults) are enforced on the host run: the request after
a ceiling is crossed is refused and the run ends `FAILED` with
`budget_exceeded` or `default_limit_reached`. The completed result records
`gateway_metered: true` and `gateway_request_count`. A run whose result shows
model output but no gateway request bypassed Preloop and ends `FAILED` with
`gateway_bypassed` (`gateway_call_check: no_gateway_rows`). Codex exposes no
per-request count, so a Codex run with at least one gateway row passes
(`gateway_call_check: at_least_one`). For Claude Code host profiles the runner
reports the exact model call count (`cli_model_calls`), and a run whose CLI
calls exceed its gateway rows by more than 2 calls and more than 20% partly
bypassed Preloop and fails the same way (`gateway_call_check:
cli_call_count`). Read-only review has no opt-out
from this.

Rules that require approval pause this run. A command that matches one does
not run: the review pauses, an approval request appears in **Approvals**, and
the run resumes on approval (the command may then run once) or ends `FAILED`
with `approval_denied` or `approval_timeout`. The read-only mode is not special:
if reviewers should never pause, write the rules so review commands do not
need approval. In the hosted container, Codex in the read-only sandbox runs
with its shell disabled, so there are no native commands to gate. Other hosted
Codex runs install the same Preloop `PreToolUse` hook in the container and
pause the same way; a hosted Codex run that has such a rule but cannot
install the hook is refused before Codex starts with `approval_unavailable`.
Other hosted harnesses have no native hook and are refused before launch with
`approval_unavailable` when such a rule applies.

Before a hosted Codex container that installs the hook starts, Preloop checks
the execution's runtime credential with the same rules
`POST /api/v1/agents/permission-check` applies on every hook call. If the hook
would be refused (for example a read-only review key with a widened scope, an
attached managed agent, or no runtime credential at all), the run ends
`FAILED` with `hook_binding_invalid` before the container starts: no model
call is made and no startup receipt is issued. The error message names the
refusal. This rule applies whether or not a deny or approval rule exists. It
points at a server-side binding fault, not at your CI configuration; retry
once, and report the execution ID if it repeats.

Limits of a host run compared with the hosted container: the Codex sandbox and
the approval hook are the only boundaries between the review and the runner
user's account. Codex's read-only sandbox blocks writes and network access for
the commands it runs, but the process still runs as your OS user and can read
whatever that user can read. Use a dedicated OS user or VM for repositories you
do not trust. Model traffic goes through the Preloop gateway with the
onboarded agent's key, so usage is metered per execution and results are
marked `gateway_metered: true`.

## Shared read-only review flows

One read-only review flow can serve several projects of the same account. Use
this when the repositories should be reviewed with the same prompt, model,
limits and result schema and you do not want one flow per repository. Supported
from Preloop 1.1.0 (the selection itself exists since 1.0.0; the allowlist API,
CLI commands and console support are new).

What stays the same: each CI identity is bound to exactly one project. It can
start a review only for that project's repository and can read, list and stop
only its own executions. Another identity on the same flow, its executions,
results, startup receipts and tool evidence, and the list of admitted projects
are not visible to it. The trigger body is still exactly `pr_number` and
`head_sha`; Preloop selects the repository, the revision and the clone source
from the identity's project. Nothing per pull request is written to the flow.

The boundary is the account. A tracker organisation is not an access boundary
in Preloop, and a flow cannot admit a project of another account. To keep
groups of repositories apart, give each group its own shared flow (each flow
admits only its own projects) or its own account.

### Configure two repositories on one flow

1. Create one read-only review flow as described in
   [Read-only native GitHub App review](#read-only-native-github-app-review):
   `git_clone_config.enabled: true`, `publication_mode: "read_only"`, Codex,
   no MCP tools. Each repository must be a synced project on a GitHub App
   tracker of this account.
2. As the account owner, set the flow's project allowlist. The list is the
   complete allowlist and replaces the previous one. Save it as
   `projects.json`:

   <!-- example: shared-flow -->
   ```json
   {
     "project_ids": [
       "00000000-0000-4000-8000-000000000001",
       "00000000-0000-4000-8000-000000000004"
     ]
   }
   ```

   ```sh
   preloop ci share-flow FLOW_ID --input projects.json
   preloop ci shared-flow FLOW_ID
   ```

   The API is `PUT /api/v1/ci-identities/shared-flows/{flow_id}` and
   `GET` on the same path, for human administrators only. Preloop writes
   `ci_project_selection: true`, the trigger projects and one clone repository
   per project, taking each repository and tracker from the project itself. It
   accepts no repository URL, tracker or wildcard.
3. Provision one identity per project, each with its own grant
   (`project_id` of that repository, `flow_id` of the shared flow), with
   `preloop ci preview` and `preloop ci create` or in **Settings → CI
   identities**, where the flow is listed as "(shared review flow)". The
   identity reports `binding_mode: "shared"`.
4. In each repository, store that repository's own token and use the
   [workflow example](#operator-run-github-actions-example) unchanged: the same
   `PRELOOP_FLOW_ID` in both, each with its own `PRELOOP_PROJECT_ID` and
   `PRELOOP_CI_TOKEN`.

Check the isolation yourself with both tokens. The script starts no review:

```sh
PRELOOP_CI_TOKEN_A=... PRELOOP_CI_TOKEN_B=... python \
  scripts/verify_restricted_ci.py shared-flow --url https://preloop.example \
  --flow FLOW_ID --project-a PROJECT_A_ID --project-b PROJECT_B_ID
```

It prints one `PASS`, `FAIL` or `SKIP` line per check and exits 0 only when
every check passed (1 on a failure, 2 when a check was skipped because there
was nothing to probe, for example before the first review or with a key that
lacks `flow:trigger`). The checks: each token lists only
executions of its own project on the shared flow, every startup receipt names
the admitted head commit, neither token can read the flow, its allowlist or
the identity list, a trigger that names the other project is refused, and
neither token can read the other's execution, result or tool evidence. Run one
review per repository first so that the cross-read checks have something to
probe. The checks pass on 1.0.x as well, where the selection already exists.

### Move a working dedicated identity to a shared flow

An identity cannot be converted: its project, flow and binding mode are fixed
when it is provisioned. Move a repository without interrupting its reviews by
running both side by side:

1. Leave the dedicated flow and its identity as they are.
2. Create a second read-only review flow with the same prompt, model and
   limits, and admit the repositories with `preloop ci share-flow`.
3. Provision a new identity for the repository on the shared flow
   (`preloop ci preview`, then `preloop ci create`). The old and the new
   identity both work; each lists only its own executions.
4. In the repository, replace `PRELOOP_FLOW_ID` and `PRELOOP_CI_TOKEN` with
   the shared flow and the new token, and run one review to confirm it.
5. Retire the old identity with `preloop ci retire PRINCIPAL_ID`. Its
   execution history is kept, and the dedicated flow can then be deleted.

### Without the allowlist API (Preloop 1.0.x)

Releases before 1.1.0 have the same selection but no `share-flow` command. Set
it with the human `PUT /api/v1/flows/{flow_id}` endpoint instead: list the
admitted projects in `trigger_project_ids` and give each exactly one entry in
`git_clone_config.repositories` with its owning tracker, next to
`"publication_mode": "read_only"` and `"ci_project_selection": true`:

<!-- example: flow-update -->
```json
{
  "trigger_project_ids": [
    "00000000-0000-4000-8000-000000000001",
    "00000000-0000-4000-8000-000000000004"
  ],
  "git_clone_config": {
    "enabled": true,
    "publication_mode": "read_only",
    "ci_project_selection": true,
    "repositories": [
      {"project_id": "00000000-0000-4000-8000-000000000001",
       "tracker_id": "00000000-0000-4000-8000-000000000002"},
      {"project_id": "00000000-0000-4000-8000-000000000004",
       "tracker_id": "00000000-0000-4000-8000-000000000002"}
    ]
  }
}
```

The two lists must name the same projects, without duplicates and without a
repository URL, or every identity on the flow is denied. `preloop ci
capabilities` on those releases still prints the older one-project wording;
the behaviour described on this page is the contract.

### Rules and refusals

- `409` from `share-flow` means one of: the flow is not an enabled read-only
  review flow; a listed project is not an active project of this account on a
  GitHub App tracker (the answer is the same for a project that does not
  exist); or the flow is dedicated and still has identities bound to it.
- An identity's binding mode is fixed when it is provisioned. To turn a
  dedicated flow into a shared one, retire its identities first
  (`preloop ci retire`), share the flow, then provision new identities.
  Editing the flow JSON by hand to flip the mode denies the existing
  identities instead of converting them.
- A project that is not on the allowlist cannot get an identity on the flow.
- Removing a project denies every identity bound to it from its next request:
  reads return 401, a pending run is refused at dispatch and no clone lease is
  minted for it. A run that is already cloning or running finishes on its
  read-only, repository-scoped lease, which expires in about an hour. Adding
  the project back restores the same identities.
- Revoking a key, letting it expire, or retiring an identity affects that
  identity only.
- A shared flow must be a read-only review. A flow that can push, open pull
  requests or use MCP tools cannot be shared.
- Every admitted project gets the same prompt, model, limits and setup
  commands, and the flow's capacity and budget are shared between them.
- A review clones exactly one repository. Admitting extra read-only context
  repositories for a review is designed but not available yet; see the
  [architecture chapter](../../architecture/shared-ci-review-flows.md).

The threat model, with the test that covers each case, is in
[Shared review flows for restricted CI](../../architecture/shared-ci-review-flows.md).

## Own-execution tool evidence

Grant the separate action `execution:evidence:read` to retrieve
`GET /api/v1/flows/executions/{id}/tool-evidence?skip=0&limit=10` (maximum 10 records
per page, with an 8 MiB aggregate records cap; a page always holds at least one
record). Continue using `next_skip`, since
the byte cap can return fewer records than `limit`. Fresh principal, project, flow and execution ownership checks apply to
every page. General logs, artifacts, prompts and other principals' executions
remain unavailable to the CI credential. Key rotation retains principal ownership;
revocation or binding changes deny subsequent reads.

Enterprise rollout requires the coordinated EE action mapping
`READ_EVIDENCE: "view_flows"` and the matching core revision.
Deploy both before granting or advertising this action; an older EE authorizer
fails closed even when a principal grant contains the new action.

New gateway requests retain tool-only observations before console preview clipping.
Request-side `request_scope: latest_tool_exchange` captures the latest tool call
batch and its results, including when prose follows that batch; response-side
capture retains the current response. Replayed
older request history is deliberately excluded, so storage grows with new exchanges
rather than accumulated history. `capture_complete` refers to this declared scope,
not a historical conversation import. Request-side observations whose call ID was
already stored with the same sanitized content digest are omitted; changed
observations under the same call ID remain retained (`request_duplicates_omitted` counts
them), so retries do not repeat an exchange. The dedupe lookup reads a small
per-record key list, never the stored documents. If an evidence write fails, the
usage row and agent log batch are still stored, and a record with
`capture_failed: true` and `capture_complete: false` takes its place.
Codex native command events are captured before the ordinary log-string cap.
Gateway call IDs and native item IDs are distinct namespaces; match only actual
correlation data, never infer that a model-requested call executed. Native exit
codes are runtime observations, not a successful execution or accepted review.
The capture excludes general conversation text. Capture-disabled content is absent;
credential and account storage-redaction rules still apply. JSONB-unsafe controls
and lone surrogates are removed before encoding; altered content sets `redacted`,
`storage_redacted`, and `capture_complete: false`.

Each record's `sha256` hashes its `document` encoded as compact JSON with sorted
keys, UTF-8, `ensure_ascii=false`, and no nonfinite numbers. Execution and gateway
usage IDs are inside that document. `page_sha256` hashes the returned `records`
array using the same encoding. These authenticated-server digests verify retained
representations; they are not an independent signature or a proof of unobserved
process activity. `captured_result_sha256` uses the same encoding of the restricted
`/result` projection, which strips private controller fields.

Read all pages and require `integrity_valid`, `capture_complete`, no `redacted`, no
`truncated`, and no `storage_redacted` when exact bytes are an acceptance condition.
A request record is limited to 512 observations and 2 MiB; omitted observations set
`capture_complete: false`. Oversized native commands are explicitly marked absent.
`missing_gateway_requests` compares capture with retained usage rows. `coverage`
is `missing`, `captured`, or `unavailable`; it is not an assertion of successful
execution. Native command coverage is explicitly `observed_events_only`. A running
execution can acquire more records after a page read. For a terminal audit, verify
stable totals and record IDs across the completed export.

The endpoint also returns safe `terminal_deliveries` for that principal: delivery
ID, state, attempt count, HTTP status and next-attempt time. No receiver URL, secret,
response body or another principal's delivery is exposed. An empty list does not
identify the cause of a missing callback. A stopped execution now queues its
completion in the same transaction as the stop, using the existing deduplication
key, without relying on a surviving orchestrator. Delivery still requires current
principal authority and an eligible subscription.

Previously clipped arguments cannot be recovered by this feature. Historic runs
without capture records report unavailable/missing coverage; the API never invents
a command, verdict, helper challenge or accepted result.

## Console and CLI setup

Open **Settings → CI identities**. The console uses the common human permissions
API, stays in OSS, and offers only currently admissible project/flow bindings
(hosted, or a read-only review on a private runner host profile). Preview
the chosen grant before creation. Choose an expiry and the least actions needed.
The backend capability check prevents issuance during an incomplete rollout.
EE adds `view_ci_identities` and `manage_ci_identities`, intersected with current
project/flow and action permissions. Only owner/admin gain the new permissions
by default; custom roles can explicitly grant them. View does not imply manage.

CLI setup uses your saved human login or `PRELOOP_TOKEN` from a secret manager.
These commands reject the plaintext `--token` flag. Never put credentials in
URLs, shell arguments or JSON request files.

The grant is the same for a dedicated and a shared flow: one `project_id` and
the `flow_id` of the flow that reviews it. On a shared flow the project must be
on the flow's allowlist (`preloop ci shared-flow FLOW_ID`), and each repository
gets its own identity with its own `project_id`. Grant only the actions you
use; add `subscription:create` (and `subscription:read`) only if you will add a
[completion subscription](#completion-subscriptions).

`preview` takes only `{"grant": ...}`, without `name` or `expires_at`. Save it
as `ci-preview.json`:

<!-- example: ci-preview -->
```json
{
  "grant": {
    "version": 1,
    "project_id": "00000000-0000-4000-8000-000000000001",
    "flow_id": "00000000-0000-4000-8000-000000000003",
    "actions": ["flow:trigger", "execution:read", "execution:result:read", "execution:stop"]
  }
}
```

`create` takes the same grant plus a `name` and an optional `expires_at`. Save
this as `ci-request.json` (use your own synthetic test resource IDs):

<!-- example: ci-create -->
```json
{
  "name": "Hosted review CI",
  "grant": {
    "version": 1,
    "project_id": "00000000-0000-4000-8000-000000000001",
    "flow_id": "00000000-0000-4000-8000-000000000003",
    "actions": ["flow:trigger", "execution:read", "execution:result:read", "execution:stop"]
  },
  "expires_at": "2030-01-01T00:00:00Z"
}
```

`--secret-file` names a new file in a directory you own and only you can
read. Create that directory once:

```sh
mkdir -m 700 ~/ci-secrets
preloop ci capabilities
preloop ci preview --input ci-preview.json
preloop ci create --input ci-request.json --secret-file ~/ci-secrets/new-ci-token
preloop ci list
preloop ci show PRINCIPAL_ID
preloop ci rotate PRINCIPAL_ID KEY_ID --secret-file ~/ci-secrets/rotated-ci-token
preloop ci revoke PRINCIPAL_ID KEY_ID
preloop ci issue PRINCIPAL_ID --secret-file ~/ci-secrets/replacement-ci-token
preloop ci retire PRINCIPAL_ID
```

The CLI reserves a new exclusive file with mode `0600` before requesting a
secret. Existing files and symlinks fail before issuance. Stdout contains only
safe metadata. The file contains the issued token followed by one newline;
move it directly into the repository secret manager and remove the local copy.
Use a private directory, including on Windows where POSIX mode alone does not
define access control. Tokens and signing secrets are shown once in the console
and cleared when the modal closes or the component disconnects.

The error says why a `--secret-file` path cannot be used: the file already
exists, its directory does not exist, or you cannot create files in that
directory (for example `/private` on macOS, which belongs to the system).

`PRINCIPAL_ID` is the identity's `id`; `KEY_ID` is the `id` of one of its
keys. `create` prints both as `identity.id` and `key_id`; `issue` and
`rotate` print them as `principal_id` and `key_id`; and `preloop ci show PRINCIPAL_ID` lists the identity's `keys` with their ids.
`rotate` and `revoke` need both ids: an identity can hold more than one key,
so the CLI does not guess which one you mean.

`preview` prints the binding Preloop will record, without issuing anything.
For a dedicated flow:

<!-- example: fragment -->
```json
{
  "grant": {"version": 1, "project_id": "00000000-0000-4000-8000-000000000001",
            "flow_id": "00000000-0000-4000-8000-000000000003",
            "actions": ["flow:trigger", "execution:read", "execution:result:read", "execution:stop"]},
  "flow_name": "Repository review",
  "tracker_id": "00000000-0000-4000-8000-000000000002",
  "repository_identifier": "123456789",
  "repository_slug": "example/repository",
  "tracker_type": "github",
  "binding_mode": "dedicated",
  "runner_pool": "server"
}
```

For a shared flow the same request, with that repository's `project_id` and
the shared flow's `flow_id`, prints `"binding_mode": "shared"`, the shared
flow's `flow_name` and the `repository_slug` of that one project. Check that
`flow_name` and `repository_slug` are the ones you meant before `create`.

Issuance and file storage cannot be a transaction across the server and local
disk. If a request times out, inspect metadata before retrying. If storage fails
after issuance, inspect the new key ID and revoke/replace it. An interrupted
reservation can be empty; remove it only after checking server metadata.

`preloop ci update PRINCIPAL_ID --input change.json` accepts a non-null grant
and/or explicit boolean `enabled`. Disable with `{"enabled": false}`. Re-enable
requires an active binding. Narrowing actions, disabling and revocation remain
available when a bound flow/project is inactive. An expired or revoked key
cannot rotate, but a human can issue a replacement on the same active principal.
Rotation keeps the old key's action ceiling and expiry unless an expiry is
explicitly supplied. It never broadens ownership or re-enables the identity.

### Retiring an identity

A CI identity is never deleted, and its flow binding cannot change, so while an
identity is bound to a flow that flow cannot be deleted (the delete returns 409
and names the identity). To release the flow, retire the identity with the
**Retire identity** button in the console, `preloop ci retire PRINCIPAL_ID`, or
`POST /api/v1/ci-identities/{id}/retire`. Retiring:

- disables the identity permanently and revokes all of its keys;
- clears its flow binding, so the flow can then be deleted;
- keeps the grant (which still records the original flow), execution
  attribution and audit history.

Retirement cannot be undone. A retired identity cannot be re-enabled, narrowed,
reissued, subscribed or used for result exports; provision a new identity to
resume CI. It uses the same authority as disabling, and retiring an already
retired identity changes nothing. Reads show `"retired": true`.

## Completion subscriptions

Include only the callback actions you need in the principal grant. For example,
`subscription:create`, `subscription:read`, `subscription:update`,
`subscription:delete` and `subscription:secret:rotate`. The grant must include
`subscription:create` before you subscribe; without it the CLI stops and names
the missing action. Add it to an existing identity with
`preloop ci update PRINCIPAL_ID --input change.json`, where `change.json` holds
the identity's complete grant with the added action:

<!-- example: ci-update -->
```json
{
  "grant": {
    "version": 1,
    "project_id": "00000000-0000-4000-8000-000000000001",
    "flow_id": "00000000-0000-4000-8000-000000000003",
    "actions": ["flow:trigger", "execution:read", "execution:result:read", "subscription:create", "subscription:read"]
  }
}
```

A human then creates the endpoint:

```sh
preloop ci subscribe PRINCIPAL_ID --input callback.json --secret-file ~/ci-secrets/new-signing-secret
```

`callback.json` names one key of the identity and the HTTPS URL of your
receiver. `description` and `active` are optional:

<!-- example: ci-subscribe -->
```json
{
  "key_id": "00000000-0000-4000-8000-000000000004",
  "url": "https://example.com/preloop-completion"
}
```

The signing secret is written once to the `--secret-file`; store it with your
receiver. Stdout shows the subscription's `id`, `url` and `event_types`.

`key_id` is a real key belonging to the principal, used only as an audit anchor.
It may be revoked/expired; it is not the human's authentication. The event filter
is fixed to `flow.execution.finished` and the stored principal/flow binding.
Subscription management by machines is own-only through the five endpoint
lifecycle operations. Generic test delivery, replay, history, retries and raw
event filtering are denied. Rotation/revocation of a key preserves the stable
subscription. Disabling the principal or removing its callback grant prevents
future dispatch, including already queued retries.

Verify `X-Preloop-Signature` over the original bytes as HMAC-SHA256 of
`timestamp.body`, accept a small freshness window, and deduplicate completion
by execution ID plus exact head. A valid signature is insufficient by itself:
fetch the current owned execution and result and compare execution, project,
flow, provider repository identifier, PR number/provider ID, exact SHA and
terminal status. No general execution logs, artifacts or session contents are
included in restricted results.

The completion `data` block, the delivery and retry contract and replay
handling are documented in
[Restricted CI completion](../webhooks.md#restricted-ci-completion). It names
the bound repository, PR number, head SHA and the base SHA recorded when the
review was accepted, plus `result_sha256`, the digest of the result the CI key
will read.

### Result readiness

Preloop stores the result together with the terminal status, then queues
`flow.execution.finished`, and materializes each delivery from that stored
state. A completion event therefore never arrives before its result:
`result_ready: true` means `GET /api/v1/flows/executions/{id}/result` returns
it now. `GET .../result` answers:

- `200` with the result;
- `409` with `detail.code: "result_pending"` and the current `detail.status`
  while the execution has not finished, even if a checkpoint is stored, for
  example while it is parked (retry after it finishes);
- `404` when the execution does not exist for the caller, or finished without
  reporting a result. This does not change later.

## Review and status posted by Preloop

A read-only review flow can have Preloop itself post the result: one pull
request review and one commit status on the commit that was reviewed. The run
stays read-only. The container, the model and your CI job never hold a GitHub
write token, and you need no publisher workflow. Design and threat model:
[Review and status posted by Preloop](../../architecture/platform-posted-review.md).

It is off by default. Turn it on with `git_clone_config.platform_review` on a
flow that has `publication_mode: "read_only"`, `validate_result: true` and a
`result_schema`. It works with and without `ci_project_selection`.

<!-- example: fragment -->
```json
"platform_review": {
  "enabled": true,
  "status": {
    "context": "preloop/review",
    "verdict_states": {"pass": "success", "fail": "failure"},
    "incomplete_state": "error"
  }
}
```

| Key | Meaning |
| --- | --- |
| `enabled` | Turns posting on. |
| `verdict_actions` | Optional map from verdict to `comment`, `approve` or `request_changes`. Without an entry the review is a comment. An approval is never implied. |
| `status` | Optional. Omit it to post the review only. |
| `status.context` | Status name, default `preloop/review`. Keep it stable so a branch protection rule can require it. |
| `status.verdict_states` | `success`, `failure` or `error` for every verdict your schema admits. Saving fails if one is missing. |
| `status.incomplete_state` | Optional `error` or `failure`, set with a neutral description when the run stopped, failed or returned no valid result, so a required status is not left waiting. Without it nothing is posted for such runs. |

What your result must contain. Preloop reads only these fields of the
validated result; your schema decides which are required beyond `verdict`:

- `verdict`: the schema must declare it as a required string with an `enum`.
- `summary`: text.
- `findings`: a list of objects with `severity`, `title`, `body` (or
  `description`, `message`, `detail`), `path` (or `file`) and `line`.
- `checked` (or `checks`): a list of short strings saying what was reviewed.

What gets posted:

- A review with the verdict, the summary, every finding with its severity,
  file and line, what was checked, the reviewed commit and a link to the
  execution. A finding whose file and line are in the pull request's diff is
  also an inline comment. All text from the result is shown as literal text:
  it cannot mention a user, link anywhere, reference or close an issue, or
  carry HTML.
- A commit status on the reviewed SHA with the state mapped from the verdict.
- Nothing from the result chooses the target. Repository, pull request and
  commit are the ones Preloop bound when it accepted the trigger
  (`pr_number` and `head_sha`).
- If the pull request head moved while the review ran, the status is still
  set on the reviewed commit and the review is not posted
  (`review_reason: "head_moved"`). Trigger a review of the new head.
- A run that stopped, failed or returned an invalid result never posts a
  verdict.

### GitHub App permissions

The GitHub App installation that owns the repository needs **Pull requests:
Read and write** and, for the status, **Commit statuses: Read and write**.
For each publication Preloop mints a token for that one repository with
those two permissions and no contents permission, uses it for the two
writes, reads both back and revokes it. If the installation does not hold
**Commit statuses**, the review is still posted and the record says
`status: "failed"`, `reason: "statuses_permission_missing"`. An organisation
owner then has to approve the added permission on the App installation;
after that the publish call below adds the status without posting the review
again. Personal access token trackers, GitLab and Bitbucket are refused when
you save the opt-in.

### Existing identities must be provisioned again

A CI identity records which provider writes its flow can cause when it is
provisioned. After you turn `platform_review` on, add `status`, or add
`approve` or `request_changes`, the flow's existing identities stop
authenticating (401) until you provision new ones and replace the keys in
your CI. This is deliberate: an identity created for a flow that could not
write is never silently upgraded. Retire the old identities
([Retiring an identity](#retiring-an-identity)).

### The posting record

`GET /api/v1/flows/executions/{id}`, `GET .../result` and the `data` of
`flow.execution.finished` carry `platform_review` (`null` when the flow has
not opted in):

<!-- example: fragment -->
```json
{
  "status": "posted",
  "reason": null,
  "head_sha": "<reviewed sha>",
  "review": "posted",
  "review_id": "2890000001",
  "review_action": "comment",
  "review_reason": null,
  "commit_status": "failure",
  "status_context": "preloop/review",
  "status_reason": null,
  "posted_at": "2030-01-01T00:00:05+00:00",
  "attempts": 1,
  "retry_scheduled": false
}
```

`status` is `pending` (not attempted yet), `publishing`, `posted`, `skipped`
(the rules decided nothing is posted) or `failed`. A posting failure never
changes the execution's status or result. `reason` values include
`head_moved`, `pull_request_closed`, `run_not_succeeded`, `result_not_valid`,
`verdict_unmapped`, `admission_revoked`, `binding_changed`,
`statuses_permission_missing`, `provider_unavailable`, `provider_denied`,
`provider_rejected`, `lease_refused`, `lease_unavailable`,
`attempt_interrupted`, `platform_review_disabled` and `readback_mismatch`. Preloop retries `provider_unavailable` and similar
transient failures by itself, up to six attempts with growing delays.

### Publish a finished review again, without a model run

```sh
curl -fsS -X POST \
  -H "Authorization: Bearer $PRELOOP_CI_KEY" \
  "$PRELOOP_URL/api/v1/flows/executions/$EXECUTION_ID/platform-review"
```

The call takes no body. It publishes the stored, validated result of that
execution on the commit it reviewed, or does nothing if it is already there,
and returns the record above. Use it after a posting failure. It is allowed
for an account user who may edit flows and for the execution's own CI
identity when its grant and key include `execution:review:publish` (the
identity does not also need `flow:trigger` for this call). It
answers 409 with `detail.code` `platform_review_disabled`,
`execution_not_finished` or `no_review_binding` when nothing can be
published, and 404 for an execution the caller does not own. An execution
whose head has moved, whose result is not valid or whose identity was
disabled is never published by this call either.

### Worked example: one flow for an organisation, two repositories

One hosted Codex flow reviews two repositories. Each repository has its own
CI identity, so neither can trigger for, read or cause a post on the other.

1. Flow (an administrator saves this with `POST /api/v1/flows`; the model,
   project and tracker IDs are yours):

    <!-- example: flow-create -->
    ```json
    {
      "name": "Organisation review",
      "prompt_template": "Review the checked-out pull request. Do not edit files. Write result.json with verdict, summary and findings.",
      "ai_model_id": "<model id>",
      "agent_type": "codex",
      "agent_config": {"limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
      "runner_pool": "server",
      "allowed_mcp_tools": [],
      "allowed_mcp_servers": [],
      "validate_result": true,
      "result_schema": {
        "type": "object",
        "required": ["verdict", "summary", "findings"],
        "properties": {
          "verdict": {"type": "string", "enum": ["pass", "fail"]},
          "summary": {"type": "string"},
          "findings": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["severity", "title", "body"],
              "properties": {
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "path": {"type": "string"},
                "line": {"type": "integer"}
              }
            }
          },
          "checked": {"type": "array", "items": {"type": "string"}}
        }
      },
      "trigger_project_ids": ["<project A id>", "<project B id>"],
      "git_clone_config": {
        "enabled": true,
        "publication_mode": "read_only",
        "ci_project_selection": true,
        "repositories": [
          {"project_id": "<project A id>", "tracker_id": "<GitHub App tracker id>"},
          {"project_id": "<project B id>", "tracker_id": "<GitHub App tracker id>"}
        ],
        "platform_review": {
          "enabled": true,
          "status": {
            "context": "preloop/review",
            "verdict_states": {"pass": "success", "fail": "failure"},
            "incomplete_state": "error"
          }
        }
      }
    }
    ```

2. Two identities, one per repository, each with the grant below and its own
   `project_id`. Store each key as a secret of its own repository only.

    <!-- example: ci-create -->
    ```json
    {
      "name": "Review CI, repository A",
      "grant": {
        "version": 1,
        "project_id": "<project A id>",
        "flow_id": "<flow id>",
        "actions": [
          "flow:trigger",
          "execution:read",
          "execution:result:read",
          "execution:stop",
          "execution:review:publish"
        ]
      },
      "expires_at": "2030-01-01T00:00:00Z"
    }
    ```

3. In each repository's CI, on a pull request event, trigger with that
   repository's key and a body of exactly
   `{"pr_number": <number>, "head_sha": "<full head sha>"}`. That is the whole
   job: no checkout, no GitHub write token, no publisher step.
4. When the run finishes, the review and the `preloop/review` status are on
   the pull request. Require `preloop/review` in branch protection if you
   want it to gate merging.

### How this relates to your own publisher

The subscription and `repository_dispatch` routes below stay supported and
work the same with the opt-in on or off. Use one or the other for posting:
with both active a pull request gets two reviews. A publisher of your own is
still the route when you need a review format of your own, a provider other
than GitHub, or writes Preloop does not make (labels, check runs, threaded
replies).

## Completion by GitHub repository_dispatch

Instead of a URL, a human administrator can have Preloop deliver the
completion as a GitHub `repository_dispatch` to the identity's bound
repository. A workflow in that repository then runs on completion: no
polling and no callback receiver to host.

```bash
curl -X POST "$PRELOOP_URL/api/v1/ci-identities/$PRINCIPAL_ID/github-dispatch-subscriptions" \
  -H "Authorization: Bearer $PRELOOP_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key_id": "<principal key id>", "event_type": "preloop-review-completed", "token": "<GitHub token>"}'
```

The principal's grant must include `subscription:create`, as for an HTTP
subscription; removing it stops future dispatches, including queued retries.
The response returns the signing secret once; store it as the publisher's
`PRELOOP_CALLBACK_SECRET`. The target URL is always derived from the bound
repository (`https://api.github.com/repos/{owner}/{repo}/dispatches`, or
`<host>/api/v3/...` for GitHub Enterprise Server) and cannot be edited.
Replace the token with
`PUT /api/v1/ci-identities/{principal_id}/github-dispatch-subscriptions/{endpoint_id}/token`.

**The dispatch token needs write access.** GitHub requires `contents: write`
on the repository for `POST /repos/{owner}/{repo}/dispatches`. Use the
narrowest credential you can:

1. A dedicated GitHub App installed on this one repository with only
   `contents: write` (preferred). Note that installation tokens expire after
   one hour, so the stored token must be refreshed with the replace route by
   your own automation, or use option 2.
2. A fine-grained personal access token limited to this one repository with
   only `Contents: read and write`, owned by a machine account.

Do not reuse the publisher's credentials or the review integration's App.
`contents: write` also allows pushing to the repository, so keep this token
out of every workflow; Preloop holds it only to send the dispatch.

The dispatch credential is configured only by a human administrator (a CI
key is refused), stored encrypted on the subscription, never returned by any
read, and used only by the delivery worker. It is never placed in the
execution, the runner or the agent environment, so the read-only reviewer
still holds no GitHub-write credential. Deleting the subscription deletes the
token.

## Callback-driven publisher example

[`restricted-ci-publish.yml`](../../examples/restricted-ci-publish.yml) with
`scripts/restricted_ci_publish.py` is a `repository_dispatch` publisher. The
flow uses the native read-only mode described above (a complete
`POST /api/v1/flows` body):

<!-- example: flow-create -->
```json
{
  "name": "Repository review",
  "prompt_template": "Review the checked-out pull request. Do not edit files. Write result.json with a review string.",
  "ai_model_id": "<model id>",
  "agent_type": "codex",
  "agent_config": {"limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
  "runner_pool": "server",
  "trigger_project_ids": ["<project id>"],
  "validate_result": true,
  "result_schema": {
    "type": "object",
    "required": ["review"],
    "properties": {"review": {"type": "string"}}
  },
  "git_clone_config": {
    "enabled": true,
    "publication_mode": "read_only",
    "repositories": [{"project_id": "<project id>", "tracker_id": "<GitHub App tracker id>"}]
  },
  "allowed_mcp_tools": [],
  "allowed_mcp_servers": []
}
```

Give the principal key used by the publisher only `execution:read` and
`execution:result:read`. Whatever starts reviews (for example a separate job
calling `POST /api/v1/flows/{flow_id}/trigger`) needs only `flow:trigger`
and no GitHub write permission. The publisher job:

1. reads `client_payload` from the event file (never from the shell
   command line) and verifies `signature` over `event` with
   `PRELOOP_CALLBACK_SECRET`;
2. re-fetches the execution and `/result` with `PRELOOP_CI_TOKEN` and
   requires the execution, flow, project, repository, PR, head SHA, base SHA,
   status and `result_sha256` to match the signed completion;
3. reads the pull request with its own `GITHUB_TOKEN`. If the head or base
   moved, it creates a `neutral` check run and publishes no review;
4. otherwise publishes one COMMENT review on the exact head and one check run
   (`success` or `failure`), using only that job's `GITHUB_TOKEN` with
   `checks: write` and `pull-requests: write`. Both are deduplicated by
   execution ID, so a redelivered dispatch publishes nothing new.

GitHub calls use the Actions-provided `GITHUB_API_URL`, so the example also
works on GitHub Enterprise Server. The example publishes only executions that
stored a result; a run that
finished without one ends the job with an error and publishes nothing.

## Operator-run GitHub Actions example

Copy [the workflow example](../../examples/restricted-ci-review.yml) and
`scripts/restricted_ci_review.py` into a disposable repository's trusted default
branch. Set repository secret `PRELOOP_CI_TOKEN`, and variables `PRELOOP_URL`,
`PRELOOP_PROJECT_ID` and `PRELOOP_FLOW_ID`. Dispatch from the default branch only
with an open same-repository PR carrying the `ci-approved` label and its current
exact head SHA. The script rejects fork PRs and never checks out or executes PR
code. Do not change this into a `pull_request_target` workflow that runs untrusted
code with secrets.

The read-only job validates authoritative context, deduplicates matching owned
executions, stops its principal's obsolete runs on the same PR, and polls the
persisted terminal result. The optional publication job has separate
`pull-requests: write` permission, repeats verification and confirms the returned
review receipt, commit and expected bot author. Only then does stdout contain a
non-null `review_id`. A successful flow or callback is not proof of publication.
Concurrency queues dispatches rather than abruptly cancelling runners, allowing
the script to stop owned obsolete executions. Use a separate principal per
workflow so one workflow cannot cancel another workflow's runs.

For a captured signed callback, pass `--callback-body` with the original body
file and supply `PRELOOP_CALLBACK_SECRET`/`PRELOOP_CALLBACK_SIGNATURE` from the
receiver. The signature covers the full production event envelope; the verifier
checks its fixed type/version and compares the completion in `data`. Supply
`--callback-account` to additionally pin the envelope to your known account UUID.
Reject stale captures. No receiver is exposed by this example. For
manual publication with a human GitHub token, set `--review-author` to that
token's public user login; Actions defaults to `github-actions[bot]`.
Draft/pending or dismissed receipts never count as published review proof. The
current exact PR head is rechecked after receipt verification. GitHub publication
and head updates are separate external operations, so a concurrent head change
can leave a review attached to the old commit; the verifier then fails and does
not report current-head publication success.

The legacy `run-flow` action accepts broader account credentials and rejects
typed `ci_` tokens. Restricted credentials cannot enroll/administer runners,
edit flows/projects/policies, create keys/users/identities, retry/resume/matrix
executions or exchange sessions.

## Local release gate and remaining proof

Create and migrate an isolated disposable PostgreSQL database using the native
development guide. Set `DATABASE_URL` to that database, activate the venv and run:

```sh
PRELOOP_DISABLE_TELEMETRY=true python scripts/verify_restricted_ci.py
cd cli
PRELOOP_DISABLE_TELEMETRY=true go test ./internal/cmd -run '^TestCI'
```

The gate uses real fixture resources and actual HTTP plus CRUD state assertions:

| Acceptance scenario | Automated evidence |
| --- | --- |
| Bound PR/head trigger and persisted own result; stop/idempotence | execution admission, dispatch, HTTP and ownership tests |
| A/B same flow, other flow/project/account, human and null legacy rows | execution and callback ownership/HTTP tests |
| Principal-owned callback lifecycle and exact event filtering | subscription HTTP and callback contract tests |
| Rotation/revocation/expiry, replacement, inactive-resource recovery | identity and human setup HTTP tests |
| Disable/narrowing affects queued dispatch and retries | callback contract/failure and execution dispatch tests |
| Valid forbidden writes, auth/session/runner/log routes, partial rollout | REST route inventory and human setup denial tests |
| Malformed/mixed markers, unknown actions/version, scope audit/off | identity, REST, admission and callback fail-closed tests |
| Signature/context mismatch and separate publication receipt | operator workflow verifier tests |
| Fresh human permissions, ordinary roles and owner/admin seed defaults | permission and seed tests; EE suite separately tests team administration and core ceiling |

Run the EE edition tests against its pinned core SHA before release. The local
provider and runner calls are synthetic: they prove admission and state controls,
not a live hosted run. No external Actions smoke or customer configuration was
performed for this implementation. The workflow is a runnable operator path;
automatic Actions acceptance is not live-proven. On disposable resources, record
the accepted exact head, signed completion, matching persisted result and the
independent GitHub review receipt before calling that external path proven.

### Credential-check resilience

The middleware uses a separate synchronous credential-check pool in each
API/gateway process. `DATABASE_AUTH_POOL_SIZE` (default 2) and
`DATABASE_AUTH_MAX_OVERFLOW` (3) control its retained and overflow connections;
`DATABASE_AUTH_POOL_TIMEOUT` (3 seconds) bounds checkout waits. Connections use a
3-second connect timeout and a 5-second statement timeout. Include this pool in
connection budgets alongside both request pools and the health connection.
The DB pool monitor includes it as `engine=auth` in saturation warnings and
`preloop.db.engine=auth` in OpenTelemetry connection gauges.

Only non-machine credential classifications are cached, using SHA-256 keys and
a maximum of 10,000 entries per process. `CI_AUTH_PASS_THROUGH_TTL_SECONDS`
(default 60) controls freshness. If a database lookup fails, an entry can be used
until `CI_AUTH_PASS_THROUGH_STALE_TTL_SECONDS` (default 600) from its original
insertion, not from the latest hit. Unknown or expired entries still fail closed
when verification is unavailable. Machine credential results are never cached.
A pass-through classification supplies no identity or authorization: the route's
normal authentication still checks current credential validity and restrictions.

## Explicit export of a completed human review

A current account owner can authorize one completed account-owned review for a
specific CI principal without changing execution ownership or rerunning the
model. This is an explicit API operation, not automatic access to history.
The principal must already carry `execution:result-export:read`. EE additionally
requires current CI identity administration and resource permissions for the
owner; those permissions are checked again at every export. An active account
flow halt denies new grants and reads with the explicit halt 403 before provider
I/O; revocation remains available.

1. Review the original execution and public result. The human tool-evidence
   response's `captured_result_sha256` is the digest of the public result
   projection: sorted, compact UTF-8 JSON, without private controller fields.
   Inspect the fixed helper's diff/challenge/verdict evidence separately; a hash
   proves byte agreement, not the correctness of the review.
2. As the current human owner, send
   `POST /api/v1/flows/executions/{execution_id}/result-export` with
   `principal_id`, `pr_number`, `head_sha`, `base_sha`, `result_sha256` and an
   aware `expires_at` no more than 24 hours ahead. No arbitrary URLs or repository
   inputs are accepted. The original event must record the matching project,
   immutable provider repository and PR IDs, same-repository head and base SHAs.
   Missing historical provenance is rejected, never filled from caller claims.
3. Use the returned grant UUID with the restricted credential at
   `GET /api/v1/flows/executions/{execution_id}/result-export/{grant_id}`.
   Every call checks the current principal/key/actions, owner authority, expiry,
   revocation, unchanged source flow/event/result and a fresh provider PR read.
   Only SUCCEEDED executions with a captured result and no failed schema
   validation qualify. STOPPED/failed runs, CI-owned runs and fork reviews do not.
4. To revoke, the current owner sends
   `DELETE /api/v1/flows/executions/{execution_id}/result-export/{grant_id}`.
   Revocation is irreversible. Key rotation preserves the principal's grant;
   disabling/narrowing the identity removes access immediately.

The export contains a public result, exact revision/digest receipt and a stable
`publication_idempotency_key`. Consent identity, digest, revisions and expiry
are immutable in the database. New grants for the same reviewed result keep the
same publication key. General execution, result and log routes continue to deny
the principal access to this historical execution. No historical grant is
created by the migration.

The GitHub operator example supports this path with these additional flags:

```sh
python scripts/restricted_ci_review.py \
  --url "$PRELOOP_URL" --project "$PROJECT_ID" --flow "$FLOW_ID" \
  --repository "$REPOSITORY" --pr "$PR_NUMBER" --head "$HEAD_SHA" \
  --execution "$EXECUTION_ID" --result-export-grant "$GRANT_ID" \
  --export-account "$ACCOUNT_ID" --base "$BASE_SHA" \
  --expected-result-sha256 "$REVIEWED_RESULT_SHA256"
```

The default verifies the export only. `--publish-review` additionally requires a
separately authorized `GITHUB_TOKEN` with review-write permission. The example
never launches, cancels, polls or adopts an execution in export mode. It checks
current head/base, reviewed digest and grant again before a write, deduplicates
by the stable marker, and independently reads back the exact review body,
commit and expected author. Reviews above the provider body limit are rejected
without truncating approved text. The example is GitHub-only; the export API
also verifies GitLab when its original event retained the necessary revisions.

Serialize publishers using one trusted workflow concurrency group per repository
and PR. The marker protects sequential retries; GitHub does not provide an
atomic review-post idempotency key across concurrent external publishers.
Consent revocation, provider head/base changes and publication are separate
systems: a change immediately after the final preflight can still race the
external write. Final read-back and grant checks then fail instead of reporting
current authorized publication success. No live activation or external receipt
is implied by the synthetic tests.
