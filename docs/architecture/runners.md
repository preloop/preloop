# Private Runners and Remote Sessions

Personal and shared private runners: capability reporting, owner consent, native host-profile publication and continuation, remote harness sessions and their workspace policy. Flow execution on hosted compute is covered in [Flows](flows.md).

## Runner log delivery

Private-runner log batches use stable delivery identities and acknowledgments so a reconnect can replay unconfirmed output without duplicating stored rows. Socket writes have deadlines, live log broadcasts have a bounded budget, and runner lease release shares the terminal result transaction. Publication and native-session markers are saved while output arrives. The execution console refreshes runner assignment and failure details at lifecycle transitions; an execution without an observed runtime is shown as unassigned.

## Native host network and agent credentials

A host profile may set `allow_network`; Codex then runs workspace-write with
`sandbox_workspace_write.network_access=true`, and the runner advertises
`host_network` (other native harnesses do not sandbox network and always
advertise it). A flow with `agent_config.requires_network: true` gets the
`host_exec_network` lease key: flow save and run start refuse it when no
runner in the pool advertises `host_network`, assignment skips runners that do
not, and the runner refuses the lease on a profile without network
(`cli/internal/cmd/runner_host_exec_network.go`). `agent_git_credential`
leaves the checkout credential in the checkout's local git config as a
URL-scoped extraHeader so the agent's own git fetch and push work; it is never
in the agent environment, but it is the tracker credential with its full API
scope and the agent can read it, so operators pair it with a minimal-scope
token. Read-only review cannot require network (refused at save, run start
and on the runner). After the CLI exits, the runner reads a
`result.json` the agent wrote during the run (execution directory or checkout
root, regular file, bounded size) and turns a reported failure into FAILED
with the agent's summary, matching the hosted result-artifact rule.

## Native host publication and Copilot continuation

Every native host profile (Codex, Claude Code, Cursor, Devin, OpenCode,
Copilot, DeepSeek) can publish through managed legacy publication
(`backend/preloop/services/host_exec_publication.py`, runner
`cli/internal/cmd/runner_host_exec_publish.go`). The flow must clone exactly one
repository with `create_pull_request` in legacy mode, and the runner profile must
set `allow_checkout` and `allow_publish`, which makes the runner advertise
`host_publication`. Flow save and run start refuse the flow when no runner in the
pool advertises it, and lease assignment skips runners that do not. Delivery adds
a transient `host_exec_publication` plan (checkout path, managed
`preloop/issue-<KEY>-<exec8>` branch from the shared branch-plan resolver, commit
message, and the flow's configured `pull_request_template` path); the push
reuses the checkout's URL-scoped header credential, which is the short-lived
repository token the control plane issued for the job (a GitHub App
installation token or the tracker's GitLab/Bitbucket token), never the host's own
git credentials, and nothing is persisted. After the CLI succeeds the runner commits with hooks disabled,
copies the head into a fresh runner-owned bare repository and pushes from there
without force, with no repository, global or system git config, to the planned
URL, and reports a `host_publication` receipt
on the completion envelope. A pushed receipt also carries the repository pull
request template, read from the pushed commit as a regular blob (never the
working tree) with the hosted discovery order. The completion path keeps only that runner receipt
(agent JSON cannot author it) and fails a publishing run without a pushed branch.
The orchestrator verifies the branch against its own plan, looks up an open pull
request for it before creating one through the bound GitHub, GitLab or Bitbucket
Cloud tracker (Bitbucket Data Center is not supported here), and binds it with `record_opened_pr`, so a lost create response never
yields a second pull request. Title and body follow the hosted precedence (agent
result, configured `pull_request_title`/`pull_request_description`, the
repository template, then a default). When `git_clone_config.publication_approval`
is set, the pull request opens only after a human approval bound to the
repository, branch, base and pushed head SHA; otherwise the run fails
`publication_failed` and the branch is kept. Isolated publication and
multi-repository publication stay refused on host profiles, and a `read_only`
review flow never publishes.
Session continuation on review feedback is Copilot only: only a publishing
Copilot profile advertises `host_continuation`.
A succeeded publishing run stores its validated Copilot session (with profile
and model alias) on `cli_session`. Feedback on that pull request reaches the host
flow through the existing feedback thread `_resume`; the orchestrator validates it
with `resolve_host_continuation` (`services/host_exec_continuation.py`): same
flow, same profile and model alias, a confirmed pull request on the resume
branch, and an originating runner that advertises `host_continuation`. Anything
else fails `resume_unavailable` instead of starting a fresh implementation. The
lease carries a control-plane `host_exec_resume`, is pinned to the originating
runner, and the runner adds `--resume=<session>` itself, checks the session is on
the host, and pushes onto the existing pull request branch. A completion naming a
different session fails `resume_identity_mismatch`.

## Runner owner consent

Private runner leases enforce `flow_execution_consent` before creating any assignment. `ask_me` (the default) binds a durable `OwnerConsent` to the execution and selected runner, with a standard approval addressed only to the runner owner. Retries keep the same owner; pending execution waits until the existing approval window expires. `always` permits assignment and `never` rejects it. Owner settings survive reconnects unless registration explicitly changes them. Shared-runner consent belongs to the resource account, while execution-account lookups retain the selected runner across retries. Owner-less runners requiring consent are skipped so routing can use another eligible runner or hosted compute.

The approval binds to `tool_args` (`runner_id`, `execution_id`) only. When the request is created, `services/runner_flow_consent_context.py` also snapshots what the owner is asked to allow into `rule_context.runner_flow` and writes a plain-language `summary`. The snapshot holds the runner and device (hostname, OS), the harness and model, the flow and its link, and the trigger (schedule, manual, retry, delegated or event). It also holds the repositories (credentials stripped) and authorized directory, whether the run can push or open a pull request, the time limit, and the scope: one approval covers this run only, and there is no per-flow durable grant yet (#2537). A poll of an existing request never rebuilds the snapshot. The console page, the approvals list, push payloads, email, `preloop approvals pending` and the signed-out token page render it. The token page receives only the `runner_flow` key, never the rest of `rule_context`.

## Remote harness sessions

A personal runner can also host remote harness sessions (#1482): the server
writes start, turn and stop requests to `runner_remote_sessions`
(`services/runner_sessions.py`) and delivers them over the runner websocket on
the next heartbeat or at once from the replica holding the socket; the runner
(`cli/internal/cmd/runner_sessions.go`) runs Copilot CLI one process per turn
with `--session-id` then `--resume`, only for harnesses the host user enabled
with `preloop runner sessions enable`. Each session pairs with a
`RuntimeSession` of source type `runner_session`, which
`resolve_session_control_mode` puts in command mode. Hook calls that carry the
runner-reported `harness_session_id` resolve to that runtime session while it is
live (`agent_session_lineage.runner_session_for_external`); a session a hook
opened before the id arrived is folded into it. See
[Remote sessions](../guide/runners/remote-sessions.md).

Who may use a runner is the owner-or-admin rule in
`account_hooks.may_start_runner_session` / `may_use_runner_for_flows`. The
session routes pass `operation`, `harness`, `workspace_kind` and
`workspace_id` to the authorizer, which may widen the rule only with an
explicit allow naming a rule (EE runner sharing, grants in
`runner_share_grant`, owner consent policy in `runner_sharing_policy`). Each
heartbeat calls `runner_sessions.reauthorize_live_sessions`, which re-checks
non-owner sessions and queues a kill for those no longer allowed before the
heartbeat's session frames go out.

## Runner capabilities and scheduling

Runner registration and heartbeats report `docker_available` from a five-second `docker ps` probe. Container leases require that capability, including explicitly pinned runners. The CRUD layer excludes paused runners from selection and slot claims; leasing rechecks scheduling preferences after locking the row. Capability tags are derived from the current daemon result and enabled, signed-in harness inventory alongside custom labels. `accept_untagged` defaults to false and `paused` defaults to false; console scheduling edits persist independently of the process's registration and heartbeat. Automatic routing uses hosted compute when no private runner accepts the job.

## Remote session workspaces

Cursor remote sessions require host consent and the Preloop approval hooks. The host creates a chat with `cursor-agent create-chat` and uses `--resume` for each turn, with the same authorized workspace policy and process cancellation as Copilot sessions.

Remote sessions on a personal runner (`docs/guide/runners/remote-sessions.md`)
use a workspace policy that keeps paths on the host: the operator lists
directories with `preloop runner dirs add` (stored in `~/.preloop/runner.json`,
advertised as id, label, mode and harness list with register and heartbeat),
and the runner (`cli/internal/cmd/runner_workspace_policy.go`) re-validates
the entry and proves a requested path stays inside it after realpath
resolution (case-folded on Windows, reparse points treated as symlinks, root
and home refused) before a session opens it; a git work tree with uncommitted
changes is refused unless the owner allows it. A tracker checkout clones into
`host-workspaces/sessions/<id>` with a credential minted per start by
`backend/preloop/services/runner_workspace_credentials.py` (a GitHub App
installation token scoped to one repository with `contents: read`, or the
managed Bitbucket Cloud OAuth token; PAT, app password and access-token
trackers are refused by name), delivered only in the start message, served
to git through an askpass helper over an inherited pipe, never written to
disk, `.git/config` or a log, stripped from persisted payloads, and the
directory is deleted when the session ends.

## Host-profile gateway attribution and hosted Codex hook

A private-runner Codex or Claude Code host profile runs the runner user's
Preloop-onboarded CLI, whose model calls reach the gateway with the agent's
own key. Host delivery adds `host_exec_gateway`: a signed, identifiers-only
execution marker the runner sends as `X-Preloop-Execution` (Codex through a
per-run provider override, Claude Code through `ANTHROPIC_CUSTOM_HEADERS`).
The gateway auth dependency verifies it against the key's account and a live
execution and folds `flow_execution_id`/`flow_id` into the detached key
snapshot, so usage attribution, per-execution ceilings and cost rollups use
their existing paths. At completion the server counts the execution's gateway
rows for `gateway_metered`; a Codex or Claude Code run with model output and
no row fails `gateway_bypassed` unless the flow opts out
(`allow_unmetered_host_model`, never in read-only review). See
`services/host_exec_gateway.py`. Hosted Codex containers install the Preloop
`PreToolUse` hook before Codex starts (`agents/codex_native_hook.py`), so
native commands follow the same deny and approval-park rules.


## Scheduled discovery

Resident CLI runners run a separate discovery worker alongside the WebSocket
loop. A fake-clock-testable scheduler applies startup jitter, daily jitter,
retry backoff and persisted success suppression. Discovery never blocks a
heartbeat or leases a flow slot. Ephemeral runners skip the worker.

`collectDiscoveryInventoryReport` is the shared read-only entry point for manual
and runner reporting. Elevated workers use bounded profile enumeration; user
workers keep the same config-path hashes as manual current-user reports.
`sendInventoryReportWithContent` shares the inventory serializer, downgrade
retries and redacted content pass. The content pass enforces every opt-out, prints
a one-time notice in the service log, and only uploads missing hashes.

`GET /runners/{id}/discovery-policy`, `POST /runners/{id}/discovery-reports`
and the two `/runners/{id}/extension-content` upload/negotiation routes accept
the runner token through separate routers without user authentication.
The server checks active account membership and the existing `inventory` runner
capability, granting reporting authority only. The report fingerprint must match
the runner's device, or binds it on the first report if none was stored.
Inventory reports reuse the normal ingestion transaction and post-commit
fleet findings hook. Content uses the shared validation and encrypted storage. They do not grant the token general REST access.

Account controls are stored under `meta_data.runner_discovery` through CRUD and
exposed to administrators with `manage_policies`. Account opt-out is enforced on
the report POST as well as in the collector. The separate local
`runner-discovery.json` contains scheduling timestamps and fixed result codes,
never credentials, paths from discovery, inventory contents or server errors.
