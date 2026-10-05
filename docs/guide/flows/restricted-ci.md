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

## Console and CLI setup

Open **Settings → CI identities**. The console uses the common human permissions
API, stays in OSS, and offers only actually dedicated hosted bindings. Preview
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
