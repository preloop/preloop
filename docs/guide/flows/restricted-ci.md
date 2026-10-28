# Native restricted CI setup

Restricted CI credentials are available in OSS for an account's human owner.
They identify a stable machine principal bound to one project and its dedicated
hosted flow. They are not account keys with scope strings. Existing account keys
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
Use the human `PUT /api/v1/flows/{flow_id}` endpoint to set the dedicated clone
binding. Replace the synthetic IDs below with your disposable resources:

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

On the hosted executor, the flow must use a model its execution credential can
invoke.
An agent-bound Codex or Claude subscription model is not eligible: selecting
its registration does not attach its owning agent to the CI execution. Use a
server-usable API-key or hosted model; Codex also requires Responses support.
Admission returns HTTP 422 before creating an execution for this incompatible
selection. Active credentials alone do not establish eligibility, and
`supports_server_side_generation` is not a complete executor compatibility check.
To review with a subscription, run the read-only review on a private runner
Codex host profile instead; there the host's own Codex login serves the model.
See [subscription credentials and execution identity](model-routing.md#subscription-credentials-and-execution-identity).

## Read-only native GitHub App review

For a review that must clone source without provider write credentials, set
`git_clone_config.publication_mode` to `"read_only"` on the dedicated flow above.
Select `agent_type: codex`. Two executors are supported:

- **Hosted:** `runner_pool: "server"`, the ephemeral hosted Codex executor. The
  model must be a server-usable API-key or hosted model (see
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
arbitrary URL, shared flow, or second repository is rejected. The flow must be
enabled for preview and provisioning; enabling a flow does not issue a CI key or
trigger a review.

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

This solves clone access for a dedicated project flow. Shared-flow authorization,
scoped attested audit evidence and publication of human-owned historical runs
remain separate capabilities. GitLab, PAT and alternate-tracker bindings are not
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
   this capability and the Codex CLI. Log Codex in as the
   user that will run the runner: `codex login`, then check with
   `codex login status`.
2. Install the Preloop approval hook for Codex and trust it, as for any Codex
   host profile:
   `preloop agents onboard "Codex CLI" --approvals`, then open `codex`, run
   `/hooks` and trust the Preloop `PreToolUse` hook. The runner checks the hook
   is enabled and trusted before every run.
3. Add a Codex profile with checkout enabled to
   `~/.preloop/runner-host-profiles.json`:

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
    model name your Codex login accepts. If the flow selects no model, omit
    `model_map` and Codex uses its own default model.

4. Start the runner in a named pool, for example
   `preloop runner fg --labels review-hosts` (or install it as a service). Run
   `preloop runner inventory --refresh` after editing profiles. In **Runners**
   the profile should list the `host_read_only_review` capability.
5. **In Preloop**, set the dedicated review flow to:

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
    model: on a host profile it only names the alias the runner maps, and no
    gateway credential is issued for it. Give the flow a prompt that asks for a
    review of the checked-out change and does not ask Codex to edit files
    (the sandbox would refuse).
6. Provision the CI identity as below (**Settings → CI identities** lists the
   flow; the preview shows your private pool), add the completion subscription,
   and trigger reviews exactly as for the hosted executor.

Limits of a host run compared with the hosted container: the Codex sandbox and
the approval hook are the only boundaries between the review and the runner
user's account. Codex's read-only sandbox blocks writes and network access for
the commands it runs, but the process still runs as your OS user and can read
whatever that user can read. Use a dedicated OS user or VM for repositories you
do not trust. Usage is not metered by the gateway (the run uses your own
login), so results are marked `gateway_metered: false`.

## Console and CLI setup

Open **Settings → CI identities**. The console uses the common human permissions
API, stays in OSS, and offers only actually dedicated bindings (hosted, or a
read-only review on a private runner host profile). Preview
the chosen grant before creation. Choose an expiry and the least actions needed.
The backend capability check prevents issuance during an incomplete rollout.
EE adds `view_ci_identities` and `manage_ci_identities`, intersected with current
project/flow and action permissions. Only owner/admin gain the new permissions
by default; custom roles can explicitly grant them. View does not imply manage.

CLI setup uses your saved human login or `PRELOOP_TOKEN` from a secret manager.
These commands reject the plaintext `--token` flag. Never put credentials in
URLs, shell arguments or JSON request files. Save this public grant as
`ci-request.json` (use your own synthetic test resource IDs):

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

`preview` takes only `{"grant": ...}`, without `name` or `expires_at`:

```sh
preloop ci capabilities
preloop ci preview --input ci-preview.json
preloop ci create --input ci-request.json --secret-file /private/new-ci-token
preloop ci list
preloop ci show PRINCIPAL_ID
preloop ci rotate PRINCIPAL_ID KEY_ID --secret-file /private/rotated-ci-token
preloop ci revoke PRINCIPAL_ID KEY_ID
preloop ci issue PRINCIPAL_ID --secret-file /private/replacement-ci-token
```

The CLI reserves a new exclusive file with mode `0600` before requesting a
secret. Existing files and symlinks fail before issuance. Stdout contains only
safe metadata. The file contains the issued token followed by one newline;
move it directly into the repository secret manager and remove the local copy.
Use a private directory, including on Windows where POSIX mode alone does not
define access control. Tokens and signing secrets are shown once in the console
and cleared when the modal closes or the component disconnects.

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

## Completion subscriptions

Include only the callback actions you need in the principal grant. For example,
`subscription:create`, `subscription:read`, `subscription:update`,
`subscription:delete` and `subscription:secret:rotate`. A human creates the
endpoint with `preloop ci subscribe PRINCIPAL_ID --input callback.json
--secret-file /private/new-signing-secret`. The request is:

```json
{
  "key_id": "00000000-0000-4000-8000-000000000004",
  "url": "https://example.com/preloop-completion"
}
```

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
