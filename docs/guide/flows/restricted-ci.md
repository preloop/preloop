# Native restricted CI setup

Restricted CI credentials are available in OSS for an account's human owner.
They identify a stable machine principal bound to one project and an approved
hosted flow. They are not account keys with scope strings. Existing account keys
remain broader; adding a scope or renaming one never converts it. There is no
OIDC or federation exchange in this version.

## Prepare a dedicated hosted flow

Use an existing GitHub or GitLab tracker and synced project, then create a hosted
review flow as a human administrator through the usual Flows API or console.
The project needs a provider repository identifier and slug. The flow must be
enabled, use `runner_pool: "server"`, and trigger on exactly that project.
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

Do not supply an arbitrary repository URL, a second repository or a runner pool.
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
See [subscription credentials and execution identity](model-routing.md#subscription-credentials-and-execution-identity).

## Read-only native GitHub App review

For a review that must clone source without provider write credentials, set
`git_clone_config.publication_mode` to `"read_only"` on the dedicated flow above.
Select `agent_type: codex`, the supported hosted native executor in this initial
mode. The default `openhands` executor is rejected because
its checkout does not implement this capability. Persistent execution, private
runners, host profiles and harness routing are unsupported in this initial mode.
Keep the repository's existing owning GitHub App tracker ID. A different tracker,
arbitrary URL, or unapproved repository is rejected. Shared flows require the
explicit project-selection mode below. The flow must be
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

Human-owned historical executions retain their human ownership. These CI
permissions do not authorize reading or publishing another execution. Historic
result recovery/publication needs separate human authorization and revision checks. GitLab, PAT and alternate-tracker bindings are not
supported by this mode. Local tests use a synthetic provider; they do not certify
an installation's live GitHub permissions.

## Shared read-only review flows

An administrator can explicitly set `git_clone_config.ci_project_selection: true`
together with `publication_mode: read_only`. List the approved project IDs in
`trigger_project_ids` and exactly one repository entry for each project, using its
owning tracker ID. Repository URLs and duplicate projects are rejected. Each CI
principal is still bound to exactly one project. Admission verifies that project's
current provider identity; launch selects only its repository and mints only its
read lease. Caller payloads cannot select another repository. Removing the
principal's repository invalidates its authority. The selection mode is snapshotted
when provisioning, so changing an existing dedicated identity to shared mode needs
explicit reprovisioning.

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
API, stays in OSS, and offers only currently admissible project/flow bindings. Preview
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
