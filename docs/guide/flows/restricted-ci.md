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

Use an existing GitHub App tracker and synced project for the read-only
examples below (legacy restricted CI also supports GitLab). Create a hosted
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
  "prompt_template": "Inspect the checked-out pull request with shell tools. Do not edit files. Return the final structured result with a substantive review string.",
  "ai_model_id": "<model id>",
  "agent_type": "codex",
  "agent_config": {"native_result": true, "limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
  "runner_pool": "server",
  "allowed_mcp_tools": [],
  "allowed_mcp_servers": [],
  "trigger_project_ids": ["<project id>"],
  "validate_result": true,
  "result_schema": {
    "type": "object",
    "additionalProperties": false,
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

These read-only Codex examples enable native structured results by default.
Their schemas follow the [OpenAI strict-output requirements](repo-review-presets.md#native-codex-structured-results):
every object is closed and every declared property is required. Include `null`
for nullable values such as a finding's absent path or line. A capture-only flow
can explicitly set `agent_config.native_result: false` instead.

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
    "publication_mode": "read_only",
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

### Changing a flow safely

A review flow is often configured by automation while a person may also edit
it in the console. Make every change conditional so neither silently
overwrites the other:

1. `GET /api/v1/flows/{flow_id}` and keep the `ETag` header (the flow's
   configuration revision in quotes, such as `"7"`; also returned as
   `revision`).
2. Send the change with `If-Match: "7"`. On `200` the response's `ETag` is
   the new revision.
3. On `412` nothing was saved. The body's `current_revision` and the `ETag`
   header give the revision now. Read the flow again, reconsider the change
   against what is stored, and resend with the new value.

A `PUT` keeps top-level fields you omit, but a nested object you send
(`git_clone_config`, `agent_config`, `webhook_config`, `result_schema`,
`custom_commands`) replaces the stored one whole, so send the complete object
you read and changed. The shared-flow allowlist,
`PUT /api/v1/ci-identities/shared-flows/{flow_id}`, accepts the same
`If-Match`.

The restricted CI trigger accepts `If-Match` too, so a pipeline can refuse to
run a configuration other than the one it reviewed: a flow at another
revision returns `412` and no execution is created. The execution's status
projection reports the revision it was created with as `flow_revision`. See
[Changing a flow safely through the API](creating-flows.md#changing-a-flow-safely-through-the-api)
for the full contract.

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
execution and operation. Results remain agent-authored. The controller-bound diff below identifies
the input; it is not an attested model command history or custom challenge proof.

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
custom challenges through the flow's own configured review inputs and your
own validator. The controller binds the reviewed repository diff as described
below.

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
  "diff": {
    "base_sha": "<admitted base SHA>",
    "merge_base_sha": "<unique merge base SHA>",
    "head_sha": "<admitted head SHA>",
    "sha256": "<canonical diff SHA-256>",
    "files": ["src/example.py"],
    "bytes": 1234
  },
  "diff_unavailable_reason": null,
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
attestation. The completion callback includes `data.diff.sha256` (or `data.diff: null`);
fetch `GET /api/v1/flows/executions/{id}` or `/result` for the complete receipt.

#### When the receipt appears

The receipt is recorded when the controller sees the agent start, after
clone, checkout verification, the diff identity and setup. An execution is
`RUNNING` from the moment its container starts, so the trigger response and
status reads in the first seconds of a run carry `startup_receipt: null`.
That null is not final. `startup_receipt_status` says which case you have:

| Value | Meaning |
| --- | --- |
| `pending` | The run is not terminal and startup has not been observed yet. Poll again. |
| `recorded` | `startup_receipt` is set, with `checkout_verified` and `setup_completed` true. |
| `failed` | The run ended without a receipt, or setup failure withdrew it. |

Poll the status endpoint until `startup_receipt_status` is `recorded` or
`failed`, or wait for the completion callback. A checkout line in the logs is
not a substitute for the receipt. The field is null for executions without a
restricted CI binding.

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

### Reproduce the reviewed diff

The hosted startup chain computes `diff` after verifying the admitted checkout,
before setup and the agent. The controller accepts its line only before the
agent start marker. Later model output cannot set or replace the receipt.
This identifies the complete diff **of the reviewed repository only**, including
binary changes; additional context repositories are outside this identity.
It does not prove the model read every changed line or found every defect.

`base_sha` is the provider's target commit at admission, `head_sha` is the
admitted PR head, and `merge_base_sha` is their unique merge base. The digest
is SHA-256 of the exact bytes of the command below. There is no whitespace,
newline or encoding normalization. Rename detection and text conversion are
disabled; binaries use Git binary patches, paths use Git quoting, and patches
use full object IDs, Myers diff, three context lines and fixed prefixes.
A temporary bare view shares only the checkout's object database, so mutable
worktree attributes, local configuration and diff drivers cannot change it.
Use the same Git version when comparing across Git implementations/releases.

Run in your checkout, with the two **receipt** revisions and full local history:

```bash
set -euo pipefail
base='<receipt base_sha>'
head='<receipt head_sha>'
export LC_ALL=C GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
export GIT_ATTR_NOSYSTEM=1 GIT_NO_REPLACE_OBJECTS=1 GIT_NO_LAZY_FETCH=1
[[ "$(git rev-parse HEAD)" == "$head" ]]
objects=$(git rev-parse --path-format=absolute --git-path objects)
bare=$(mktemp -d)
trap 'rm -rf "$bare"' EXIT
git init --bare --quiet --object-format="$(git rev-parse --show-object-format)" "$bare"
merge_base=$(GIT_OBJECT_DIRECTORY="$objects" git --git-dir="$bare" merge-base --all "$base" "$head")
# Require exactly one merge base; never choose one from an ambiguous set.
[[ "$merge_base" != *$'\n'* && -n "$merge_base" ]]
GIT_OBJECT_DIRECTORY="$objects" git --git-dir="$bare" \
  -c core.quotePath=true -c core.attributesFile=/dev/null diff \
  --no-ext-diff --no-textconv --no-color --no-renames --binary --full-index \
  --diff-algorithm=myers --no-indent-heuristic --unified=3 \
  --inter-hunk-context=0 --src-prefix=a/ --dst-prefix=b/ --submodule=short \
  --ignore-submodules=none "$merge_base" "$head" -- | sha256sum
```

For `files`, run `git --git-dir="$bare" diff --no-renames --name-only -z
"$merge_base" "$head" --` with the same `GIT_OBJECT_DIRECTORY`. Paths are the
NUL-delimited entries, including both sides of a rename. `bytes` is the byte
length of the patch, before hashing. No credential or network fetch is used.

`diff` is null with `diff_unavailable_reason` when the base is unknown
(`base_unknown`), history/objects are unavailable (`history_unavailable`,
including shallow checkouts), the merge base is ambiguous
(`ambiguous_merge_base`), revisions are invalid (`invalid_revision`), HEAD does
not match (`head_mismatch`), or the path list exceeds the 64 KiB receipt limit
(`receipt_too_large`), or a changed path cannot be represented as UTF-8
(`unsupported_path_encoding`). `not_recorded` identifies older receipts;
`unverified_receipt` means the stored receipt no longer passes its startup or
admitted-revision checks. Missing facts
are never inferred from a model's binding fields. A publisher that requires
diff binding must reject a missing or mismatched digest.

### Phase timings and effective requests

The owned status and result projections include `timings`. Seconds are measured
from stored controller observations; missing boundaries are null, including on
older runs. No prompts, response text, credentials or account IDs are included.

| Field | Evidence and limits |
| --- | --- |
| `queue_wait_seconds` | Execution creation to recorded launch request. |
| `startup_seconds` | Launch request to observed agent start, including container scheduling. |
| `clone_seconds`, `setup_seconds` | Controller observation of startup boundaries; setup includes configured preparation after diff calculation. |
| `first_model_response_seconds` | Seconds from the recorded agent start to the earliest observed upstream response body event. The gateway records the first chunk or Codex event for streams, or receipt of the body for non-streaming calls. Null with `first_model_response_unavailable_reason: not_recorded` when either boundary is missing, including historical requests. Total request duration is never substituted. |
| `model_request_count` | Number of recorded gateway request rows belonging to this execution. |
| `retry_count` | Recorded client resends plus gateway internal retries. Null if any request predates internal retry recording. It does not count unrecorded CLI attempts. |
| `total_model_seconds` | Sum of recorded gateway request durations, including upstream waits, streaming and retries. Concurrent requests can overlap; this is not wall-clock model compute time. |
| `finalization_seconds` | Observed agent completion (or timeout cleanup beginning) to terminal persistence. Later notification and lease cleanup are outside this interval. |
| `first_causal_upstream_error_class` | First classified error in the final uninterrupted sequence of failed requests when the terminal cause is upstream failure; otherwise null. No error message is copied. |
| `effective_requests` | Unique recorded combinations of `model`, `provider`, `reasoning_effort`, `max_output_tokens`, with `request_count`. Null controls mean not recorded or left to the provider default. Values come from request records, never model registration defaults. |

`model` is the resolved outbound gateway model identifier (the LiteLLM route
identifier for translated requests); `provider` identifies the gateway's upstream
service. These do not establish an aggregator's internal provider route or prove
that it honored a parameter. Historical rows lacking the outbound identifier
remain null. Retention gaps and calls still in flight are not counted as
completed request observations.

`terminal_cause` is a stable enum, null while the execution is nonterminal:

| Cause | Meaning |
| --- | --- |
| `completed` | The flow's required success gates passed. |
| `timeout` | The execution deadline expired. |
| `budget` | An execution or default token/turn/spend ceiling stopped the run. |
| `upstream_error` | The stored failure category identifies model authentication, billing, quota, transient or repeated upstream failure. |
| `no_result` | Completion was not confirmed or no result was reported, without a more specific stored cause. |
| `stopped` | The run was stopped/cancelled. |
| `post_result_failure` | A failed flow has a retained result and a separately recorded later failure. |
| `failed` | Another failure, such as setup or schema validation; inspect `failure_category`. |

For a terminal run without an artifact, `/result` keeps its existing 404
contract. Read `timings` from the owned status endpoint to distinguish timeout,
upstream failure and no-result termination.

### Independent completion facts and recovery

A successful `/result` response adds `completion`:

<!-- example: fragment -->
```json
{
  "agent_finished": {"status": "SUCCEEDED", "exit_code": 0},
  "result_captured": {
    "captured": true,
    "source": "native_structured_result",
    "result_sha256": "<digest of the restricted result>"
  },
  "result_valid": true,
  "publication": null,
  "post_result_failures": []
}
```

`agent_finished` is the executor's observed process outcome (including its
wrapper), or null when it was
not recorded. It is independent of overall flow status. Exit zero proves neither
capture nor validity. For native results with schema validation enabled, a
valid verdict confirms completion without a success marker or a `status` field.
Log-text heuristics, including a traceback from a reviewed test, cannot override
a valid native verdict. Actual runtime failure and required later gates still
apply. Controller-added `container_termination` is withheld from the restricted
`result` and listed in `result_withheld_paths`, so it does not break a strict
result schema; process status and exit code live in `completion.agent_finished`.
Both `approve` and `request-changes` mean the review finished; approval is a
separate verdict. Explicit failure/incomplete statuses and required later gates
remain authoritative. `result_captured.source` is `native_structured_result` or
`file`. For historical rows, capture and source are null when only a result
field remains and capture provenance was never recorded. The digest uses the same canonical
JSON as the completion callback and export grant, **after withholding private
members**. `result_valid` is the existing capture-time schema outcome, null
when no schema was checked. Only capture plus a valid strict schema makes the
review usable; neither proves review completeness or authorizes publication.

`publication` is a pointer to the publication package's `platform_review`
projection when present, otherwise null. It never trusts an agent-authored
publication claim. Publication remains separately authorized.

A captured document is committed before bulk artifact transfers and subsequent
finalization. `post_result_failures` names later failures independently:
`checkpoint_upload`, `artifact_quota`, `artifact_capture`, `artifact_storage`,
`log_shipping`, `lease_revocation`, `completion_event_queueing`, `publication`
or `finalization`. Required later steps may still make the flow `FAILED`;
the result remains retrievable. Optional cleanup failures do not rewrite success.
A validated captured result also prevents an automatic repeat of paid model work
just to repair those later steps. Storage must be available for the initial
commit; this is not a guarantee against loss of the database itself.

Recover without rerunning:

1. Re-read the owned `GET /api/v1/flows/executions/{id}/result`. Verify head,
   base, diff digest, result digest, schema validation and withheld paths.
2. Repair the named export/publication/storage problem. For a historical
   human-owned run, use an explicit
   [result export grant](#explicit-export-of-a-completed-human-review) instead of widening
   the CI credential.
3. For an opted-in flow, call the authorized
   [`POST /api/v1/flows/executions/{id}/platform-review`](#publish-a-finished-review-manual-mode-or-again)
   to republish the stored document. Read `platform_review` to confirm the
   posting outcome. This does not rerun the model; current head, validity and
   permission checks still apply.

### Result binding and repository inspection

A schema-valid result is not by itself a review of the admitted change. For
every restricted-CI read-only review that would otherwise succeed, the
controller runs three checks, and runs them again before Preloop posts anything
on the pull request:

- **Result binding.** Result fields named `head_sha`, `base_sha`,
  `merge_base_sha` and `diff_sha256` that your result schema declares, or the
  result contains, must equal the admitted head, the admitted base, the
  computed merge base and the computed diff digest from `startup_receipt`.
  Comparison is exact on the full SHA, ignoring case. An all-zero, abbreviated
  or other commit is a mismatch. A field the schema requires but the result
  omits is a mismatch; an optional field may be omitted. When a bound fact is
  unknown (for example `diff` is null), a result that names it is rejected
  with `<field>_fact_unavailable`.
- **Repository inspection.** At least one successful completed native command
  must read the bound patch or a changed file. The controller and hosted
  continuation use the same command matcher; neither interprets assistant
  prose as evidence. These qualify:
  - `git diff <base>...<head>`; when the bound base is the merge base (the
    usual case), also `git diff <base>..<head>` and `git diff <base> <head>`.
    Revisions may be full SHAs, abbreviations of seven or more hex digits, or
    the `PRELOOP_REVIEW_*` variables below. Options that keep the whole patch
    are allowed: `--find-renames`, `--find-copies`, `-M`, `-C` (with or
    without a threshold), `--unified=<n>`/`-U<n>`, `--no-color`,
    `--no-ext-diff`, `--no-textconv`, `-p`, `--binary`, `--full-index` and the
    diff algorithm options; so is `git --no-pager diff`. A pathspec after `--`
    must be `.` or name changed files.
  - `git show <base-or-head>:<changed-path>`, or `cat`, `head`, `tail`, `nl`
    or `sed -n '<line-range>p'` of a checkout-relative path in the trusted
    diff's file list.

  The read may be wrapped in `bash -c`/`bash -lc` and sit in a chain, as long
  as the exit code shows it succeeded: it comes first or after `;` or `&&`,
  and only `&&` follows it. `cd <dir> && git diff ...` and
  `git diff --stat <base>...<head>; git diff <base>...<head>` qualify;
  `git diff <base>...<head>; echo done` does not, because its exit code is the
  `echo`'s. Literal `HEAD` does not stand for the head SHA: the agent owns the
  checkout and could have moved it. These never qualify: summary options
  (`--stat`, `--name-only`, `--name-status`, `--numstat`, `--quiet`,
  `--check`), options that hide changes (`-w`), another range, a pathspec
  with no changed file, piping the read into another command or redirecting
  its output, `||`, background jobs, subshells, command substitution, control
  flow, writing a file, `export`/`source`/`eval` or variable assignments,
  `git config` changes, revision queries, directory listings, failed commands
  and arbitrary scripts. This is a minimum evidence floor; it does not prove
  complete coverage or correctness.
- **Usable narrative.** `review_degenerate` rejects results whose narrative
  strings are all below three letters/digits, any required free-text field has
  fewer than three letters/digits (including `review: "..."` alongside useful
  findings), or which have no findings
  and only single-word narrative strings (for example `review: "starting"`).
  Revision bindings, status/verdict/severity/result/confidence/priority controls,
  schema enum/constant
  values (including local schema references) and platform metadata are excluded. `No issues found` is accepted.
  Degenerate results are rejected before revision binding evaluation.

When content capture is disabled, Preloop classifies the command before redaction
and retains a receipt tied to the admitted revisions, diff and checkout. It does
not retain the command text. Evidence scans use keyset batches and stop at the
first qualifying read, without imposing a ceiling on review length.

**Bound review context.** Preloop prepends the admitted head SHA, base SHA and
base ref to the prompt for restricted-CI and tracker-triggered read-only reviews.
Hosted checkouts export `PRELOOP_REVIEW_HEAD_SHA`, `PRELOOP_REVIEW_BASE_SHA` and
`PRELOOP_REVIEW_BASE_REF`. These are controller-bound values, independent of
which template fields the flow uses. The hosted checkout fetches a missing
admitted base before removing clone credentials and verifies that
`git diff "$PRELOOP_REVIEW_BASE_SHA"..."$PRELOOP_REVIEW_HEAD_SHA"` works.
Result-field instructions follow the schema and configured bindings, including
custom field names; they never request extra fields. Use the admitted base in
`base_sha` when declared; it is distinct from `merge_base_sha`.

**Inspection continuation.** With `agent_config.native_result: true`, the
Codex CLI ends a session when the model answers without a tool call and captures
that answer as the result. If the session exits successfully without a qualifying
read, Preloop starts up to two fresh sessions in the same container. Each gets
the same prompt/schema and a short instruction naming the bound revisions,
the command that qualifies (`git diff <base>...<head>` on its own), the forms
that do not (summary options, `HEAD`, pipes, redirections, a diff followed by
`;`), and describing a usable review. The
`PRELOOP_REVIEW_CONTINUATION` log records each attempt. Existing timeout and
model budgets apply; no token, turn or cost ceiling is added. The continuation
is off with `review_integrity.require_inspection: false` or
`sandbox_type: read-only` (no shell).

**Result repair continuation.** Some upstreams get JSON mode with the schema
written into the instructions instead of an enforced schema, so a model can
finish a real review and still leave out a required field, use a value outside
an enum, use the wrong type, or answer with something that is not JSON. When
the final native answer does not match the result schema, Preloop asks again
instead of failing at once. It starts up to two more fresh sessions in the same
container. Each gets the same prompt and schema, the list of validation errors
(JSON path and message, for example `$.findings: 'findings' is a required
property`) and the previous answer, and is asked for the same review again as
one complete object. The inspection already done still counts, so a repair
session does not have to read the change again. A repair only runs when the
session before it passed the inspection check; it never replaces an inspection
continuation. No repair starts with less than 30 seconds of the execution
deadline left, and a repair session is stopped before the deadline. The
`PRELOOP_REVIEW_CONTINUATION` log records each repair with
`"reason":"result_schema_invalid"`, its attempt number and the failing paths
(no values). Preloop never edits the answer itself: the controller validates
whatever the last session wrote and applies every check above. If it is still
invalid, the run fails with `result_schema_invalid` and the validation errors,
as before. Flows without `agent_config.native_result: true` are unchanged.

The wrapper asks the controller's Draft 2020-12 validator for this decision,
including the same failing paths and messages used at final capture. Conditional
rules (`if`/`then`/`else`), composition (`allOf`, `anyOf`, `oneOf`, `not`), `const`,
and constraints on nested objects and array items therefore trigger repair too.
Saving a flow retains the existing Draft 2020-12 schema contract: keywords such
as `dependentSchemas`, `prefixItems`, and `unevaluatedProperties` are evaluated
by the controller for both repair and final capture. Unknown extension keywords
and annotation-only `format` retain that validator's behavior in both places.
If the validation service cannot be reached, the log records
`PRELOOP_RESULT_VALIDATION_UNAVAILABLE`; final capture still validates the answer
and never accepts it on the strength of an unavailable repair check.
Local failures in reading or preparing the repair prompt instead record
`PRELOOP_RESULT_REPAIR_CHECK_FAILED` with the exception type and stack frames,
without exception values or answer contents. Validation requests have a shared
per-capability budget; see [rate limiting](../../operations/rate-limiting.md).

**A fenced answer.** A model that is not held to the schema sometimes writes
its final answer as the JSON document inside a Markdown code fence
(```` ```json ````, the document, ```` ``` ````). Preloop removes exactly that
one wrapper: after trimming whitespace, the whole answer must be a single fenced
block, with no tag or a `json` tag in any case, and its body must parse as one
JSON value. Nothing else is accepted: no JSON taken out of surrounding prose,
no second block, no text after the fence, no repair of invalid JSON. The fence
carries no content, and the unwrapped document goes through the same schema,
result binding, inspection and narrative checks; a fenced answer that is not
valid JSON inside still fails as before. The in-container repair check and the
controller use the same reader, so a fenced valid review needs no repair
session. The run records `answer_unwrapped: "markdown_fence"` in
`completion.result_captured` and a `native_answer_unwrapped` log milestone.
When an answer is still not JSON, the repair prompt tells the model that its
answer was wrapped in a fence or had text outside the JSON, and to reply with
the JSON object only.

One run is not covered by either rule above: the inspection continuations are
used up, the last session still did not read the change, and its final answer
is not usable either, because it is missing, not JSON or does not match the
schema. The usual case is a session that stops on a progress note such as
"fetching the diff before review". Such a run gets one combined continuation,
at most once per run. Its prompt is the original prompt plus three things in
plain words: the review is not complete; the repository has not been inspected,
with what the check requires (a successful command in that session that reads
the bound change, `git diff <base>...<head>` and the changed files); and the
validation errors with the previous answer. The session has to inspect and
answer. The log records it as
`PRELOOP_REVIEW_CONTINUATION {"attempt":1,"reason":"uninspected_invalid_result","paths":[...]}`.
The same deadline rules apply: it does not start with less than 30 seconds
left and is stopped 10 seconds before the deadline. After it the normal order
applies again. If that session inspected and its answer is still invalid, the
repairs above run; if it did not inspect, nothing more is started. A session
that did not inspect but answered in the schema's shape gets no combined
continuation. A run therefore starts at most five sessions after its first:
two inspection continuations, one combined continuation and two repairs.

None of this changes what the controller decides. A review without inspection
evidence fails with `review_not_inspected`, whatever its answer looks like. An
inspected run whose last answer fails the schema fails with
`result_schema_invalid`; the message names the first error and the other
failing paths. This also holds when that answer carries no completion status,
as a progress note does: such a run used to be reported as `no_confirmation`
with a message saying that no result had been written. A run that does end as
`no_confirmation` now says what was observed, for example that no final answer
was captured. A final answer that exists but is not valid JSON, or not a JSON
object, fails with `result_schema_invalid` and that reason, not with
`no_confirmation`. When the model provider rejects the request before any answer exists,
the recorded validation says that the model request failed and points to the
provider error, instead of `No result.json was captured`.

**Result ready when the time limit fires.** If the execution deadline expires
after the agent has already written its native answer (for example because the
container did not exit in time), Preloop captures that answer and applies the
same checks it applies to a review that survives a repeated-tool-loop stop: the
result schema, result binding, repository inspection and usable narrative, then
the normal finalization gates. If all of them pass, the review is finalized as
usual and the completion facts record `time_limit_after_result: true`. If any
check fails, or there is no answer, the run still ends with `timeout`.

**Native completion.** Native-result prompts ask for a final answer in the
required format, which ends the run. They do not append file-writing or sentinel
instructions. For read-only reviews the CLI captures the final answer separately from an agent-written
`/workspace/result.json`. A schema-valid native answer that passes review integrity
wins regardless of write order. If it is absent or unusable, Preloop accepts the
file only when it passes the same schema, revision, narrative and inspection
checks. An explicit schema-valid native failure remains authoritative. If neither
candidate is usable, ordinary validation fails the run. A verified completed
review in the file also survives a subsequent repeated-tool-loop stop or an
execution timeout (see above); user stops are unchanged.

**Native format compatibility.** For Codex CLI requests identified by the
`codex_exec/` or `codex_cli_rs/` user agent, with tools and an output format,
the gateway withholds the upstream format until the supplied conversation
contains a tool result. It applies across gateway Responses transports and
chat translation. Requests with `tool_choice: none` or `previous_response_id`
retain their format, because tools are disabled or history is held upstream.
Other clients and requests without tools retain their format. Final result
schema validation still applies. DeepSeek routes, including OpenRouter and
other aggregators with a `deepseek` model namespace, use JSON-object mode
with the schema in instructions once formatting resumes. DeepSeek reasoning
continuity also follows the family through these aggregators.

A run that fails a check ends `FAILED` with `failure_category`
`review_binding_mismatch`, `review_not_inspected` or `review_degenerate`. For non-degenerate results with no
repository inspection, the category is `review_not_inspected` even if its
ids also mismatch: ids in a result that never read the change are a symptom,
not the cause. The document is still
stored. The first terminal status event reflects these final checks, rather
than the provisional agent exit. Results include a controller-owned `_review_integrity` member (withheld from the
restricted `result`), and `/result` reports the outcome without values in
`completion.review_integrity`. Platform posting additionally requires a
result field bound to `head_sha` that matches (publication reason
`review_binding_mismatch` otherwise), so a posted verdict always names the commit it reviewed; a run
whose schema declares `head_sha` as optional still succeeds without it, but
is not posted:

<!-- example: fragment -->
```json
{
  "status": "rejected",
  "category": "review_not_inspected",
  "reasons": ["base_sha_mismatch", "head_sha_mismatch", "no_repository_inspection"],
  "bindings": [
    {"field": "base_sha", "fact": "base_sha", "outcome": "mismatch"},
    {"field": "head_sha", "fact": "head_sha", "outcome": "mismatch"}
  ],
  "inspection": {
    "required": true,
    "inspected": false,
    "repository_tool_calls": 0,
    "other_tool_calls": 0,
    "native_commands": 0,
    "diff_commands": 0
  }
}
```

Binding, inspection and narrative checks are on by default. To bind other field names, or to turn the
inspection floor off for a flow, set `git_clone_config.review_integrity`:

<!-- example: fragment -->
```json
{
  "review_integrity": {
    "result_bindings": {"reviewed_commit": "head_sha", "diff_digest": "diff_sha256"},
    "require_inspection": true
  }
}
```

`result_bindings` replaces the default field names; every field it lists is
required. Facts are `head_sha`, `base_sha`, `merge_base_sha` and
`diff_sha256`. An empty map binds nothing. `require_inspection: false` keeps
the binding check but accepts a review with no tool evidence; do not use it
with platform posting unless your prompt supplies everything the review
needs. A flow whose model writes a different value into a field with one of
the default names (for example the target branch tip in `base_sha` after the
base moved) now fails loudly; rename the field or map it explicitly.

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
need approval. Hosted Codex runs install the Preloop permission hook, so deny
and approval rules apply to their shell commands. With `sandbox_type:
read-only`, Codex has no shell tool at all. A hosted Codex run that has such
a rule but cannot install the hook is refused before Codex starts with
`approval_unavailable`.
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
credential and account storage-redaction rules still apply.

Text is stored exactly, control characters included: form feeds in text
converted from a PDF, ESC colour sequences in command output, and a U+0003 or
U+0004 that an agent sends to a terminal. The record is stored as canonical
JSON text, which escapes every C0 control (U+0000 too); DEL and C1 controls are
valid JSON as they are. A record whose only unusual content is such characters
has `capture_complete: true`, `storage_redacted: false`, and its digest covers
the exact text. Call arguments are kept as the agent sent them. They are
decoded and re-encoded only when a masking rule changes a nested value.

Lone surrogates (U+D800 to U+DFFF without a partner) have no UTF-8 encoding,
so they cannot be stored, hashed or returned. They are removed. The
observation then carries `removed: [{"category": "lone_surrogates", "count":
<n>}]` (category and count, never content), sets `redacted` and
`storage_redacted`, and the record is incomplete with reason
`lone_surrogates`. A lone surrogate outside an observation value, in an id
for example, is counted in a document-level `removed`. A surrogate written as
a `\udXXX` escape inside JSON argument text is six ASCII characters and is
kept as is.

`incomplete_reasons` names every reason a record is incomplete:

| Reason | Meaning |
| --- | --- |
| `masking` | A named masking rule fired (`redactions` lists the rules). |
| `lone_surrogates` | Lone surrogates were removed (`removed` counts them). |
| `credential_scrub` | The known-token credential scrubber masked text no named rule matched. |
| `storage_rule` | An account sensitive-data storage rule changed the record. |
| `truncated` | An observation did not fit the 2 MiB record and was left out. |
| `capped` | More than 512 observations; the rest were left out. |
| `scan_depth` | The message nesting was deeper than the capture walks. |
| `value_depth` | An observation value (call arguments included) nested deeper than 64 levels. Its `value` is null, because masking cannot walk it. |
| `missing_id` | A call or result had no usable id. |
| `invalid_name` | A tool name was not a string or longer than 256 characters. |
| `unsafe_text` | The stored value differs from the wire value and nothing above explains it. No known path sets it; it is kept so that an unexplained change is still refused. |

Records stored before these reasons existed are not rewritten. One that says
`unsafe_text` from that time usually lost control characters.

Text redaction uses ruleset `2026-11-19`. Every new document stores that
version in `redaction_ruleset`. An observation that matched a rule also stores
`redactions`, a list of `{"rule": "<category>", "count": <n>}`. The category is
a fixed name. It is not the matched text and not a hash of it. Documents
already stored do not gain these fields; nothing rewrites them.

A keyword rule masks `api_key`, `token`, `secret`, or `password` only when that
word is the whole key or the key ends with it (`github_token`, `client_secret`),
the `=` or `:` stays on the same line, and the value is a literal that looks
like a credential. It does not mask a permission or boolean word (`write`,
`read`, `none`, `true`, `false`, `null`), an empty value, a call, an attribute
or subscript, a `$VAR` / `${VAR}` / `${{ ... }}` reference, a bare variable
name, a type annotation, or a string joined with `+`. A quote that closes
the string containing the key is left, as in
`'https://x-access-token:' + token`, and so is a value whose text is an
operator and an identifier or dotted name (`obj.attr`, `self.token`). A value that starts with a placeholder
(`{name}`, `{{ name }}`, `%s`, `%(name)s`, `<name>`) is left. A quoted
literal is masked, including a short password such as `"hunter2"` or
`"changeme"`. A bare `password: hunter2` or `secret: hunter2` is masked
because the value has a letter and a digit; that also masks
`password = sha256`, which has the same shape. A bare `token: hunter2` or
`api_key: hunter2` is left as written, because a short unquoted word after
`token` is usually a name. An unquoted single-case word with no digit, such
as `password: changeme`, is left as written, unless the key is upper case
and glued to `=` with no spaces (`PASSWORD=opensesame`,
`DB_PASSWORD=correcthorsebatterystaple`). A bare hex value of 24 or more
characters is masked (`token=deadbeefcafebabedeadbeefcafebabe`). A call such
as `connect(user=user, password=password)` is left. A longer bare literal is
masked from 16 characters when it mixes two of lowercase, uppercase, and
digits, or from 20 characters when it contains a digit.

Other rules do not need that keyword. They mask a PEM private key, a bearer
token, an `sk-` key (including `sk-proj-` and `sk-ant-`), a GitHub
`ghp_` / `gho_` / `ghu_` / `ghs_` / `ghr_` token, a GitHub fine-grained
`github_pat_`, a GitLab `glpat-`, a JWT (`eyJ` plus two further segments), an
AWS access key id (`AKIA`, `ASIA`, `AIDA`, `AROA`), a Slack
`xoxb` / `xoxa` / `xoxp` / `xoxr` / `xoxs` token, a Preloop `preloop_`, `ci_`,
`agt_`, `enr_`, or `whsec_` key, and a Preloop `flow_` run token. The category
names are `private_key`, `authorization_bearer`, `bearer_token`,
`keyword_assignment`, `sk_key`, `github_token`, `github_fine_grained_pat`,
`gitlab_pat`, `jwt`, `aws_access_key_id`, `slack_token`, `preloop_key`,
`preloop_run_token`, and `sensitive_field` (a structured key such as
`password` whose whole value is replaced).

On a gateway tool observation, a real match still sets `redacted`,
`storage_redacted`, and `capture_complete: false`. Ordinary source that
matches none of these stays byte for byte, with `capture_complete: true`
and `storage_redacted: false`. A native command observation records the
same rule facts when its text matches. Its completeness stays the size
check, plus the reasons above.

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

`preloop ci retire` ships with CLI 1.0.0. An older CLI answers `unknown
command "retire"`: upgrade it, or call
`POST /api/v1/ci-identities/{id}/retire` with a human API token.

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
will read, and `diff.sha256` for the controller-bound reviewed diff (null when
unavailable).

### Result readiness

Preloop commits a captured result before later artifact/finalization work.
A stop transaction durably queues `flow.execution.finished` even when capture
is still pending. The delivery worker waits for capture to settle without
consuming retries, then refreshes the envelope from stored state. A stopped
launch settles immediately. After a controller claim expires, a confirmed
runtime stop also permits settlement; an unconfirmed stop remains pending,
without guessing that no result exists. A completion event therefore never
arrives before its result:
`result_ready: true` means `GET /api/v1/flows/executions/{id}/result` returns
it now. `GET .../result` answers:

- `200` with the result;
- `409` with `detail.code: "result_pending"` and the current `detail.status`
  while the execution has not finished, even if a checkpoint is stored, for
  example while it is parked. It also answers 409 if a stop/limit has already
  made the status terminal but controller capture or finalization is still
  pending; retry until finalization settles;
- `404` when the execution does not exist for the caller, or finished without
  reporting a result and capture has settled. Controller-only termination
  metadata or private publication policy state is not a result artifact;
  historical PR/publication outputs remain
  readable without a schema or capture provenance. A settled 404 does not change
  merely because delayed artifact capture finishes; that interval uses 409.

## Review and status posted by Preloop

A read-only review flow can have Preloop itself post the result: one pull
request review and one commit status on the commit that was reviewed. The run
stays read-only. The container, the model and your CI job never hold a GitHub
write token, and you need no publisher workflow. Design and threat model:
[Review and status posted by Preloop](../../architecture/platform-posted-review.md).

It is off by default. Turn it on with `git_clone_config.platform_review` on a
flow that has `publication_mode: "read_only"`, `validate_result: true` and a
`result_schema`. It works with and without `ci_project_selection`. Enabling it
needs no new CI identities.

### No CI integration

A GitHub App tracker can start the same read-only review when a pull request
is opened, updated with new commits, or reopened. No CI identity and no
workflow are required. The run stays read-only: Preloop posts the review
and the commit status after the run finishes.

```yaml
publication_mode: read_only
platform_review:
  enabled: true
trigger_event_source: <GitHub App tracker id>
trigger_event_types:
  - pull_request_opened
  - pull_request_updated
  - pull_request_reopened
trigger_project_ids:
  - <project id>
webhook_config:
  supersede_on_update: true
```

The GitHub App needs **Pull requests: Read and write**. It also needs
**Commit statuses: Read and write** when a status is set. The admission
read of the pull request uses the installation token and needs **Pull
requests: Read**. A failure to mint that token is retried. Ready for
review is `pull_request_ready_for_review`. `trigger_config: {draft: false}`
skips drafts. Fork pull requests are not reviewed by default. A maintainer
of the base repository can opt a fork pull request in by applying the
`preloop-review` label. Preloop reads the label application and the actor's
current repository permission from GitHub with the tracker's installation
token; write, maintain or admin is required. Triage and read do not qualify.

For an opted-in fork, applying `preloop-review` starts a review. The label
must be the label changed by that event. GitHub label actions currently
match `pull_request_opened`, so select that trigger type to review when the
label is applied. The flow's project and trigger filters still apply. While
the label remains present, the selected opened, synchronize, reopened and
ready-for-review actions also start reviews. New commits need no new label
application. This lets an outside contributor cause model spend on the
account and a review posted by the App by pushing new commits to that pull
request. The existing flow limits and account controls still apply.

The label application and the actor's current permission are checked for
each run, again at launch, and immediately before each review and status
write. Removing the label stops
a pending run at launch or skips its pending post. Removing and reapplying
the label starts a new application; an older run cannot use that new
application to publish. A same-repository pull request behaves as before,
with or without the label; its label actions do not start reviews.
Restricted CI continues to refuse forks.

Fork reads use a `contents: read` lease on the base repository, fetch
`pull/{number}/head` there, and verify the admitted head SHA. The publish
lease, review and commit status also target the base repository only.
A pull request retargeted onto another branch is not posted; the status
stays on the reviewed commit. The base commit advancing on the same branch
does not skip the review.

The console and the trigger API do not start it. Retries do not post.
Manual mode is the same publish call, made by an account user who may edit
flows. How many runs one pull request can start is bounded by
`trigger_project_ids`, `trigger_config` (including `draft: false` and
`labels`), `agent_config.limits`, the account kill switch, one active run
per pull request, and `supersede_on_update`. On a flow that sets
`platform_review.project_ids`, only an event for a listed project starts a
run; see [Post for some of a shared flow's projects](#post-for-some-of-a-shared-flows-projects).
There is no separate rate limit for this route. The threat model is in the [platform-posted review
architecture page](../../architecture/platform-posted-review.md).

### Posting modes

| Mode | Stored as | What is posted |
| --- | --- | --- |
| Off | `"enabled": false`, or no `platform_review` block | Nothing. The publish call answers 409 `platform_review_disabled`. |
| Automatic | `"enabled": true` (with `"mode": "automatic"` or no `mode`) | The review and status are posted when each run finishes, and retried by Preloop after a transient failure. The publish call can also post. |
| Manual | `"enabled": true, "mode": "manual"` | Nothing when a run finishes and no automatic retry. Only the [publish call](#publish-a-finished-review-manual-mode-or-again) posts, so your own controller decides, per run, whether the review goes out. |

A block saved before the mode existed has no `mode` and stays automatic.
Manual mode is the one to use when your CI validates each result itself and
posts only what it approved: send the publish call with
[preconditions](#preconditions) so Preloop posts exactly the result, commit
and flow configuration your controller checked.

### Turn it on in the console

Open the flow. Read-only review flows show a **Post review on the pull
request** card under the publication policy:

1. **What it needs** lists the prerequisites as checks: the flow is a
   read-only review, result validation is on, the result schema has a
   required `verdict` with fixed values, the flow names its repositories,
   and every repository is connected through a GitHub App (not a personal
   access token, not GitLab). A failed check states the exact reason. The
   GitHub App permissions (**Pull requests: Read and write**, **Commit
   statuses: Read and write**) cannot be read from Preloop, so the card asks
   you to confirm them in GitHub.
2. Under **When to post**, choose **When the run finishes** (automatic) or
   **Only when requested** (manual: nothing is posted until the publish call
   is made). **Off** turns posting off. The badge on the card shows the saved
   mode.
3. On a flow with more than one project, choose under **Repositories to
   post for** between **Every repository of the flow** (no `project_ids`)
   and **Only selected repositories**, then tick the repositories. Preloop
   posts only for those; the others keep running and keep their results,
   and on a flow with pull request event triggers an event starts a review
   only for them. A repository added to the flow later is not selected.
   With none ticked nothing is posted, and the card says so. An id that is
   no longer one of the flow's projects is flagged and dropped on the next
   save. Turning posting off clears the list.
4. Under **Tool evidence**, choose **Any** (post whether or not the run's
   tool evidence is complete), **Captured** (every model request has an
   evidence record, and the only incomplete records are masked credentials)
   or **Complete** (no record is incomplete, including masked credentials).
5. To set a commit status, tick **Set a commit status for each verdict** and
   choose Success, Failure or Error for every verdict of your schema. Keep
   the status name (`preloop/review` by default) stable if a branch
   protection rule requires it. Optionally choose what to set when a run
   stops, fails or returns no valid result.
6. **Save**. The card refuses a save the server would refuse and says why;
   a refusal from the server is shown as it was returned.

Users who may not edit flows see the card read-only.

### Turn it on with the CLI

```sh
preloop flow review-posting show pull-request-reviewer
preloop flow review-posting enable pull-request-reviewer \
  --state pass=success --state fail=failure --incomplete-state error \
  --evidence captured
preloop flow review-posting enable pull-request-reviewer --no-status   # review only
preloop flow review-posting set-states pull-request-reviewer --state fail=error
preloop flow review-posting set-states pull-request-reviewer --status-context ci/review
preloop flow review-posting disable pull-request-reviewer              # keeps the mapping
```

`show` prints whether posting is on, the evidence level (`any` when the
key is absent), the verdict mapping and the prerequisites it can read from
the flow. `--evidence` is `any`, `captured` or `complete` on `enable` and
`set-states`. `enable` and `set-states` refuse,
before saving, a verdict that is not in the schema or a schema verdict
without a state. Each write changes only `git_clone_config.platform_review`.
Every command takes `--json`. The commands use your saved login or
`PRELOOP_TOKEN`.

### The same in JSON

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
| `mode` | `automatic` (default) or `manual`; see [Posting modes](#posting-modes). |
| `enabled_at` | Read-only. Set by Preloop when posting is turned on; runs that finished before it are not published automatically (the publish call below still can). |
| `verdict_actions` | Optional map from verdict to `comment`, `approve` or `request_changes`. Without an entry the review is a comment. An approval is never implied. |
| `status` | Optional. Omit it to post the review only. |
| `status.context` | Status name, default `preloop/review`. Keep it stable so a branch protection rule can require it. |
| `status.verdict_states` | `success`, `failure` or `error` for every verdict your schema admits. Saving fails if one is missing. |
| `status.incomplete_state` | Optional `error` or `failure`, set with a neutral description when the run stopped, failed or returned no valid result, so a required status is not left waiting. Without it nothing is posted for such runs. |
| `evidence` | Optional. Which stored tool evidence may be posted. See [Tool evidence](#tool-evidence). |
| `project_ids` | Optional. The flow's projects Preloop posts for, each in `trigger_project_ids` and listed once. Omit it (or `null`) for every project; `[]` posts for none. It also decides which projects a pull request event starts a review for. Only valid while `enabled` is true. See [Post for some of a shared flow's projects](#post-for-some-of-a-shared-flows-projects). |

### What your result must contain

`verdict` is the one required field: a required string with an `enum`.
Everything else below is posted when the validated result carries it. Your
schema decides which of them are required. Nothing a finding holds is left
out of the review without the review saying so.

- **Summary.** `summary`, or if that is absent `review`, or else `overview`.
- **Findings.** A list of objects. For each object:
  - **Severity** is `severity`, shown in the heading.
  - **Title** is `title`. When a finding has no title, the heading is the
    number, the severity and the location. The word "Finding" is not added.
  - **Text** is the first non-empty string among `body`, `description`,
    `message`, `detail`, `problem`, `issue`, `finding` and `summary`, in
    that order.
  - **Location** is `path` or `file`, together with `line`. A line may be a
    number or a decimal string such as `"17"`. When there is no path, a
    string `location` or `where` is read: `path`, `path:LINE`,
    `path:LINE-LINE` or `path#LLINE`. The path and the first line are used.
    Anything else is shown as code and is not an inline comment.
  - **Every other field** is a labelled block under the text, in the order
    the result schema declares the properties (or the order in the result
    when the schema does not name them). A string, number or boolean is
    shown as text. A list of strings is shown as a list. Anything else is
    counted ("2 fields not shown") and not rendered. A value cut for length
    says "text shortened". The label is the field name, shown as text, so a
    name the schema allows cannot become markup.
- **Checked.** `checked` or `checks`, a list of short strings.

`preloop flow review-posting show` and the **Post review on the pull
request** card list the finding fields your schema declares and how each
one appears (severity, title, text, location or a labelled block). When the
schema declares findings and none of those fields is a text field, the flow's
`config_warnings` says so. The flow still saves.

A finding becomes an inline comment only when its path and line are in the
pull request's diff. The comment carries the same blocks as the body. All
text from the result is shown as literal text: it cannot mention a user,
link anywhere, reference or close an issue, or carry HTML. File paths are
shown as code. When the review is cut for size it says how many findings
are not shown and points at the execution, which holds the full result.

One result in the original field names:

<!-- example: fragment -->
```json
{
  "verdict": "fail",
  "summary": "One problem in the parser.",
  "findings": [
    {
      "severity": "high",
      "title": "Unchecked length",
      "body": "The length is read before it is validated.",
      "path": "src/parser.py",
      "line": 12
    }
  ]
}
```

The review GitHub shows for that result (the hidden marker is omitted):

```text
### Preloop review: fail

Reviewed commit `<sha>`. This review covers that commit only.

**Summary**

> One problem in the parser.

**Findings (1)**

**1. [high] Unchecked length** (`src/parser.py:12`)

> The length is read before it is validated.

Execution: <link to the execution>

Posted by Preloop from the validated result of a read-only review run.
```

The same review from `problem`, `evidence`, `fix` and `location`. There is
no `title` and no `summary`; the narrative is `review`:

<!-- example: fragment -->
```json
{
  "verdict": "changes_requested",
  "review": "Reserved stock can still be sold.",
  "findings": [
    {
      "severity": "MEDIUM",
      "problem": "A sale ignores reservations.",
      "evidence": "The count is written with no reservation check.",
      "fix": "Refuse the sale when reserved units cover it.",
      "location": "shop/inventory.py:17"
    }
  ]
}
```

```text
### Preloop review: changes_requested

Reviewed commit `<sha>`. This review covers that commit only.

**Summary**

> Reserved stock can still be sold.

**Findings (1)**

**1. [MEDIUM]** (`shop/inventory.py:17`)

> A sale ignores reservations.

**evidence**

> The count is written with no reservation check.

**fix**

> Refuse the sale when reserved units cover it.

Execution: <link to the execution>

Posted by Preloop from the validated result of a read-only review run.
```

With line 17 in the pull request diff, that finding is also one inline
comment with the same problem, evidence and fix.

What gets posted:

- A review with the verdict, the summary, every finding as above, what was
  checked, the reviewed commit and a link to the execution.
- A commit status on the reviewed SHA with the state mapped from the verdict.
- Nothing from the result chooses the target. Repository, pull request and
  commit are the ones Preloop bound when it accepted the trigger
  (`pr_number` and `head_sha`).
- If the pull request head moved while the review ran, the status is still
  set on the reviewed commit and the review is not posted
  (`review_reason: "head_moved"`). Trigger a review of the new head.
- A run that stopped, failed or returned an invalid result never posts a
  verdict.

### Tool evidence

`platform_review.evidence` decides whether the stored tool evidence of the
run is complete enough to post. It is checked when the run finishes, when
the sweep retries, and on the publish call, from the records stored at that
moment.

| Value | What may be posted |
| --- | --- |
| absent or `any` | Today's behaviour. The records are not consulted. |
| `captured` | Every model (gateway) request of the run has an evidence record, no record is a capture failure, and no record is incomplete for a reason other than a masked credential. A record that is incomplete only because a named masking rule fired is accepted. An older incomplete record that does not say why is not. |
| `complete` | The same as `captured`, and no record is incomplete for any reason, including a masked credential. |

A masked credential still marks that gateway record incomplete. `captured`
accepts that record; `complete` does not. `captured` and `complete` both
refuse a record changed by the credential scrubber (`credential_scrub`), by
an account storage-redaction rule (`storage_rule`), by removed lone
surrogates (`lone_surrogates`) or by an unexplained change (`unsafe_text`);
control characters alone do not make a record incomplete; a record masked only by named rules is still
accepted by `captured` and refused by `complete`, and level `any` or a flow
without the setting is unchanged. A native command the credential scrubber
changed is stored with `capture_complete: false` and reason
`credential_scrub`, the same as a gateway record. Saving `captured` or `complete`
requires model-gateway content capture to be on for the instance
(`MODEL_GATEWAY_CAPTURE_CONTENT`). Without it the setting is rejected,
because the gate could never pass. That check is only at save. Turning
content capture off later does not by itself reject a publish: a record
stored while capture was on can still meet the gate, and a record stored
with content capture off does not.

When the gate refuses, no review is posted. The publication is recorded with
`reason: "evidence_incomplete"` and the counts below. The commit status, when
the flow sets one, is the flow's `incomplete_state`, or `error` when the flow
sets a status and no incomplete state: never the verdict's state.

The last streaming model request can still be writing its evidence after the
run is marked finished. For about 30 seconds after the run finishes, a flow
with `captured` or `complete` (or a publish call that asks for one of those)
neither posts nor records `evidence_incomplete`. An automatic post wakes at
that deadline on a fresh database session, so finish-to-post time is normally
30 seconds plus the code host's write and verification time. The settle
window is unchanged. An explicit publish call answers **409** with
`detail.code` `evidence_pending` during the window.

While the run has no publication attempt, its record reads `status: "pending"`
with `reason: "evidence_settling"` and `due_at`, the settle deadline. After
that deadline it reads `reason: "awaiting_sweep"` with `due_at` set to the
settle deadline plus the configured execution-monitor interval (60 seconds
by default). This is the fallback pickup deadline if a restart loses the
timer: normally within 90 seconds of finish, plus publication time. The sweep
and timer share the exclusive publication claim and receipts. A delayed or
backlogged monitor can miss that deadline; the timestamp stays in the past
so the overdue wait is visible. Manual mode instead reads
`reason: "manual_publish_required"` once evidence has settled, with no
automatic deadline. Runs finished before posting was enabled, or outside
its 24-hour recovery lookback, read `explicit_publish_required` with no
automatic deadline; they can still be published through the explicit call.

`GET /api/v1/flows/executions/{id}/result` carries the counts once, both at
the top level (`tool_evidence`) and on the publication record
(`platform_review.tool_evidence`). The execution an operator opens carries
them on its publication record too. The execution list, a CI status poll
and the `flow.execution.finished` event do not: those reads must not decode
every evidence record. A CI identity that can read its own result can read
the counts. They contain no observation values, paths or text:

| Field | Meaning |
| --- | --- |
| `gateway_requests` | Model requests recorded for the run. |
| `missing_records` | Model requests with no evidence record. |
| `records` | Stored evidence records, model requests and native commands. |
| `incomplete_records` | Records that are incomplete, unreadable, a capture failure, or stored with content capture off. |
| `masking_only_records` | Incomplete records whose only stored reason is a named masking rule. |
| `masking_by_rule` | How many times each masking rule fired. Rule ids only. |
| `incomplete_by_reason` | Incomplete records by reason: one of the reasons in [Own-execution tool evidence](#own-execution-tool-evidence), or `integrity_invalid` (unreadable or the digest does not verify), `capture_failed`, `content_capture_off`, or `unstated` (an older record marked incomplete without a reason). A record with two reasons counts under both. |
| `removed_by_category` | Characters removed because they cannot be stored, by category (`lone_surrogates`). Counts only. |
| `agent_clipped_results` | Tool results the agent CLI cut to its per-result limit before the model saw them. |
| `bound_reads` | Successful commands that print the bound patch or a changed file, by the inspection floor's own matcher, joined to their result by call id. |
| `clipped_bound_reads` | Of `bound_reads`, those whose result was clipped. |
| `unknown_bound_reads` | Shell calls that may be bound reads but could not be joined to a result with an exit code (a session still running, a different working directory, a non-`exec_command` shell tool, or arguments not stored). |

#### Tool output limit and clipped results

Hosted Codex cuts each tool result to a per-result limit before the model
sees it, keeping the start and end, and writes
`Warning: truncated output (original token count: N)` at the top of the
result. For a model Codex does not know, Preloop writes the limit into the
model catalog as one tenth of the model's context window in tokens, at most
64,000: 3,200 tokens for a 32K window, 12,800 for 128K, 20,000 for 200K,
64,000 for 1M, and 27,200 when no window is registered (Codex's 272K
fallback). Models Codex ships keep Codex's own 10,000-token profile. A call
can only lower the limit: Codex uses the smaller of the catalog limit and
the call's `max_output_tokens`, 10,000 when the model sends none. The
read-only review preamble states the limit and tells the reviewer to read a
long patch per file or in line ranges.

A gateway result observation that Codex cut carries `agent_clipped: true`
and `agent_original_tokens`, the count Codex reported; every other result
carries `agent_clipped: false`. Only Codex's warning at the position Codex
writes it counts, so a file that quotes the sentence is not a clip. This is
separate from `truncated`, which still means only that Preloop's own
storage limit dropped data. A clipped result is a whole record of what the
model saw: it keeps `capture_complete: true` and does not affect
`platform_review.evidence`.

The inspection floor accepts a successful command that prints the bound
patch or a changed file. It is not proof of coverage: a read whose output
was clipped still satisfies it. To check that the reviewer saw every such
read whole, read `tool_evidence` from the result and require
`bound_reads > 0`, `clipped_bound_reads == 0` and
`unknown_bound_reads == 0`. Preloop does not refuse publication for a
clipped read; a controller that wants that can make the check itself before
calling publish in manual mode.

On the publish call, `expected_evidence` of `captured` or `complete` is a
precondition. It can only tighten the flow setting: `captured` does not post
a run whose flow requires `complete`. A mismatch answers **412**
`review_precondition_failed`, with `source: "tool_evidence"`, and posts
nothing. `any` is not a precondition value.

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

### Existing identities keep working

Turning `platform_review` on, changing it or turning it off does not touch
the flow's CI identities. Their keys keep triggering and reading exactly as
before, and the first run after you enable posting is posted. Preloop posts
with a token of its own, so an identity gains no authority from the flow
posting.

The one new thing an identity can be allowed to do is ask Preloop to publish
its own finished execution again (below). That needs the action
`execution:review:publish`, which an administrator adds to an existing
identity in place, without a new identity or a new key:

<!-- example: ci-update -->
```json
{
  "grant": {
    "version": 1,
    "project_id": "<project id>",
    "flow_id": "<flow id>",
    "actions": [
      "flow:trigger",
      "execution:read",
      "execution:result:read",
      "execution:stop",
      "execution:review:publish"
    ]
  },
  "extend_keys": true
}
```

Send it with `preloop ci update PRINCIPAL_ID --input change.json`, or with
`PATCH /api/v1/ci-identities/{id}` and a human API token.

`grant.actions` is the complete new list. `extend_keys: true` also adds the
actions this change adds to the identity's active keys, so the token already
stored in your CI gains exactly those actions. Without it only keys issued
afterwards carry them.

What does invalidate an identity (it then answers 401 until you provision a
new one): its project is removed from the flow's allowlist, the flow changes
between dedicated and shared selection, the repository entry changes tracker
or the repository's provider identity or path changes, `publication_mode`
changes, or the flow stops being a supported read-only review executor
(another agent type or runner pool, MCP tools added). Disabling the flow
suspends its identities until it is enabled again. Prompt, model, limits and
the result schema never affect an identity.

### The posting record

`GET /api/v1/flows/executions/{id}`, `GET .../result` and the `data` of
`flow.execution.finished` carry `platform_review` (`null` when the flow has
not opted in). `tool_evidence` is present on the result and on the
execution an operator opens; the list, a status poll and the finished event
omit it:

<!-- example: fragment -->
```json
{
  "mode": "manual",
  "posted_by": "manual",
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
  "retry_scheduled": false,
  "due_at": null,
  "tool_evidence": {
    "gateway_requests": 2,
    "missing_records": 0,
    "records": 2,
    "incomplete_records": 0,
    "masking_only_records": 0,
    "masking_by_rule": {},
    "incomplete_by_reason": {},
    "removed_by_category": {},
    "agent_clipped_results": 0,
    "bound_reads": 1,
    "clipped_bound_reads": 0,
    "unknown_bound_reads": 0
  },
  "readback_check": null
}
```

`mode` is the flow's current posting mode. `posted_by` says how the review
reached the pull request: `automatic` (when the run finished, or by an
automatic retry), `manual` (the publish call) or `none` (nothing posted yet,
or nothing will be). It is `null` for a review posted before this field
existed. The `flow.execution.finished` event is sent once, when the run
finishes, so for a manual-mode flow it carries `posted_by: "none"` and
`status: "pending"`; read the execution afterwards for the posted record.
Every publication attempt, automatic or manual, also writes an audit entry
(`platform_review_publish`) with the mode, `posted_by` and, for a refused
call, the failed preconditions.

On `GET /api/v1/flows/executions/{id}` for an account user the record also
carries `pull_request_url`, `review_url` and `commit_url` (the commit that
holds the status), built from the execution's own repository, pull request
and reviewed commit. Each is `null` until there is something to link to.

In the console the execution page shows the record under **Posted on the
pull request**: posted, skipped (for example because the head moved) or
failed with the reason in words, such as a missing **Commit statuses**
permission or incomplete tool evidence, with the evidence counts and links
to the review, the status and the pull request. From
the CLI:

```sh
preloop flow review-posting status <execution-id>          # add --json for the record
```

`status` says what is on the pull request:

- `posted`: the review is posted (and the status, when configured).
- `status_only`: a commit status was set and, by rule, no review: the run
  did not succeed or returned no valid verdict (`reason`
  `run_not_succeeded`, `result_not_valid`, `verdict_unmapped`), the result
  failed the binding, inspection or narrative check (`review_binding_mismatch`,
  `review_not_inspected`, `review_degenerate`, `review_unverified`), the head moved
  (`head_moved`) or the pull request is closed. `review` is `"skipped"` and
  `commit_status` holds the state that was set. A result that fails the
  binding, inspection or narrative check always sets `incomplete_state`, or `error`
  when none is configured, never the verdict's state. Preloop recomputes these
  checks from stored evidence before every attempt, including an explicit
  publish of a run that finished before this check existed.
- `skipped`: nothing was posted, for the same reasons, when no status applies.
- `failed`: an attempt did not complete; `reason` says why, and `review` and
  `commit_status` say what is already there.
- `pending`, `publishing`: not attempted yet, or in progress. A `pending`
  record with no attempt names its cause in `reason`:
  `execution_not_finished` while the run runs, or `evidence_settling` while
  a flow with an evidence level waits for the last model request's tool
  evidence, with `due_at` set to when the attempt becomes due. After that,
  `awaiting_sweep` keeps the fallback pickup deadline visible, including
  when overdue. A finished manual-mode run reads `manual_publish_required`
  with no automatic deadline. A run without a review binding reads
  `no_review_binding` with no deadline. Runs outside the sweep's opt-in cutoff
  or 24-hour lookback read `explicit_publish_required` with no deadline.
  On a record with a scheduled retry, `due_at`
  is when that retry is due.

A posting failure never
changes the execution's status or result. `reason` values include
`head_moved`, `pull_request_closed`, `run_not_succeeded`, `result_not_valid`,
`verdict_unmapped`, `review_binding_mismatch`, `review_not_inspected`, `review_degenerate`,
`review_unverified`, `admission_revoked`, `binding_changed`,
`statuses_permission_missing`, `provider_unavailable`, `provider_denied`,
`provider_rejected`, `lease_refused`, `lease_unavailable`,
`attempt_interrupted`, `platform_review_disabled`, `precondition_failed`,
`manual_mode`, `evidence_incomplete`, `readback_pending` and
`readback_mismatch`. In automatic mode
Preloop retries `provider_unavailable`, `readback_pending` and similar
transient failures by itself, up to six attempts with growing delays. In
manual mode it never retries on its own: a failed attempt waits for your
next publish call.

`readback_check` on the same record names the last comparison that failed.
It stays set when a later attempt fails for another reason while that
read-back is still owed, and it is null once the publication is posted or
no read-back has failed. It is one of
`review_absent`, `review_actor`, `review_commit`, `review_state`,
`review_body`, `status_absent`, `status_state`, `status_context` and
`status_target_url`. The log line for the attempt carries the execution id
and that value, and neither carries the review text. A record written
before this field existed can show `readback_check` null with `reason`
`readback_mismatch`. The publish call still reads the recorded ids and does
not mark that record posted without the comparison.

| `reason` | What it means | What to do |
| --- | --- | --- |
| `readback_pending` | The review or status was accepted, then was not visible yet (`readback_check` is `review_absent` or `status_absent`). The review id and the status id stay on the record. | Wait. In automatic mode Preloop reads them again and completes the record without posting a second time. In either mode the publish call does the same. |
| `readback_mismatch` | GitHub has the object and it differs: the actor, commit, state, context, target URL or body (`readback_check` names which). | Look at the pull request. An explicit publish call reads the recorded ids again. When they match what was sent, the record becomes `posted`. When they still differ, it stays `failed` with the same check. Nothing is posted again, and automatic retry does not take the row. A record posted before the sent body and status URL were stored is compared with a fresh render. When that differs, the record stays `failed` with `review_body`. The review and the status are already on the pull request and are listed on the record, so leaving it failed is safe. |
| `provider_unavailable` | GitHub was unavailable or rate limited. | Automatic mode retries. Otherwise publish again. |
| `statuses_permission_missing` | The review is posted. The App may not write commit statuses. | Grant **Commit statuses: Read and write**, then publish again. The review is not posted twice. |
| `head_moved` | The pull request moved on. The status is on the reviewed commit. No review was posted. | Trigger a review of the new head. |
| `precondition_failed` | A value on the publish call does not match the stored run or the live pull request. Nothing was posted for that call. | Publish again with the current values. |

### Publish a finished review (manual mode) or again

`POST /api/v1/flows/executions/{id}/platform-review` posts the stored,
validated result of a finished execution as the pull request review and
commit status, on the commit the execution reviewed. No model runs. In
manual mode it is the only way a review is posted; in automatic mode use it
after a posting failure.

```sh
curl -fsS -X POST \
  -H "Authorization: Bearer $PRELOOP_CI_KEY" \
  "$PRELOOP_URL/api/v1/flows/executions/$EXECUTION_ID/platform-review"
```

In the console, **Publish again** on the execution page makes the same call.
It is shown to users who may edit flows, once the run has finished and while
something is left to post. From the CLI:

```sh
preloop flow review-posting publish <execution-id>
```

The request cannot name a repository, pull request or commit: they come from
the execution's own binding. Without a body (or with `{}`) the call publishes
what is stored. The response is `{"execution_id": ..., "platform_review":
<the record above>}`.

**Idempotent.** A review or status already posted for the execution is never
posted again; a repeat call returns the existing record. A call that was
refused (a failed precondition, 412) or failed can be repeated, with
corrected values where that was the cause.

**Who may call it.** An account user who may edit flows, and the execution's
own restricted CI identity when its grant and key include
`execution:review:publish` (the identity does not also need `flow:trigger`).
A CI identity may publish only executions it triggered itself (another
identity's execution answers 404), may send the preconditions below, and may
read the record and the digest. It cannot change the posting mode or any
other flow setting, cannot choose the target, and gains no GitHub
credential: Preloop posts with a token it mints for that one post.

#### Preconditions

The optional JSON body names what your controller checked. Every member is
optional; send the ones you want enforced:

<!-- example: fragment -->
```json
{
  "expected_head_sha": "<head_sha from GET .../result>",
  "expected_base_sha": "<base_sha from GET .../result>",
  "expected_result_sha256": "<result_sha256 from GET .../result>",
  "expected_flow_revision": 3,
  "expected_evidence": "captured"
}
```

| Member | Holds when |
| --- | --- |
| `expected_head_sha` | Full commit SHA (40 or 64 hex characters, any case). Equals the head the execution is bound to **and** the live head of the pull request. |
| `expected_base_sha` | Full commit SHA. Equals the bound base, when the binding records one, **and** the live base of the pull request. |
| `expected_result_sha256` | 64 hex characters. Equals the digest of the stored result (below). |
| `expected_flow_revision` | Positive integer. The integer you send is compared with the flow's configuration `revision` at publish time. The execution's `flow_revision` is the revision from when the run was created; send it to refuse the post after a later configuration change. Posting reads the flow (verdict mapping, review actions, status) at post time. See [What must still be in place when you publish](#what-must-still-be-in-place-when-you-publish). |
| `expected_evidence` | `captured` or `complete`. The stored tool evidence meets that level. It can only tighten `platform_review.evidence`; a weaker value does not lower the flow. See [Tool evidence](#tool-evidence). |

Each supplied precondition is checked before anything is posted, again on
the flow and execution as they are read for the post, and the commit SHAs
once more against the live pull request just before the first write. If one
does not hold, nothing is posted and the publication is not recorded as
posted (the record shows `status: "failed"`, `reason:
"precondition_failed"`, which a later call replaces). A body that is not an
object, a member not listed above (for example a misspelt name) or a
malformed value answers 400 with `detail.code`
`review_precondition_malformed` and `detail.precondition` naming the member.

A failed precondition answers **412**:

<!-- example: fragment -->
```json
{
  "detail": {
    "code": "review_precondition_failed",
    "message": "Nothing was posted: expected_head_sha did not match",
    "precondition": "expected_head_sha",
    "expected": "<sha you sent>",
    "current": "<sha it is now>",
    "failed": [
      {
        "precondition": "expected_head_sha",
        "expected": "<sha you sent>",
        "current": "<sha it is now>",
        "source": "live"
      }
    ]
  }
}
```

`precondition`, `expected` and `current` describe the first failure;
`failed` lists every one, in the order of the table above. `source` says what
was compared: `binding` (the commit Preloop bound when it accepted the
trigger), `live` (the pull request on GitHub now), `result` (the stored
result) or `flow` (the flow's current revision).

#### The result digest

`GET /api/v1/flows/executions/{id}/result` returns `result_sha256`, the same
value as `completion.result_captured.result_sha256` and the digest in the CI
completion callback. It is SHA-256 over this canonical form of the result:

1. Take `result` as `GET .../result` returns it to the execution's CI
   identity: the stored document without controller-private members (keys
   listed in `result_withheld_paths` are not in it). An account user's
   `result` holds the whole document; the digest still covers only the
   restricted form.
2. Serialise it as JSON with object keys sorted, no whitespace (separators
   `,` and `:`), non-ASCII characters written as UTF-8 rather than `\u`
   escapes, and no `NaN` or `Infinity`.
3. Hash the UTF-8 bytes with SHA-256 and write it as 64 lowercase hex
   characters.

Key order and whitespace in the document never change the digest. In Python:

```sh
python3 -c 'import hashlib, json, sys
doc = json.load(sys.stdin)["result"]
print(hashlib.sha256(json.dumps(doc, sort_keys=True, separators=(",", ":"),
      ensure_ascii=False).encode("utf-8")).hexdigest())' < result.json
```

Numbers are written the way Python's `json` module writes them, so compute
the digest of a result with floating-point values in Python, or compare with
the `result_sha256` Preloop returned for the document you validated.

#### End to end: post only what your controller approved

```sh
API="$PRELOOP_URL/api/v1/flows/executions/$EXECUTION_ID"
AUTH="Authorization: Bearer $PRELOOP_CI_KEY"

# 1. After flow.execution.finished (or polling until the status is terminal),
#    read the result together with its digest, commits and flow revision.
curl -fsS -H "$AUTH" "$API/result" > result.json

# 2. Your controller validates .result. Stop here if it does not approve.
./validate-review result.json || exit 1

# 3. Ask Preloop to post exactly what was validated.
jq '{expected_head_sha: .head_sha,
     expected_base_sha: .base_sha,
     expected_result_sha256: .result_sha256,
     expected_flow_revision: .flow_revision}' result.json > preconditions.json
status=$(curl -sS -o posted.json -w '%{http_code}' -X POST -H "$AUTH" \
  -H "Content-Type: application/json" --data @preconditions.json \
  "$API/platform-review")

# 4. Read a 200 from the body. platform_review.status posted (or
#    status_only) means the review is on the pull request. status failed
#    and reason admission_revoked means nothing was posted. 412: a
#    precondition did not hold; nothing was posted. detail.failed says
#    what, with the current value.
case "$status" in
  200) jq '.platform_review | {status, reason, posted_by, review_id}' posted.json ;;
  412) jq '.detail.failed' posted.json; exit 1 ;;
  *) cat posted.json; exit 1 ;;
esac
```

Other refusals: 409 with `detail.code` `platform_review_disabled` (posting
is off), `execution_not_finished` or `no_review_binding` when nothing can be
published, and 404 for an execution the caller does not own. An execution
whose result is not valid, whose binding or inspection check failed, or whose
identity was disabled is never published by this call either. Without
`expected_head_sha`, a head that moved while the review ran still gets the
status on the reviewed commit and no review (`review_reason:
"head_moved"`); with it, the call answers 412 and posts nothing. What must
still be in place, and how the call refuses when it is not, is in
[What must still be in place when you publish](#what-must-still-be-in-place-when-you-publish).

#### What must still be in place when you publish

The publish call reads the flow and the CI identity that triggered the run
as they are when you publish. The execution does not store the posting mode
from when the run started.

At publish time the call reads:

- the flow's current posting mode (off answers 409
  `platform_review_disabled`, and that refusal writes no
  `platform_review_publish` audit row);
- the flow's current configuration revision, when you send
  `expected_flow_revision`;
- whether the flow is enabled;
- whether the triggering identity is still active, still bound to the same
  flow and project with the same provisioning snapshot, and still holds
  `flow:trigger` or `execution:review:publish`.

That identity check applies to every caller that reaches publication,
including an account user. Revoking the identity's key stops requests that
present the key. An account user who may edit flows can still publish.
Disabling the identity stops the publish, and you can enable it again while
it is still bound to the flow. Retiring the identity stops the publish, and
retirement cannot be undone. A different identity, including one you
provision afterwards, does not own the execution, so its publish call
answers 404.

`expected_flow_revision` is the integer in your request, compared with the
flow's configuration revision at publish time. `flow_revision` on the
execution is the revision from when the run was created. Sending that
recorded value refuses the post once a later configuration change has moved
the flow's revision. A change to the flow's configuration moves the
revision. Turning posting on, switching between automatic and manual, and
enabling or disabling the flow are configuration changes. A save that
writes the values already stored leaves the revision unchanged.

To use `expected_flow_revision` as a check that the flow is unchanged since
the run, configure posting before you trigger, and send the execution's
`flow_revision`. A run that finished while posting was off cannot be
published with `expected_flow_revision` set to that execution's
`flow_revision`. Turning posting on moves the revision, so the call answers
412. Publish that run by omitting `expected_flow_revision`, or by sending
the flow's revision from after the save that turned posting on.

Configure posting (manual or automatic) and leave the flow enabled, then
trigger, then validate, then publish. Retire the identity or disable the
flow only after that publish has succeeded.

A finished run whose triggering identity has been retired cannot be
published; run the review again with posting configured first.

An event-started run has no CI identity to retire. At publish time the
flow is still enabled, the project is still in `trigger_project_ids` with
the same tracker, the tracker is still that GitHub App installation and is
not suspended, and the event type is still selected. Disabling the flow,
removing the project, pointing the trigger at another tracker, or removing
the event type answers `admission_revoked`. A suspended installation
answers `installation_suspended` and is not retried. A tracker that is no
longer a GitHub App answers `tracker_unsupported`. A project whose
identifier or slug no longer matches the binding answers `binding_changed`.
For an opted-in fork, the same label application must still be present and
its actor must still have write, maintain or admin permission. Removing the
label before publication skips the pending post.

The identity's provisioning binding (project, flow and snapshot) produces
`admission_revoked`. A live pull request whose id, repository id or name no
longer matches the execution's binding produces `binding_changed`. Both
answer HTTP 200. Read `platform_review.status` and `platform_review.reason`.

| Situation | Response |
| --- | --- |
| Posting is off | 409, `detail.code` `platform_review_disabled`. No `platform_review_publish` audit row. |
| The run has not finished, or it has no review binding | 409, `detail.code` `execution_not_finished` or `no_review_binding`. |
| The body is not an object, names an unknown field, or a value is malformed | 400, `detail.code` `review_precondition_malformed`. `detail.precondition` names the member. |
| A stored precondition does not match | 412, `detail.code` `review_precondition_failed`. Audit action `platform_review_publish`, status `denied`. Nothing is posted. |
| Posting is still on, and the caller reached publication. The triggering identity is retired or disabled, its provisioning snapshot no longer matches, its grant holds neither `flow:trigger` nor `execution:review:publish`, or the flow is disabled | HTTP **200**. Read the body: `platform_review.status` is `failed` and `platform_review.reason` is `admission_revoked`. Audit action `platform_review_publish`, status `failure`. |
| The live pull request's id, repository id or name no longer matches the execution's binding | HTTP **200**. `platform_review.status` is `failed` and `platform_review.reason` is `binding_changed`. Audit status `failure`. |
| The flow's posting settings no longer validate, for example result validation was turned off | HTTP **200**. `platform_review.status` is `failed` and `platform_review.reason` is `configuration_invalid`. Audit status `failure`. |
| The repository's tracker is missing or is no longer a supported GitHub App | HTTP **200**. `platform_review.status` is `failed` and `platform_review.reason` is `tracker_unsupported`. Audit status `failure`. |
| A CI identity publishes an execution it did not trigger | 404. |
| A CI credential calls while its flow is disabled | 401, `{"detail": "Restricted CI authorization denied"}`. The publish call does not run. |

Disabling the flow moves the configuration revision. A publish that also
sends the execution's `flow_revision` then answers 412, and the admission
check does not run. The 200 body above is the answer when `expected_flow_revision`
is omitted or equals the revision after the disable. Retiring the identity
leaves the flow's revision unchanged, so the same precondition still holds
and the answer is the 200 body.

A CI credential whose identity is inactive, or whose key is revoked, is
refused before publication with the same 401 detail. When the key is valid
and the flow is enabled, but the key does not hold
`execution:review:publish`, the detail text is the same and the status is
403.

### Post for some of a shared flow's projects

`platform_review.project_ids` names the projects of the flow that Preloop
posts for. Without it Preloop posts for every project of the flow, as
before. With it:

- A run of a project outside the list still runs, and its CI key still
  triggers, reads and stops it exactly as before. Preloop does not post
  it. Its posting record says `skipped`, reason
  `project_not_in_posting_scope`; nothing is retried. The explicit publish
  call answers 409 `project_not_in_posting_scope`.
- The list as it stood when a run finished decides. Adding its project to
  the list later does not post that run, and its explicit publish call
  still answers 409. A run started after the project is added posts as
  usual. Runs that finished before this rule shipped are not backfilled.
  The run is closed when it finishes: by the completion step, by the
  monitor that fails a stale run, or by a failed dispatch. A run that ends
  on another path without the completion step is closed by the next sweep
  pass, so adding its project in the minutes before that pass can still
  post it.
- A pull request event starts a review only for a listed project. An event
  for another project of the flow inserts no execution.
- A project removed from the list while its run is in flight is not
  posted (`admission_revoked`).
- No CI identity is invalidated by setting, changing or removing the list,
  and no CI identity can read it.
- Each id must be in `trigger_project_ids`; saving refuses any other id, a
  duplicate or a value that is not a project id, and names the offending
  id. When the shared-flow allowlist is replaced
  (`PUT /api/v1/ci-identities/shared-flows/{flow_id}`), a project that
  leaves the allowlist leaves `project_ids` in the same write. A project
  added to the allowlist is not added to the list.

Move one repository of a shared flow to event-started review, and leave the
others on their CI keys. The administrator saves the flow with
`PUT /api/v1/flows/{flow_id}`; the other keys of the flow stay as they are:

<!-- example: flow-update -->
```json
{
  "trigger_event_source": "<GitHub App tracker id>",
  "trigger_event_types": [
    "pull_request_opened",
    "pull_request_updated",
    "pull_request_reopened",
    "pull_request_ready_for_review"
  ],
  "trigger_project_ids": ["<project A id>", "<project B id>", "<project C id>"],
  "webhook_config": {"supersede_on_update": true},
  "git_clone_config": {
    "enabled": true,
    "publication_mode": "read_only",
    "ci_project_selection": true,
    "repositories": [
      {"project_id": "<project A id>", "tracker_id": "<GitHub App tracker id>"},
      {"project_id": "<project B id>", "tracker_id": "<GitHub App tracker id>"},
      {"project_id": "<project C id>", "tracker_id": "<GitHub App tracker id>"}
    ],
    "platform_review": {
      "enabled": true,
      "project_ids": ["<project A id>"],
      "status": {
        "context": "preloop/review",
        "verdict_states": {"pass": "success", "fail": "failure"}
      }
    }
  }
}
```

Pull request events on project A now start a review that Preloop posts.
Projects B and C keep their CI keys and workflows; their runs and results
are unchanged, and Preloop posts nothing for them. With the CLI:
`preloop flow review-posting set-projects <flow> --project <project A id>`,
and `--all` to post for every project again.

Remove the CI workflow from project A once its events start reviews. While
both are in place, each can start a run for the same commit: an event for a
commit that a CI-started run of the same repository is still reviewing is
dropped, but a CI trigger does not check for a running event review, and an
event that arrives after the CI run finished starts a new one. Each run is
posted as its own review.

### Worked example: one flow for an organisation, two repositories

One hosted Codex flow reviews two repositories. Each repository has its own
CI identity, so neither can trigger for, read or cause a post on the other.

1. Flow (an administrator saves this with `POST /api/v1/flows`; the model,
   project and tracker IDs are yours):

    <!-- example: flow-create -->
    ```json
    {
      "name": "Organisation review",
      "prompt_template": "Inspect the checked-out pull request with shell tools. Do not edit files. Return the final structured result with verdict, summary and findings.",
      "ai_model_id": "<model id>",
      "agent_type": "codex",
      "agent_config": {"native_result": true, "limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
      "runner_pool": "server",
      "allowed_mcp_tools": [],
      "allowed_mcp_servers": [],
      "validate_result": true,
      "result_schema": {
        "type": "object",
        "additionalProperties": false,
        "required": ["verdict", "summary", "findings", "checked"],
        "properties": {
          "verdict": {"type": "string", "enum": ["pass", "fail"]},
          "summary": {"type": "string"},
          "findings": {
            "type": "array",
            "items": {
              "type": "object",
              "additionalProperties": false,
              "required": ["severity", "title", "body", "path", "line"],
              "properties": {
                "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                "title": {"type": "string"},
                "body": {"type": "string"},
                "path": {"type": ["string", "null"]},
                "line": {"type": ["integer", "null"]}
              }
            }
          },
          "checked": {"type": ["array", "null"], "items": {"type": "string"}}
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

   `execution:review:publish` is only needed for the publish call above,
   not for posting. It can be granted on Preloop versions that include
   platform-posted review; on Preloop Cloud and Preloop Enterprise the
   access-control component must also include that action, otherwise the
   grant is refused with 403. Leave it out of the list until then.

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
List an identity's dispatch subscriptions with
`GET /api/v1/ci-identities/{principal_id}/github-dispatch-subscriptions`; the
response never includes the token or signing secret, and `X-Total-Count` gives
the number of subscriptions.

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
  "prompt_template": "Inspect the checked-out pull request with shell tools. Do not edit files. Return the final structured result with a substantive review string.",
  "ai_model_id": "<model id>",
  "agent_type": "codex",
  "agent_config": {"native_result": true, "limits": {"max_total_tokens": 5000000, "max_turns": 300, "max_usd": 25}},
  "runner_pool": "server",
  "trigger_project_ids": ["<project id>"],
  "validate_result": true,
  "result_schema": {
    "type": "object",
    "additionalProperties": false,
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

## Automatic subset review with a shadow publisher

The repository's [restricted CI route workflow](https://github.com/preloop/preloop/blob/main/.github/workflows/preloop-ci-route.yml)
and `scripts/ci_review_route.py` extend the authenticated polling example to
same-repository PRs. The existing reviewer remains independent.
This route is inert until the operator supplies its own configuration; merging
the workflow does not create a flow, identity, key or model registration.

**Trust boundary.** The workflow uses `pull_request_target`, rejects forks,
drafts and Dependabot before loading the CI key, and checks out only
the trusted default branch, then pins completion and publication to that exact
checkout SHA. Never change that
checkout to a PR head or merge ref, run a PR-supplied installer, or evaluate
captured command text. Only the publisher has `statuses: write` and
`pull-requests: write`; selection and completion have read-only GitHub tokens.
The publisher refetches authenticated server documents, so no PR artifact can
supply its result. A `workflow_run` or callback receiver is unnecessary here:
completion is polled inside the trusted workflow. The callback example above
remains an alternative for installations using signed dispatch subscriptions.

The selector retrieves **every** PR file page, checks the count against
`changed_files`, and includes both names of a rename. GitHub's 3,000-file API cap
fails closed. GitHub Actions `paths` filters alone are insufficient: they mean
*any* changed file matches, whereas this route requires *all* changed files.
The default union is `docs/**`, `runtime-plugins/**`, `helm/**`, `cli/**`.
`PRELOOP_CI_ROUTE_PATHS` overrides it with a JSON array of case-sensitive,
repository-relative shell globs. Matching uses Python `fnmatchcase`: `*` can
cross `/`, `**` has the same meaning, and negations are unsupported. For example,
`["docs/**", "cli/**"]` selects only changes wholly within those two trees.

The trigger body is exactly `{"pr_number": 7, "head_sha": "<full SHA>"}`.
The completion step polls `GET /api/v1/flows/executions/{id}` with the CI key.
The publisher independently fetches that endpoint, `/result` (retrying HTTP 409
result-pending responses for up to 30 seconds) and every page of
`/tool-evidence?skip=0&limit=10`, following `next_skip`. It rechecks the current
PR head and base before publication. Matching owned executions are reused on
reruns; failed results are not silently retried with another model run.
The workflow allows ten minutes for scheduling and the 300-second execution;
obsolete running work or a polling deadline uses the owned stop endpoint.
A lost runner can leave work alive until the flow's own timeout.

A result is accepted only when the execution succeeds, the schema and controller
inspection checks pass, no review fields were withheld, and both returned SHAs
and the startup receipt match the current PR. `/container_termination` is an
allowed withheld path: the controller adds it after capture, and the restricted
projection intentionally removes it even on success. Other withheld paths are
rejected. The review must contain at least
80 characters and 12 words (maximum 60,000 characters), the unified Preloop
review heading, Checks section and exact reviewed-SHA marker. The strict schema is
[`ci-route-result.schema.json`](../../examples/ci-route-result.schema.json);
all object properties are required, all objects are closed, and absent finding
locations use `null`. `approve` or `comment` with critical/high findings is rejected. Missing PR
context (`context_complete: false`) is an error, never a passing review.

Repository-inspection counters alone do **not** show that a diff was read.
Configure the prompt to obtain the full head and target-branch SHA from the
checkout, compute their unique merge base, and issue this standalone native
shell command in the repository, substituting the literal full SHAs:

```sh
git --no-pager diff --no-ext-diff --no-textconv <merge_base_sha> <head_sha> --
```

Use the tool's working-directory argument, with no `cd`, pipeline, redirection,
path filter or `--stat`. The validator accepts a shell `-c`/`-lc` wrapper around
that exact command. It requires an integrity-valid evidence record with
`document.execution_id`, `capture_complete: true`, no storage redaction, and
an observation with `source: native`, `kind: command`, `event: item.completed`,
`exit_code: 0`, `redacted: false` and `truncated: false`. It checks record/page
digests and the captured result digest. Requested calls, mere command counts,
echoed command strings and started/failed commands do not satisfy this policy.
Other redacted records need not be accepted as proof. Enable tool-content
capture for this flow's account; without readable native commands the publisher
returns `error`. This is captured execution evidence, not proof the model
understood every changed line. The startup receipt's canonical diff digest
uses the separate reproduction command above.

The target branch can advance between admission, checkout and publication.
The checkout's `origin/main` is not a substitute for the admitted base when
that happens: the returned base and current PR must still agree with the
controller's binding, or the publisher reports `error`. A stale result never
becomes a successful check for the newer head or base.

**Shadow publication.** By default only the commit status
`preloop-review/ci-route` and a job summary (verdict, counts by severity,
execution URL) are written. `approve` and `comment` map to `success`, `request_changes` to
`failure`, and any missing/invalid result or evidence maps to `error` with a
safe reason. No review or comment is posted in shadow mode. Leave this status
out of branch protection and rulesets. Set `PRELOOP_CI_ROUTE_POST_REVIEW` to
`enabled` later to also post one COMMENT review, deduplicated by execution/head
and verified against the bot's publication receipt. This never submits an
APPROVE or REQUEST_CHANGES review. GitHub cannot atomically publish and lock
PR updates; a concurrent update can leave a review on its old commit, after
which the final head/base check reports an error.

The prompt uses anonymous public GitHub GETs for PR metadata, comments and up
to two referenced issues; clone credentials are never repurposed for those
requests. An unavailable issue is `UNCLEAR`, not invented acceptance criteria.
Public REST cannot establish whether an inline thread was resolved. The prompt
suppresses matching previous feedback when that state is unknown and reports
the limitation. It performs a full/fast-path review, without taking an
incremental baseline from the other reviewer. Keep shadow mode until comparison
shows these context differences do not degrade review quality. Private
repositories need a separately designed trusted context-delivery mechanism;
this public-repository prompt does not claim that capability.

Before enabling posting, compare both reviews on the **same head and base**:
open the execution URL from the shadow summary, read its result alongside the
existing review, and reconcile severity, verified file/line evidence, missed
findings, false positives, checks actually run, issue criteria and coverage,
acknowledged/resolved feedback, and summary format. Record differences and
execution token usage, cost and latency. Reject comparisons across different
commits. Require representative docs, CLI, Helm and plugin changes, including
at least one issue-linked PR and one re-review. A green status alone proves
validation, not review quality. Keep the posting variable unset until those
comparisons are accepted; the existing reviewer continues throughout.

### Operator activation checklist

1. Register a server-usable OpenRouter model with identifier
   `deepseek/deepseek-v4-flash-0731`, endpoint
   `https://openrouter.ai/api/v1`, and model parameter `reasoning_effort: low`.
   Select that registration explicitly in the flow. Keep provider credentials
   in Preloop; the Actions jobs never need them.
2. Create an enabled, dedicated hosted Codex flow with `runner_pool: server`,
   `agent_config.native_result: true`, `timeout_seconds: 300`,
   `validate_result: true` and the strict schema linked above. Set explicit
   run limits, empty MCP tool/server allowlists, and exactly this synced
   project's owning GitHub App clone binding with `publication_mode: read_only`.
   Use the [adapted Pull Request Reviewer prompt](../../examples/ci-route-prompt.md),
   which preserves severity calibration, verification budgets, issue coverage
   and the unified summary while returning a native structured result. Leave automatic event triggers, setup commands,
   platform review posting and other publication features unset.
3. Create a new CI identity bound to this project and flow. Grant its key
   `flow:trigger`, `execution:read`, `execution:result:read`,
   `execution:evidence:read` and `execution:stop`. The evidence permission is
   additional to the basic polling/publishing examples. A callback subscription
   and callback signing secret are not needed for this polling route.
4. Create repository secret `PRELOOP_CI_ROUTE_TOKEN` and variables
   `PRELOOP_CI_ROUTE_URL`, `PRELOOP_CI_ROUTE_PROJECT_ID`,
   `PRELOOP_CI_ROUTE_FLOW_ID`, `PRELOOP_CI_ROUTE_ENABLED`.
   Optional variables: `PRELOOP_CI_ROUTE_PATHS`, `PRELOOP_CI_ROUTE_POST_REVIEW`.
   Set `PRELOOP_CI_ROUTE_ENABLED` to `enabled` last. A missing key, URL, flow ID,
   project ID or enabling variable causes a clean skip. Disable the enabling
   variable to stop new runs. Keep the review-posting variable unset for shadow.

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

### Rejected model keys at trigger time

Before creating an execution, clone lease or container, a restricted CI trigger
checks whether its selected customer model has a standing authentication
rejection. Only a marked model receives an authenticated probe with that model's
stored key. Success clears the mark and starts the review. Another upstream
authentication rejection returns HTTP `409` with
`detail.code: "model_credentials_rejected"` and `detail.model_name`. The
restricted response includes no provider sentence, secret reference or key
material. A human owner can replace or rotate the API key for that model.
Timeouts, network failures, rate limits and provider `5xx` responses allow the
review to proceed. Billing rejection marks do not block reviews; unmarked models
have no extra provider call. Probe calls appear in gateway usage and audit.

## Find a refused trigger in the audit trail

Restricted CI refusals are recorded as `restricted_ci_denied`, including
credential and transport failures before the trigger handler, invalid request
shapes, flow binding mismatches, and trusted provider admission failures.
Each record has the stored CI principal (or key when no principal is bound),
the requested flow UUID, `details.reason_code`, a plain `details.message`, and
`details.count`. Other refused operations use a fixed operation label instead
of retaining arbitrary request URLs.

Use your console session or a human API credential with `view_audit_logs`:

```http
GET /api/v1/audit-logs?action=restricted_ci_denied&reason_code=ci_head_mismatch
GET /api/v1/audit-logs/grouped?reason_code=ci_flow_denied
```

Common reasons are `ci_transport_denied` (use one bearer header),
`ci_key_inactive`, `ci_key_expired`, `ci_principal_inactive`,
`ci_action_denied`, `ci_credential_invalid` (check the credential binding), `ci_authorization_denied`
(check the principal's current grant and binding), `ci_flow_denied` (wrong
flow), `ci_input_invalid` (send only the PR number and exact head),
`ci_pr_closed`, `ci_repository_mismatch`, and `ci_head_mismatch` (refresh the
provider's PR state). Verification or dispatch unavailability is recorded as
`ci_verification_unavailable` rather than inventing a provider diagnosis.

Identical refusals from one principal to one target with one reason share a
single record per UTC minute. `count` includes the first attempt; `window_start`
and `window_end` identify the interval. The current minute is visible immediately
but remains unsealed. After the minute closes, the normal audit sealer hashes the
final counter. Later retries create a new row; sealed evidence is never changed.
The list's `total` counts records, so sum `details.count` to count refused attempts.

Bodies, credentials, provider response text and arbitrary URLs are excluded.
Only credentials that resolve to a stored account can be attributed to that
account's audit trail. Unknown or missing credentials cannot safely be assigned
to an account from a caller-supplied account or session ID.


## Re-finalize a saved review without another model run

An account owner or administrator can recover an intact saved answer after a
control plane finalization failure. Use a personal credential: CI identities,
agent credentials and flow execution credentials cannot call this operation.
The execution page offers **Re-finalize saved answer** only after the server's
preview confirms that the person and execution qualify. It shows the selected
answer, review checks and expected posting decision before application.

The API defaults to a dry run:

```http
POST /api/v1/flows/executions/22222222-3333-4444-8555-666666666666/refinalize
Content-Type: application/json

{"dry_run": true}
```

The response includes `eligible`, `selected_source`, `selected_result`,
`selected_result_sha256`, `previous_result_sha256`, `expected_flow_revision`,
`gates`, `publication` and `digest`. Each gate reports `passed` and a refusal
`reason` when needed. `publication.would_publish` reports whether normal
posting would proceed; a moved pull request reports `head_moved` or `base_moved`.
Preview records no audit event and changes no execution or publication row.
It may read the pull request through the connected provider.

To apply, repeat the request with `dry_run: false` and `expected_digest` set to
the preview's `digest`. A missing or stale digest returns 412; a failed gate
returns 409 and names the gate. The digest binds the entire stored result,
selected answer, flow configuration, admitted revision and checked evidence.
It is distinct from the ordinary result digest: private transport members must
also be covered. The stored execution's `flow_revision` must be known and equal
the current flow revision. Editing the schema, prompt or posting configuration
requires a new execution, even if the edit was later reverted.

The CLI prints the same preview as JSON:

```sh
preloop flow review-posting refinalize "$EXECUTION_ID"
preloop flow review-posting refinalize "$EXECUTION_ID" --apply --expected-digest "$PREVIEW_DIGEST"
```

Qualifying executions have a controller-recorded captured answer and a normal
exit, an unchanged flow, and a recoverable control plane failure such as
`model_transient` from a log heuristic or `result_selection_failed`. A recorded
finalization, storage, log shipping or completion delivery failure can also
qualify under `execution_error` or `orchestration_error`. An otherwise succeeded
execution with an unusable transport as its result can qualify. The native
transport requires the execution's controller-recorded native capture decision;
a model-written envelope is not authority to recover a run. The selected
answer must pass the same schema, binding, inspection and integrity checks as
live finalization, and the flow's evidence requirement (at least captured).
Clipped or unknown bound reads are refused during recovery.

Stopped, cancelled and timed out runs never qualify. Neither do recorded review
gate rejections, `result_schema_invalid` after repairs, absent answers, missing
capture provenance, incomplete evidence, changed or unknown flow revisions, or
an execution already succeeded with a valid result. A gate whose required facts
are unavailable is refused by name. Flows requiring an agent-posted review
receipt are outside this recovery operation.

Application changes the stored result, validation and terminal outcome and
records what it replaced in the audit log and execution's `refinalization`
field. The console shows when and by whom it was re-finalized. Original run
usage, cost, token counts and start/end timestamps remain unchanged. No model or container
runs and there is no second bill. Repeating an applied operation returns
`already_refinalized` and changes nothing.

Posting retains its existing rules: automatic/manual mode, project scope,
identity admission, enabled flow, open pull request and one review per execution.
A recovered execution also retains its stored base SHA and flow/result checks
on every publication attempt, including the periodic pass. A moved pull request
can leave a successfully re-finalized execution unposted. Manual posting and
old executions outside automatic recovery still require the ordinary publish
operation; re-finalization does not override that decision. Posting failures do
not undo a completed re-finalization.
