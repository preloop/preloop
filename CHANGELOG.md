# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `python -m preloop.cra measure` prints the platform's NTIA minimum-elements
  measurement for one or more CycloneDX or SPDX JSON files. SBOM Verify and
  Release Security Audit copy `passed` and `missing` from that object.
  Persisted SBOM and release audits carry the same object as
  `minimum_elements_measured` (on `sbom_audit` for a release audit).

- Plain console API keys can opt into a runtime session by sending
  `X-Preloop-Session-Id` on a gateway request. Vendor session headers and
  body-level ids still require a runtime principal, and a request with no
  valid header records usage without creating a session. Refs #912.
- `FLOW_EVIDENCE_LOG_PLAINTEXT` (default true) keeps today's Kubernetes
  behavior: without a direct-upload token, `result.json`, the evidence pack,
  and the workspace snapshot are still written to the pod log as base64.
  Set it false, and use direct upload, when those bytes must not be in pod
  logs. If plaintext is off and an execution has no upload token, the
  wrapper fails closed: no artifact bytes, and an evidence receipt of
  `failed` / `plaintext_disabled`. Encrypted log transport remains a
  separate decision in #268.

- `preloop agents install-runtime --desktop` installs a loopback-only headless
  desktop (Xvfb on `:99`, x11vnc on `127.0.0.1:5900`, Chromium) and exports
  `DISPLAY=:99`. `POST /api/v1/agent-deployments` accepts `desktop` and reports
  `installed`, `failed`, or `skipped` without failing a runtime that already
  validated. Non-root users install packages with `sudo -n`. The VNC password
  is passed only to `x11vnc -storepasswd` (briefly visible to other local
  users; VNC DES keeps the first 8 characters) and is not written elsewhere.

### Changed

- At persist, a `minimum_elements.passed: true` claim is replaced when the
  delivered SBOM bytes are missing elements, and the agent's claim is kept
  on `verdict_corrected`. The verdict floor then moves the label to `fail`.
  An agent who already failed minimum elements keeps that claim. When
  `counts_by_severity` is the only contract failure, it is recomputed from
  the findings list and recorded per key. A run is never made less severe.

- One-year usage summaries aggregate session and model totals before joining
  session, agent, flow, and principal labels, and hash the daily series by
  materialized day bucket. Per-user windows use
  `ix_api_usage_account_principal_id_ts`. Replay exclusion, retry handling, and
  the breakdown limit are unchanged. Refs #914.

### Fixed

- A gated tool call whose approval window is longer than
  `approval_park_after_seconds` parks the execution when the request is
  created, instead of polling in process for that long first. The park is
  stored before the tool result is returned, so a harness that drops the
  call still leaves a run waiting for the human. A failed, cancelled, or
  timed out execution cancels approval requests it still holds as pending.
- Native host execution profiles reject `publication_mode: isolated` before
  execution. A stored publication snapshot is no longer stripped from the
  host lease, and a missing snapshot still fails with the existing policy
  error. Container isolated publication is unchanged.
- Legacy publication now appends the current execution and head SHA to an
  existing pull request or merge request, keeping earlier records and human
  prose. A malformed, oversized, or rejected provider update leaves the
  description unchanged and is not reported as a successful publication.
  Isolated GitLab publication is still unsupported.
- Harness images pin Node and install Pi and DeepSeek from lockfiles, so
  Scorecard no longer reports floating image or npm dependencies. Empty
  `except` handlers that intentionally ignore an optional driver or an
  expected flush failure now say why.
- PR follow-up trusts a reviewer by username or GitHub App slug. Enabling
  follow-up starts with `preloop`, which matches reviews from `preloop[bot]`.
  An empty list still ignores every bot. Cursor flows no longer ask for a
  Preloop catalog model. Leave Cursor model blank to use Cursor Auto, or set
  a Cursor model id such as `grok-4.7-high` and map it on the runner profile.
  The first repair of a publication that stored no native session continues
  on the published branch when checkpoint uploads are disabled. A later
  repair still requires its own checkpoint. A repair that failed or timed
  out before it stored a session is tried again from that same published
  branch, and the review it already picked up is not dropped. That failure
  does not count as no progress, and a thread already stopped for no
  progress in that situation is picked up again.
- Pi and DeepSeek can check out a pull request on Kubernetes. The workspace
  volume stays owned by root, so Git accepted the clone and then refused
  the commit checkout as dubious ownership. Both harnesses run as that
  non-root user.

- Codex no longer opens a Preloop MCP session when both MCP allowlists are
  empty, so a failing HTTP transport cannot reconnect until the flow
  timeout. `agent_config.sandbox_type: read-only` launches Codex with
  `--sandbox read-only`, disables the `shell_tool` feature, and does not
  pass `--yolo`. `codex exec` already defaults to never asking for
  approval; the read-only config also sets `approval_policy = "never"`
  so a resume cannot wait on a person. Any other value, including the
  preset default `exec`, is unchanged.

## [0.16.0] - 2026-09-21

Highlights: **Alibaba Cloud Model Studio (Qwen)** and **AWS Bedrock** join the
model providers with live discovery and honest cost estimates, **operator
notes** steer a running agent at its next turn boundary from the console, the
CLI or another agent, **session search** makes transcripts findable by keyword
or embedding with saved searches and an index that retention and legal holds
cover, **flow composition** lets one execution start another through
`run_flow` with an execution tree, a cost rollup and a parent that parks on
`WAITING_FOR_CHILDREN`, **runners** hold several executions each and gain a
one-shot ephemeral mode behind a `run-flow` GitHub Action, **OTLP export**
ships gateway and MCP telemetry to any collector, a **repo review preset
family** adds architecture, code health, standards, docs currency and
portfolio lenses with one-page verdict covers, and the **console** gains an
Activity feed, an Inventory box, model prices you can read and set, and user
avatars.

### Added

- No-progress guard for runs that never edit anything. A live run whose
  checkout is still provably clean after `agent_config.no_progress_after_seconds`
  gets one reminder delivered into the session that is still running, and is
  stopped after a further `no_progress_grace_seconds` (default 600) if nothing
  has changed. Terminally, an agent that reports failure while the container's
  post-execution git block found no commit is now classified
  `agent_no_progress` instead of `unknown`, and
  `agent_config.retry_on_no_progress` can create exactly one retry, optionally
  on a stronger model or a higher reasoning effort. Both keys are unset by
  default, so existing flows are unaffected, and a run with any workspace
  change is never nudged or stopped by the guard.
- Price overrides can be read, edited and removed in the console: the Cost page
  lists every override with its rates, effective dates and notes, and a model's
  detail page can drop its override and fall back to the catalog price.
- A model whose requests carry no price can be marked "unpriced is expected"
  (or snoozed for seven days) from the Models list or the model detail page.
  The marker is stored as `model-unpriced:<alias>` with a stable
  `unpriced:<alias>` fingerprint, so a new unpriced request does not bring the
  model back, and the Models count, the row badge and the inbox's
  "N models unpriced" item all honour it. Restore undoes it.
- Issue triage durably reuses executions for the same issue revision and project
  context across automatic events and manual single/batch runs. Verified issue
  assessments persist as bounded, versioned context for the existing readiness
  lifecycle; they do not authorize implementation. Triage execution keys receive
  HTTP 403 on mutating REST routes; scoped MCP assessments on their bound issue
  remain supported. Persistent agent execution,
  matrix runs and delegated triage runs are rejected because those paths cannot
  preserve the required execution scope and shared revision ownership.

- Model by label: `agent_config.model_by_label` maps a complexity label to a
  model and a reasoning effort, first match wins, evaluated at trigger time
  after the existing `model_routing` rules. A rule that only names an effort
  keeps the flow's model and asks it to think harder; Codex receives the
  choice as `model_reasoning_effort` in `config.toml`. The flow form in the
  console edits the list, the chosen label and effort are logged on the
  execution, and a label that arrives on an untrusted webhook payload can
  never introduce a rule of its own. The list is empty by default, so
  existing flows route exactly as before.

- Agent harnesses are told the context window and output ceiling of the
  model they run on. Codex gets `model_context_window` and
  `model_max_output_tokens` in `config.toml`, OpenCode gets
  `limit.context` and `limit.output` in its provider model entry. Each
  number comes from `ai_model.model_parameters` when an operator set one
  and from the vendored price catalog otherwise; when neither source knows,
  the setting is left out and one INFO line says so, so the harness keeps
  its own default rather than trusting a guess. Harnesses that assumed a
  small window were compacting early and re-reading context they already
  had.

- Persistent flow execution: a flow with
  `agent_config.execution_path = "persistent"` delivers the rendered prompt
  to `target_agent_id` as one audited Agent Control `send_message`. The
  execution binds the command id, takes its terminal status from
  `command_result` / `command_error`, and the flow timeout interrupts the
  session. Missing or offline targets fail at start; there is no ephemeral
  fallback.

- Cost summaries accept optional `include_breakdown=false` and repeatable
  `breakdown` selections while preserving the full response by default.
  The Cost console shows totals first and loads tab details independently,
  with section retries and protection against stale date-range responses.

- Pi and DeepSeek Harness agents: CLI installation and onboarding, console/API
  identities, ephemeral Docker/Kubernetes/private-runner flows, native tool
  approvals, lifecycle events, and message/interrupt control of active sessions.
  The shared `@preloop-ai/harness-plugin` connects both runtimes to Preloop.
  Pi MCP startup failures keep native tools blocked and log a sanitized
  category (HTTP status or OS error code) to stderr, not the raw exception.

- `run-flow` composite GitHub Action (`.github/actions/run-flow`) and a
  guide, `docs/guide/flows/github-actions.md`. The action installs a
  pinned CLI, triggers a flow with the payload piped through stdin, and
  fails the job on the execution's verdict, writing `execution-id`,
  `execution-url` and `status` outputs first so a later step can still
  post the link. With `mode: runner` it starts a one-shot ephemeral
  runner on the job's own VM, pins the execution to it with a per-run
  label, and stops it on the way out. The guide covers both modes, token
  setup, the double-review trap when a webhook already drives the same
  flow, and what changes on self-hosted GitHub runners.

- Private runners hold several executions at once (default 2, owner
  ceiling 1..32 editable in the console, `PATCH
  /api/v1/runners/{runner_id}/concurrency`). `--concurrency`,
  `PRELOOP_RUNNER_CONCURRENCY` and `runner.concurrency` set what the
  process will hold (flag, then env, then file, then 2). Halt and
  status are per execution. A connected process can only lower the
  ceiling, never raise it, and a process that does not report
  concurrency is treated as one slot, including a rollback to a
  pre-multi-slot CLI that reuses the same runner id. The migration
  carries live single-slot leases into `flow_runner_assignment`.

- One-shot ephemeral runner mode for CI: `preloop runner fg --once
  --ephemeral [--labels ...] [--wait-for-job 15m]` registers a runner that
  belongs to a single process, runs exactly one execution, prints its
  console URL, and unregisters on every exit path (clean finish, Ctrl-C,
  SIGTERM, SIGHUP). It never reads or writes `~/.preloop/runner.json`, so
  it cannot take over a persistent runner's identity on the same host, and
  it defaults its label to `ci-<hostname>-<pid>` so a pinned flow reaches
  that process and no other. The exit status is the job's verdict: `0`
  SUCCEEDED, `1` FAILED/STOPPED/TIMEOUT, `2` nothing leased within
  `--wait-for-job`. The control plane records the row as ephemeral and
  deletes it when the runner unregisters or its heartbeat lapses past the
  online grace, instead of leaving an offline row behind; the console
  Runners page badges one while it is connected.

- Portfolio Review preset (`portfolio-review`). Discovers the
  independently built projects in one repository from manifests and build
  descriptors, asks a human which of them to review, then starts one
  child execution per selected project per lens (docs currency, code
  health, release security audit) and parks while they run. The report
  aggregates the children's own result envelopes: per project the child
  execution id, its state and its recorded cost. Projects a cap never
  reached are listed under coverage and the run still completes. The
  security lens runs only where the project ships an SBOM; elsewhere the
  row reads `not_checkable` with the reason `no SBOM available` and buys
  one "add SBOM generation" follow up. A lens that is not on the flow's
  callable list is refused rather than skipped. The selection form also
  asks how deep to read and for how much: `max_cost_usd` is a ceiling
  the human sets, never a forecast, and fan out stops when measured
  spend crosses it. The agent itself holds no write tool, so it files
  nothing and opens nothing. Two platform steps run after it exits, each
  off unless the flow configures it. `report_publication` commits the
  generated report as one file on a stable `preloop/report/<slug>` branch
  built in a throwaway worktree and opens or updates one pull request
  against it; a byte-identical document commits nothing and records
  `identical_document`, and every failure degrades into
  `result.report_publication` from a closed vocabulary.
  `follow_up_filing` turns the rows a human approved at the gate into one
  tracker issue each, keyed on a stable follow-up id so a re-run files
  nothing twice, and writes the issue identifiers back into the stored
  result. Both keys are reserved to the control plane, so an agent cannot
  author a receipt for work it has no tool to do. Guide at
  `docs/guide/flows/portfolio-review.md`.
- `models.crud.billing_preflight` reports the entitled half of the fleet:
  entitled accounts per plan, how many of them sit at or over a given seat
  or agent ceiling (seats counted as active users plus live invitations, the
  way the seat gate counts them), and the same counts for a single account.
  Read-only aggregates, no names, emails or provider identifiers.
- The Sessions search box searches session content. A typed query goes to
  the ranked content search endpoint instead of the identifier filter, and
  results render per session with a few snippets each, every snippet carrying
  its timestamp and a tag naming why it matched. Opening a snippet lands on
  that turn in the transcript and puts the query, the session and the turn in
  the location, so the link is shareable and the back button returns to the
  results. An empty box goes back to the plain list. The page states what the
  answer could not do: a partial coverage notice when the corpus stops inside
  the range being searched, and the endpoint's degraded marker when semantic
  ranking did not run.
- Stopping a flow that is parked on the flows it started stops those flows
  too, at any depth. The parent leaves `WAITING_FOR_CHILDREN` before anything
  else, so a child finishing at that instant resumes nothing and the sweep
  never picks the run up again; a child that had already finished, or that
  finishes while the stop is in flight, keeps its status, its result and its
  cost. Each stopped child records why it changed, which the execution tree
  now shows, and the stopped parent records the coverage it reached and what
  the tree had cost. Docs in `docs/guide/flows/flow-delegation.md`.
- Session embedding scope. `session_embedding_setting` carries `scope`,
  `summaries_only` (the shipped default for a new account) or `full`.
  Under `summaries_only` the worker claims only a session's own title and
  summary chunk, which is about one short chunk per session instead of the
  roughly 40 a transcript produces, so 10k sessions cost about 60 MB of
  vectors rather than about 2.4 GB plus the index. `full` embeds every
  chunk, under the same daily cap. Changing the scope in either direction
  touches no vector that exists: narrowing stops new transcript chunks from
  the next pass, widening hands the untouched backlog back to the worker.
  Keyword search still reads the whole corpus. Read and write it at `GET`
  and `PUT /api/v1/runtime-sessions/settings/embedding`; an unknown scope is
  a 422.
- Every session content search is audited. One row per call through the
  existing audit path, with `action="query"`, `resource_type="session_search"`
  and a status of `success`, `denied` or `failure`; details carry the mode, the
  filters that narrowed the read, the result count and a stable hash of the
  query. The query text itself is stored only when the account sets
  `session_search_audit_store_query_text`, because a query is often the secret
  somebody is hunting for, and even then it is cut at 512 characters. An
  unknown-scope refusal echoes at most 64 characters of the caller string.
  A search made through the `search_sessions` tool is recorded with the agent
  as the actor and `source="mcp"`. An audit write failure is logged and never
  changes the search answer. Docs at `docs/guide/session-search-audit.md`.
- `search_sessions` built-in tool. An agent searches the runtime session
  corpus before repeating work: ranked results, one trimmed snippet per
  session, a match reason and the endpoint's degraded markers. Scope is the
  calling agent's own sessions; `scope: "account"` is refused by name until an
  operator grants it, never silently narrowed. The response is size-capped and
  reports what it dropped. Default-off, so a flow selects it in its allow-list
  or an account enables it on the Tools page, and an access rule that denies it
  stops the call. Docs at `docs/guide/agent-session-search.md`.
- Sessions similar to the one being read. `GET
  /api/v1/runtime-sessions/{id}/similar` ranks other sessions of the account
  against a stride sample of this session's own chunks, using vectors the
  indexing worker already wrote: nothing is embedded, no provider is called
  and no spend is recorded, so an account at its daily embedding cap still
  gets the list. A session is only compared with chunks carrying the same
  embedding model identity, and only `clear` chunks are probes or matches.
  Each result carries its best similarity plus a coarse band (`close`,
  `related`, `loose`); the console shows the band and keeps the number in a
  tooltip, because a cosine number reads as a measurement it is not.
  Everything the comparison could not do is named in a degraded block rather
  than raised: nothing indexed for this session, no other session in the same
  vector space, only part of a long session sampled, a time window applied.
  There is no default time window: the session worth finding is often an old
  one. The console shows the neighbours in a collapsed panel under the session
  replay, each entry linking to the other session with its matching passage
  inline. Ranking constants and band cut points are tunable and not validated
  against a labelled set; the decisions behind them are recorded in
  `docs/architecture/similar-sessions.md`.
- Saved session searches. `POST/GET/PATCH/DELETE
  /api/v1/runtime-sessions/search/saved` and
  `POST /api/v1/runtime-sessions/search/saved/{id}/run` store a query, mode,
  filters and snippet preferences under a name and re-run them, under the same
  `view_runtime_sessions` permission the search endpoint takes. Nothing about
  a past answer is stored. A saved search is private until its author shares
  it with the account, and only its author may rename, edit, share or delete
  it. A run reports every saved filter that no longer resolves (a deleted
  flow, a rotated api key, a key the filter schema no longer defines) and
  still applies the ones it can, rather than silently widening the search; it
  also says whether the ranking constants have moved since the search was
  saved, which is reported rather than pinned. A saved mode that cannot run
  today comes back as the search endpoint's degraded answer, not an error.
  See `docs/guide/session-saved-searches.md`.
- Semantic and hybrid ranking on `POST /api/v1/runtime-sessions/search`.
  `mode` accepts `keyword`, `semantic` or `hybrid`. The vector half only
  scores chunks stamped with the model that embedded the query, and the two
  candidate lists are fused by reciprocal rank (`RRF_K = 60`, both weights
  `1.0`, ties broken on session id; the constants are tunable and not yet
  validated against a labelled set). Every result says which half found it
  (`keyword`, `semantic`, `both`) and carries the similarity when the vector
  half scored it. A semantic half that cannot run (no account opt-in,
  deployment kill switch off, daily cap reached, provider failure, vectors
  from another model, backfill behind) returns keyword results with a named
  reason in the degraded block instead of an error, except in `semantic`
  mode, which returns an empty set with the same marker rather than a silent
  keyword fallback. Query vectors are cached per process for
  `SESSION_SEARCH_QUERY_EMBEDDING_TTL_SECONDS` (default 300, size
  `SESSION_SEARCH_QUERY_EMBEDDING_CACHE_SIZE`, default 256), so paging a
  result set does not re-embed the query; query embedding spend is recorded
  against the same daily cap as the indexing worker.
- A session is findable by its own generated summary. Persisting a session
  title and summary (plugin title hook, usage-import metadata, or a
  gateway auto-summary) writes one `session_summary` chunk into the search
  corpus, so a search for the words that describe what a session was about
  matches the summary sentence and not only the transcript. Regenerating the
  title rewrites that one chunk, and an identical regeneration (plugin title
  hook, usage-import metadata, or gateway auto-summary) changes nothing:
  `summary_updated_at` moves only when the text changes, so the corpus does
  not delete and reinsert an unchanged chunk. A session left with neither a
  title nor a summary keeps no chunk.
  Indexing failures are logged and never fail the title write.
- Callable-flows picker on the flow editor. When the delegation tool is
  on, the form lists the account's other flows (paging past the 100-row
  list default) and lets the operator choose which this flow may call,
  with optional per-entry ceilings. Entries that do not name a flow in
  the account get a row they can clear, but only once the full list has
  loaded. The field is omitted from a save that did not edit it, and
  from a save while the tool is off.
- Operator notes reach hook path agents. The permission hook writes the
  rendered note block into the Claude Code `PreToolUse` and Codex CLI
  `PreToolUse` `hookSpecificOutput.additionalContext`, and into the Cursor CLI
  `preToolUse` `additional_context`. Codex `PermissionRequest` and the Cursor
  `before*` hooks have no field that reaches the model, so a note claimed there
  is held for its session and rides the next tool call's carrying hook, once. A
  turn with no pending note produces the same response as before.
- `preloop sessions search` queries session content from a terminal
  through `POST /api/v1/runtime-sessions/search`. The query is the
  argument, `--from` / `--to` bound the time range, `--mode` picks the
  ranking mode and `--limit` pages past `--page-size` (at most 50) by
  offset. Default output is one readable block per session: identifiers,
  timestamps, why it matched and the matching turns. `--json` emits each
  response page exactly as the endpoint sent it; degraded markers, the
  indexed-through marker and the result count go to standard error so a
  piped payload stays clean. Exit status is 0 for results, 2 for no
  results and 1 for a failure, which prints one sentence rather than a
  server stack trace. Docs in `cli/README.md`.
- `preloop notes send` posts one operator note from the terminal to
  `POST /api/v1/operator-notes`. Name exactly one of `--agent`,
  `--session`, or `--execution`. The body is the argument, or stdin when
  piped. `--expires-in` is a Go duration between 60s and 7d; omitted, the
  server keeps the note deliverable for 24 hours. `--json` emits the note
  id and target only.

- `{{name|truncate(N)}}` prompt-template filter. `N` is a byte cap, the
  cut is on a UTF-8 boundary, and a marker names the full size so the
  agent can fetch the rest. Bare `|truncate` is 16 KiB. Preset 002
  (pull-request reviewer) caps the description at 16 KiB.
- Chunked agent launch-payload environment:
  `PRELOOP_AGENT_PROMPT_0..N` / `_CHUNKS` / `_BYTES` reassembled at
  `AGENT_PROMPT_FILE` (`/tmp/preloop/prompt.txt`), and
  `PRELOOP_INNER_SCRIPT_0..N` for the Kubernetes inner script.
  `AGENT_PROMPT` (and OpenHands `PROMPT`) is set only when the prompt
  is 64 KiB or less. Custom images must not require `AGENT_PROMPT`
  above 64 KiB. Docs in `ARCHITECTURE.md` and
  `docs/architecture/flows.md`.
- Flow-execution workers run up to `FLOW_EXECUTION_MAX_INFLIGHT` hosted
  monitors per process (default 10). The monitor loop is wait-bound; other
  worker pools stay serial. Helm sets `flowExecution.maxInflight` and a
  dedicated `flowExecution.databasePool` on the flow-execution pool.
- Issue triage on the standard issue tools. `get_issue` takes an optional
  `include` list (`label_catalog`, `revision`) that returns a fresh provider
  snapshot, the permitted complexity scheme and an expected revision.
  `update_issue` takes optional `expected_revision`, `assessment` and
  `complexity_label` and then returns the triage receipt. Triage writes are
  GitHub and GitLab only, require `edit_issues`, and follow the existing MCP
  approval path. No separate triage tools are advertised. The preset's
  triage-only write restriction is prompt-enforced; owners who want a
  mechanical gate can attach an approval policy to `update_issue`. Docs at
  `docs/guide/flows/issue-triage.md`.
- DORA agent-slice exports. `GET /api/v1/exports/asset-register` lists agents,
  tools, MCP servers, models, providers and runner hosts as one flat table
  with owners, first and last seen, and attached policies; it feeds an Art. 8
  ICT asset inventory and the agent-slice lines of an Art. 28 register of
  information. `GET /api/v1/exports/incident-candidates?from=&to=` lists
  failed executions, kill-switch activations, policy denies (persisted only
  with the Enterprise audit plugin), budget denials and gateway upstream
  failures, with timestamps, correlation ids and the affected agent, for the
  Art. 17 incident process. They are candidates: classification under Art. 17
  to 19 stays with the financial entity, so no severity, major flag or
  client-impact field is emitted. Both serve CSV or JSON, both are wrapped in
  the same manifest and digest as the CRA evidence pack, and both require
  `view_audit_logs` and audit themselves. Columns are identical in every
  edition; fields a deployment cannot record are empty and named in the
  manifest. Console buttons on the Audit page, `preloop export
  asset-register` and `preloop export incident-candidates` in the CLI, and
  docs at `docs/guide/dora-agent-slice.md`.

- Execution lineage fields on the flow-execution API response.
  `GET /api/v1/flows/executions/{id}`, `GET /api/v1/flows/executions`,
  and `GET /api/v1/flows/batches/{batch_id}/executions` now report
  `parent_execution_id`, `root_execution_id` and `delegation_depth` so a
  caller can tell a root run from a delegated child without reading logs.
  Executions that predate the columns, and every creation path that does
  not set lineage, read back as roots (no parent, no root id, depth 0).
- `run_flow` builtin tool: a flow execution can start another flow of the
  same account as a child of itself. Default off, so a flow opts in through
  `allowed_mcp_tools`, and the target must also be named on the flow's
  `callable_flows` allowlist. The call is asynchronous: it returns the
  delegation task record as soon as the child row exists and never blocks
  the calling turn. The child carries `parent_execution_id`,
  `root_execution_id` and `delegation_depth`. Refusals come back as rejected
  task records with a reason (`tool_not_allowed`, `flow_not_found`,
  `flow_not_callable`, `depth_exceeded`, `cycle_detected`,
  `fanout_exceeded`), each audited with the calling correlation id. Bounded
  by `FLOW_DELEGATION_MAX_DEPTH` (default 2) and
  `FLOW_DELEGATION_MAX_CHILDREN` (default 25, the matrix fan out ceiling).
  Docs at `docs/guide/flows/flow-delegation.md`.
- Cost ceilings for delegated children. `run_flow` takes an optional
  `max_cost_usd`, lowered to the calling flow's `max_usd_per_child` on the
  matching `callable_flows` entry and refused with `budget_exceeded` when
  the delegation tree cannot afford it. A ceiling covers a subtree, so it
  cannot be avoided by delegating one level deeper, and a refusal happens
  before the child row exists: children already running are never killed to
  make room. What a tree may commit is bounded by
  `FLOW_DELEGATION_MAX_TREE_USD` (default 50) and a child nobody named a
  ceiling for takes `FLOW_DELEGATION_DEFAULT_CHILD_USD` (default 2); either
  set to 0 removes that ceiling. Children of one execution now share a
  `batch_id`, so `GET /api/v1/flows/batches/{batch_id}/executions` rolls up
  a fan out's cost, tokens and tool calls with no new endpoint. Existing
  budget policies are unchanged: this is an admission rule layered on them.
- Execution tree on the execution page. A delegating run lists what it
  started: one row per child with the flow, the label the caller passed, the
  state, the duration and the cost, expandable to grandchildren and linked to
  each child's own page. A failed child shows its failure category and a
  refused one is visibly distinct, carrying no cost. The panel totals the
  subtree (launched, succeeded, failed, refused, cost, tokens) and shows the
  run's own cost beside that total rather than added to it. A run that
  delegated nothing says so in one line. Behind it,
  `GET /api/v1/flows/executions/{id}/tree` returns the execution, every
  descendant of it and a rollup over them in the same shape the batch listing
  uses; asking a child returns that child's subtree.
- `get_execution` builtin tool: a flow execution can read the state, cost,
  tokens and result of an execution it started, or of itself. Default off,
  like `run_flow`. Scope is enforced on the server and is exactly the caller
  and its descendants; a sibling, an unrelated execution of the same
  account, an execution of another account and an id that names nothing are
  all refused with the same reason (`execution_not_found`) and the same
  message, so a refusal cannot be used to learn what exists. The answer is
  the same task record `run_flow` returns: a failed execution carries its
  failure category on the status message, and the result comes back as an
  artifact only when the execution is terminal and `include_result` is set.
  A result larger than `FLOW_DELEGATION_RESULT_MAX_BYTES` (default 16384) is
  truncated, flagged and pointed at `GET /flows/executions/{id}/result`. One
  audit row per call, permitted or refused.
- `run_flow(wait=true)` waits for every child the calling execution has
  started. It waits in process for `FLOW_DELEGATION_WAIT_SECONDS` (default
  90) so a fast child never costs a park cycle, then parks the run on the
  new `WAITING_FOR_CHILDREN` status: the container, the runner and the
  runtime token are released and the flow timeout budget pauses, exactly as
  a park on a human decision does. The parent resumes as a new execution
  that natively continues the same agent session once every tracked child
  is terminal (completed, failed, stopped or refused), with one completion
  record per call in the trigger payload under `children` and a prompt block
  with one row per call: execution id, flow, label, final state, cost and a
  result pointer. The results arrive as that next turn, not as the return
  value of the `run_flow` call, which is the same honest limitation the
  human park has. A parent whose children are still running at
  `FLOW_DELEGATION_CHILD_WAIT_SECONDS` (default 6 hours) resumes anyway with
  an expired record for each of them; the children are not stopped. The
  execution monitor sweep recovers a parent whose resume never landed, and
  concurrent child completions resume it exactly once. Docs at
  `docs/guide/flows/flow-delegation.md`.
- Per-account flow-execution admission cap
  `FLOW_EXECUTION_MAX_RUNNING_PER_ACCOUNT` (default 5, Helm
  `flowExecution.maxRunningPerAccount`). The cap counts hosted
  executions only. Work assigned to a private runner is bounded by
  that runner's own concurrency and is not counted against the shared
  allowance. An account may override it through
  `account.meta_data["flow_execution_max_running_per_account"]`. A refused
  execution stays PENDING with `queued_reason=account_concurrency_cap`.
  One flow also keeps at most one active run per tracker object.

- Review runs can start from a previous execution. The trigger payload
  accepts `previous_result_execution_id` (an execution id or the `last`
  sentinel). The runner resolves that execution inside the same account
  and writes its stored result to `previous/result.json`, or a mismatch
  marker if it cannot. Schedules gain a bounded static `payload` (20 keys,
  4096 UTF-8 bytes) so a weekly subscription can name the baseline without
  a caller on the tick. Guide at `docs/guide/flows/repo-review-presets.md`.

- **CRA Article 14 reporting: the judgement and the clock**: presets 005 and
  006 now emit a `reporting` block instead of a bare `art14_candidates` list of
  KEV CVE ids. KEV membership says a vulnerability is exploited somewhere, not
  that this product is affected, and it carries no deadline. The block records
  `actively_exploited` (from named exploitation evidence), `affected` (from VEX
  or reachability, with the source named, `undetermined` when neither
  answered), `reportable` (exactly `actively_exploited AND affected is True`)
  and the three Article 14 deadlines computed in UTC from a single
  `discovered_at`: early warning at 24 hours, notification at 72 hours, final
  report at 14 days. The validator forces `undetermined` when the KEV fetch
  failed or the scan did not complete, so silence is not read as safety. The
  audit report cover prints an ARTICLE 14 REPORTING BOX and the new
  `cra.reportable_vulnerability` webhook event fires once per candidate,
  idempotent on (execution, cve). The obligation applies from
  11 September 2026. Preloop computes the judgement and the clock and does
  not file: there
  is no ENISA submission client, the payload carries
  `not_a_legal_determination` and `filing_is_manufacturer_responsibility`, and
  the filing decision stays with the manufacturer.
- **Record retention, legal hold and period export**: an account now states
  how long it keeps each class of record (audit rows, approvals, evidence
  pack records, runtime sessions, usage) in days, with a floor of 183 days
  (six months, the AI Act Art. 26(6) horizon) that a deployment can raise and
  nothing can lower, and a 365 day default.
  `GET/PUT /api/v1/retention/settings` and `GET /api/v1/retention/purge-preview`.
  A bounded background sweeper deletes what is past retention: batches of
  1000, a batch ceiling, a wall clock budget per pass and an off-peak UTC
  window, one audit row per class per pass with the cutoff and the count. It
  is **off by default** (`RETENTION_PURGE_ENABLED`), so an upgrade never
  silently starts deleting audit history, and `RETENTION_PURGE_DRY_RUN` gives
  the counts without the deletes. A legal hold
  (`POST /api/v1/retention/holds`, mandatory reason, actor recorded, audited
  on both place and release) freezes one execution, approval or evidence
  pack: the purge skips it and the evidence janitor leaves the ciphertext
  alone past `expires_at`, so a held pack is still downloadable. Evidence
  receipts now report the real `legal_hold` state instead of a hardcoded
  false; `object_lock` stays false because Preloop cannot verify a property
  of the storage layer beneath it.
  `POST /api/v1/retention/exports?start=&end=` returns a tar.gz of one period
  (audit rows, approvals, evidence receipts, holds) with a `manifest.json`
  carrying a sha256 per member and a digest over the member list, in the same
  shape an evidence pack manifest uses. The bundle is signed as of the entry
  below; the digests and the signature show the archive is the one Preloop
  built and has not been altered since, not that the records were true when
  they were written.

- **Tamper-evident audit log and signed, verifiable exports**: audit rows are
  now sealed into a per-account hash chain. A bounded background pass gives
  each row a `chain_seq`, the previous row's `row_hash` as `prev_hash`, and
  its own `row_hash` over a canonical serialisation, so an edit, a deletion
  from the middle or a reordering breaks every hash from that point on
  (`AUDIT_CHAIN_ENABLED`, on by default: it only adds hashes, it never
  removes a record). `GET /api/v1/audit/chain/status`, `/chain/verify`,
  `/chain/segment` and `/chain/checkpoints`; the segment endpoint serves the
  canonical payloads and stored hashes so `preloop audit verify` recomputes
  every hash locally, reports the first break with its sequence and row id,
  exits non-zero on a break, and says so when its verdict differs from ours.
  Every `AUDIT_CHAIN_CHECKPOINT_INTERVAL` sealed rows a checkpoint over the
  chain head is signed, which is the anchor a customer keeps off the
  platform. The retention purge raises the chain's `pruned_below_seq` floor
  as it deletes, so enforcing retention does not read as tampering.
  Each account gets an Ed25519 signing key, stored encrypted like other
  secrets, with the public half on `GET /api/v1/signing/keys` and rotation
  through `POST /api/v1/signing/keys/rotate` (retired keys stay published and
  their signatures stay valid). Period exports carry a detached
  `signature.json` over the manifest digest, and evidence packs are signed at
  capture with the signature served on the receipt and the download headers.
  `preloop evidence verify <archive>` checks both, and `--public-key` checks
  a bundle against a key you kept yourself, without contacting us.
  What this does not do, stated in the docs as well: a compromised server can
  forge a record before it is signed, and the chain proves order and
  non-deletion within the range it names, not that the server told the truth.

- **Approval windows on a human timescale, with parked executions**: an
  approval or question can now stay open for hours or days instead of the
  fixed 5 minutes. `approval_window_seconds` is a per-flow setting (flow form
  takes an amount plus minutes/hours/days) that overrides the workflow
  default, and `ask_user`/`request_approval` accept an optional
  `timeout_seconds` bounded by that window; an account can only tighten the
  cap through `meta_data.approval_window_max_seconds`. When the window is
  longer than the in-process wait (90 s by default,
  `APPROVAL_PARK_AFTER_SECONDS`), the tool returns a `parked_for_human`
  result instead of burning the window: the execution moves to the new
  non-terminal `WAITING_FOR_HUMAN` status and the container and runner are
  released. The decision (console, mobile, API or public link) enqueues a
  resume that restarts the same agent session through the existing `_resume`
  continuation path with the answer injected, and an expired window resumes
  with an "expired" answer so the agent finishes instead of failing with
  `cra_result_missing`. The per-flow `timeout_seconds` budget is paused while
  parked (only compute time counts), the resume claim is a single conditional
  UPDATE so a duplicate decision is harmless, and reminders re-notify at 50
  and 90 percent of the window. Executions list and execution page show
  "waiting for <who>, since <when>, expires <when>". Presets 006 and 014 set
  a 3 day window for interactive waiver collection.
- **CRA runtime result.json contracts and fail-closed CI gate**: versioned
  validation for presets 004–007 at the hosted and private-runner persist
  boundary, contradiction reconciliation, and `python -m preloop.cra.ci`.
  Default release policy is a clean pass; `pass_with_findings` is explicit;
  fail and unknown verdicts cannot be accepted. Evidence archives use the
  durable size caps, gzip/tar integrity, digest binding, and content
  fingerprints so a packed rejected decision cannot bind to an accepted
  API result when envelope/SBOM fields match. Known controller
  publication/provenance/dossier/verification annotations are ignored;
  unknown agent fields are compared. Claimed approvals and waivers fail
  closed when platform authority is unavailable; due-diligence matching
  requires an exact human `request_approval` operation (AI-judged and
  auto-approved rows are rejected). Interactive release-audit waivers
  bind stored `ask_user` answers (exact finding ids and human reason);
  ambiguous `request_approval` prose, including `waive_finding`, is
  not a waiver. KEV/CVSS gate thresholds come
  from trigger/CI `gate.fail_on_kev` / `gate.fail_on_cvss_gte` (default
  KEV or CVSS >= 9.0), never from model-authored `gate.policy` text.
  Webhook URLs are redacted in errors and the webhook POST is
  not retried. Guide: `docs/guide/flows/security-audit-presets.md`.

- **Supported-release vulnerability maintenance**: opt-in product/release
  inventory, one durable item per advisory/component, implementation then
  human approval then re-audit before a new baseline. Completion reads
  controller publication receipts and evidence artifacts; agent availability
  flags, test claims, and approval JSON are not authority. Guide:
  `docs/guide/flows/security-maintenance.md`. Presets `004`–`007` are
  unchanged; `011` remains the generic implementer and `014` is a
  conservative isolated-publication overlay.
- **Durable evidence transport**: hosted containers and private Docker
  runners can upload CRA evidence packs through the existing encrypted
  artifact API (`kind=evidence`) instead of the Kubernetes log channel.
  Retrieval verifies digest and account/execution binding and reports
  missing, expired and failed distinctly. Retention is configurable
  (`FLOW_EVIDENCE_RETENTION_HOURS`) and is not a legal hold. Guide:
  `docs/guide/flows/evidence-storage.md`. Status polls use the persisted
  receipt only (`kind=evidence`); download headers carry the verified
  digest. Tracking: issues #268, #386. Private Docker completions carry the
  trusted bootstrap `evidence_upload` outcome (`uploaded` / `failed` /
  `absent`) outside agent JSON so a failed final PUT cannot leave a stale
  trap artifact marked available. The upload outcome is emitted even when
  `result.json` is missing or invalid. WebSocket complete snapshots the
  leased job before publication close and lease clear so a direct-upload
  flag is not lost when `pending_job` is committed to null.

- **Product/release provenance mapping and isolated multi-repo publication**:
  optional `product_provenance` (`preloop.cra.product_provenance/v1`) names
  a supported release, build, SBOM digest, and constituent repos+SHAs.
  Product-mode audits reject duplicate, ambiguous, unauthorized, or
  mismatched mappings against pinned, then observed, checkout SHAs and
  supplied artifact bytes. A moving branch tip is not a verified checkout.
  Agent-written SHAs are declarations, not build attestation. A
  deterministic dossier manifest records raw versus annotated result
  digests and a `kind=evidence` receipt. Hosted and private isolated
  publication can publish the CRA code-repos-plus-compliance-repo topology
  with per-repo receipts; resume keeps branch/base/head history; local
  commits and partial remotes are not success. Optional
  `git_clone_config.publication_approval` binds a human platform approval
  to each frozen candidate `(repository, branch, base, head_sha)` before
  any writer lease is minted. A missing saved execution or flow, or an
  unreadable clone config, refuses the lease instead of treating absence
  as opt-out. Human decisions have `auto_approved_reason is None`. Default
  flows without that opt-in are unchanged. Isolated publication approval is
  requested through the builtin `request_approval` tool's optional
  `publication_candidates` (exact `repository_url`, `branch`, `base`,
  `head_sha` tuples). Context text is not authority. Ordinary callers that
  omit the parameter are unchanged. Managed execution and agent API keys
  cannot approve, decline, decide, or batch-decide a publication-authority
  request; human console/JWT and token-link decisions are unchanged. Guide:
  `docs/guide/flows/product-evidence.md`.

- **Per-flow label-based model routing**: a flow can store optional ordered
  rules in `agent_config.model_routing` that map current issue labels
  (`any` / `all`) to an account-owned model and compatible harness. The
  flow's selected model and harness remain the default. The chosen rule
  or default is recorded on the execution and pinned for retries and
  native continuation from the persisted source execution; webhook
  bodies, tracker payloads, and authenticated trigger JSON (including
  planted `_resume` / `_matrix` / `_model_routing`) are ignored. Guide:
  `docs/guide/flows/model-routing.md`. Legacy executions without a complete
  recorded model/harness require an explicit new run. Durable feedback blocks
  with `model_identity_unavailable` instead of silently adopting new defaults.
- **Private-runner host execution profiles**: a self-hosted runner can advertise
  named local CLI profiles (first slice: Cursor `agent` / `cursor-agent` with
  the operator's existing local login). Flows select only the advertised
  profile name; the control plane never receives an executable, argv, env, or
  Cursor auth store. Hosted compute rejects this path. Native success requires
  a structured Cursor stream-json result plus exit 0 (`completion_protocol:
  host_exec`); Docker jobs keep the Docker launch v1 contract. Isolated
  publication and native CLI `--resume` fail closed. Cursor usage follows the
  operator's Cursor plan, not Preloop billing. Tracking: issue #450.

- **Account emergency halt**: audited gateway/tool/flow controls, persistent
  console banner, frozen approval deadlines and durable managed runtime stop
  requests with explicit termination confirmation. Recovery is staged, records
  a reason and preserves outstanding stops across process restart or re-enable.
- **Issue readiness and merge completion audits**: store reviewable acceptance
  contracts, authorize pickup using an existing project-configured label, and
  independently audit provider-verified merged code at its exact SHA. Explicit
  reconciliation can authorize revised scope after a prior implementation ends,
  preserving execution history; audit comments and bounded follow-ups deduplicate.

- **Approved flow environments and workspace recovery**: optional pinned profiles
  provide bounded setup and isolated services. Hosted executions can retain encrypted,
  scoped checkpoints and recover unpushed commits plus dirty/untracked files. Private
  runners retain leased local workspaces with quota and expiry controls. Raw private
  custom images remain supported without a named profile.
- **Issue Triage Assistant first slice**: rewrite of preset
  `issue-triage-assistant` for `issue_opened` / `issue_updated` (legacy
  `issue.opened` clones still match). Manual Run triage on a single issue
  or up to 25 selected issues via `POST /flows/run-preset`. Proposals only:
  comment plus `result.json`, no label apply, no `create_issue` /
  `update_issue`. See [issue-triage.md](docs/guide/flows/issue-triage.md).

- **Publication verification gate**: implementation flows can set
  `git_clone_config.verification` (`mode: gate` plus a trusted test
  profile). The runner re-runs required checks on the exact commit about
  to be published and refuses the push/PR when they fail or cannot run.
  Failures are classified as `verification_failed` (a required check ran
  and failed) or `verification_blocked` (a required check could not run).
  The automated-issue-implementation preset ships with the gate on and a
  conservative default that refuses non-docs code changes until operators
  add repository-specific rules.

- **`control_last_heartbeat_at` on managed agent summaries**: Agent Control
  presence now exposes the last WebSocket heartbeat timestamp on
  `ManagedAgentSummary` (list and detail, OpenAPI, TypeScript type) so the
  age of the signal is readable when debugging replica disagreement.

- **Codex ChatGPT-OAuth model auto-registration**: when Codex CLI (or any
  OpenAI-protocol client using a ChatGPT subscription-OAuth credential)
  asks for a `gpt-*` / o-series / `chatgpt-*` model the account has not
  registered (for example `gpt-6-astra` after a Codex update), the gateway
  lazily creates a sibling `AIModel` sharing the same OAuth secret and
  binds it to the requesting agent instead of answering 404. OpenAI remains
  the authorization boundary; `preloop models sync` still cannot discover
  against those credentials. Flag:
  `MODEL_GATEWAY_CODEX_FAMILY_AUTOREGISTER_ENABLED` (default on).

- **Native (no Docker) dev environment**: `docs/native-dev.md` describes
  running PostgreSQL, NATS, the API, and Vite on one host. `Dockerfile.dev`
  installs those system packages without copying the app;
  `.cursor/environment.json` uses it for Cursor Cloud.

- **Per-flow timeout budget**: `timeout_seconds` on a flow (create/update
  API, preset YAML, 60..86400 seconds) sets the wall-clock budget for one
  execution; unset keeps the deployment default
  (`FLOW_EXECUTION_MAX_WAIT_SECONDS`, 3600). A run that overruns it fails
  with a message that names the budget that expired, so a stuck run is
  distinguishable from work that legitimately needs longer. The PR reviewer
  preset ships with 1800 and the release security audit with 7200.
- **In-place completion nudge**: when an agent exits cleanly without
  confirming completion, its own container now reminds it once, on the same
  harness session and workspace, to write `result.json` and print the
  completion sentinel. The reminder runs before any push or PR creation and
  can never repeat a side effect, is bounded to one round
  (`FLOW_COMPLETION_NUDGE_TIMEOUT_SECONDS`, default 300; disable fleet-wide
  with `FLOW_COMPLETION_NUDGE_ENABLED=false`), and appears on the execution
  timeline as `completion_nudge`. Runs that used to fail as "exited 0 but
  did not produce the success sentinel", the largest failure class on
  staging, now mostly confirm themselves. For runtimes that cannot resume a
  session (Gemini, Aider, OpenHands, remote runners) a written
  `/workspace/result.json` is accepted as the completion signal instead.
- **Issue-implementation pickup and PR-comment resume**: `update_issue` can add a GitHub reaction (eyes on pickup) with no other fields. Flow prompts can use `{{execution.url}}`. Opening a PR records its URL on the execution, so a human comment on that PR restarts the same flow on the same branch. Unmatched comments do not start a run. Native CLI `--resume` is a follow-up (#356).
- **`preloop agents refresh` (alias `sync`), `preloop models sync`, and `POST /api/v1/ai-models/sync`**: refresh rewrites managed model sections of onboarded agent configs from the account catalog; models sync (and the endpoint) pull newly released provider models into that catalog using stored credentials.
- **Opt-in scheduled model-catalog sync**: `MODEL_CATALOG_SYNC_SCHEDULED_ENABLED` (default false; helm `config.modelCatalogSync.*`) runs the same discovery as `preloop models sync` for every account, attributing audit events to the `model-catalog-sync` system actor.
- **`preloop usage hook` accepts harness-agnostic events**: stdin is
  auto-detected as Cursor hooks (unchanged), generic NDJSON
  (`preloop.usage.event.v1`), or Codex CLI session rollouts. Codex
  one-shot import uses `--from codex --file`. Guide:
  `docs/guide/usage-hooks.md` (old `cursor-usage-hooks.md` path kept as
  a stub).

- **`resolve_sbom_upstreams` builtin (default-disabled)**: maps vendored
  Arduino/PlatformIO SBOM components (name + version) to an upstream
  repository URL and version-shaped tag candidates via the public library
  registries. A resolution requires a registry-confirmed name AND version
  match with a usable repository URL; everything else is unresolved with a
  reason. Default-off so regular sessions do not pay the tools/list context
  tax; security-audit presets 005 (SBOM Exploit Check) and 006 (Release
  Security Audit) allow-list it.
- **CRA result.json contract**: the four security-audit presets pin
  `/workspace/result.json` as a versioned contract (`preloop.cra.sbomaudit/v1`,
  `vulnscan/v1`, `releaseaudit/v1`, `duediligence/v1`). Tests parse each YAML
  Required shape, require the honesty line, validate example artifacts against
  those keys, and reject banned claims (`compliant: true`, `ce_mark: true`,
  "Article 14 filed").
- **CRA / AI Act evidence runbook**: rewrite of
  `docs/guide/flows/security-audit-presets.md` as a manufacturer-facing
  runbook for the shipped Apache presets (SBOM Verify, SBOM Exploit
  Check, Release Security Audit, Component Due Diligence). Opens with
  what the pack is not (Regulation (EU) 2024/2847; Art. 14 reporting
  from 11 Sep 2026; full CRA 11 Dec 2027; Preloop does not file Article
  14 reports), then the `result.json` contract aligned to the YAML
  prompts, a copy-paste CI hook (`workspace_files` plus poll `/result`
  and retain `/evidence`), and honest limits. Not a conformity
  assessment, CE marking, or certification.
- **Model I/O content policies**: instance policies can `allow`, `deny`,
  or `require_approval` on `model.request` and `model.response` using
  the existing policy engine. Built-in detectors cover PII, prompt
  injection heuristics, and a local moderation ruleset. The console
  restores `/console/policies` (sidebar next to Tools;
  `/console/governance` redirects there) as a rule-centric page. Describe
  a change edits the current policy with the account default model and
  shows a unified YAML diff that must be Saved. YAML import/export
  round-trips the new targets. Streaming buffers until the assembled
  response can be evaluated (deny cannot retract tokens already sent).
  See `docs/guide/model-content-policies.md`.
- **Private-cluster Helm install**: `helm/preloop/README.md` documents a
  ClusterIP + ingress install with private registry pull secrets, existing
  Postgres, Kubernetes Secrets (not values committed to git), and mounting a
  private CA via `extraVolumes` / `extraEnv` (`SSL_CERT_FILE`). Example
  overlay: `helm/preloop/values-private-cluster.yaml`. Compose and Helm are
  the supported install surfaces; this repo does not ship Terraform.
- **OpenAI-compatible upstream TLS**: LiteLLM completions and model
  discovery honor `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` / `CURL_CA_BUNDLE`
  (and `PRELOOP_SSL_VERIFY=false` as a last resort) so a private
  OpenAI-compatible base URL such as `https://gateway.internal/v1` works
  with operator PKI. Public OpenAI, Anthropic, and OpenRouter keep the
  default trust store (including an `openai-compatible` model whose
  endpoint is `https://openrouter.ai/api/v1`).
- **OTLP export for gateway and MCP telemetry**: optional OpenTelemetry
  export (disabled by default) emits GenAI spans for governed model
  calls and MCP tool calls, including `gen_ai.conversation.id` when a
  runtime session id is present. Token and cost attributes match the
  `ApiUsage` row for that request. Exporter errors are logged and never
  fail the user-facing call. Helm `otlp.*` values and
  `docs/guide/observability-otlp.md` cover a generic collector, Langfuse
  OTLP ingest, and Datadog OTLP ingest.
- **GitLab `issue_labeled`**: an Issue Hook whose `changes.labels` adds
  a label now normalizes to `issue_labeled` (remove-only is
  `issue_unlabeled`). Filter field `added_labels` is set on GitHub and
  GitLab.
- **Alibaba Cloud Model Studio (Qwen)** as a model provider: the add-model
  dialog names it, existing rows keep the provider id `qwen`, and Fetch
  Models lists the live catalog for the region the API URL names (Beijing,
  Singapore International, a Singapore workspace host, US), with the key
  link following that hostname to the right regional console. The chat
  adapter covers streaming, tool calls, provider usage, thinking controls
  and ephemeral cache markers; omni, speech-to-speech and translate SKUs
  are matched by family token and hidden from the chat picker. Costs are
  estimates: Singapore International USD list tariffs from the native
  catalog seed the map, live `GET /api/v1/models` prices overlay it per
  host and region, and CNY sites stay unpriced rather than guessed.
  Time-banded Singapore International SKUs use Model Studio night hours
  (22:00-08:00 UTC+8). Mixed leftover audio/vision rates stay unpriced
  when usage reports those token classes. AI approval policies honour
  the configured region.
  Docs: `docs/guide/alibaba-model-studio.md`.
- **AWS Bedrock** as a model provider. The add-model dialog asks for an
  access key, a secret key, an optional session token and a region in place
  of the API URL and key pair, and discovery lists foundation models live
  from the Bedrock control plane (`list_foundation_models`), filtered to
  ACTIVE text-output chat models; embedding, image and video ids are
  dropped. Rejected credentials answer 401, and a small bundled catalog is
  the fallback only when boto3 is missing or the control plane is
  unreachable. Secret material is masked in serialized copies the way
  `api_key` already is; submit stores the credential blob the existing
  Bedrock completion path parses plus `meta_data.provider_runtime.region`.
  `preloop agents onboard` for Claude Code warns when a truthy
  `CLAUDE_CODE_USE_BEDROCK` survives in the launching shell, naming the AWS
  variables exported beside it, because that routes traffic past the
  gateway's budgets, policies and accounting.
- **Operator notes**: a short instruction from an identified human to a
  running agent, delivered at its next turn boundary. `POST` and `GET
  /api/v1/operator-notes` send and list one against an agent, a runtime
  session or an execution, and `POST
  /api/v1/operator-notes/{note_id}/cancel` withdraws an undelivered one.
  Sending takes `control_managed_agent` and is audited before the API
  answers the author. The gateway appends a claimed note
  to the outbound body as a trailing user message in the protocol's own
  shape, after policy enforcement and before anything reaches upstream, so
  no harness polls and a turn with no note costs nothing. Notes live on the
  agent-control table behind a `kind` discriminator and `claim_note` is an
  exactly-once UPDATE, so a retry cannot deliver the same note twice. The
  console carries an Operator notes composer on the agent page and on a
  live execution, showing each note's state (pending, delivered with
  channel and turn, acknowledged, cancelled, expired) and letting an
  undelivered one be withdrawn. Docs at `docs/guide/operator-notes.md`.
- **`send_note` builtin tool**: an agent can note another agent, session or
  execution, default off, through the same store, envelope and delivery
  rail a human note uses; the only new fact is the author, which the server
  stamps and the sender cannot choose. The default scope is descent: a run
  may note the runs it started, at any depth, and nothing else, keyed on
  lineage the platform wrote rather than an argument the caller passed. A
  sibling, the run that started it, and a caller with no lineage are
  refused before target resolution completes. Wider than descent is a
  grant, not a setting: the refusal falls through to a rule evaluation on
  `send_note` and only an explicit allow widens it, so deny,
  approval-required and evaluation failure all fail closed. Every refusal
  writes one `agent.note_scope_denied` audit row. Every console surface
  that renders a note names the author and the credential kind, and the
  sessions list reports per row how many notes a session received and who
  wrote the most recent one.
- **A subagent session records its parent**: runtime sessions carry a
  nullable, indexed `parent_session_id`, derived only where a harness says
  so on the wire (OpenCode's `X-Parent-Session-Id`, and a Claude Code
  subagent turn that still sends the parent's session id alongside
  `X-Claude-Code-Agent-Id`), and the usage-hook ingest path carries the
  `parent_conversation_id` it already reports onto the row the same way.
  Null means lineage unknown, not an error: an incapable harness, a hostile
  or oversized value, and a session that names itself all leave a null
  parent with the session still created. The parent is stored as a real
  session row id, looked up or created so a subagent that reaches the
  gateway first cannot race its parent into a second row, and is
  write-once. Per-harness findings in
  `docs/guide/subagent-session-identity.md`.
- **Every approval is attributed to its agent, key, session and run**:
  `approval_request` gains `api_key_id` (nullable FK, `ON DELETE SET NULL`,
  so revoking a key does not erase the history it produced), and the MCP
  creation paths (the builtin and proxied tool gate, the approval wrapper,
  the dynamic MCP server) record who was asking instead of nothing. The
  console renders one attribution line on every approval surface ("Agent x,
  Key y, Session z, Flow run w"), each part linked and each part omitted
  when nothing names it, with an id shortened to eight characters rather
  than falling back to a label nobody can click. Approval email and push
  name the agent when the request carries one, and keep the previous
  subject, lead line and subtitle verbatim when it does not.
- **`POST /openai/v1/embeddings`** on the gateway, beside chat completions
  and responses, with the same authentication, account-scoped model
  resolution, kill switch, budget preflight, retry ownership and usage
  accounting: one call lands one `ApiUsage` row with its account, key,
  model, prompt tokens and catalogue-priced cost. The ledger and the event
  copy of the response keep the model, the usage and the count and width of
  the returned vectors, not the float arrays; the caller still gets the
  vectors. Embedding models are now kept when the vendored price catalogue
  is refreshed (46 rows added), so the recorded cost comes from the
  catalogue.
- **Full-repo review preset family**: three read-only whole-repo presets
  sharing one skeleton, `008-architecture-strategy-review` (declared intent
  versus observed structure), `009-repo-code-health-review` (five health
  lenses over a sampled pass) and `010-standards-compliance-walk`
  (payload-named standards to a requirement register, refusing to run
  without a named standard), plus `016-docs-currency-review` as a fourth
  lens: it extracts five checkable claim types from a project's
  documentation (entry points, services, dependencies, environment
  variables, build or run commands), verifies each against the code with a
  recorded search, and emits a drift list of claim, document pointer and
  what the code shows under `preloop.review.docscurrency/v1`. Prose
  quality, tone and completeness are never reported and no documentation is
  ever written. Shared guarantees: command-only inventory, deterministic
  sampling with declared coverage, depth and budget knobs, empty MCP
  allowlists, a 006-style evidence pack, freeze-floor drift, evidence
  pointers on every claim, and a register that can never upgrade a verdict.
  Security lenses stay with the 004 to 006 audit family, referred to and
  never duplicated. Every report in the family now opens with the same
  one-page three-box verdict cover 006 mandates, adapted per lens and with
  the verdict sentence first; `result.json` is unchanged. Guide at
  `docs/guide/flows/repo-review-presets.md`.
- **The release security audit can be scoped to one project**: payload
  `project_path` makes one project inside a larger repository the unit of
  audit, the way the code health review already takes a path selector. SBOM
  lookup, the gap register file walk and every evidence pointer stay inside
  that path, and the result envelope records what was audited in an
  additive nullable scope block; no path means the whole repository and
  unchanged behaviour. A project with no SBOM of its own is reported
  `not_checkable` with its reason. This family verifies SBOMs and never
  generates them, so there is no fallback to manifests and never a
  neighbour's SBOM.
- **Per-source screening matrix and governed waiver inputs** for presets
  005 and 006: `db_resolvable` becomes a component-by-source coverage
  matrix, with `osv_purl` and `osv_git` (OSV git-range and commit queries
  through the `vcs_url` qualifier) as database sources and `nvd_cpe` and
  `osv_distro` labelled as heuristics, one negative control per source, the
  full matrix in `evidence/source-matrix.json`, and cover-page coverage
  lines derived from it. Heuristic hits never enter the severity gate.
  Preset 006 accepts an optional human-authored waiver file (id, reason,
  author, date) factored into the gate deterministically: an unwaived fail
  stays fail, every entry is echoed verbatim in `evidence/waivers.json` and
  listed on the cover page, and a model can never author a waiver.
- **Opt-in repo-audit MCP**: tools that emit classifiable SHA-plus-path
  rows, never values, so the release security audit can freeze a gap
  register instead of rediscovering the same findings on every run.
- **Generic Automated Issue Implementation preset**: the OSS implementation
  flow has one job, to read the issue, implement it, test, lint, commit to
  the checkout and report in `result.json`. Pushing and opening the pull
  request stay with the flow (`git_clone_config.create_pull_request`), so
  the agent carries no tool that can publish anything, and eligibility is
  decided by `trigger_config`, which already filters labels, instead of a
  prompt guard. Guide at
  `docs/guide/flows/automated-issue-implementation.md`.
- **PR feedback continuation is configurable from the console**: a flow
  form can turn on continue-implementation-after-PR-review and edit its
  controls, and a successful execution that published a pull request offers
  a preview of the follow-up on the execution detail page before it is
  adopted. Turning the option on does not merge a pull request. Guide at
  `docs/guide/flows/durable-implementation-feedback.md`.
- **Weekly model price review preset and a reviewed price feed**: preset
  `015-weekly-model-price-review` (disabled by default, cron Monday 06:00
  UTC) compares supported provider prices with the catalog, records
  first-party evidence, and prepares a pull request carrying the price
  fixes and a reviewed runtime feed. It publishes that pull request and
  never merges it or activates a price on an account. The optional
  reviewed-feed service runs in each API, dedicated gateway and worker
  process and fetches an operator-controlled HTTPS JSON artifact every six
  hours (`MODEL_PRICE_REFRESH_URL`, `MODEL_PRICE_REFRESH_ALLOWED_MODELS`,
  `MODEL_PRICE_REFRESH_INTERVAL_SECONDS`; an empty URL, the default,
  disables polling), so publishing a new artifact updates current estimates
  without restarting them. Docs at `docs/guide/model-price-refresh.md`.
- **A model's price is visible and settable on its page**: a Pricing card
  reports input, output, cached-input and per-request price in USD per
  million tokens plus which source produced them (an account override, the
  model's own configuration, the provider catalog, or nothing at all, which
  is why some requests land unpriced). `GET
  /api/v1/ai-models/{model_id}/pricing` resolves in the gateway's own
  order, so the card cannot claim a price the
  cost estimator would not use. Editing writes an account price override in
  force from a date the operator picks; an empty field stays empty instead
  of becoming $0, because "we do not know" and "free" price differently,
  and a saved price then offers "Apply to past usage since <date>", which
  reprices every gateway row in that window and reports how many changed.
  `POST /api/v1/ai-models/{model_id}/pricing/fetch` reads the prices
  OpenRouter publishes and fills the form without saving, because a price
  rewrites
  what past requests cost; a provider that publishes nothing says so on the
  button instead of failing when pressed.
- **Unpriced and zero-priced requests are counted apart**:
  `get_gateway_usage_by_model` now returns `unpriced_request_count`,
  `zero_priced_request_count`, `failed_request_count` and `last_request_at`
  on the same unpriced condition the account-level summary uses, so the
  Attention list can tell a hole in the price list from a free tier.
  Unpriced stays a warning; zero-priced becomes a low-tone item with one
  Expected action. An account override in force counts as a price,
  including $0, while an override that is off, not started or ended prices
  nothing. A server that sends neither count falls back to the old test on
  estimated cost, so nothing disappears.
- **Each budget period carries a forecast**: a global budget row shows a
  straight-line projection under the spend ("On track for $120.00 by Sep
  30"), amber once the forecast passes the soft limit and red once it
  passes the hard one. The projection is linear and says so in its tooltip.
  Nothing is shown before a tenth of the period has elapsed, where one
  expensive minute after midnight would forecast a five-figure day, nor for
  all-time budgets, which have no end to aim at. Period bounds come from
  the server when it sends them; otherwise the console mirrors the server's
  alignment, weeks from Monday included.
- **Activity feed, Inventory box and Users tab on the console Overview**:
  the feed is one time-ordered column (a tone dot, one line, the time, and
  a link to the most specific page that can act on it), filled from the
  last 24 hours of the audit timeline and kept up over the topics the page
  already subscribes to, capped at 30 rows and deduped by event id, with
  successful gateway calls and session heartbeats dropped. A row expands in
  place onto the fields that matter for its kind, a tool line leads with
  the caller, and a run of identical tool lines folds into one row with a
  count. From 1200px the feed is a sticky rail bounded to the viewport with
  its own scroll. The Inventory box puts four counts in tab labels over one
  table (agents, flows, models, tools), reuses the Usage card's range
  rather than offering a second one, and remembers the tab in
  localStorage. On Cloud and Enterprise a fifth Users tab lists who is on
  the account, their role, when they last logged in, how many agents they
  own and what those agents spent in the range; OSS is one operator per
  account, so the tab is absent rather than there and empty.
- **An agent's available models are editable in the console**: the Models
  and Spend tab gains an allow toggle per configured AI model plus a manual
  override for aliases that are not configured yet, persisted through the
  existing governance PUT. Budget edits no longer derive `allowed_models`
  from the budget keys, so a model can be granted or revoked without also
  being given a budget. Generated agent configs now list every authorized
  gateway model at session start instead of only the primary one (the
  primary stays the default), so the harness model picker shows what the
  console granted.
- **User profile avatars**: `avatar_url` and `avatar_source` on the user
  row, filled from the SSO provider's picture (Google, GitHub, GitLab) or
  by upload through `PUT /api/v1/users/me/avatar` and removed with the
  matching `DELETE`. An upload is validated, EXIF-stripped, cropped to a
  centre square, resized to 256x256 and stored as a base64 data URI.
  Precedence is manual upload, then the SSO image, then the initials
  placeholder, so an SSO refresh never overwrites a picture somebody chose.
- **`GET /api/v1/auth/users/me` returns `id`, `account_id` and `team_ids`**
  (teams inside the caller's own account, ordered by team name), purely
  additive on the wire. A client can now answer "is this pending approval
  waiting for me?" against a workflow's `approver_user_ids` and
  `approver_team_ids` instead of reading an `id` that was never there.
- **`@preloop-ai/opencode-plugin`**: an in-process OpenCode plugin that
  routes tool-permission approvals through Preloop Agent Control, mirroring
  the openclaw and Claude Code integrations, and forwards operator turns
  and stop commands from Agent Control into the local OpenCode session with
  delivered, acked and result status events and message-id dedupe. The
  opencode, openclaw and hermes plugins also refresh the gateway model list
  on WebSocket open and at session start, deriving the `GET /models` URL
  from the Agent Control WebSocket URL and reusing its bearer token, so a
  model granted in the console reaches the local picker without restarting
  the agent.
- **`preloop cursor`**: interactive `preloop cursor` is a TTY passthrough,
  because cursor-agent only emits structured events in `--print` mode.
  `preloop cursor run` injects stream-json, ships the usage it can measure
  as estimated, and never fabricates token counts. Docs in
  `docs/guide/cursor-cli.md`.
- **Per-conversation rollup for imported usage**:
  `imported_usage.usage_by_conversation` on `GET /api/v1/cost/summary`
  extends the existing imported-usage block rather than adding an endpoint,
  and the console cost summary renders it. Estimated and reconciled costs
  are separate fields per conversation and are never combined, a sum with
  no contributing rows stays null rather than 0, rows with no conversation
  id (CSV and JSON batch imports) are excluded, and
  `parent_conversation_id` is surfaced so a subagent conversation can nest
  under its parent thread.
- **Cloud billing surfaces (billing plugin only)**:
  `/console/settings/plan` renders the public pricing cards and comparison
  table from one shared component, with the account's own plan marked, an
  annual default, and a per-card action: an upgrade applies immediately and
  quotes the prorated amount, a downgrade or a move to Free takes effect at
  period end and never refunds, and an account with no subscription goes
  straight to checkout. An anonymous click on a cloud plan opens Stripe
  checkout for that plan and interval; the completed session creates the
  account and the welcome page collects the name and password, with the
  address marked verified because checkout supplied it, and a cancel at
  Stripe returns to the pricing page saying nothing was charged and no
  account was created. Someone who registers without a plan sees a one-time
  trial step on their first console visit, with the answer recorded before
  Stripe opens, so declining, cancelling, or a trial that ends without a
  card all land on Free and the step does not come back. A usage nudge
  banner replaces the upgrade prompt that used to open on first load: one
  line per limit at or past half of a plan ceiling, with the number in it,
  dismissable per limit and repeated at the next band (50, 80, 100
  percent), from `GET /api/v1/billing/nudges`. A 404 from that route means
  no nudges, so an OSS console renders no banner, no history cutoff row and
  no modal. The emergency (kill switch) controls move to
  `/console/settings/emergency`, off the billing surface.

### Changed

- The issue implementation preset asks for a commit at each milestone, with
  the first one within 20 minutes of the first edit and WIP commits
  explicitly allowed, so a run that is cut short keeps the work it already
  did instead of leaving an empty branch. Phase 1 now asks the agent to read
  by range with `grep -n` and `sed -n` instead of reading whole files, which
  leaves context for the edits. Flows derived from this preset are flagged
  with `preset_update_available`.

- Enable the Policies console by default for users with policy permissions.
  Operators can still hide it with `PRELOOP_POLICIES_CONSOLE=false`.

- CI prefers a matching system Python (through a venv) and only then
  falls back to `actions/setup-python`. The action has no Debian 12
  builds, so a self-hosted bookworm runner with `python3.11` already
  installed used to fail before any test ran. Distro Python is PEP 668
  managed; the venv is what makes `pip install` legal. Needs
  `python3.11-venv` on Debian. Public `ubuntu-latest` jobs are
  unchanged: they have no system 3.11, so they still use setup-python.
- Self-hosted backend shards run in a `python:<version>-bookworm` job
  container and reach Postgres by service hostname, so they do not bind
  host 5432. Two runner processes on one VM can run shards together.
  Public `ubuntu-latest` backend jobs stay on the VM with
  `localhost:5432`.
- Self-hosted GitHub runners are extra CI capacity, not a replacement
  pool. Frontend, plugins, and coverage stay on `ubuntu-latest`. Backend
  keeps its eight hosted shards by default; idle self-hosted Linux/X64
  runners take only overflow shards (the tail of the matrix, in a
  `python:<version>-bookworm` job container so they do not bind host
  5432). Sending every test job to three VMs serialized the suite and
  was slower than public runners.
- The setup-ci-python composite invokes its helper via
  `GITHUB_WORKSPACE`, not `github.action_path`. The latter is a host
  path and does not exist inside the self-hosted job container.

- Session embedding that was already opted in now embeds only a session's
  title and summary (`summaries_only`) after this upgrade. There is no
  flag that keeps the previous "every chunk" behaviour. An account that
  wants the old behaviour sends `PUT
  /api/v1/runtime-sessions/settings/embedding` with `{"scope": "full"}`.
  Narrowing deletes no existing vector; widening hands the untouched
  backlog back to the worker.
- Brand pricing config: `landing.pricing.deployment_options` is no longer
  read. The Dedicated tab is `landing.pricing.dedicated` (same card-plus-table
  shape as Cloud) with optional `cloud_label`. A leftover
  `deployment_options` key is ignored and will not render. EE brands.yaml
  already ships the replacement block.

- Helm gateway Deployments set `PRELOOP_SERVICE_ROLE=gateway` (API pods
  set `api`). `create_app` lazy-imports control-plane routers so a gateway
  process does not load flow orchestration or MCP HTTP. LiteLLM defaults to
  its bundled price map (`LITELLM_LOCAL_MODEL_COST_MAP=true`) unless the
  operator already chose otherwise. Account-governance, live-price
  negative, and Responses-capability caches cap at 4096 entries, and
  LiteLLM's retained stream-chunk list is dropped after cost copy.
- Gateway memory request is 768Mi (limit 2Gi). HPA minReplicas 2 / max 5
  with a 90% memory target. Hosted idle RSS is ~650Mi; a 256Mi request
  made HPA report ~250% and pin at maxReplicas while CPU was idle. More
  replicas copy that idle RSS. Use maxReplicas for real CPU/traffic, not to
  paper over an undersized request. Search-corpus indexing is queued off
  the response path, bounded by `GATEWAY_USAGE_INDEX_QUEUE_MAX_PENDING`
  (default 256) and `GATEWAY_USAGE_INDEX_QUEUE_ENABLED`. Dedicated gateway
  pods still run no audit-seal, retention, or optimization passes, so at
  least one `api` or `all` process must remain.
- CodeQL advanced setup uploads SARIF so Scorecard SAST sees every push and
  pull request. Disable GitHub default CodeQL setup or the upload is
  rejected.
- `execute_flow` / `resume_flow_execution` NATS publishes set `Nats-Msg-Id`
  `{task}:{execution_id}` so the 2m duplicate window collapses reaper
  republishes of the same unclaimed execution.
- Preset 001 (Issue Triage Assistant) writes remaining scope, acceptance
  and readiness onto the issue body and applies a complexity label.
  Operators who sync this preset to linked flows move from a
  proposals-only assessor to an issue writer.
- Failed implementation publication keeps the configured PR/MR when
  commits were already pushed. A failed `result.json` no longer refuses
  publication: the publisher opens a disclosed, non-closing PR/MR with
  `Refs` and the execution link. Configured verification still gates new
  pushes.
- **CRA VEX suppressions are applied before the severity gate, not after it**:
  preset 006 asked for VEX and the gate in one breath, so a run could escalate
  a finding to a human and then annotate it as `not_affected`, which made
  authoring VEX cost an approval interrupt instead of saving one. Order is now
  stated and deterministic, and `gate.vex_suppressed` must equal the set the
  body implies, field for field. A status only suppresses with a non-empty
  justification beside it: a bare `not_affected` stays in the gate, where
  before it was dropped from the gate silently. `affected` and
  `under_investigation` never suppress.
- bcrypt 5.0.0 raises on secrets longer than 72 bytes instead of truncating.
  New passwords stay capped at 72 characters. Login and `current_password`
  do not: hashing and verify use bcrypt's 72-byte prefix so existing longer
  passwords still authenticate (the same truncation passlib used to apply).
  Forgot-password remains available to set a new password under the cap.
- CRA `dossier_manifest.evidence` no longer reports a run's evidence pack as
  `missing` while `evidence-status` reports it `available`. The dossier is
  built before finalize persists the captured pack, so `load_evidence` sees a
  stale row; the orchestrator's in-memory captured receipt (the same receipt
  finalize stores) now fills that window, and a genuinely failed or expired
  DB receipt stays authoritative.
- The flow form only offers PR-dependent options where they apply. PR review
  and CI follow-up render when "Create a pull request on commit" is checked,
  and the success comment on the triggering issue also requires a tracker
  trigger with at least one issue or comment event. Hidden sections are
  preserved byte for byte in the submitted payload, since a flow can also open
  its pull request through the MCP `create_pull_request` tool.
- Security-maintenance repair: rebuilt SBOM ingest after approval, controller
  checkout from frozen publication records (not agent-writable `HEAD.txt`),
  per-component screening, background reconcile without
  GET, per-request approval policy, and managed-credential denial on console
  approval routes. Recheck removal is derived from the submitted SBOM bytes
  (advertised CycloneDX/SPDX JSON), not from model inventory omission.
  Baseline acceptance requires exact `release_id` and the digest of the
  supplied SBOM bytes on the controller envelope. The initial-baseline audit
  is scheduled through
  `POST /api/v1/security-maintenance/releases/{release_id}/baseline/audit`,
  which commits the execution then dispatches it through the existing flow
  trigger path.
  Omitted or null SBOM component lists, unsupported format versions, and
  malformed nesting cannot prove component removal.
- Abandoned security-maintenance dispatch claims expire after the same
  interval as flow-execution recovery (default 120 seconds). Sweep and the
  initial-baseline retry route redeliver a still-`PENDING` execution id;
  a live claim is not duplicated, and a started or finished execution is
  not restarted. Legacy `dispatching` records without a timestamp are
  treated as expired. Claim helpers flush and re-read the locked row and
  bound execution with `populate_existing` so a second session cannot
  finish or redeliver from a stale identity-map copy.
- Security-maintenance audit and recheck completion observes frozen Git
  bundles even when isolated publication is off. A hex40 pin plus
  `git_clone_config.repositories[].repository_url` produces a controller
  `product_provenance` mapping; agent `HEAD.txt` and forged
  `sha_status=verified` rows cannot establish checkout. Hosted and
  private post-exec export `evidence/branch.bundle` for those opted-in
  audits even with no code changes, no target branch, and publication
  off. The export does not commit, push, open a pull request, or mint
  writer credentials. Isolated publication is unchanged. The mapping is
  checkout observation, not signed build attestation.

- **Issue-duplicates AI errors use `{code, message}`**: `GET /issue-duplicates/check` and `POST /ai-suggestion` return `detail` as `{code, message}` instead of a string. `no_default_ai_model` is HTTP 422; `ai_model_error` (model-call failure) is still HTTP 500. Clients that parsed `detail` as a string need to read `detail.message`.

- **GitHub merged PRs emit `pull_request_merged`**: a closed-and-merged pull request is no longer normalized as `pull_request_closed`. GitLab Job Hooks expose `build_name` / `build_status`, and GitHub issue close exposes `state_reason`, so flows can filter `deploy:staging` success and merge-completed closes. The event pickers list Job Event and Deployment.

- **GitHub CI backend tests run in parallel, across 8 pytest-split shards**:
  the backend unit suite is sharded across GitHub Actions jobs with
  pytest-split, each with its own Postgres, so PRs are no longer gated on a
  single ~12-minute pytest process. Four shards left group 1 as the
  wall-clock pole (~7-9m vs ~3-4.5m), so eight `duration_based_chunks`
  slices split that first quarter in half. Coverage from the shards is
  combined before the 60% floor is applied.

- **Blog posts can show a hero image**: `og_image` in frontmatter is
  rendered as a figure under the tags on the post, as a linked thumbnail
  on `/blog`, and a missing `og_image` logs a build warning.

- **Policies console hidden behind `policies_console` (off by default)**:
  the page is still being reworked, so `/api/v1/features` now advertises
  `policies_console: false` unless an operator sets
  `PRELOOP_POLICIES_CONSOLE=true`. Instance admins (`is_superuser`) still
  see the sidebar link and the page. A direct `/console/policies` URL
  without access renders the usual permission-denied surface instead of an
  empty shell, and `/console/governance` still redirects there. Backend
  policy APIs are untouched, and per-tool policy on the Tools page is
  unaffected.
- **Policies page reworked into a working editor**: primary actions
  (Describe a change, Add rule, Import YAML, Export YAML) now live once in
  the view header, matching Tools, so Export is no longer duplicated and
  Import no longer hides inside the YAML tab. The YAML tab is a live editor
  over the active policy: it loads the current export, validates through
  `POST /api/v1/policies/validate` and shows schema errors inline, and only
  applies YAML that validates. Version history stays below the editor and
  the format example moved into a collapsed section.
- **YAML editor Save shows a diff first**: editor Save now uses the same
  `previewPolicyFile` flow as Import YAML, so applying a full policy cannot
  silently drop rules, MCP servers, or workflows. Validate-before-save,
  inline schema errors, Revert, and version history are unchanged.
- **Public markdown routes come from discovered content files**: `lit-app`
  registers `/terms`, `/dora`, and other static pages from
  `BRAND_CONFIG.static_markdown_pages` (Vite scans `content/<brand>/*.md`
  and `resources/*.md`). Nginx serves any `/<slug>.html` the build emitted
  instead of an allowlist. OSS does not hardcode EU instrument paths; EE
  adds a page by dropping a markdown file.

- **Provider model pickers are live-only**: bundled fallback catalogs
  are gone. A failed or keyless listing returns an empty picker with a
  safe `source`/`error` reason (timeout, network, empty_response,
  missing_endpoint, sdk_missing, missing_key, auth) instead of a stale
  guess. OpenAI STT/TTS ids are filtered from the same live
  `GET /v1/models` list. The pricing table is unchanged.
- **README is the product intro, not the operator manual**: ~390 lines
  down to ~200. Locked category line and lead, install + evidence first,
  ops (TLS, SMTP, Agent Control internals, QM proxy, smoke tests) moved
  to docs.preloop.ai and in-repo docs. Agents get a repo map
  (ARCHITECTURE.md by section, AGENTS.md, CONTRIBUTING.md). Capability
  pass after inventory: Flows and Talk named; Audit/AI Act pack is
  Cloud/Enterprise (`audit_logs`); `preloop policy apply` next to the
  YAML sample; trackers as flow triggers; imported usage in Cost.
- **ARCHITECTURE.md is an index of per-subsystem chapters**: subsystem
  docs moved to `docs/architecture/*.md`. The index is the map; read
  the chapter for the subsystem you are changing. Flows architecture
  lives at `docs/architecture/flows.md`. Empty leftover headers in
  `docs/architecture/overview.md` were removed. Redaction comments
  in `approval_service` point at `docs/architecture/security.md`.
  Frontend test/auth conventions that were only in
  `frontend/CLAUDE.md` now live in `frontend/README.md`; the stale
  file is not restored.
- **Named-instrument EU pages**: SaaS landing can ship `/cra-readiness`,
  `/dora`, and `/nis2` next to `/ai-act-readiness` when those markdown
  files exist. Each page names the regulation and article or date.
  Homepage FAQ can repeat the not-a-law-firm disclaimer. Evidence packs
  stay Apache presets, not an edition gate. Page titles and descriptions
  are brand-parameterized, and the footer links only the regulation pages
  a build actually pre-rendered.
- **Editions table lists differences only**: OSS is one operator per
  account. Users, teams, and RBAC are Cloud / Enterprise. Cloud is
  managed hosting; Cloud and Enterprise include support plans. Dropped
  Yes/Yes capability-tour rows and overclaims (CEL, AI-driven/quorum
  evaluation, AI Act pack, chargeback/forecasting as edition gates).
  CEL, AI-driven approvals, and quorum evaluation are OSS. Chargeback
  and forecasting stay Cloud / Enterprise cost features; they were
  dropped from the table because they are not users/teams/RBAC
  edition gates, not because they went away.
- **CRA / AI Act evidence named as an OSS use case**: README intro and
  What-you-get name the security-audit presets (`result.json`) as machine
  evidence, not a conformity assessment. Editions table still lists only
  users, teams, and RBAC.
- **Overview Top Models shows a preview per model**: each model lists its
  top four agents/flows/sessions by spend or usage, with a See N more
  control when there are more. Expanded groups cap nested sessions the
  same way so a busy model cannot dominate the card.
- **Codex onboarding is config-only**: `preloop agents onboard` no longer
  installs a `~/.local/bin/codex` PATH wrapper. Codex only requires a
  process environment variable when `env_key` or `bearer_token_env_var` is
  set; if `env_key` is set and the var is missing, Codex errors and never
  falls back to an inline token. Desktop onboarding writes
  `experimental_bearer_token` and inlined MCP `http_headers` instead, so
  Homebrew's `codex` can run without a wrapper. The flow runner still uses
  `env_key` because it launches Codex as a subprocess. Re-onboarding
  removes leftover Preloop wrappers. Gemini CLI still uses a wrapper
  because it reads gateway credentials from the environment.
- **Cloud and Self-hosted are the top-level axis of `/pricing`**: both tabs
  render through the same card row and comparison table, so they cannot
  drift into different layouts, and the tab bar is full width under the H1
  with `role=tablist` and its own one-line lead per tab. Monthly versus
  Yearly is a compact pill above the card row, shown on Cloud only, and it
  rewrites the price lines without changing the set of plans or table rows;
  Self-hosted editions are quoted, not bought. Price formatting moved into
  one module shared by the hydrated card and the server-rendered markup, so
  a crawler and a browser read the same sentence from the same numbers, and
  every plan follows one rule instead of the Business card carrying a
  special case. The default Self-hosted tab label is "Self-hosted"; the
  `dedicated` config key and the plan ids are unchanged.
- **Console visual refresh**: card, alert and empty-state styling is
  unified on the proposed dark glowing treatment, header action buttons are
  de-duplicated instead of appearing once in the view header and again in
  the list, empty states across trackers, flows, agents and governance
  share one pattern with the action that fills them, flow executions gain
  filters and copy actions, an audit event can be linked to and copied, the
  Overview gives model names room and ages its "Updated ... ago" line, and
  a custom agent's owner is told what that agent can actually do. Talk is
  also in the agents list kebab, disabled with the rest of the menu when
  Agent Control is not connected; cards and canvas nodes keep their own
  button rather than gaining a duplicate.
- **The Usage card keeps its numbers up while a new range loads**: the last
  range stays on screen at 60% opacity with a spinner in the header instead
  of dropping back to skeletons, the card says "from rollup" when the
  server reports the totals came out of a pre-aggregated rollup, and it
  adds "Long ranges take longer" under a year that has been loading for
  more than two seconds. A server that does not roll up sends neither
  provenance field, and the card then claims nothing about where the number
  came from.

### Fixed

- **`@preloop-ai/openclaw-plugin` 0.3.1**: `config.enabled=false` now
  registers nothing (no Agent Control channel, no tool-call hook), matching
  the manifest. Previously the flag was advertised and ignored (#857).
- Refresh the vendored model price catalog so Gemini 3.8 Flash is priced from
  the catalog, cache-read rate included, for both the `google` and `gemini`
  provider spellings (#850).

- Apply response content policies to returned reasoning and thinking text as
  well as final answers, including buffered streams and reasoning summaries.

- Private runner registration and the WebSocket hello send an empty
  `host_exec_profiles` list when none are configured, and registration
  accepts JSON `null` so older clients cannot be locked out (#838).
- Keep repeated model policy approvals on the application event loop, including
  background optimization jobs, so pooled database connections remain usable.
- Hermes fail-closed errors name the config file that was read and the
  `HERMES_HOME` / `HOME` values that selected it, so a systemd user unit
  pointing at a different YAML is visible. Discovery prefers the candidate
  (`config.yaml` or `config.yml` under `$HERMES_HOME` then `~/.hermes`)
  that actually contains `preloop.control`. Offboard, `preloop agents
  restore Hermes`, and `preloop agents install-plugin Hermes` restart the
  gateway the same way onboarding does, and on Linux print `hermes-*`
  systemd user units with the exact restart command. Guide:
  [hermes.md](docs/guide/hermes.md). `preloop-hermes-plugin` 0.3.1.

- Alibaba Model Studio flow estimates cover the Singapore International
  native catalog, not only chat SKUs. `GET /api/v1/models` is fetched
  without a text-generation filter so image, embedding, TTS, ASR, omni,
  and time-banded DeepSeek rows keep their first-party list prices.
  `deepseek-v4.1-flash` uses Model Studio night hours 22:00-08:00 UTC+8.
  Reviewed feeds round-trip `time_bands` instead of crashing or flattening
  a 2x idle/busy gap. Omni usage that reports audio or vision tokens on
  prompt or completion fails closed instead of using the text chat pair.
  Native rows that publish no prices stay unpriced. See
  `docs/pricing/reviews/2026-09-19-alibaba.md`.
- Claude Code onboarding through AWS Bedrock keeps the Bedrock inference
  profile (`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) instead of
  rewriting `/model` selectors such as `sonnet` to the Anthropic Messages
  name `anthropic/claude-sonnet-4-5`. Gateway completions use LiteLLM's
  Converse route (`bedrock/converse/<id>`). A leftover slash-form vendor
  name is rewritten to the dotted Bedrock id
  (`anthropic.claude-sonnet-4-5`). Discovery also lists system inference
  profiles (`bedrock:ListInferenceProfiles`); a permission miss keeps
  foundation model ids and logs a warning.
- Native MCP calls commit remaining database work on success and roll back on
  failure or cancellation. Database errors are sanitized in both raised errors
  and compliance batch results, while unrelated provider errors retain their
  original status and detail. Failed batch items no longer poison the next
  item's database transaction (#805).

- Isolated publication checks receive the exact published base/head range and
  retain profile, environment and selection evidence. Successful checks can be
  reused only inside the controller for identical execution, artifact, profile
  and runtime inputs after confirmed teardown. Unavailable checks and runtimes
  are classified as `verification_blocked`, separately from failing tests.
  Failed durable repairs keep their latest workspace and conversation while
  recovering the prior PR binding through validated execution ancestry,
  including explicitly adopted publishing executions. New commits still pass
  the current verification gate before publication.

- Saved flow details expose the effective publication policy, including ungated
  legacy flows and configuration blockers, without claiming that configured
  isolation is an execution verification receipt.
- Hosted workspace recovery preserves never-pushed branches and their base
  commit identity across repeated checkpoints. Remote absence, divergence and
  connection failures have distinct outcomes. Codex command transcripts no
  longer masquerade as container/setup failures. The optional direct-checkpoint
  Helm overlay supports 64 MiB archives with matching proxy limits.

- Codex CLI enrollments share one OAuth SecretReference per managed agent
  instead of minting a second single-use lineage per model family. A
  re-onboard of a split pre-fix enrollment repoints family rows onto the
  live sibling secret (newest `last_verified` / `updated_at`), rather
  than rotating another grant and tripping provider reuse detection.

- Private-runner launch and server logs report the configured container image,
  including the legacy `docker_image` alias, instead of the harness default.
- CI feedback uses bounded current-head GitHub Actions job evidence to separate
  runner setup failures from code failures. Startup failures and explicit
  pipeline infrastructure reasons use bounded escalation rather than code repair.
- Agents in a split Kubernetes deployment call the gateway Service instead
  of the API Service. API pods run `PRELOOP_SERVICE_ROLE=api` and never
  mount `/openai/v1`, so a model row without an explicit
  `meta_data.gateway.url` sent every model call to a 404 and failed the
  flow execution with no upstream request. The chart now renders
  `PRELOOP_MODEL_GATEWAY_URL_K8S` (`gateway.inClusterUrl`, default
  `http://<release>-gateway:80/openai/v1`) on the API and worker pods, and
  the resolver falls back to the sibling `-gateway` Service. Runners keep
  using the public `${PRELOOP_URL}/openai/v1`. See
  `docs/operations/model-gateway-url.md`.
- The console keeps a model's `meta_data.gateway.url`. Enabling gateway
  routing from the model page, or saving any edit in the model modal,
  rebuilt the gateway block from three fields and dropped the URL written
  by `preloop agents onboard`.
- Tree-stop and child-wait tests bind the session factory to an object
  that still looks like a Session. GitLab sets `INIT_TEST_DATA=true`,
  so `TestClient` lifespan seeds via `next(get_db_session()).query`,
  and a contextmanager-only stub made those client tests ERROR at
  setup with `Database setup failed`.
- The log-persistence backpressure test waits long enough for a
  saturated sqlite pool inside a self-hosted job container. Overflow
  shards run there; a 5s/10s budget passed on `ubuntu-latest` and
  timed out on the VMs.
- GitHub CI overflow-plan tests compute `backend_plan` in Python, so
  GitLab's unit image (no `jq`) can still pin hosted-first routing.
- Backend shard routing indexes `backend_plan` with `matrix.group`.
  GitHub expressions reject minus, so `matrix.group - 1` made the
  workflow file invalid and no GitHub CI job could start. The plan
  array is 1-based (dummy `null` at index 0).
- Issues similar-duplicates tests drop coalesced GETs between cases,
  so a later spec cannot join an earlier in-flight `/issue-duplicates`
  response and render zero rows.
- The upgrade e2e checks out the 2026 `pro` plan, not the withdrawn
  `teams` id. Free accounts see the AI-titles upsell hint on the
  sessions list again. Custom-agent e2e opens the wizard from
  "Onboard existing agent". Overview e2e treats an empty gateway card
  as first-usable.

- A plan withdrawn from sale (`plan.is_active = False`) is grandfathered
  only for a subscription somebody is paying for, or for a trial of it that
  is still running. An ended or cancelled trial, a cancelled subscription,
  and an `active` row with no provider subscription id behind it all resolve
  to the default plan instead of keeping the withdrawn plan's terms and
  name. The rule lives once, in `preloop.models.crud.entitlement`, and is
  applied by `entitled_subscription`, `get_active_for_account` and the
  billing preflight aggregates, so console, checkout and operator counts
  agree. Operator note: a grant hand-provisioned on a withdrawn plan without
  a provider subscription id stops resolving when this ships. Count those
  rows before deploying (newest subscription per account, status `active` or
  `past_due`, plan row with `is_active = False`, provider id null or blank);
  re-establish any that are real by reconciling them against the provider,
  or by moving the account onto a custom plan row, which is on sale by
  construction and never subject to this rule.
- Code scanning and Code Quality findings on main: report-publication
  logs only closed-vocabulary outcomes, session-search credential
  redaction uses length-bounded patterns that still consume a labelled
  value past 4096 characters so it cannot leave a plaintext tail, the
  in-repo flow-trigger workflow checks out the default branch and
  installs a checksum-verified CLI from that tree's `scripts/install-cli.sh`
  instead of piping curl to sh, and the remaining CodeQL quality notes
  (unclosed publication fds, lock-file Close, unused locals/imports,
  mixed returns, test lambdas) are cleared.

- The console upgrade modal repeats what the server said instead of
  "Unexpected checkout response". `startCheckout` resolves a `refresh`
  answer (asking the billing views to re-read the subscription summary and
  returning the reason), surfaces the server's sentence for any other
  action, and keeps the redirect path. A deployment refusal such as
  `catalog_not_synced` now reaches the dialog word for word.

- The stale-claim reaper no longer re-publishes every unclaimed execution
  from every worker on every pass. One replica runs the pass per interval
  (a database lease), an execution nobody claims is re-dispatched on a
  doubling delay recorded on the row (30s, 60s, 2m, ... up to
  `FLOW_EXECUTION_REDISPATCH_BACKOFF_MAX_SECONDS`, default 900), and a pass
  that finds flow tasks already queued undelivered publishes nothing.
  Recovery of an execution whose owner died is unchanged: a claim clears
  the backoff, so it is adopted inside one stale window. Each pass logs one
  summary line with its counts instead of a line per candidate.
- Agent launch no longer fails with `exec /bin/bash: argument list too
  long` when a rendered prompt or Kubernetes inner script exceeds
  Linux `MAX_ARG_STRLEN` (131072 bytes). The prompt and script travel
  as base64 chunks. A pre-launch guard refuses a payload that reaches
  or exceeds the per-string or total budget, with a named
  `runner_error`. OpenHands (the default `agent_type`) uses the same
  transport. Refs #609.
- A labeled trigger matches the label the event carries, not the issue's
  whole label list. A flow already active on that issue or pull request
  coalesces further triggers instead of starting another run.

- GitHub App trackers keep their installation binding when edited. The
  edit modal used to run the API-token path: `POST
  /api/v1/trackers/test-and-list-orgs` built a token client for a tracker
  whose `auth_type` is `github_app`, offered the `personal` login instead of
  the installation's numeric owner ids, and saving replaced the scope rules
  with ones that matched no project. Both `test-and-list-orgs` and
  `list-projects-for-org` now build the client from the tracker's
  installation (same as the scanner) when `tracker_id` refers to an App
  tracker. `TrackerResponse` gains `auth_type`, `oauth_installation_id` and
  `github_installation_target_login` so the console can tell App trackers
  from token trackers; the edit form no longer asks for a token. A new
  App tracker is scoped to the installation being bound only, not to every
  installation on the account. When the App is already installed on the
  target account (GitHub shows its Configure page and never calls the setup
  callback), the add form offers a "Use an existing installation" picker
  next to "Connect with GitHub".

- Optional gateway session-summary failures no longer page as primary gateway
  outages or retry generation after every request. Failed primary requests skip
  summaries; successful calls use a bounded refresh cadence and isolated state.
- Native Responses forwarding to OpenCode Zen preserves authentic caller identity
  headers so the provider receives the original client identity for eligibility
  checks. Upstream credentials remain separate.

- Hosted OpenCode preserves each model's Responses or chat-completions protocol,
  including title generation and mixed model inventories. Known OpenCode Zen
  Responses models and explicit native overrides use the matching SDK adapter.
- Gateway retries no longer multiply with hidden SDK retries. Transient failures
  retain bounded recovery, explicit protocol mismatches return a terminal error,
  and native Responses can recover from a disconnect before body output begins
  without replaying an emitted stream. Handled upstream SDK errors no longer
  appear as unhandled application failures in Sentry.
- Gateway incident alerts share a bounded reservation across replicas. A local
  throttle remains available when the broker cannot be reached.

- **`workspace_files` beside `payload` is no longer silently ignored**: a
  manual trigger body shaped `{"payload": {...}, "workspace_files": [...]}`
  was accepted with 200, stored on the execution, and seeded nothing,
  because every reader looked only inside `payload` while the neighbouring
  `product_provenance` key had a top-level fallback. Both keys now use one
  lookup: inside `payload` first, then beside it. Declaring `workspace_files`
  in both places is a 400 rather than a silent winner. The same lookup now
  feeds the container seed environment, the trigger-time budget check, the
  `_workspace_file_paths` audit stamp and the evidence pack manifest, so a
  run's manifest digests the files it was actually given and records the
  declared source wherever `product_provenance` sat in the body.

- **A `product_provenance` mapping is usable on SBOM-only flows**:
  `repositories[]` was mandatory, so a flow with `git_clone_config: null`
  (preset 004) had no accepted body at all: omitting the list was rejected as
  incomplete and supplying it was rejected as unauthorized. `repositories` is
  now optional when `sbom.digest` identifies the product, and a mapping with
  neither is still refused. Naming a repository the flow does not clone is
  unchanged, still refused.

- **A malformed product mapping no longer consumes an execution**: shape
  validation (schema, identity, `repositories[]`, SBOM digest and path) runs
  at the trigger (manual and webhook) and answers 400, the way the
  workspace-seed budget check already did, instead of creating an execution
  that immediately fails.
  Contract errors name the schema, the offending key and
  `docs/guide/flows/product-evidence.md`. Fact-dependent checks (declared SHA
  versus observed checkout, declared digest versus supplied bytes) still run
  during the execution, where the facts are.

- **The manual trigger refuses reserved keys instead of carrying them**:
  `_resume`, `_answers`, `_answers_prompt`, `_feedback_prompt`, `_ci_failure`,
  `_workspace_file_paths` and `_subject` are platform-written control state,
  and a forged `_resume.source_branch` reached the agent and decided which
  branch it cloned and pushed to. They now get a 400 naming the key. Other
  top-level keys stay free-form and usable as template variables. `_matrix`
  and `_model_routing` keep their existing stripped-and-recomputed contract.
- **`tool_calls_count` was 0 on runs that used MCP tools**: the log parser
  matched three phrasings the runner never prints, so nothing incremented the
  counter, and the execution page read only agent-derived logs while the
  executions list also counted runtime session activity, so the two could
  disagree. The parser now matches the runner's own `mcp: <server>/<tool>
  started` marker (the started form only, so a call counts once) and the
  metrics take the largest of parsed logs, recorded activity and the stored
  rollup. Largest and not sum: a call is usually recorded twice, once by the
  agent and once by the server that served it.
- **A derivable CRA verdict label is corrected instead of discarding the
  audit**: a run that measured everything correctly but wrote
  `pass_with_findings` next to its own `minimum_elements.passed: false` was
  rejected whole as `cra_result_invalid`. The persist boundary now retries once
  with the verdict the body implies. Only the label moves, every measurement is
  persisted as submitted, the correction is escalation only (`fail` is never
  softened), the corrected document is re-validated in full so a second defect
  still fails closed, an incomplete run is never repaired, and the body records
  `verdict_corrected` with the submitted value and the reason.
- **A drift report and a null `drift` field can no longer both be true**: a
  release audit that completed its drift comparison and then stopped at a
  waiver question emitted an incompletion envelope with `drift: null` next to
  `artifacts.drift_report` naming a real file, which reads as no drift at all.
  `drift` is now allowed on the envelope for the release audit schema only, it
  is validated in full there, and a named report and a populated field imply
  each other in both directions. The verdict stays `error`, so the release is
  still denied.
- **`evidence-status` reported `integrity_verified: false` on packs that
  verify**: the field promised an integrity judgement and delivered "this
  endpoint did not look". It is now a three-state `integrity` (`verified`,
  `not_checked`, `failed`) plus an `integrity_note`, with the boolean kept for
  compatibility and true only for `verified`. On the legacy transport the poll
  hashes the archive it already has and answers `failed` with the observed
  digest on a mismatch, rather than letting the download be the first place
  anyone finds out. The direct transport answers `not_checked`, because a poll
  does not decrypt ciphertext, and `GET .../evidence` repeats the same word in
  `X-Preloop-Evidence-Integrity-State`.
- **`GET /flows/executions/{id}/artifacts` 401 explains itself**: that route
  pair is the runner's artifact transport and only ever accepts a minted
  `flow-artifact` capability, but `openapi.yaml` published it under
  `bearerAuth` as though an operator could call it, and the refusal was one
  word. Both routes are dropped from the published schema (no generated client
  or frontend referenced them) and the 401 now names the error code and the
  audience, sends a `WWW-Authenticate: Bearer realm="flow-artifact"` challenge
  and points at `GET .../evidence` and `.../evidence-status`. The reply names
  no execution and no artifact, and the route still answers 401 rather than
  404: undocumented is not disabled.
- **An approval that should park the run no longer ends it**: when the
  routing approval workflow had `async_approval_enabled` set (the shipped
  "Default Approval Workflow" does), `require_approval` returned its
  `pending_approval` payload before it reached the park handshake, so no
  window length could park the execution. The agent got an answerless
  result, wrote its incompletion envelope and exited, and the run was
  completed and failed closed while a human still held the question. Both
  paths now go through one `_park_and_build_payload` helper, so the
  decision to park is made in a single place. The monitor loop also
  re-checks for a park request in its terminal branch: the park is written
  by another process and observed on a 5 second poll, so an agent that
  exits inside that window used to be finalized first. A parked run is no
  longer fail-closed by the CRA persist boundary, since a park is not a
  release. Observed on staging execution
  `e42c6086-f637-4d18-be09-2395c4d488ca`, approval
  `6a7cd2dc-a9f8-4fa5-9870-b834f5bc1db2`: the waiver was answered 2 minutes
  20 seconds after the run had already been marked FAILED, against a 3 day
  window.

- **The failure message names the field that classified the run**: a
  result artifact rejected on its `verdict` reported `status=None`, which
  named a key the CRA incompletion envelope does not carry. The override
  now reports the field that actually decided on both the terminal exit
  and the sentinel-grace path, and records it on the milestone as
  `signal_field` / `signal_value`.

- **Preset sync no longer drops fields on existing presets**:
  `scripts/sync_flow_presets.py` updated existing global presets from a
  hand-maintained dict that omitted `approval_window_seconds`,
  `timeout_seconds`, `runner_pool`, `custom_commands`, `webhook_config`
  and `schedule_config`, and its change detection compared only 8 fields,
  so a preset whose only change was one of those was reported up to date.
  Create and update now derive from the same `FlowCreate`, and drift is
  computed over every field a preset can set. Effect: the 3 day approval
  window that presets 006 and 014 declare reaches the flow row instead of
  falling back to the 300 second default. A preset key that no schema
  claims is now logged rather than silently ignored.

- **Talk stays clickable on the agent page in a narrow container**: an
  action that renders its own element has no click handler an overflow
  menu item can call, so folding it produced a menu row that did nothing.
  Those actions now stay on the row, their width is reserved when the
  rest fold, and they are not clipped by the row's hidden overflow.

- **Console list bulk bar no longer shifts the table**: selecting rows
  swaps the bar into the existing toolbar instead of inserting a strip
  above the list, and the bar offers "Select all N" for the current page.

- **Hosted isolated publication keeps the controller `trusted_publication`
  receipt on the persisted result**: `_attach_product_evidence_records`
  still strips any agent-authored copy, then reattaches only the
  controller-passed receipt so resume can rebind. Omitted repository
  `clone_path` now defaults to `workspace`, then `workspace-2`, matching
  isolated bind/resume. Guide: `docs/guide/flows/product-evidence.md`.

- **Maintenance checkout uses frozen publication records**: `HEAD.txt` in an
  evidence archive is not release or build provenance. Recheck matches the
  candidate SHA against controller-verified `product_provenance` repositories
  (`sha_status=verified`) and isolated publication receipts.

- **Security-maintenance sweep rotates past a stuck prefix**: idle
  waiting-for-human `approval_pending` rows no longer occupy the bounded
  page. A durable per-account keyset continues later dispatch retries and
  baseline audits on the next sweep, wrapping when the cursor walks off
  the end. Expiry, claim recovery, and PENDING-only restart are unchanged.

- **CRA evidence binding recognizes controller `product_provenance` and
  `dossier_manifest` annotations**: packed agent JSON is still compared in
  full. Those controller records (and the older `provenance`/`dossier`
  spellings) do not fail a matching pack; a changed decision, waiver, or
  gate still does.

- **Invalid CRA completion keeps the original runner failure**: when a
  private runner already reported `FAILED` or `STOPPED`, persist-time
  contract diagnostics are appended instead of replacing that reason.
  Known credential formats are scrubbed first.

- **Duplicate evidence-identity helpers after a stacked merge** are
  removed. Artifact cache identity, execution refresh, and terminal
  receipts keep a single canonical implementation.

- **Ordinary evidence capture ignores non-string transport errors**:
  `evidence_transport_error` is only a failure when it is a non-empty
  string. Truthy mock auto-attributes no longer drop a getter-captured
  pack before it is persisted.

- **Checkpoint restore keeps its own response cap**: the shared artifact
  client reads workspace archives up to `PRELOOP_CHECKPOINT_MAX_BYTES`
  even when `PRELOOP_EVIDENCE_MAX_BYTES` is also set. Evidence PUTs stay
  on the evidence cap.

- **Hosted Docker direct upload stores once**: the EXIT-trap PUT is the
  durable write. After exit the control plane binds that artifact from
  `PRELOOP_EVIDENCE` log lines and does not copy leftover workspace files
  into a second store.

- Prevent database waits in authentication and approval summaries from blocking
  the API event loop; cancellation now waits for shared-session workers to finish.
  Native permission checks release authentication connections before human waits.
  Artifact quota locks permit independent account foreign-key inserts, OAuth
  refresh reloads current credentials and releases unnecessary locks, and agent
  heartbeats use the same row-lock order as operator lifecycle changes.

- **OpenCode log-filter tests no longer crash without Node**:
  `TestOpenCodeLogFilterJs` spawned `node` and raised `FileNotFoundError`
  in the GitLab agents job (Python slim image) and any local env without
  Node on `PATH`. Those cases skip; the agents unit job installs `nodejs`
  so CI still parses and executes the embedded filter.

- **Landing build fails on missing screenshots**: the brand Vite plugin
  now errors when a landing `hero.image` or feature `placeholderImg` is
  missing from `frontend/public`, so a 404 like the onboard-dialog still
  cannot ship silent again.

- **Talk window follows the latest message**: new turns (including after
  the 50-event page is full, and replies that arrive as activity rows)
  keep the thread at the bottom unless the reader scrolled up with a
  gesture. Session switches rebind the thread; layout growth re-sticks
  via ResizeObserver. The Jump to latest pill remains the only way back.

- **Agent Control presence is honest across replicas**: `control_online`
  is computed from a persisted heartbeat (~90s TTL) instead of whichever
  replica holds the WebSocket, so the badge no longer flaps offline on
  the replica that does not own the socket. `last_seen_at` (enrollment
  and gateway traffic) is no longer used as a stand-in for a live plugin.

- **Wrapper PR/MR create no longer sends invalid JSON**:
  `git_clone_config.create_pull_request` interpolated title and body into
  a curl JSON payload, so a multi-line `pull_request_description` (preset
  011) or a commit body with quotes made GitHub/GitLab reject the create
  after a successful push. The wrapper now `json.dumps` the payload, reads
  `pr_title` / `pr_body` from `/workspace/result.json` when the agent
  writes them, interpolates git-config placeholders (GitHub `issue.*`
  aliases GitLab `object_attributes.*`), names new branches
  `preloop/issue-{n}-{exec[:8]}`, logs the HTTP status, and restores the
  non-custom fallback body (flow execution link, plus a `**Commits:**`
  list on multi-commit pushes).
- **Cursor permission hook raised two approvals per shell command**:
  `preloop agents onboard Cursor --approvals` installs the same hook for
  `beforeShellExecution`, `beforeMCPExecution`, and `preToolUse`, and
  Cursor's `preToolUse` fires for every tool, so each shell command and MCP
  call reached the permission-check endpoint twice (two approval rows, or
  two prompts under enforce). The `preToolUse` hook now answers Shell and
  MCP tools locally and only raises approvals for native file tools such as
  `Write`, `StrReplace`, and `Delete`. The hook also reads the documented
  `mcp_server_name` key when matching the Cursor MCP allowlist and
  detecting the Preloop MCP server.
- **Unpinned flows fall back to hosted compute when every private runner is
  busy**: a busy runner cannot be leased, so it no longer counts as online
  capacity for the account default pool. The Runners page still shows a
  saved default that is currently offline.
- **Native tool approval workflow select reverts on a failed save**: the
  dropdown no longer keeps an unsaved pick after "Could not save", and the
  account default workflow is listed once (the empty option).
- **Automated Issue Implementation prompt uses normalized issue fields**:
  title, description, and number come from
  ``trigger_event.payload.object_attributes`` so GitHub ``issue.body`` and
  GitLab ``description`` both resolve. Label filters match GitHub
  ``issue.labels[].name`` and GitLab ``labels[].title`` as well as
  already-enriched string lists.
- **Similarity search embeddings no longer block the event loop**: comment,
  issue, and generic search query embeddings run in a worker thread so a
  slow OpenAI embedding call cannot serialize concurrent requests. Gemini
  aux 429s (`google.api_core.exceptions.ResourceExhausted`) classify as
  retryable rate limits, matching the OpenAI SDK path.
- **Unpriced-model admin mail skips customer-owned endpoints**:
  ``openai-compatible`` / ``custom`` models on a host we do not catalog
  (home LiteLLM, OpenCode Zen, a private proxy) stay unpriced on the
  Attention page, but no longer page an admin to add a global price.
  OpenRouter- and DashScope-fronted configs still alert.
- **Spending-limit save no longer posts a null notify user**: `/auth/users/me`
  has no `id`, so the limits editor used to send `notification_user_ids: [null]`
  and the API rejected the create. Recipients now come from the users list
  (matched by email, case-insensitive) or are omitted. Overlay clicks no
  longer close the limits dialog, so opening the notify dropdowns does not
  dismiss it.
- **Picking a period or subject no longer closes spending limits**:
  `sl-select` / `sl-dropdown` fire composed `sl-hide` when their popup
  closes. Overview and Attention treated that as the dialog hiding.
  The outer dialog ignores nested hides; parents listen for
  `budget-limits-hide`. The inner "Delete limit" confirm still receives
  its own `sl-hide` so dismissals clear `pendingDeleteId`.
- **Overview first paint no longer waits on the attention usage breakdown**:
  the shared attention loader used to fetch ``include_breakdown=true`` in
  parallel with wave 1, and the page waited for it before drawing. Attention
  now starts after the fold and still uses the shared 30-day window, so
  Overview and ``/console/attention`` stay in agreement. Wave 2's selected
  range is a separate query for the cards; a calendar-month Overview range
  is not reused as the 30-day attention window. The background refresh
  also bumps the "Updated … ago" stamp. `relative-time-label` does not
  start its timer until a timestamp is set.
- **CLI runner interrupt test no longer races `exec.Cmd`**: GitLab
  `test:unit:cli` (`-race`) failed because the test read `Process` /
  `ProcessState` while the runner called `Start`/`Wait`. It now watches a
  pid file from a helper process. Windows GitHub CLI CI does not implement
  `Signal(0)`, so liveness uses `tasklist` there.
- **Unused dashboard test helper and redundant asyncio import**: drop
  `isReddish` from `dashboard-view.test.ts` and the second `import asyncio`
  in the Kubernetes log streamer (`container.py` already imports it at
  module scope).
- **`POST /openai/v1/responses` forwards to the upstream Responses API**:
  a Responses request used to be transcoded into a chat-completions call,
  so an upstream that implements `/responses` but not `/chat/completions`
  (measured: OpenCode Zen) answered every request with
  `502 InternalServerError - Internal server error`. OpenAI-shaped API-key
  upstreams now receive the payload on their own `/responses` endpoint and
  the answer is relayed verbatim, streaming included, which also stops
  `instructions`, `reasoning`, `include`, `store` and `prompt_cache_key`
  from being dropped in translation. Upstreams that only speak chat
  completions are detected automatically (404/405/501, remembered per base
  URL for 15 minutes) and keep the previous behaviour with no configuration.
  `meta_data.gateway.responses_api` on the model row pins the choice
  (`auto` default, `native`, `transcode`). Auth, budgets, governance
  tool-stripping, accounting and audit run on both paths.
- **`preloop runner fg` reconnects when the control-plane WebSocket drops**: close 1006 (and other transport errors) no longer exit the process. The runner redials with backoff, keeps an in-flight Docker job, resends `complete`/`logs` on the new socket, and sends WebSocket pings alongside the JSON heartbeat. Ctrl-C still unregisters. Auth/`gone` errors stay fatal.
- **Console Runners page updates live**: register, connect, disconnect, lease, and complete publish `runner_updated` on the account websocket (`runners` topic) so status changes without a manual refresh.

- **Migration job now syncs global flow presets after alembic**: the
  post-upgrade hook runs `scripts/sync_flow_presets.py --no-propagate`
  in the same container so global presets stop drifting between deploys
  without rewriting derived user flows.
- **API and console pods carry `app:` labels**: the selector lived only
  on Deployment metadata, so `kubectl -l app=api` found nothing. Extra
  pod-template labels do not change `spec.selector.matchLabels`.
- **Console nginx accepts avatar uploads over 1 MB**: `client_max_body_size`
  now matches `gateway.proxy.bodySize` (default 32m). Oversized requests
  used to 413 at nginx; the console now surfaces an HTML 413 as "Image
  too large to upload." instead of a generic failure.
- **Scorecard supply-chain job pulls from GHCR**: `ossf/scorecard-action`
  is pinned to v2.4.4 (`ghcr.io/ossf/scorecard-action`). v2.4.0 pulled
  `gcr.io/openssf/scorecard-action`, which now denies unauthenticated pulls
  without GCP billing.

- **Blog posts no longer repeat the title**: the article template already
  emits `<h1>` from frontmatter. A leading `# Title` in the markdown (or
  the matching `<h1>` in the rendered body) is stripped so
  `/blog/preloop-0-15-0` does not show the headline twice.

- **Avatar upload rejects oversized files before buffering the body**:
  `PUT /users/me/avatar` reads the multipart in 1 MiB chunks and returns
  413 once the 5 MB cap is crossed, matching the audio upload helper.
  `process_avatar` still validates size after a complete read; this closes
  the same memory-exhaustion class as the decompression-bomb fix, on the
  upload-read path.
- **Describe a change no longer opens against a stale policy**: the button
  refetches the current export first and reports an error instead of
  silently opening an empty dialog when the export fails. Closing the dialog
  (including a programmatic hide after Save) resets the prompt and YAML so
  the next open is a fresh form; nested `sl-details` toggles do not reset it.
- **Tool-rule CEL detection matches the backend**: the Policies editor and
  the access-rule create/update endpoints classify `!`, ` in `, ternaries,
  and CEL functions as `cel`, so a deny rule cannot be stored as `simple`
  and silently fail closed to an approval prompt.
- **Add rule dialog no longer closes on every choice**: the dialog listened
  for `sl-hide`, which every inner `sl-select` emits when its dropdown
  closes, so picking a target or action dismissed the form. It now listens
  for `sl-request-close` and only Cancel, the close button, or Escape can
  dismiss it. The form also asks for the rule type first (tool call versus
  model text, then request versus response in plain words), offers presets
  that wire detector, condition, and action together, explains that
  detectors only produce facts (`pii.found`, `injection.score`,
  `moderation.flagged`) while the condition decides when a rule fires, warns
  when a condition reads a detector that is switched off, and refuses to
  save a deny or require_approval rule with an empty condition rather than
  defaulting it to match everything.
- **Switching back to a policy preset re-applies it**: choosing
  "Start from a preset" after writing a custom expression restores that
  preset's detectors and condition, instead of keeping the custom values
  while the preset card still looks selected.
- **Dismissing the policy diff dialog clears a pending YAML save**: Escape
  or the dialog close control now resets `_pendingYamlSave`, so a later
  Import apply cannot be treated as an editor save.
- **`get_route_from_filename` maps `pandora.html` to `/pandora`, not `/dora`**:
  top-level HTML files use the basename as the route, with no substring
  match against `dora`.
- **Token-free approval links open the console**: MCP and in-session
  notices now emit `/console/approval/<id>` (the registered SPA route)
  instead of `/approval/<id>`, which is the public token page and 404s
  without `?token=`. Bare `/approval/<id>` 404s unless `id` is a UUID,
  then 302s to `/console/approval/<id>`; email/Slack links with
  `?token=` are unchanged.
- **Edit-mode model refresh lists live models**: refreshing the picker
  while editing a saved AI model (for example a Z.ai key that now serves
  `glm-5.3-flash`) sends the model id so the server decrypts the stored
  key and lists live. Create-with-a-typed-key already did this; edit
  previously sent an empty key and fell back to a stale bundled catalog.
  Stored secrets are never returned to the browser. Typed keys still win.
- **Model I/O policy API 500 under RBAC**: `/api/v1/policies/model-io-rules`
  list/create/update/patch/delete used `@require_permission` without a
  `current_user` FastAPI dependency. Nested `get_account_for_user` does
  not put `current_user` in the handler kwargs, so the fail-closed
  permission check returned 500
  `Permission check requires current_user and db dependencies`.
- **Dev compose no longer races postgres/NATS or schema init**:
  `docker-compose.yml` healthchecks postgres (`pg_isready`) and NATS
  (`/healthz`, with `-m 8222`) and runs `init_db.py --force` in a
  one-shot `migrate` service. api/gateway/scheduler/worker wait for
  postgres, NATS, and `migrate` (`service_completed_successfully`) so
  they no longer crash-loop on an empty schema or race two concurrent
  inits. `start.sh` still waits for `DATABASE_URL` before `init_db.py`
  for non-compose local runs.
- **Vite blocked hosts behind a public hostname**: the console honors
  `VITE_ALLOWED_HOSTS` / `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS` (and
  the hostname from `VITE_HMR_HOST` / `VITE_API_URL`) so Docker Compose
  behind nginx does not fail with "host is not allowed".
- **Approval poll logs approver lookup failures**: resolving a voter
  user-id to email is still best-effort (raw id is kept), but the except
  path now logs the traceback instead of a silent `pass`.
- **OTLP init-failed flag is process state, not a write-only global**:
  exporter setup failure is stored on a runtime object that `is_enabled()`
  and `_ensure_provider()` both read, so a broken collector is not retried
  on every span and CodeQL no longer flags an unused global.
- **OSS installer Compose `.env` `$` escaping**: passwords and other
  secrets that start with (or contain) `$` are written as `$$` so Docker
  Compose does not interpolate them or leak the rest of the value via
  `variable is not set` warnings. Re-runs unescape on read so values
  round-trip.
- **Overview Top Models card no longer flashes on live refresh**: websocket
  reloads fetched a lightweight gateway summary that cleared
  `usage_by_session`, then a second request filled the nested list back in.
  The card now keeps the breakdown until the detailed summary arrives and
  does not flip loading flags on background refresh.
- **Private-cluster Helm tests after OTLP merge**: default `values.yaml`
  now includes the `otlp` block from main. The private-cluster suite no
  longer asserts that block is absent, and the README no longer claims
  the chart does not define `otlp` values.
- **Bot-sender loop guard no longer swallows legitimate PR events**: the
  loop guard in `flow_trigger_service._is_preloop_triggered_event`
  dropped all webhook events whose sender started with "preloop",
  including `pull_request.opened` from the Preloop GitHub App. PRs
  created by the App on a human's behalf (e.g. #306, #307) never
  reached trigger matching, so the reviewer flow did not run. The guard
  now exempts PR/MR opened/reopened event types (intentional actions,
  not loop vectors) and matches bot identities by exact name instead of
  prefix, preventing false positives on usernames like "preloop-fan".

- **Agent Control eviction now sends close code 4000**: when a second
  WebSocket connects for the same managed agent, the server closes the
  previous connection with close code 4000 and a reason string instead
  of silently orphaning it. All runtime plugin clients (Python shared
  library, Hermes, OpenClaw, OpenCode, Claude Code sidecar) treat
  close code 4000 as a non-retryable eviction and stop reconnecting to
  avoid an eviction ping-pong loop. A warning-level log on the server
  names both connection identities.
- **Empty upstream streams no longer complete "successfully"**: an
  OpenAI-Responses stream whose upstream produced zero output items
  (or reported an in-band `error` chunk) used to be folded into a
  successful empty `response.completed`. Codex treats that as a
  completed no-op turn and exits 0 without printing anything, which a
  flow then fails as a missing success confirmation (staging
  executions 1ded95c8 / ffb122bd: 18,268 prompt / 0 completion
  tokens, agent silent). Such streams now emit an SSE `error` event
  and are recorded as a 502 upstream failure, so Codex retries the
  turn (verified against codex-cli 0.149.0: 5 retries, then a loud
  stream error) instead of dying silently.
- **z.ai GLM-5.3 was unpriced**: first-party list prices from docs.z.ai
  are now in the vendored catalog ($1.4 input, $0.26 cached input, $4.4
  output per 1M). z.ai has no price API, so
  `scripts/update_model_prices.py` refreshes those rows from the public
  pricing page alongside the litellm map.
- **Preloop-bot label events were dropped**: `_is_preloop_triggered_event`
  no longer skips `issue_labeled` / `issue_unlabeled`, so
  `update_issue` adding `agent-ready` can start an implementation flow.
- **Attention dismissals**: marking a model item fixed or restoring it
  returned 404 when the model alias contained a slash (reported and fixed
  by Alex Lennon, Dynamic Devices).

### Removed

- Flow failure comments. `notifications.on_failure.comment_on_trigger_issue`
  is still accepted by `POST/PATCH /api/v1/flows` (no 422 for stored flows or
  older clients) but is parsed and ignored: a failed or timed out execution no
  longer posts a comment with the redacted log tail on the triggering issue.
  A failed execution is an attention item on Overview instead. `notifications`
  is a JSONB column, so there is no migration; the ignored block is dropped
  the next time the flow is saved from the console. This matches the existing
  treatment of `notifications.on_failure.attention_item`.

### Security

- **Revoke CLI and console JWT sessions.** `user.auth_generation` is
  carried in every JWT as `gen`. `POST /auth/sessions/revoke-all` (CLI:
  `preloop auth logout --all`; console: Sign out everywhere) increments
  it so every outstanding access and refresh token is rejected. Tokens
  minted before this change have no `gen` and are treated as generation
  0, so the first bump invalidates them too. `POST /oauth/revoke` no
  longer reports success for a CLI JWT; it returns
  `unsupported_token_type` and points at revoke-all. The CLI refresh
  path now refuses an inactive user. API keys and runner tokens are
  unchanged.

- **Frontend `fflate` 0.7.5**: override the `deck.gl` transitive so ZIP64
  inflate cannot loop on a malformed archive (GHSA-px8p-9vwx-vf98 /
  Dependabot #126).
- **CodeQL runs from an in-repo workflow** on every pull request and every
  push to `main`, so OpenSSF Scorecard can see `github/codeql-action` on
  all commits rather than only the GitHub default-setup checks that some
  merged PRs skipped. SARIF upload stays off until GitHub default CodeQL
  setup is disabled (both cannot upload at once); then set ``upload: true``
  in ``.github/workflows/codeql.yml``.
- **`@preloop-ai/claude-plugin` overrides `fast-uri` 3.1.6 and `qs` 6.16.0**
  (Dependabot GHSA-5jgf-p345-68v8 / GHSA-f65p-4m7j-42xc / GHSA-fph4-wmhf-6fwf
  / GHSA-jqff-g426-hqxp, GHSA-x5fp-wj9c-mxmx / GHSA-4mjr-xmp4-gh2g).
- **Model I/O ``text_sha256`` stays a SHA-256 prompt fingerprint**:
  GitHub default CodeQL traces the Anthropic OAuth HTTP response into
  scanned model I/O and reports ``hashlib.sha256(...)`` as a password
  KDF. Inline ``# codeql[...]`` comments are not honored by that check.
  The digest is still SHA-256 (via ``hashlib.file_digest``) so existing
  ``text_sha256`` rows keep matching; it is an audit fingerprint, not
  password storage.

- **Drop python-jose for PyJWT**: auth tokens, email/reset tokens, WebAuthn
  challenge state, MCP OAuth authorize codes, and APNs ES256 client
  assertions now use PyJWT. python-jose pulled unmaintained
  `python-ecdsa` (CVE-2024-23342, no patch). Auth is HS256; APNs ES256
  already uses `cryptography` when present. PyJWT was already in the
  tree via firebase-admin / MCP.

## [0.15.0] - 2026-08-20

Highlights: **native flow schedules** run flows on cron or friendly
interval/daily/weekly cadences with a console editor and next-run previews,
**self-hosted runners** lease flow jobs onto your own machines with
`preloop runner`, **`preloop claude`** brings Happy-class remote control of
Claude Code sessions, **eval-grade flows** gain matrix fan-out, workspace
seeding, and a first-class `result.json` verdict channel, and **cost
accounting gets honest**: provider-reported cost is authoritative, unpriced
usage is never shown as $0.00, and reprice plus ledger backfill repair
history.

### Added

- **Security audit preset pack** (#259): three single-execution presets built
  on the Observe/Eval pattern (read-only toolset, mandatory
  `/workspace/result.json` with a versioned schema, evidence pack under
  `/workspace/evidence/`). **SBOM Verify** (`preloop.cra.sbomaudit/v1`)
  checks validity, NTIA/CRA minimum elements, completeness against delivered
  build manifests, and license flags for CI-emitted SPDX/CycloneDX documents
  (it verifies, never generates, an SBOM). **SBOM Exploit Check**
  (`preloop.cra.vulnscan/v1`) maps components to CVEs via OSV.dev, adds
  known-exploited flags from the CISA KEV catalog and best-effort EPSS
  scores, uses NVD as a rate-limited fallback only, applies a severity gate,
  and echoes VEX suppressions instead of dropping them. **Release Security
  Audit** (`preloop.cra.releaseaudit/v1`) runs both in one execution plus a
  drift comparison against a previous run's `result.json`, intended for
  webhook-fed release builds and scheduled re-audits. Payload contract,
  schemas, and honest limits in `docs/guide/flows/security-audit-presets.md`.
- **Layered preset directories** (#261): `PRELOOP_PRESETS_PATH` accepts an
  `os.pathsep`-separated list of directories. Later directories override
  earlier ones only when a preset declares the same slug; otherwise catalogs
  union, and a `disabled: true` preset in a later directory suppresses its
  same-slug predecessor (tombstone). Single-directory values match the
  previous behavior except that two files resolving to the same slug now
  de-duplicate (later file wins, with a warning) instead of both loading.
  Overlay deployments can now surface upstream presets without re-shipping
  them.
- **Observe / Eval preset with a first-class `result.json` artifact** (#231):
  new global preset with an empty MCP toolset by default (no write tools)
  whose prompt enforces a run-measure-report protocol ending with
  `/workspace/result.json` (`preloop.eval.result/v1`:
  status/summary/metrics/checks/artifacts). The runner captures the artifact
  after the agent finishes via the Docker archive API (works on exited
  containers; 256 KiB cap; invalid or oversized artifacts recorded as
  wrapped error objects), persists it as `flow_execution.result` (new JSONB
  migration), and serves it on `GET /flows/executions/{id}` plus a new
  `GET /flows/executions/{id}/result` (404 when no artifact). List rows stay
  light. Kubernetes capture is a stubbed TODO.
- **`result.json` is a second success-confirmation channel** (#234): agent
  CLIs exit 0 even when the agent died mid-task, so the printed sentinel
  stays a fail-closed positive-confirmation contract; but a
  `/workspace/result.json` with a success status now counts as positive
  confirmation of equal standing, cutting false negatives (a verifiably
  completed review was FAILED because the model forgot to print the sentinel
  after a 3.7M-token run). An explicit failure status in `result.json` wins
  over everything, including a printed sentinel, and an eval "fail" verdict
  is a completed evaluation, not a flow failure. A run failing only for
  missing confirmation says so explicitly and names both channels. The PR
  reviewer preset writes `result.json` as its completion act.
- **Matrix/batch trigger fan-out** (#230): one flow definition can drive a
  model x harness evaluation grid without cloning the flow per cell. The
  trigger body accepts a reserved `matrix` key: up to 25
  `{agent_type?, ai_model_id?}` cells, each producing one execution, all
  sharing a `batch_id` (new indexed column). Validation is all-or-nothing
  (cap, allowed keys, factory agent types, account-visible model, else 422)
  and all rows are committed before any cell is dispatched, so a mid-batch
  crash leaves visible PENDING rows rather than silently missing cells. The
  response returns `batch_id` plus per-cell execution refs, and a new
  `GET /flows/batches/{batch_id}/executions` lists a batch with a
  status/cost/token rollup. Non-matrix triggers are wire-identical to before.
- **Workspace seeding from trigger payloads** (#236, #238, #239): webhook
  trigger payloads can declare inline files to materialize in the agent's
  `/workspace` before the agent starts
  (`{"workspace_files": [{"path": ..., "content_base64": ...}]}`) instead of
  embedding fixtures into the prompt. Strict validation: relative paths only
  with path-traversal and `.git` guards (including nested `.git` and runtime
  symlink containment), strict base64, a 1 MiB total cap enforced on encoded
  size before decoding, and a 50-file cap. The orchestrator validates before
  any container starts; seeded paths are stamped into
  `trigger_event_details._workspace_file_paths` for audit and
  `content_base64` is redacted from prompt embeds.
- **Usage ingest push API** (#254): `POST /api/v1/usage/ingest`, the
  continuous-push evolution of the CSV usage import: an API-key-authenticated
  harness posts sanitized spend records as they occur. Records are identified
  by (source, external_id) per account; replays return 200 with per-record
  `deduplicated` flags and never double-count spend (a replay whose content
  hash differs is additionally flagged `conflict=true`, never a 409).
  Hook-shaped lifecycle events (`session_start`, `session_end`,
  `subagent_start`, `subagent_stop`, `response`, `compaction`) land as
  zero-cost imported rows so subagent fan-out is countable in near-real-time;
  `conversation_id` / `parent_conversation_id` are first-class indexed
  columns so worker spend can roll up under its parent thread. `cost_basis`
  distinguishes reconciled billing-export rows from hook-derived estimates:
  reconciled rows supersede estimates for the same scope and the two are
  never summed. Rows land as `usage_source='imported'`, identical to the CSV
  path, and stay out of gateway budget accounting.
- **CNPG scheduled backups in the Helm chart** (#257): CloudNativePG
  continuous backups replace the deploy-time pg_dump. The Cluster template
  gains `endpointURL` (S3-compatible stores), optional `serverName`,
  base-backup compression, and fail-fast validation when backups are enabled
  without a destination; a new ScheduledBackup CR template runs periodic base
  backups while WAL archiving stays continuous. Backup profiles for
  production and staging ship as value overlays, the chart README documents
  enablement, verification, on-demand backups, and the full restore
  procedure including PITR, and `scripts/helm-render-check.sh` runs lint and
  render assertions in CI. Base backups default to
  `backupOwnerReference=none` so pausing or uninstalling the schedule can
  never garbage-collect the restore anchors.
- **Signed release provenance**: the release workflow attests every published
  asset with SLSA build provenance and ships the `.intoto.jsonl` bundle as a
  release asset. Verify any downloaded artifact with
  `gh attestation verify <file> --repo preloop/preloop`.

- **Happy-class Claude Code control**: `preloop claude` owns the process
  (native TUI locally, Agent SDK when phone/web/watch takes over, any-key
  or Release returns to the TUI). Sidecar `@preloop-ai/claude-plugin`
  (`runtime-plugins/claude-preloop`) plus Agent Control G1/G2 (`claude_code`
  kind, native `session_source_id` on command envelopes). Approvals stay
  on the existing PreToolUse hook. Config lives in
  `~/.claude/preloop-control.json`. Live e2e in
  `runtime-plugins/claude-preloop/test/live-sdk.e2e.mjs` exercises query,
  session reuse, interrupt, takeover, and release against the latest
  Claude Agent SDK (`PRELOOP_LIVE_CLAUDE_SDK=1`).
- **Provider daily-ledger CSV backfill**: `POST /api/v1/cost/ledger-backfill/csv`
  (permission `manage_budgets`) and `scripts/backfill_openrouter_ledger.py
  --csv` accept an OpenRouter Activity → Explore daily export
  (`date__day,model,total_usage`) and distribute each (day × model) total
  across that account's still-unpriced gateway rows for the day — pro-rata
  by tokens, equal split when the bucket recorded no tokens. Display names
  are matched to recorded aliases via a shared family key; the export's
  "Other" bucket names no model, so its spend is reported as a residual and
  never allocated. Allocated rows are tagged `cost_source='reconciled'`
  (never mixed with estimates), re-runs are idempotent (only still-unpriced
  rows are ever written), and the default is a dry run that returns the full
  allocation plan. CSV mode needs no management API key and has no 30-day
  activity-endpoint horizon.
- **Synchronous reprice endpoint**: `POST /api/v1/cost/reprice` (permission
  `manage_budgets`) scans the requested window in-request — keyset-paginated,
  up to 92 days — and returns real examined/updated counters, avoiding the
  billing plugin's 7-day async cliff whose acknowledgement serialized as
  "examined 0 rows".

- **Flow execution duration in the console**: the executions table now shows a
  **Duration** column in place of the raw "End Time" (the start time already
  said when the run happened; the end time alone never said how long it took),
  and the same value is appended to the "Started …" line on the Flows and
  dashboard execution lists. Running executions display `Running · <elapsed>`,
  ticking every second on the execution detail page and recomputed on each
  render elsewhere; runs that ended without an `end_time` show `—` instead of
  claiming to still be running. Both timestamps were already returned by the
  API, so this is a console-only change.

- **Chat-style session transcript ("Conversation" view)**: the session observer
  now reconstructs a chat-shaped transcript from the captured gateway events
  and activity rows. Only top-level user prompts and final agent responses are
  expanded; tool calls, tool results, system prompts, injected harness segments
  (system reminders, compaction summaries, Preloop question notices) and
  intermediate agent output are collapsed into expandable step groups.
  Tool results are detected exactly from the raw request body structure when it
  was captured; otherwise the view discloses how many requests lacked structure
  instead of guessing.

- **Per-request and per-session prompt-cache accounting**: the session request
  timeline now reports each request's cache read/write/miss tokens (`null`
  means "not reported by the provider", never zero; misses are labelled
  `reported` or `derived`) and a whole-session rollup with hit ratio over
  covered requests, coverage disclosure, a per-model breakdown, and estimated
  cache savings computed only from exact catalog prices (`catalog_exact`, or
  `catalog_exact_partial` as a lower bound in mixed-model sessions; omitted
  with a stated reason otherwise). Replay-validation traffic is excluded.

- **Nginx route parity test**: `backend/tests/test_nginx_route_parity.py`
  asserts that every prerendered marketing route resolves to prerendered HTML
  in BOTH the docker nginx template and the production Helm ConfigMap by
  implementing nginx location-matching precedence, preventing the recurring
  "works locally, serves the SPA homepage in production" drift. Also adds the
  missing `/ai-act-readiness` route to the docker template.

- **Admin alert for unpriceable models**: the gateway now notifies admins the
  first time a `(model_alias, provider)` pair proves unpriceable, including the
  account and token volume, so missing pricing is noticed instead of silently
  surfacing as no spend. Deduplicated via a persisted `audit_log` marker with a
  24h cooldown (`UNPRICED_MODEL_ALERT_COOLDOWN_HOURS`), so it holds across
  replicas rather than firing once per process, and every failure path is
  swallowed so alerting can never break a user request.

- **`scripts/reprice_unpriced_usage.py`**: operator script to backfill costs for
  historical rows recorded while a model was unpriced. Dry run by default;
  requires `--apply` to persist.

- **`test:integration:cli-onboard` CI job** (manual): builds the CLI from the
  branch, onboards a planted Claude Code install in API-key mode against the
  deployed test environment, and asserts that the enrollment routes through the
  gateway and that a request made with the minted credential is metered. Shares
  its onboarding semantics with the recorded e2e rig module 08 via
  `scripts/e2e-rig/lib/cli_onboard.py`. Uses the existing `PRELOOP_TEST_API_KEY`
  variable; no new secrets.
- **`test:unit:scripts` CI job**: runs the install script's shell-helper tests
  and the e2e rig's pure-python unit tests, neither of which any existing job
  executed.

- **Agent Control for Claude Code (G1) and native session targeting (G2)**:
  `claude_code` is now a supported Agent Control kind. `control_enabled`
  still requires sidecar/capability flags, not a blanket true. When a
  command targets an existing session, the persisted outbound envelope
  includes that session's `session_source_id` and `session_reference`.
  Clients keep sending the Preloop `target_session_id` UUID. Those
  native fields are response-only: request models ignore any
  client-supplied value. `start_new_session` responses also return
  the minted history session's native identity.
- **Claude Code Agent Control sidecar** (`@preloop-ai/claude-plugin`,
  `runtime-plugins/claude-preloop`): steer sidecar-owned Claude Code sessions
  (send_message, resume, interrupt, takeover, release) over the
  `preloop.agent_control.v1` WebSocket via the Claude Agent SDK.
- **`preloop update`**: download the matching GitHub release asset for this
  OS/architecture and replace the current binary in place. `--check` prints
  the latest version and exits; `--yes` / `-y` skips the confirmation
  prompt. Version lookup honors `PRELOOP_DISABLE_TELEMETRY` the same way
  `preloop version --check` does. The daily update notice now asks
  "Update now? [y/N]" when stdin is a TTY and the running binary is
  writable. If the binary cannot be replaced, the CLI stays silent (no
  nag, no sudo hint).
- **Gateway overhead script**: `scripts/measure_gateway_overhead.py`
  (Python 3 stdlib) times streaming TTFB and time-to-close through the
  gateway versus an optional same-model direct upstream. Keys stay in
  the environment. See the script docstring for the env vars.
- **`preloop flow trigger`**: CI-native trigger for an existing flow by id
  or name. Posts to `POST /api/v1/flows/{flow_id}/trigger`, accepts
  `--payload JSON` or `--payload -` (stdin), and waits for a terminal
  status when stdin is not a TTY (override with `--wait=false`). Logs are
  polled from `GET /api/v1/flows/executions/{id}/logs` and printed to
  stdout. Non-zero exit on FAILED, STOPPED, or TIMEOUT. `--runner` pins
  the execution to a self-hosted runner id, name, or label. See
  `docs/guide/flows/ci-trigger.md`.
- **Self-hosted runners**: `preloop runner fg` registers with the account,
  keeps a durable WebSocket, heartbeats, leases matching flow jobs, and
  uploads logs (server republishes to `flow-updates.{id}`).
  `enable`/`disable`/`start`/`stop`/`restart`/`status` install a launchd
  plist (Darwin), systemd user unit (Linux), or scheduled task (Windows).
  Flows may set `runner_pool`; offline matching runners queue for 15
  minutes then FAIL with no hosted-compute fallback. Console
  `/console/settings/runners` lists this account's runners. This is the
  lease path, not a claim that every agent harness already runs
  identically on the CLI host.
- **Matched-rule context on approval requests**: the approval the human
  reviews now records which access rule gated the call (id, name, expression,
  priority, and any lower-priority rules that also matched), snapshotted at
  create time so later rule edits cannot rewrite history. The console shows a
  "Why this needs approval" block with the expression verbatim; list rows and
  push payloads show the rule name only. Rule-less gates (tool default,
  evaluation error, agent permission hook) say so plainly instead of
  inventing an expression. New nullable JSONB `rule_context` column; the
  API field is optional so historical rows stay blank.
- **Native scheduled (cron) flow triggers**: flows can now run on a schedule
  without an external cron caller hitting the webhook endpoint. Create or
  update a flow with `trigger_event_source: "schedule"` and
  `schedule_config: {"cron": "<5-field crontab>", "timezone": "<IANA name>"}`
  (sending a `schedule_config` alone implies the schedule source, mirroring
  the webhook default; a `schedule_config` on any other trigger source is
  rejected instead of stored inert). Cron expressions are validated against a
  5-minute minimum interval by simulating the schedule's own future fire
  times, so month/day-restricted expressions are checked too. Flow responses
  expose a read-only `schedule_state` (active, cron, timezone, next run).
  Ticks are reconciled by the existing sync scheduler daemon and dispatched
  as a new `run_scheduled_flow` NATS worker task; paused flows never fire,
  and a tick that lands while a previous execution is still running is
  skipped and recorded as a `flow_schedule_tick_skipped` audit event. New
  migration adds the nullable `flow.schedule_config` column.
- **Friendly schedule forms and schedule preview**: `schedule_config` is now
  a typed union — besides the raw cron form (`{"type": "cron", "expr": ...}`;
  the legacy `{"cron": ...}` shape is still accepted), flows can use
  `{"type": "interval", "every": N, "unit": "minutes"|"hours"|"days"}`,
  `{"type": "daily", "at": "HH:MM"}`, or
  `{"type": "weekly", "days": ["mon", ...], "at": "HH:MM"}` (all with an
  optional IANA `timezone`, default UTC). Intervals are bounded between the
  5-minute minimum and a 366-day maximum. A new
  `POST /api/v1/flows/schedule/preview` endpoint (permission-gated like flow
  reads) validates a config without saving and returns its `type`, a human
  `description`, and the next few run times; `schedule_state` on flow
  responses now carries the same `type`/`description` fields.
- **Schedules in the console** (#235): the flow editor gains a "Schedule"
  trigger type with friendly-first forms (interval / daily / weekly) and
  cron behind an Advanced toggle, a timezone picker defaulting to the
  browser timezone, and a live preview of the next 3 run times with backend
  validation errors surfaced inline. The flows list shows a next-run
  indicator on scheduled flow cards (with a warning badge when the flow is
  paused and the schedule suspended), and the flow detail page shows a
  schedule summary card with cadence description, active/paused state, the
  next 3 runs, and the last run status.
- **Provider-reported cost is ingested as authoritative**: when the upstream
  reports the request's actual cost inside the response usage payload
  (OpenRouter usage accounting: `usage.cost` and
  `usage.cost_details.upstream_inference_cost`; on BYOK requests the two are
  complementary and are summed), the gateway now records that figure as
  `estimated_cost` with the new `cost_source='provider'` marker, winning over
  catalog estimates. Explicit operator pricing (account overrides /
  model-config pricing) still outranks it. To make the provider figure
  present on every response, OpenRouter-bound requests (the `openrouter`
  provider or any model with an openrouter.ai base URL, both endpoint kinds,
  streaming included) now ask for usage accounting via
  `usage: {"include": true}` — strictly provider-scoped, config-gated by
  `OPENROUTER_USAGE_ACCOUNTING` (default on). This fixes models that have no
  catalog price at all — OpenRouter's Auto Router (`openrouter/auto-beta`)
  lists price `-1` by design, so its traffic was recorded as unpriced/$0 and
  a customer's real spend was understated ~1.5x against OpenRouter's ledger.
  The per-row repricing entry point also adopts a provider cost stored in a
  row's `usage_details`, so historical rows can be fixed retroactively.

### Security

- **Hash-pinned application installs**: Docker and GitHub CI install
  third-party Python deps from `uv pip compile --generate-hashes` locks,
  then `pip install --no-deps -e .` for the local package. ClawHub CLI
  and the Claude live e2e SDK install go through `npm ci` lockfiles
  instead of unpinned `npm i -g` / `@latest`.
- **image-size DoS advisories**: the console lockfile now resolves
  `image-size` to the `image-size-next@2.1.1` fork. Upstream never
  published `2.0.3`, which is the version GHSA-w3rx-r6r6-pgpr /
  GHSA-5p2g-fcmc-qvqq advertise as patched.
- **Hono pin**: `@preloop-ai/claude-plugin` installs `hono@4.13.3`
  instead of a floating `^4`.

### Changed

- **Model gateway stream close** (#263): the gateway yields the terminal
  SSE event (`message_stop` / `[DONE]`) and finishes the HTTP body
  before writing the usage row, so bookkeeping cannot hold the last
  event on the client-visible stream. A client that disconnects after
  that terminal event is recorded as 200 with captured usage, not
  499/partial. The Gemini `streamGenerateContent` route now uses the
  same `GatewayStreamingResponse`, so deferred success rows flush after
  the body instead of being dropped.
- **OSS TLS proxy and Helm ingress skip the console hop for the model
  gateway**: `/openai`, `/anthropic`, and `/gemini` now proxy straight to
  the gateway instead of hairpinning through console nginx. Helm does
  this with a second Ingress on the same host so SSE buffering can stay
  off on those prefixes without changing `/`. Usage accounting is
  unchanged: the gateway process still writes the request row. Re-run
  `scripts/measure_gateway_overhead.py` against a public install to
  confirm the TTFB delta.
- **Qwen / Model Studio catalog**: the keyless picker now lists current chat
  models (`qwen3.8-max` first) instead of `qwen-plus` / `qwen-turbo` /
  `qwen-max` / `qwq-32b-preview`. Live `/models` listing honors a
  user-supplied DashScope or Model Studio base URL (China Beijing default is
  unchanged so existing keys keep working) and drops dedicated image, video,
  audio, and NSFW ids. International list prices were added for the fallback
  ids. DeepSeek-V4 / GLM 5.2 / Kimi remain their own providers; a Model
  Studio key that also serves those SKUs will surface them via live listing.
- **PR Reviewer preset: token-optimised prompt**: the stock Pull Request
  Reviewer preset now bounds every open-ended read that previously let agents
  walk the repository. Project doc reads are capped (agent-instruction files in
  full, README/ARCHITECTURE/CONTRIBUTING heads only, CHANGELOG dropped,
  manifests/CI/linter configs only when the diff touches them); project-context
  discovery is limited to the diff plus at most 3 files outside it; Phase 2
  works hunk-first instead of opening whole changed files; each finding gets a
  verification budget (2 greps + 2 file reads, then phrase as a question); the
  documentation-impact pass runs only when the diff adds user-facing surface;
  previous-finding re-verification reads the ±40-line region instead of the
  whole file; the PR description is only rewritten when its content changed;
  persisting-issue stamps are replaced instead of stacked and unchanged-status
  comments are left alone; a single-fetch rule forbids re-calling
  `get_pull_request`; empty severity sections are omitted from the summary; and
  small first-time PRs (<~50 changed lines) take a fast path that skips the
  ceremony while keeping the full security/quality checks. New: incremental
  re-review — the summary comment now records the reviewed HEAD SHA in a
  `<!-- preloop-review:reviewed-sha:... -->` marker, and on
  `pull_request_updated` triggers the reviewer diffs against that SHA via git
  in the clone and reviews only the new hunks (with a full-review fallback on
  force-push/rebase or a missing marker), making per-push review cost
  proportional to the push delta instead of the whole PR.

- **"Halt" is now "Pause" throughout the console**, rendered as a play/pause
  toggle in amber/warning tones. Red/danger styling stays reserved for the
  genuinely destructive offboard and remove actions, matching the fact that
  pausing is now reversible.

- **`identity.*` tags are hidden from the default agent tag chips** and shown
  instead under a collapsed "Identity history" disclosure on the agent detail
  view. These tags are server-written bookkeeping from agent re-keying, not
  operator labels; they are preserved unchanged when an operator edits tags.

### Fixed

- **Edit and Delete on the AI model detail page** (#265): the header
  actions on `/console/ai-models/{id}` had no click handlers and the
  edit modal was not mounted, so Edit worked from the models list but
  did nothing on the detail page. Both now use the same dialog as the
  list; Delete confirms and returns to the list.
- **Webhook trigger returns `execution_id` and fails honestly** (#227): the
  public webhook endpoint validated the addressed flow (id, secret, enabled)
  but then routed through generic event matching that swallowed failures, so
  a flow whose trigger filters did not match (or any dispatch error) was
  silently dropped while the endpoint still answered
  `{"status": "triggered"}` with no execution reference. The endpoint now
  triggers the addressed flow directly and returns
  `execution_id`/`execution_status`/`execution_url` (plus a nested
  `execution` object aligned with `/flows/{id}/trigger`). Semantics are
  explicit: a redelivered payload for the same repo and commit returns 200
  with the existing `execution_id` and `deduplicated: true`; a
  `trigger_config` mismatch is a 422 with actionable detail; 500 is reserved
  for "no execution row was created"; and a post-commit dispatch failure
  returns 202 with the committed `execution_id` so callers poll instead of
  retrying into duplicates.
- **Flow deletion no longer orphans a running agent's logs** (#237):
  `DELETE /flows/{flow_id}` cascaded to executions (and their logs) with no
  guard for running agents, so an agent still streaming logs hit
  foreign-key-violating inserts and a spurious data-loss admin alert.
  Deleting a flow with executions in progress is now refused with a 409
  pointing at the stop command, the log persister drops entries for
  since-deleted executions with a single structured warning (persisting the
  rest of the batch, no alert for known orphans), and the residual drop
  alert reports the real attempt count and carries the captured exception.
- **OpenCode aborted long LLM requests at ~120s**: the generated
  `opencode.json` hardcoded a 120s whole-request timeout, far below the rest
  of the stack (gateway proxy 900s, MCP tools 600s), so reviewer runs with
  large prompts died with "The operation timed out." while the upstream call
  completed seconds later. The timeout is now 600s (aligned with the MCP
  tool budget, under the proxy's 900s so gateway failures still surface as
  retryable HTTP errors), the SSE inter-chunk timeout gets the same budget
  so a long silent reasoning gap is not treated as a dead stream, and
  operators can override via `OPENCODE_LLM_TIMEOUT_SEC` (malformed values
  are tolerated and logged, not fatal).
- **Credits-based OpenRouter provider cost was recorded at exactly 2x**
  (#224): credits-based responses return `usage.cost` AND an identical
  `usage.cost_details.upstream_inference_cost`; summing both doubled the
  real charge. The two are now summed only in the BYOK shape (where `cost`
  is OpenRouter's fee excluding the vendor charge); otherwise `cost` alone
  is the total. Retained precision widened from 10 to 12 decimal places so
  live micro-charges round-trip, and historical rows carrying the duplicated
  shape reprice correctly through the same helper.
- **Deploy rollouts killed in-flight gateway streams**: gateway and api pods
  ran with the Kubernetes default 30s termination grace period, so kubelet
  SIGKILLed uvicorn while it was still draining streaming connections and
  agents' flow executions failed during every deploy window.
  `terminationGracePeriodSeconds` is now pinned via values (default 900,
  aligned with the proxy read timeout). The grace period is a ceiling, not a
  delay: idle pods still terminate in about 10s.
- **Blog URLs served the SPA homepage in production**: the Helm nginx
  ConfigMap never received the `/blog` rules, so every blog URL returned
  homepage HTML, and the RSS feed was served with the wrong MIME type. Both
  are fixed and the route parity test now locks the docker and Helm configs
  together.
- **Access rules with a bare `true`/`false` condition failed closed** (#213):
  a literal `true` condition expression was normalised to `args.true`, which
  failed to parse, so allow rules configured with a catch-all condition fell
  back to require_approval. Boolean literals are now handled
  case-insensitively before normalisation.
- **`create_project` returned 500 on the duplicate check** (#214):
  `CRUDProject.get_by_identifier` did not accept the `organization_id`
  argument its callers passed (also breaking `create_issue` and project
  `test_connection`). It now takes the optional filter and the duplicate
  check is scoped to the target organization as intended.

- **OpenRouter Kimi slug is unpriced under provider `openai`**: traffic
  recorded as `moonshotai/kimi-k3` is the same SKU as bundled
  `moonshot/kimi-k3` ($3/$15 per million). Lookup now maps the OpenRouter
  org slug onto the Moonshot catalog key so those rows get a cost instead
  of `$0`. Reprice still-unpriced historical rows after deploy
  (`POST /api/v1/cost/reprice` with `only_unpriced=true`).
- **Reprice row selection**: `only_unpriced` repricing now also examines rows
  tagged `cost_source='unpriced'` that carry a stray stored cost (legacy $0
  writes), and the ledger backfill additionally admits legacy rows recorded
  before cost provenance existed (`cost_source IS NULL` with a NULL cost).
- **Estimates can no longer overwrite actuals**: repricing (bulk and
  single-row) refuses to touch `provider`, `reconciled`, and `imported`
  cost sources even with `only_unpriced=false` — provider-reported and
  ledger-reconciled figures are never replaced by catalog estimates.
- **Async reprice acknowledgements**: `RepriceResponse` counters are `null`
  (not `0`) when the run was dispatched to a background worker, so an async
  submission is no longer indistinguishable from "the window contained no
  rows".
- **Ledger CSV parser rejects non-finite totals**: `nan` / `inf` in
  `total_usage` are skipped like negatives, so they cannot land in
  `estimated_cost`.

- **GitLab CI against MCP Python SDK v2 and CLI telemetry**: integration
  jobs now pin `mcp>=1.0.0,<2` (`pip install mcp` was pulling v2, which
  removed `streamablehttp_client`). CLI unit tests disable adoption
  telemetry so `preloop login --token` does not POST `/api/v1/events/batch`
  at hermetic httptest servers. The frontend e2e seeder looks up the admin
  account through ``User`` CRUD; ``CRUDAccount.get_by_email`` is gone.

- **Unpriced-model admin alert on accounted $0 and empty completions**:
  when OpenRouter usage accounting was requested, an explicit `usage.cost`
  of `0` is now recorded as provider $0 (`cost_source=provider`) instead of
  treated as "not accounted". A response with `completion_tokens == 0` and
  no `cost` / `cost_details` fields may still land unpriced, but it no
  longer pages admins to add catalog pricing. `cost: -1` stays the catalog
  sentinel (not accounted). Prompt plus completion with no cost and no
  catalog price still alerts.

- **Per-execution cost rollup understated real gateway spend (#209)**:
  `flow_execution.estimated_cost` is written once when the run finishes, but
  most gateway usage rows are priced *later* — the live price lookup and the
  repricing backfill fill in `api_usage.estimated_cost` after the fact — so
  the stored rollup kept its `0.0` placeholder (or a stale partial sum) while
  the usage views showed the real cost (~14x understatement in production).
  Both repricing paths now re-derive the affected executions' rollups from
  the attributed usage rows (same rule the metrics endpoint uses:
  `action_type='model_gateway'` rows with a matching `flow_execution_id`,
  replay-validation traffic excluded), and a bulk reprice pass heals every
  rollup its window touches — including rollups left stale by earlier
  backfills. When nothing attributable is priced the rollup becomes `NULL`
  ("unknown"), never a `0.0` that reads as "free". Running a repricing
  backfill over the affected window (`only_unpriced=True` suffices) also
  repairs historical rows.
- **Unpriced-model alerts triple-fired for alias spellings of one model**:
  the alert dedup key used the raw recorded alias, so one model reachable as
  `openrouter/auto-beta`, `openai-compatible/openrouter/auto-beta` and
  `openrouter/openrouter/auto-beta` produced three admin alerts. The dedup
  key now canonicalises through the runtime resolver's alias candidates, so
  every spelling of a model shares one alert cooldown.

- **Preset updates never reached renamed flow clones**: preset propagation
  (`sync_preset_to_derived_flows`) only finds flows via `source_preset_id`,
  and the one-time linking migration only matched flows named
  "Copy of <preset name>". A flow cloned from a preset and then renamed —
  with a prompt still byte-identical to the preset — stayed unlinked forever
  and silently never received preset updates. A new content-hash linking pass
  (`link_unlinked_flows_by_content`, also run by
  `scripts/sync_flow_presets.py` before propagation) links unlinked,
  non-preset, account-owned flows whose prompt hash equals a preset's current
  prompt or a historical link-time version of it, regardless of name.
  Conservative by construction: only byte-identical prompts link (customized
  prompts can never match, so user edits can never be overwritten), hashes
  matching multiple presets are skipped and logged, and differing tools are
  marked customized and notified rather than replaced.

- **Unhelpful failure messages and no retry when an upstream model provider
  failed**: when the model provider in front of an agent returned a gateway
  timeout, the agent CLI exhausted its own internal retries hundreds of log
  lines before exiting, and the extractor that builds
  `FlowExecution.error_message` returned only the tail of the log. A user
  reviewing a failed run saw exactly
  `"  status: 504\n}\nAn unexpected critical error occurred:[object Object]"`
  — 69 characters that name no cause and suggest no action. Agent-log failure
  analysis now scans the whole log for the *meaningful* signal (an upstream
  HTTP status plus the agent's exhausted retry loop) instead of the last
  error-shaped line, and produces messages like `Upstream model provider timed
  out (HTTP 504) after 3 attempts.` Lines that carry no information
  (`[object Object]`, bare `status: NNN` fragments, proxy HTML error pages) are
  never surfaced as the cause when a real signal exists. Classification reuses
  the shared upstream-error taxonomy, so a hard quota exhaustion is still
  distinguished from a transient throttle.

  A transient upstream failure is also no longer terminal: a flow execution
  whose attempt failed on a retryable upstream error (timeout, bad gateway,
  overload, throttling, connection reset) is retried with exponential backoff
  (`FLOW_EXECUTION_MAX_ATTEMPTS`, default 2;
  `FLOW_EXECUTION_RETRY_BACKOFF_SECONDS`, default 15). Retries are never
  silent — each one is recorded as an `execution_retry_scheduled` milestone and
  surfaced on the execution timeline. Non-transient failures (bad credentials,
  denied permissions, exhausted quota, unknown model) are never retried. To
  rule out double-posting a review comment, push or pull request, an attempt is
  only retried when the agent process exited non-zero, which is the condition
  under which the container's post-execution git block does not run.

- **Streaming gateway requests killed in front of the gateway left no trace**:
  every streaming endpoint calls the upstream model provider *before* handing
  its SSE generator to the web layer, so that upstream failures surface as real
  HTTP errors instead of empty `200` streams. If the client was already gone
  when the first chunk was due — which is exactly what a proxy read-timeout in
  front of the gateway looks like — the generator body never ran, and neither
  did the usage accounting inside it (a Python generator closed before its
  first `next()` never executes, `finally` included). The provider had already
  been asked to generate and was billing for it, but Preloop recorded no usage
  row, no status code and no error class: the user's agent failed while the
  console reported a clean bill of health. Such requests are now recorded as
  status `499` with a new `stream_abandoned` error class, distinct from the
  `client_cancelled` class used when a client drops a stream it was actively
  reading. `ApiUsage.error_class` is also exposed on the per-request session
  timeline API, so failures that share a status code (a proxy timeout versus a
  user cancelling) can finally be told apart in the product. Spend semantics
  are unchanged: an abandoned stream streamed nothing, so no provider tokens
  are invented, and the already-working mid-stream disconnect path still
  records exactly one row.

- **Backfilled costs stayed $0 for models missing from the price snapshot**:
  `reprice_unpriced_usage.py` recomputed every row against the locally bundled
  price catalog only. A row is recorded `unpriced` precisely when the model was
  absent from that snapshot, so the backfill re-derived the same "unpriced"
  result and reported `updated=0` — those rows could never become priceable by
  repricing, and the account's dashboard kept showing ~$0 for real usage. The
  gateway already resolves this at record time via the live upstream price
  lookup; repricing now performs the same lookup (once per model, not per row,
  and never fatal when the upstream source is unavailable).

- **Tracker sync loop on out-of-scope repositories**: a webhook naming a
  project we never imported triggered a full forced tracker re-sync on *every*
  event, and logged a "Project ... not found. Triggering a sync" warning plus
  an admin notification each time. When the repository is outside the
  integration's scope (a GitHub App installed on *selected repositories*, or an
  `EXCLUDE` scope rule) the sync can never resolve it, so every subsequent
  webhook repeated the whole cycle — burning GitHub API calls and admin noise
  indefinitely. Unknown projects are now tracked per (tracker, project) with
  exponential backoff (5m doubling to a 1h cap) and are marked **degraded**
  after 5 failed attempts, at which point syncs stop entirely and the project
  is surfaced to the user via the new `degraded_projects` field on the tracker
  API response. The log line is now actionable (account, tracker, project,
  attempt count and the likely cause) and the per-event admin notification is
  gone. State clears automatically when the project later syncs successfully.
- **Database connection pool exhaustion and execution-log data loss**: a
  production gateway pod exhausted its SQLAlchemy pool
  (`QueuePool limit of size 3 overflow 7 reached`) under PR-reviewer load,
  which dropped a batch of NATS execution logs, failed the readiness probe,
  broke token validation, and ended in a pod restart. Four changes:
  - `_sync_batch_insert_logs` now retries transient failures
    (`TimeoutError`/`OperationalError`) up to 3 times with exponential backoff
    (0.5s, 1.0s) before dropping a batch, rolls back on failure so no dirty
    transaction is returned to the pool, and returns a success boolean.
    Background log persistence is additionally bounded by a semaphore so it
    cannot starve request-serving connections. Batches are still only dropped
    as a last resort, and admins are still notified when that happens.
  - Health checks use a dedicated single-connection engine with fast timeouts
    instead of a pooled request session, so readiness reports "can I reach
    Postgres" rather than "is the request pool momentarily full".
  - `/api/v1/ping` (the liveness probe) is now `async`, keeping it on the event
    loop. As a sync endpoint it ran in Starlette's bounded anyio threadpool and
    could queue behind blocked database calls, causing Kubernetes to SIGKILL a
    pod that was merely busy.
  - Gateway connection pool sizing raised from 3+7 to 6+14 per pod, api tuned
    to 8+12 and workers to 2+4. Chart comments now document that each pod
    creates two pools (sync + async engine) and reflect real production replica
    counts (api=2, gateway=5, workers=8).

- **Gateway log noise**: WebSocket broadcasts with no matching listeners logged
  at INFO on every event, accounting for ~69% of gateway log lines (8170 of
  11810 in a two-hour sample). These now log at DEBUG; broadcasts with actual
  listeners still log at INFO.
- **PR reviewer flows killed by a false "repeated MCP tool loop"**: removing a
  reaction that was already gone (the `eyes` "I'm looking at this" marker that
  PR-review presets clear when finishing) was reported by
  `update_pull_request` as `FAILED: remove reaction (eyes)`. Agents believed
  the call had failed and retried it verbatim; after four identical retries the
  orchestrator's loop guard stopped the run and marked the whole execution
  FAILED — even though the review had already been posted to the PR. Reaction
  removal is now idempotent: "already absent" is reported as success. This was
  the single largest source of PR-reviewer failures for daily users.

- **User-requested stops reported as "Execution timed out after 3600 seconds"**:
  the stop branch of the agent monitoring loop used `break`, falling through to
  the timeout handler at the end of the loop. Executions cancelled after a few
  seconds were persisted as FAILED with a bogus 3600-second timeout message.
  Stops now return status `STOPPED` with an accurate elapsed time.

- **Opaque git checkout failures**: every step of the checkout fallback chain
  discards stderr, so an unrecoverable failure produced only
  `FATAL ERROR: Could not checkout commit <sha>` with no cause. The failure
  path now re-runs the fetch/checkout with stderr attached and prints the
  remote plus available refs, so the log shows whether the commit was
  force-pushed away, the ref is missing, or credentials failed.

- **Unpriced usage no longer reports as $0.00**: gateway traffic routed through
  OpenRouter/`openai-compatible` endpoints was metered correctly (tokens
  captured) but could never be priced, so flows that cost real money displayed
  a confident `$0.00`. Three defects combined: the synthetic
  `openai-compatible/` prefix was carried into price-catalog lookups where it
  can never match; OpenRouter-routed models were not tried under litellm's
  `openrouter/vendor/model` keys; and a `sum()` over NULL costs was coalesced
  to `0.0` in `get_gateway_usage_for_execution`, with `FlowOrchestrator`
  defaulting `estimated_cost` to `0.0`. Cost now stays NULL when nothing could
  be priced, and the token volume is surfaced instead of a fake zero. Aggregates
  that mix priced and unpriced rows expose `cost_is_partial` plus the unpriced
  request/token counts so a subtotal is never presented as a complete bill.
  Subscription-covered traffic (`cost_source='subscription'`) is unchanged and
  still reports a legitimate `$0.00`.

- **OpenRouter model pricing**: models served from `openrouter.ai` are now
  priced from OpenRouter's own `/api/v1/models` endpoint when litellm's map
  does not carry them, cached and backed off like the existing price-map fetch.
  Date-stamped marketplace ids (e.g. `deepseek-v4-flash-0731`) are deliberately
  NOT aliased to their undated entry: they are separately priced SKUs, and the
  fallback would have overstated cost by ~55% for that model. Models that still
  cannot be priced stay explicitly unpriced rather than being given a guess.
- **Re-onboarding could reactivate an enrollment server-side and then refuse
  it locally**: when `preloop agents onboard` matched an existing enrollment by
  a v1 or legacy runtime-principal id, it PATCHed `lifecycle_action=reenroll`
  and printed "Reactivated ..." *before* deciding whether to re-attach — and
  the re-attach confirmation only ever ran for fuzzy ("fallback") matches. An
  interactive run without `-y` therefore failed with "declined re-attaching
  enrollment ..." without ever asking, leaving the account with a reactivated
  agent and the machine with no config written. The confirmation now runs for
  every non-v2 match (default yes) and happens before any server-side change;
  if the subsequent re-key fails, the enrollment is restored to its previous
  lifecycle state instead of being left reactivated.
- **Paused (suspended) enrollments are now resumed automatically on
  re-onboarding**: only decommissioned agents were revived, so re-onboarding a
  suspended agent left it suspended and every token issuance for it kept
  failing with 403. Onboarding now maps the lifecycle state to the correct
  revival action — `decommissioned` → `reenroll`, `suspended` → `resume`, per
  the backend's lifecycle map — and prints "Resuming paused enrollment ..."
  before doing it.
- **`preloop login` no longer re-authenticates when you are already signed
  in**: it prints the current identity and exits; pass `--force` to switch
  accounts. `--token` and non-interactive logins are unchanged. The install
  script does the same check before prompting, so re-running the installer on
  a machine that already has a valid session goes straight to onboarding
  instead of asking you to log in again.
- **Keychain service names in onboarding output are now quoted**, so the
  hyphenated macOS service `"Claude Code-credentials"` can no longer be
  misread as running into the surrounding prose.
- **Agent pause is now fully reversible** (#193): pausing an agent
  (`lifecycle_action=suspend`) deactivated *every* runtime API key the agent
  owned and closed its runtime session, but resume only flipped the lifecycle
  flag back to `active` — nothing reactivated the keys or reopened the session.
  A resumed agent therefore looked healthy in the console while every gateway
  request 401'd before a usage row could be written, so the agent silently
  logged nothing. Pause is now enforced purely as a lifecycle check on the
  read-through auth path (`authenticate_bearer_token`, API-key auth, and
  runtime token issuance already re-read `lifecycle_state` from the database on
  every request), so credentials are left untouched and resume is an exact
  inverse. Hard credential revocation is now reserved for the terminal states:
  decommission and delete. Resume and `reenroll` additionally heal agents
  bricked by the previous behavior — reactivating that agent's own unexpired
  keys and clearing `ended_at` — without ever reviving a credential an operator
  revoked on purpose.

- **Deterministic agent lookup by source**: `managed_agent.get_by_source()`
  returned an arbitrary row when several agents shared a session source, so a
  stale suspended or decommissioned sibling could shadow the live agent during
  token issuance. It now orders by lifecycle state (active, then suspended,
  then decommissioned) and falls back to most-recent-first.

- **Streaming model-gateway requests were cut off at 60s with a 504**: the
  `/openai`, `/anthropic` and `/gemini` routes are the only ones that relay
  streaming LLM responses, and they were the only proxied routes with no
  `proxy_read_timeout` override — so they inherited nginx's 60s default at
  *both* proxy layers (the console nginx and the ingress, which has its own
  independent default). Time-to-first-byte on a streaming completion is the
  model's thinking time, so this was deterministic on prompt size rather than
  intermittent: short prompts answered in seconds, while one large enough to
  make the model reason past a minute was killed by the proxy. The client saw
  a 504 that the gateway never observed and could not report, since the
  request was terminated in front of the application. All three routes now
  carry an explicit timeout (configurable via `gateway.proxy.*`, default
  900s), the ingress carries the matching annotations so a default install is
  correct without extra flags, and `proxy_buffering` is off so tokens are
  relayed as they arrive instead of being accumulated by nginx. A chart test
  asserts all of this so the override cannot be silently dropped again.

## [0.14.0] - 2026-08-07

Highlights: **Cursor usage import** brings bundled-model spend into Cost
analytics, **rate-limit intelligence** turns upstream 429s into a headroom
report, and **agent identity v2** gives managed agents a stable durable id
across renames and re-onboarding.

### Added

- **Auxiliary model fallback to system-wide default**: approval summaries and
  session/interaction titles now automatically retry with the system-wide
  default model when the account's primary model fails (auth error, provider
  error, or timeout). The fallback is subject to a per-account daily cap
  (default 50, configurable via `PRELOOP_AUX_FALLBACK_DAILY_CAP`) and emits a
  deduped warning per account per day when triggered. No fallback occurs if the
  system default is the same model that failed or if no system default is
  configured. The main gateway/completion path is unaffected and never falls
  back. Failures degrade gracefully (approval summary returns None, session
  titles use local fallback).

- **Cursor bundled-model usage import** (#123): Cursor's Composer/Auto models
  never traverse the model gateway, so their spend was invisible. Two new
  endpoints ingest it: `POST /api/v1/usage/import` for normalized events and
  `POST /api/v1/usage/import/csv` for the Cursor dashboard Usage CSV export
  (case-insensitive, order-independent headers, with an optional `column_map`
  for other export shapes). Imported events are attributed to a managed agent
  and land in the cost ledger as `action_type='imported_usage'` rows labeled
  `usage_source='imported'` / `cost_source='imported'`. Imports are idempotent:
  every event carries a dedupe fingerprint backed by a unique database index,
  so re-importing the same CSV reports `skipped_duplicates` instead of
  double-counting. Imported spend surfaces as a separate `imported_usage`
  block in `GET /api/v1/cost/summary` and never mixes into gateway
  `estimated_cost`, budgets, or spend caps. Cost analytics renders this as an
  "Imported usage" section showing imported events, tokens, and cost, plus a
  per-model table with the source and the last event time. The section carries a
  "Not gateway metered" badge and stays out of the spend metrics, budgets, and
  breakdowns, which continue to describe gateway-metered traffic only. It is
  hidden when the selected window holds no imported usage.
- **Rate-limit intelligence and subscription headroom** (#136): the gateway now
  captures upstream 429s and provider rate-limit headers (`Retry-After`,
  `anthropic-ratelimit-*`, `x-ratelimit-*`) as real observations, normalizes
  them into rate-limit snapshots, and persists them on the usage row.
  `GET /api/v1/account/gateway-usage/rate-limits` reports rate-limited request
  counts, blocked time, quota-exhausted vs transient breakdown, and per-model
  and per-session detail. Undocumented provider headers are preserved verbatim
  rather than presented as normalized facts.
- **Stable v2 managed-agent identity**: the CLI derives `session_source_id`
  from host + source type + config path, so an agent keeps one durable
  identity when its display name changes or it is re-onboarded. Use
  `--no-reuse` for a salted escape hatch. Adds `enrollment_hostname` and
  `identity_derivation` columns.
- **`POST /api/v1/agents/{id}/rekey`** and **`POST /api/v1/agents/{id}/merge`**:
  rewrite or consolidate durable principal ids across usage, sessions,
  budgets, and approvals, with dry-run support. Exposed as
  `preloop agents merge`.
- **`permission_prompt` builtin for Claude Code approvals** (#132): implements
  Claude Code's `--permission-prompt-tool` contract, resolving native tool
  permissions through Preloop policies and approvals.
  `PRELOOP_PERMISSION_PROMPT_WAIT_SECONDS` (default 25) tunes the in-call wait
  before a retryable pending deny. Default-off, so accounts that do not opt in
  pay no context tax. Ships with a general per-agent tool-config scope
  (`ToolConfiguration.managed_agent_id`); null preserves account-wide
  semantics.
- **Per-tool context cost on the Tools page** (#128): every tool shows
  `~N tokens/request` computed from the schema as actually served (including
  injected justification parameters), plus a summary line totalling what
  enabled tools add to every agent request.
- **Optimizer recommends disabling unused builtins** (#146): a deterministic
  `disable-builtin-tools` suggestion for Preloop builtins unused in the session
  with zero account-wide invocations over 30 days, with a one-click apply.
  Savings are not double-counted against `scope-tools`, and agent-provided
  tools are never touched.
- **ask_user in-session delivery** (#130): pending `ask_user` /
  `request_approval` responses now include token-free deep links to the
  specific question (`approval_console_url`, `approval_mobile_link` /
  `preloop://approve/<id>`), and when the asking session's runtime has an
  active Agent Control connection (hermes-preloop / openclaw-preloop), the
  question is also delivered as an audited in-session prompt through the
  existing `send_message` channel. Answers still flow only through the
  governed approval surfaces; first answer wins and late answers get an
  already-resolved response.
- **Review newly unlocked tracker tools after connecting a tracker**:
  `POST /trackers` returns additive `unlocked_tool_names` (server-side
  before/after diff of tracker-gated builtins that are effectively enabled).
  The Trackers page opens an opt-out review dialog listing each unlocked
  tool with its `~N tokens/request` cost and the keep-enabled context-tax
  delta; deselected tools are persisted as builtin `ToolConfiguration`
  rows with `is_enabled: false`.
- **Idle prompt-cache expiry detection**: session context analysis now flags
  content-stable request pairs whose inter-request gap exceeds the provider
  cache TTL and whose ApiUsage rows show a `cache_read` collapse with a
  `cache_creation` spike. Optimize surfaces a measured write-vs-read premium
  (``reduce-idle-cache-expiry`` suggestion + aggregate line); Replay annotates
  the expiry turn. USD figures are catalog-priced or omitted, never invented
  from session averages.
- **Passkey (WebAuthn) sign-in and registration**: register passkeys in user
  settings and sign in from the login page with discoverable credentials (no
  username needed). Feature-flagged via `PASSKEYS_ENABLED` (default `true`);
  relying party and origin overridable with `WEBAUTHN_RP_ID` and
  `WEBAUTHN_ORIGIN`. Passkey logins are audit-logged and trigger the same
  inactivity notifications as password logins.
- **Approval email staggered behind push** (#119): for users with both channels
  enabled, push goes out immediately and email waits 60 seconds, sending only
  if the approval is still pending. Email-only users are unaffected, and any
  push failure falls back to immediate email so delivery never degrades.
  Per-user `stagger_email` toggle (default on) in notification preferences.
- **Security-screen scoring endpoint** (#155):
  `POST /api/v1/security-screen/score` implements QM's external
  security-screen proxy contract. Accepts `{text, hook, metadata}` with the
  operator token in `x-api-key` and returns
  `{score, threshold, primary_outcome}` from a deterministic rule-based
  scorer (prompt-injection markers, destructive commands, destructive SQL,
  secret-exfiltration patterns). Threshold configurable via
  `PRELOOP_SECURITY_SCREEN_THRESHOLD` (default 0.7). Screened text is never
  logged or persisted; no schema changes.
- **`preloop agents remove`**: permanently delete a managed-agent registry
  entry. Refuses when the agent has usage history unless `--force` is passed.
- **CLI install-runtime UX** (#113): an interactive managed-model picker before
  `agents onboard` / `install-runtime` (with `--model` for non-interactive
  use), an explicit gateway round-trip check at the end of install
  (`round-trip OK, model=..., latency=...s`) with an actionable failure
  message, and printed reconfigure/undo hints after mutating agents commands.
- **OpenRouter as a first-class provider in the add-model dialog**: the backend
  has routed OpenRouter since the gateway fix, but the console never listed it,
  so adding an OpenRouter model meant choosing "OpenAI-compatible" and knowing
  the base URL by heart. OpenRouter is now its own entry in the provider list
  with `https://openrouter.ai/api/v1` prefilled, so "Fetch Available Models"
  works without typing an endpoint. The model list comes from OpenRouter's own
  `GET /models` (300+ entries render in full), and the "Other..." escape hatch
  still accepts custom identifiers such as the Auto Router
  (`openrouter/auto-beta`).
- **Moonshot (Kimi), Z.ai (GLM) and Mistral as first-class providers**: three
  new entries in the add-model dialog, each with its base URL prefilled and a
  link to the provider's key page, so "Fetch Available Models" works without
  typing an endpoint. Moonshot ships with bundled pricing for `kimi-k3`,
  `kimi-k2.7-code`, `kimi-k2.7-code-highspeed` and `kimi-k2.6` taken from
  Moonshot's published price list, so Kimi traffic is cost attributed from the
  first request instead of landing as unpriced usage. `kimi-k3` leads the
  keyless Moonshot list. Mistral is a BYOK option that keeps model traffic with
  a European provider for teams that care where inference runs.
- **Model lists say where they came from**: the available-models endpoint now
  returns `{models, source, error}` instead of a bare array, and the dialog
  renders a short notice when a list is the bundled fallback rather than the
  provider's live catalog, naming the reason (request timed out, network error,
  provider returned nothing, no API endpoint configured). With no key entered
  the notice invites you to add one and fetch again instead of blaming the
  provider. The reason vocabulary is fixed and carries no provider text, so a
  failing provider cannot echo a URL or key material into the console.

### Changed

- **`preloop agents offboard` archives instead of deleting**: offboard now
  decommissions the managed-agent row (PATCH `lifecycle_action=decommission`)
  so usage history and audit trail remain. Re-onboarding reactivates an
  archived match; use `preloop agents remove` for permanent deletion.

### Fixed

- **Session tracking for all agents** (#190): agents that authenticate with a
  durable managed-agent credential share one machine-scoped runtime principal,
  so every conversation on a machine collapsed into a single runtime session
  that never ended. Codex (`Session-Id`/`Thread-Id`), OpenCode (`X-Session-Id`)
  and any client sending OpenAI's `prompt_cache_key` are now split per
  conversation. Agent-native headers are only trusted when the credential
  identifies that agent, since `Session-Id` and `X-Session-Id` are generic
  names an intermediary may stamp. `X-Preloop-Session-Id` still takes
  precedence over everything. The `preloop agents` OpenClaw provider now
  enables `supportsPromptCacheKey`, so OpenClaw stops stripping its own
  conversation key against the Preloop gateway (this also improves upstream
  prompt-cache hit rate). Session identity telemetry follows OpenTelemetry
  GenAI's `gen_ai.conversation.id` vocabulary.

- **Sessions from agents that send no conversation id are now bounded by
  inactivity** (#190): Gemini CLI, Hermes and OpenClaw's Anthropic transport
  put no session id on the wire at all, so their sessions previously grew
  forever. After an idle window (`RUNTIME_SESSION_IDLE_TIMEOUT_MINUTES`,
  default 720, set to `0` to disable) the stale session is closed at its own
  last activity, so history is never rewritten, and the next request starts a
  new one. This is a fallback only: an agent that does identify its
  conversation is never split by the clock.

- **Codex flows failed at their first model call** (#190): every Codex flow
  errored with `Missing required parameter: 'tools[N].name'` on OpenAI models
  or `unknown variant 'namespace'` on DeepSeek, because the Codex CLI sends
  tool shapes (freeform `custom` tools, `namespace` containers, host-executed
  search tools) that upstreams reject, and the gateway rewrote custom tools
  into a form that dropped their name for models routed to the Responses API.
  Tools are now translated into plain function tools, with namespaced tools
  flattened rather than dropped so an agent keeps its MCP toolset, and tool
  calls are rendered back in the shape Codex expects.

- **OpenClaw plugin manifest migrated to the OpenClaw 2026.7.2-beta.7 schema**
  (`@preloop-ai/openclaw-plugin` 0.2.1): the ClawHub listing showed a
  `manifest-unknown-fields` warning because `openclaw.plugin.json` declared 11
  top-level keys that are not part of OpenClaw's published `PluginManifest`
  type. The manifest now carries only `id`, `name`, `description`, `version`
  and a real JSON Schema `configSchema`; the packaging and runtime metadata
  (`before_tool_call` hook, `tool_approval` capability, permission strings,
  config path, and the `preloop-openclaw-plugin verify` command) moved into the
  `openclaw` object in `package.json`, which is where OpenClaw and ClawHub read
  package-level metadata. Plugin behaviour is unchanged: the hook is registered
  in code and the config is read from the same
  `plugins.entries.preloop-plugin.config` path as before. The package lockfile
  was also regenerated (it still claimed 0.1.0 while the package said 0.2.0).
  ClawHub validation against an OpenClaw 2026.7.2-beta.7 checkout now reports 0
  errors and 0 warnings.
- **Gateway no longer returns 502 when activity metadata contains binary
  content**: an agent that fetched a gzip or otherwise binary URL through the
  gateway could take down its own request. The response body was embedded into
  `runtime_session_activity.metadata` (JSONB), Postgres rejected the NUL byte
  it contained (`UntranslatableCharacter`), and because that insert shares the
  request's database session, the failed flush left the session in a
  pending-rollback state. A model call that had already succeeded upstream came
  back to the customer as a 502, and later operations on the same session
  failed too. Three changes: all activity and usage metadata is now sanitized
  of NUL, control characters and lone surrogates (tab, newline and carriage
  return are preserved) before any JSONB write; request and response bodies
  stored in activity metadata are capped (default 8192 characters per string,
  `MODEL_GATEWAY_ACTIVITY_MAX_BODY_CHARS`) with an explicit truncation marker,
  since one incident row reached 533,682 characters; and usage recording is now
  non-fatal, rolling the session back and logging the failure type so that
  bookkeeping can never fail a request whose model call succeeded.

- **Auxiliary model calls resolve credentials from the secret service**: nine
  internal sites (approval summaries, session/interaction titles, policy
  generation, agent name extraction, issue compliance/duplicates/dependencies)
  were reading the raw `api_key` column directly instead of resolving via the
  secret service, so accounts whose models use `credentials_secret_id` got
  silent 401s. All nine sites now route through a shared credential resolver
  that handles legacy plaintext keys, vault-backed secrets, OAuth, and ambient
  credentials identically to the main gateway path. The `os.getenv` fallback in
  issue compliance and duplicates endpoints is preserved.
  - The compliance improvement suggestion and duplicate resolution suggestion
    endpoints built their client with no credentials at all, so they used
    whatever ambient `OPENAI_API_KEY` the process happened to have (and failed
    outright when it had none) regardless of the account's configured model.
    Both now resolve the account model's credentials and honor its custom
    endpoint.
  - Dependency detection now forwards the model's custom endpoint as
    `base_url`; previously it resolved the key but dropped the endpoint,
    sending traffic for custom-endpoint models to the default provider.

- **DeepSeek and Qwen model pickers show the models the provider actually
  serves**: both providers were queried with a valid API key and the response
  was thrown away, the key being validated and nothing more, so the picker only
  ever offered a catalog hardcoded in early 2025. Newer models such as
  `deepseek-v4-flash` and `deepseek-v4-pro` were invisible in the console even
  though the bundled price table already prices them. The live list is now
  returned (sorted and de-duplicated) whenever a key is supplied. An invalid
  key still surfaces as an authentication error; a network or listing failure
  falls back to the bundled catalog instead of emptying the picker. The
  keyless fallback catalog now includes the DeepSeek v4 models.

- **Every provider now attempts a live model list**: DeepSeek and Qwen were
  fixed earlier, but the same fetch-and-discard pattern survived elsewhere.
  Anthropic returned a list hardcoded in early 2025 after spending a paid
  `messages.create` call purely to check the key; it now lists models through
  the Anthropic models endpoint and costs nothing to refresh. Google spent a
  paid `generate_content` call for the same reason and silently returned its
  hardcoded list when the listing came back empty; the paid ping is gone and an
  empty listing is reported rather than hidden. OpenAI truncated the account's
  catalog to the first ten ids and filtered to `gpt-*`, which hid the entire
  o-series and would have hidden every future family; the cap is removed and
  non-chat ids (embeddings, whisper, tts, image, moderation) are excluded
  instead. No provider returns a bundled list without saying so.

- **Failed model tests said nothing useful**: testing a model that the upstream
  provider rejected showed only "Failed to run model request" while the real
  reason, for example "No allowed providers are available for the selected
  model", was visible only in the gateway log. The provider's own message is now
  lifted out of the upstream error and shown, with a short hint naming the
  provider. The surfaced text is scrubbed for credentials and capped in length,
  and stack traces and provider metadata blobs are not included.

- **Editing a model with a stored key failed**: opening any saved model and
  changing a field posted the whole form back, including the credential fields
  belonging to the stored secret, and the API rejected it with
  `credential_type/credential_payload cannot be combined with external
  credential fields`. Editing was effectively impossible without deleting and
  recreating the model. The update now sends only the fields the form manages,
  and credential fields only when a new API key is actually typed.

- **Commit statuses post to the repository that triggered the flow** (#175): a
  flow watching several projects always posted its GitHub check to
  `trigger_project_ids[0]`, so a push or pull request in any other watched
  repository targeted the wrong repository and the provider rejected the call
  with `422 No commit found for SHA`. The failure was swallowed, so the run
  still looked healthy while no check ever appeared. The project is now
  resolved from the repository that actually triggered the execution. When the
  triggering repository cannot be matched to a Preloop project, Preloop refuses
  to guess and skips the status instead of posting to an unrelated repository,
  and every skip or provider failure is surfaced as a warning on the execution
  timeline rather than only in the server log.
- **Dashboard Recent Flow Executions dismiss control stays reachable** (#174):
  a long error message, typically a git clone failure containing an unbreakable
  repository URL, widened the text column past the card and pushed the dismiss
  button outside it, so a failed run could not be cleared from the dashboard.
  The text column can now shrink, long URLs and paths wrap, the message is
  capped at three lines with the full text available on hover, and the status
  tag, links, and dismiss button keep their size at every viewport width.
- **Managed agents report their real product kind** (#123): Cursor, Windsurf,
  VS Code, Antigravity, and Devin agents all recorded `agent_kind` as
  `desktop_agent` (or `custom` when created via `POST /api/v1/agents`), because
  the kind was derived from the connection's `session_source_type`. As a
  result they showed as generic agents in the console, and the default
  attribution target for `POST /api/v1/usage/import` could never be resolved
  (a bare import returned HTTP 422 for every account). `agent_kind` is now
  decoupled from `session_source_type`: `POST /api/v1/agents` accepts an
  optional `agent_kind`, and the CLI reports the product it is onboarding when
  minting a runtime-session token. `session_source_type` is deliberately
  unchanged, since it is part of the durable v2 principal-id fingerprint:
  existing enrollments keep their identity, spend history, and credentials,
  and are refined in place rather than re-keyed. An older CLI that does not
  send `agent_kind` can no longer reset a known kind back to the generic one.
- **Gateway upstream provider errors are classified** (#116, #117, #118): a
  shared `classify_upstream_error` taxonomy (`network`,
  `upstream_overloaded`, `upstream_rate_limited`, `upstream_quota_exhausted`,
  `upstream_auth`, `upstream_disconnect`, `upstream_error`,
  `client_cancelled`) covers streaming and non-streaming paths. Connection
  refused and transport failures now return a clear **503** instead of an
  opaque 500, mid-stream provider disconnects emit an SSE
  `upstream_disconnect` event followed by `[DONE]`, and quota-exhausted 429s
  are marked terminal with `Retry-After` / `X-Preloop-Retry-Terminal` so
  runtimes fail fast. The failure class is persisted on the usage row
  (`ApiUsage.error_class`) so cost and session views can separate
  provider-side failures.
- **ask_user approve→execute handoff**: replaying an approved `ask_user`
  through `get_approval_status` now returns the approver's comment (the
  human's answer) as the tool result instead of losing it; async-workflow
  pending payloads pass through to the agent instead of being misreported
  as "No answer provided".
- **Sessions no longer expire aggressively**: refresh failures caused by
  transient errors (5xx, network) no longer clear tokens and force re-login;
  only definitive 401/403 does. OAuth logins now store refresh tokens.
  Active sessions slide up to a 30-day cap.
- **CLI build repair** (#144): restore `recoverDeferredGatewayValidationFailure`
  in `cli/internal/cmd`, which a bad merge left uncompilable and which broke
  the Windows CLI test job on every open PR.
- **Code Quality / Scorecard hygiene**: clear GitHub Code Quality
  maintainability warnings (implicit string concat in gateway tests;
  unused-export false positives), pin GitHub Actions and Docker base images by
  digest, bump the CLI Go toolchain to 1.26.5 and
  `golang.org/x/{text,crypto,sys}` for Scorecard vulnerability findings,
  override frontend `basic-ftp`/`yaml` advisories, harden refresh-token error
  responses, and document/wire `REFRESH_TOKEN_EXPIRE_DAYS` / `MAX_SESSION_DAYS`
  in Helm.
- **Single Alembic head restored** (#162, #163): parallel feature merges left
  the migration graph with multiple heads, breaking `alembic upgrade head` on
  self-hosted upgrades. The heads are collapsed into one mergepoint
  (`20260801_stagger_email`) with a regression guard.
- **Hermes plugin verify crash** #165: AgentControlConfig in preloop 0.13.x
  does not expose a runtime attribute. The Hermes plugin reads config.runtime,
  but that field is only present in the raw YAML config block, not the parsed
  dataclass.
- **OpenRouter models routed to the wrong vendor** (#172): a model id
  containing a slash was treated as `provider/model` before the stored
  provider and endpoint were consulted, so an "OpenAI-compatible" model on
  `https://openrouter.ai/api/v1` with the id
  `deepseek/deepseek-v4-flash-0731` was sent to api.deepseek.com with the
  vendor prefix stripped, producing upstream `502 Invalid URL` errors. The
  Auto Router (`openrouter/auto-beta`) failed for the same reason. The stored
  `provider_name` and `api_endpoint` now take precedence over the prefix
  heuristic: concrete OpenRouter ids, the `openrouter/`-prefixed workaround
  form, and the Auto Router all route to OpenRouter with the model id intact.
  Users who added the `openrouter/` prefix by hand are not broken by the fix.
- **Model picker was empty for OpenRouter** (#171):
  `available-models` returned `[]` for every openai-compatible provider,
  because only the built-in providers had a discovery path. The configured
  endpoint's OpenAI-compatible `GET /models` is now queried, so OpenRouter,
  vLLM, LM Studio, and similar endpoints populate the picker. The request
  takes an `api_endpoint`, which the picker sends from the form.

### Security

- **Provider API keys no longer travel in the URL**: the
  `/api/v1/ai-models/providers/{provider}/available-models` endpoint accepted
  `api_key` as a query parameter, so live provider keys were written to
  server access logs in plaintext. The key now travels in the POST body (or
  the `X-Provider-Api-Key` header on the deprecated GET form) and the query
  parameter has been removed rather than deprecated. These endpoints also now
  require authentication, and the endpoint they fetch is validated so it
  cannot be aimed at loopback or link-local addresses. **Operators should
  rotate any provider key entered through the model picker before this
  release**, and check access logs for `api_key=`.

## [0.13.1] - 2026-07-28

### Added

- **SignPath code signing policy** on the README and release notes (Windows
  binary section), required for SignPath Foundation attribution
  (`docs/code-signing-policy.md`).

- **Windows PowerShell CLI installer** (`install-cli.ps1`) with
  `irm https://preloop.ai/install/cli.ps1 | iex`, release `SHA256SUMS`, and
  docs for Defender false-positive recovery (`docs/windows-cli.md`).
- **Optional SignPath Authenticode signing** for Windows CLI release
  binaries, plus PE version metadata via `go-winres`, and optional
  VirusTotal upload when `VIRUSTOTAL_API_KEY` is set
  (`docs/windows-code-signing.md`).

### Fixed

- **CLI installer on 32-bit Git Bash / MSYS**: `detect_arch` now prefers
  `PROCESSOR_ARCHITEW6432` / `PROCESSOR_ARCHITECTURE` so 64-bit Windows no
  longer fails with `Unsupported architecture: i686`.

## [0.13.0] - 2026-07-26

### Added

- **Account-wide governance defaults for native tool approvals.** New
  `GET/PUT /api/v1/account/governance-defaults` endpoints store account-level
  defaults that every managed agent inherits, with per-agent
  inherit/override controls in the Console (Tools view account panel and the
  agent detail view). Resolution is fail-closed: explicit per-agent value →
  account default → enforce.

- **Claude Code model-family fidelity.** Onboarding imports one gateway
  model per selectable Claude family (opus/fable/sonnet/haiku) sharing a
  single credential secret, so `/model` switching, background fast-path
  requests, and subagents keep native UX while routing through Preloop. The
  gateway lazily auto-registers unknown `claude-*` identifiers requested
  over a subscription-OAuth credential (e.g. new dated snapshots after a
  Claude Code update) against the same credential
  (`MODEL_GATEWAY_CLAUDE_FAMILY_AUTOREGISTER_ENABLED`, default on). Fable is
  now a first-class family, and Fable-defaulted Max accounts (including
  `[1m]` context-window variants) route correctly.

- **Session History redesign.** The transcript now defaults to newest-first
  (messages within each turn follow the turn sort), partially cached
  requests collapse their re-sent prompt-cached prefix behind a labeled
  strip, and the session list hands its column to the transcript once a
  session is selected — collapsing into a compact picker bar with an
  animated hand-off. Plus: keyboard navigation (`j`/`k`/arrows, `Home`/`End`,
  `Enter`/`o` to expand), clickable summary-bar stats (Cost jumps to the most
  expensive turn, Outcome to the first failure), relative turn timestamps
  with the absolute time on hover, and a deep-linkable replay mode
  (`?replay=` alongside `?sessionId=`). All motion respects
  `prefers-reduced-motion`.

- **Onboarding UX hardening (CLI).** Batch onboarding runs verified-model
  agents first, uninstalled runtimes left behind as config-only are
  detected and skipped, interactive onboarding asks for the agent name
  exactly once, failures point at the troubleshooting docs, and OpenClaw's
  plugin trust gate is satisfied with guidance for unboarded installs.
  Claude Desktop onboarding writes a stdio `mcp-remote` bridge, and
  non-Anthropic managed models map all Claude Code model selectors so
  background/fast-path requests resolve too.

- **User hard-delete.** User/account hard-delete CRUD that preserves
  audit/usage history; Claude Code custom API key fingerprints are
  pre-approved on onboard. Permanent delete stays out of the OSS console
  (site-admin/billing paths own Stripe cleanup when the billing plugin is
  present).

### Changed

- **Native `Write`/`Edit` mirroring for Claude Code asks by default.** The
  permission hook now mirrors stock Claude Code — which prompts for
  workspace edits in default permission mode — instead of silently
  auto-allowing them. Approval-timeout denials now carry a `timed_out`
  marker so hook adapters with a native "ask" verdict hand the prompt back
  to the agent's local UI instead of hard-denying.

### Fixed

- **Windows: slash-rooted paths in the workspace-edit check.**
  `filepath.IsAbs("/etc/passwd")` is false on Windows, which routed
  slash-rooted paths down the workspace-local branch and auto-allowed them
  on Windows only. Slash- and backslash-prefixed paths are now treated as
  rooted on every host OS.

- **Claude family auto-registration is savepoint-scoped.** A registration
  failure now rolls back only its own writes instead of discarding
  unrelated pending state from the request pipeline.

## [0.12.8] - 2026-07-19

### Fixed

- **Models imported from custom OpenAI-compatible providers failed at the
  gateway.** Hermes configs can declare arbitrary provider names for
  OpenAI-compatible endpoints (`model.provider: custom`, e.g. a
  `kimi-for-coding/k3` entry); the gateway forwarded the name to litellm as a
  provider prefix, which litellm rejects ("LLM Provider NOT provided") even
  with `api_base` set — so onboarding live-validation and all model traffic
  for such agents failed. Unknown providers with their own endpoint now route
  through litellm's generic OpenAI-compatible adapter. All litellm
  model-string building is unified in one shared module, fixing the same
  latent break in policy generation, approval summaries, session-explorer
  analysis, and agent-name extraction when the account default model is a
  custom-provider one.

## [0.12.7] - 2026-07-19

### Added

- **Bootstrap setup token for the first registration.** On a fresh (zero-user)
  instance with `PRELOOP_BOOTSTRAP_TOKEN` configured, `/register` requires the
  token: the installer generates one, persists it to the instance `.env`, and
  prints a `/register#bootstrap=<token>` setup link to the terminal only. This
  closes the race where a stranger could claim a freshly installed public
  instance before its operator. First signup is serialized with a database
  advisory lock, and account+user+role now commit in a single transaction.
- **Async session-optimization jobs.** `POST /account/runtime-sessions/{id}/optimizations/jobs`
  runs the analysis in a bounded background worker and returns `202` with a
  pollable job; the console Optimize tab shows analyzing / failed+retry /
  no-waste states instead of a spinnerless multi-minute wait. The synchronous
  endpoint is unchanged.
- **Activation telemetry markers.** Self-hosted instances report a one-time
  `install_completed` marker on the existing daily version check-in, and the
  CLI reports a one-time `cli_first_run` marker — both suppressed by
  `PRELOOP_DISABLE_TELEMETRY`. A new in-instance `first_session_seen` hook
  registry lets plugins observe first agent activity; the event never leaves
  the instance. The full contract is documented in SECURITY.md.

### Fixed

- **Per-principal model authorization at the gateway.** Model listing,
  requested-model resolution (exact and suffix), and default-model selection
  now all consume one authorized-model computation: principal-bound
  subscription-OAuth models are visible only to their bound managed agent, and
  credentials with no bound models fail closed instead of seeing everything.
  Rejections return `model_not_authorized` with the usable model ids.
- CLI first-run telemetry read `first_run=false` on the very first run.

## [0.12.6] - 2026-07-19

### Fixed

-  Hero title rendering: escaped gradient span shown as literal text

## [0.12.5] - 2026-07-19

### Fixed

- **The free/trial hosted-model spend cap could be bypassed by cheap traffic.**
  The cap summed gateway usage from a query that orders models by *request
  count* and then truncates to 20 rows, so an account making thousands of cheap
  BYOK calls pushed its low-volume, high-cost hosted-model usage past the
  cutoff. That spend was never summed and the account counted as $0 against
  `billing_trial_hosted_model_hard_cap_usd` — the hard cap silently stopped
  enforcing, and founder-paid hosted inference ran unmetered for exactly the
  accounts spending the most. The cap now filters to hosted models in SQL and
  reads every matching row, since truncating a SUM by request count can never
  produce a correct spend total.
- **A per-subject `allowed_models` policy only governed one spelling of a
  model.** The gateway resolver accepts both a model's canonical alias
  (`anthropic/claude-opus-4-1`) and its bare provider-suffix form
  (`claude-opus-4-1`), but the budget preflight compared the raw client wire
  string, so the two spellings were separate policy keys and an admin who
  listed one had not listed the other. The check now keys off the resolved
  model and matches any spelling that reaches it — canonical alias, configured
  alias, bare identifier, or the raw request string — so one allowlist entry
  covers the model however a client names it. Relatedly, a request naming no
  model at all skipped the allowlist entirely; enforcement now runs whenever an
  allowlist exists and fails closed. Accounts with no `allowed_models`
  configured are unaffected.
### Added

- **One provider key can now back several models.** After a provider key
  validates, the add-model dialog offers the rest of that provider's models
  under "Also add", creating an `AIModel` row per selection that reuses the
  single stored credential. Deleting one of them leaves the key in place for
  the others; deleting the last one removes it.

### Fixed

- **Model resolution was nondeterministic when several models shared an
  identifier suffix.** The gateway matched a requested model against an
  unordered query and returned on the first suffix match, so a bare
  `claude-sonnet-4-5` could resolve to `anthropic/…` on one request and
  `bedrock/…` on the next — a different `ai_model_id`, and therefore different
  pricing, between otherwise identical requests. A suffix match on an earlier
  row could also beat an exact match on a later one. Exact alias matches now
  always win, and the candidate list is ordered deterministically
  (account-owned models before system defaults, then oldest first). This was
  latent while accounts held one model each; multi-model keys make it
  reachable.

## [0.12.4] - 2026-07-18

### Fixed

- **Push notifications were failing for every Android device.** Three defects
  composed: the app registered a literal `fcm_unavailable_<millis>` placeholder
  when the Firebase token fetch failed (non-blank, so it passed every backend
  check before FCM rejected it); error classification matched substrings that
  miss `INVALID_ARGUMENT`, so the bad token was never pruned and was retried on
  every approval; and the token-refresh endpoint 404'd inside a log-only catch,
  so it could never be replaced. FCM errors are now classified by type, only
  client-side faults prune (a credential outage must not wipe every user's
  token), the Firebase `project_id` is logged on failure, and placeholder-shaped
  tokens are rejected at registration.
- **The Hermes plugin gated nothing.** `pre_tool_call` was registered as `async
  def`, but Hermes invokes plugin hooks synchronously and discards non-dict
  results, so every tool call proceeded ungated. A synchronous bridge now spans
  the async decision path. Unreadable configuration and non-mapping response
  bodies were two further silent-allow paths and now block.
- **Estimated savings could exceed the analyzed scope** (131% observed). Schema
  tokens were already resend-aware and were then multiplied by `resend_count`
  again, making `scope-tools` savings quadratic. Savings now roll up through a
  deduped profile-level total, clamped to analyzed scope with a logged warning.
- Principal-bound OAuth models are no longer auto-selected as the default. They
  cannot serve server-side generation, so a user whose only credential was a
  Claude Code or Codex subscription hit a server-side failure on their first
  optimization run. The first BYOK model wins instead.
- `release.py` now restamps `openapi.yaml`, which previously went stale on every
  version bump and failed the lint job on each release.
- The installer no longer breaks under Git Bash: MSYS rewrote the certbot `-w`
  path into the Git install directory.

### Added

- Bundled example session on the Optimize tab, shown when an analysis yields no
  savings. It runs the production analyzers over an in-memory transcript with
  zero database writes, so it cannot contaminate cost or savings aggregates, and
  is labelled as an example rather than the user's own data.
- Admin-only push test-send that exercises the real provider path and surfaces
  the verbatim provider error. The synthetic approval is persisted nowhere, so it
  cannot appear in approval lists, feeds, or metrics.
- Windows documentation: the OSS stack already ran under Docker Desktop with the
  WSL2 backend and Windows CLI binaries already shipped on every release, but
  neither was documented. Native Windows support is explicitly not claimed.
- Claude Desktop discovery now resolves `%APPDATA%` on Windows via
  `UserConfigDir`, which also picks up `~/Library/Application Support` on macOS.
- Non-gating `windows-latest` CI job for the CLI.
- PyPI metadata (keywords, classifiers, license, project URLs), which was
  entirely absent, and a refreshed Helm chart description and keywords.

### Changed

- OpenClaw and Hermes runtime plugins to 0.2.0. ClawHub publishing is automated
  alongside npm and guarded by a post-publish digest comparison — the two
  registries had shipped different artifacts under the same version.

## [0.12.3] - 2026-07-18

### Fixed

- Console nginx no longer 504s long-running API requests: the `/api/` proxy
  timeouts (console image and helm chart nginx configmap) were raised from 60s
  to 300s. LLM-powered cost optimization suggestions
  (`POST /api/v1/billing/cost/runtime-sessions/{id}/optimizations`) and replay
  verification legitimately take ~90s on slower BYOK models; the browser got a
  504 while the backend finished and cached the result, so a retry succeeded
  instantly and the feature read as broken. Static-asset serving is unchanged.
  The durable fix — running these analyses as async jobs with polling/SSE — is
  tracked as a follow-up.

## [0.12.2] - 2026-07-18

## [0.12.1] - 2026-07-17

## [0.12.0] - 2026-07-17

### Added

- Recorded end-to-end lifecycle rig (`scripts/e2e-rig/`): drives the full
  offboard → teardown → reinstall → onboard → verify → offboard cycle of an
  OSS instance on a real VM, records every browser and terminal step, and
  asserts each agent's model/MCP config is restored after offboarding. The
  deep, recorded complement to CI's release smoke test
  (`scripts/release_smoke_test.sh`); see `scripts/e2e-rig/README.md`.
- **Session optimization, one-click apply, and replay verification are now
  open source.** The full value loop moved from the proprietary billing plugin
  into the core backend: evidence-grounded waste findings for a runtime
  session (`POST /api/v1/billing/cost/runtime-sessions/{id}/optimizations`),
  one-click apply of suggested governance/budget actions (`.../optimizations/apply`,
  `GET .../optimizations/actions`), and consent-gated replay verification of a
  candidate's savings (`.../replay`) — all powered by your own model keys
  (BYOK). New service modules: `preloop.services.session_optimization`,
  `preloop.services.context_analysis`, `preloop.services.replay_savings_service`,
  `preloop.services.replay_harness`, `preloop.services.savings_measurement`,
  and `preloop.services.budget_headroom` (account hard-cap headroom for the
  replay feasibility precheck).
- **Analysis-model authorizer extension point**
  (`preloop.services.optimization_gating`): deployments that meter built-in
  hosted models (operator-paid compute) can register an authorizer consulted
  before any LLM-powered analysis runs. The open-source default allows all
  models; deterministic analysis and BYOK models are never gated.
- The `optimization_result_viewed` audit event is now emitted by the core
  optimize endpoint in every edition, so deployments can measure when users
  first see their own waste number.

### Fixed

- **OSS installer trust repairs** (`scripts/install-oss.sh`):
  - **Admin email is validated at the prompt.** The installer previously
    accepted an empty admin email and only failed at the very end of the
    install (`create_first_user.py` requires an email), leaving the operator
    with an account that does not exist and signups silently open. The prompt
    now loops until a plausible address is given (or first-user creation is
    explicitly skipped), and unattended runs (`PRELOOP_ADMIN_*`) fail fast
    before any work when the email is missing or malformed. If first-user
    creation still fails at runtime, the installer now ends with a loud `!!!`
    banner — what failed, that signups are still open, and the exact retry
    commands — and exits non-zero, instead of a warning that scrolled away.
  - **`curl | sh` stdin theft fixed.** `docker compose exec`/`run` inherited
    the pipe sh was still reading the script from, consuming unparsed script
    bytes and crashing the installer mid-run ("Syntax error: Unterminated
    quoted string"). Every docker invocation now redirects stdin from
    `/dev/null`, and the whole script is wrapped in a `main()` invoked on the
    last line, so a partial download or stdin consumption can never execute a
    half-parsed script.
  - **Docker daemon preflight.** Before doing anything, the installer verifies
    the docker CLI exists AND the daemon answers within 10 seconds
    (`timeout 10 docker info`, with a fallback when `timeout` is absent), and
    that Docker Compose v2 is available — with distinct, actionable messages
    for "not installed", "daemon not running", and "daemon wedged — restart
    Docker Desktop". Previously a hung Docker Desktop passed the binary check
    and the install stalled forever at the first pull with no message.
  - **Quiet, logged docker output.** Image pulls and `compose up` chatter
    (~3,500 lines of layer-progress redraws) now go to
    `~/.preloop-oss/install.log`; the terminal gets a few curated status lines
    and the log path. Failures print the last log lines inline.

## [0.11.1] - 2026-07-14

### Fixed

Add missing create_first_user.py script

## [0.11.0] - 2026-07-13

### Overview — 0.11.0 since 0.10.0

Where 0.10.0 turned Preloop into an agent control plane, **0.11.0 makes it
trustworthy to run**: the money is counted correctly, the control channel stays
up, agents can ask you questions instead of only asking permission, and the
self-hosted install is something you can actually put on the public internet.
This rolls up everything in 0.11.0-rc.0 and 0.11.0-rc.1; the highlights:

- **Token and cost accounting you can audit.** Streaming requests recorded zero
  tokens unless the client happened to opt in — the gateway now always requests
  usage from upstream, estimates when a provider withholds it, and still records
  a row when the client disconnects mid-stream. Prices come from a vendored,
  versioned catalog (`scripts/update_model_prices.py`) instead of whatever
  litellm version happened to be installed, with live lookup for models it has
  never seen. Overrides are resolved through one code path (with currency and
  FX support), cache-read and reasoning tokens are first-class columns, and
  historical rows can be repriced retroactively. Unpriced usage is now visible
  rather than silently summing to zero.
- **Agent questions, not just approvals.** The new `ask_user` tool lets an agent
  ask you a real question — multiple choice, free text, or both — routed through
  the same approval, notification, and audit pipeline. It is answerable from the
  Console, the iPhone, and the Apple Watch (standalone, with dictation and
  spoken summaries, so an answer never requires reaching for your phone).
- **Agent Control that stays connected.** Durable managed-agent credentials were
  rejected by the control WebSocket, and the CLI wired the control channel with
  a token that expired after two hours — together these took OpenClaw and Hermes
  offline shortly after every onboard. Both are fixed, and a rejected control
  connection now logs why instead of a bare 403.
- **A self-hosted install that survives contact with the internet.** The OSS
  installer now asks for the instance's public URL, provisions a Let's Encrypt
  certificate, configures SMTP, creates the first user, closes public signup,
  and upgrades an existing instance in place (with a database backup taken
  first) instead of half-reconfiguring it.
- **Hardening.** A dozen security fixes, including refresh tokens being accepted
  as access tokens, an unauthenticated debug endpoint that echoed credentials,
  MCP firewall and approval checks that failed *open* on error, and tracker
  credentials stored in plaintext.

**Upgrade notes:** PostgreSQL **15+ is now required** (see 0.11.0-rc.0). Run
`alembic upgrade head`; the budget spend-bucket migration deduplicates existing
rows automatically.

### Fixed

- **Agent Control died two hours after every onboard**: the CLI wrote the
  short-lived runtime *session* token (120-minute expiry) into the runtime
  plugin's control config, while every other integration got the 365-day durable
  managed-agent credential. The control channel has no token refresh, so OpenClaw
  and Hermes silently dropped offline once it expired and only came back after a
  re-onboard. The control config now carries the durable credential, and the
  helper takes the credential rather than a bare token so a short-lived one
  cannot be wired in again. A rejected control WebSocket also logs the reason —
  previously a pre-accept close surfaced as a bare `403` with no explanation
  anywhere.
- **Self-hosted console could not reach its own API**: the released compose file
  never set `API_URL` on the console container, so it fell back to the image
  default `http://localhost:8000` — which inside that container is the console
  itself. Every `/api` call returned 502 and nobody could log in to a fresh OSS
  install. The nginx template also proxies through a variable without declaring a
  resolver, which fails for *any* hostname; both are fixed, and the release smoke
  test now exercises the console → API path a browser actually uses instead of
  only hitting the API directly.
- **Installer attempted impossible certificates**: hostnames under
  `*.googleusercontent.com` (and similar cloud-provider names) publish a CAA
  record that forbids Let's Encrypt from ever issuing for them. The installer now
  detects this before running certbot, explains that it is permanent rather than
  a DNS or firewall problem, and continues over plain HTTP at the given hostname
  instead of leaving a broken `https://` URL behind. The CAA check works on a
  stock cloud image (no `dig` required).
- **Installer wrote a mangled `.env`**: an unquoted heredoc executed the
  backticks in a comment, which both corrupted the comment and printed a stray
  `no configuration file provided: not found` error during install.

### Added

- **Install-time first user and signup lockdown**: the OSS installer offers to
  create the operator's account and disable public registration, so a freshly
  exposed instance is never reachable-and-open to whoever finds it first. Driven
  interactively or unattended via `PRELOOP_ADMIN_USERNAME` / `PRELOOP_ADMIN_EMAIL`
  / `PRELOOP_ADMIN_PASSWORD` (`PRELOOP_SKIP_ADMIN=1` opts out). The new
  `scripts/create_first_user.py` performs the same account setup as signup —
  owner role, default approval workflow — with the email pre-verified, since a
  fresh install has no SMTP to send a verification mail. The user is created
  *before* registration is closed, so a failure leaves signup open rather than
  locking the operator out.
- **Watch agent status stays fresh**: the watch fetched Agent Control state once
  per launch, so an agent that came back online still showed "plugin offline"
  indefinitely (`onAppear` does not re-fire across wrist raises). It now refreshes
  when the app becomes active, refreshes stale data on appear, polls while the
  agent list is on screen, and offers an explicit Refresh control with a
  last-updated line. A failed refresh keeps the last known agents instead of
  blanking the list.

## [0.11.0-rc.1] - 2026-07-13

- **Answer agent questions from the web console**: `ask_user` requests now render as questions in the Console approvals list and on the single-approval page — one button per offered option plus a free-text answer box when the agent allows it (Dismiss declines the question). Previously the web UI could only approve or decline them.

## [0.11.0-rc.0] - 2026-07-13

**Breaking / upgrade notes:** PostgreSQL **15 or newer is now required** — the
budget spend-bucket migration recreates a unique constraint with
`NULLS NOT DISTINCT` (PG 15+ syntax) so account-level buckets accumulate
instead of inserting one row per request. Deployments on PG 13/14 must upgrade
Postgres before running `alembic upgrade head` (the failed migration rolls
back cleanly, but the upgrade will not proceed). The stack has shipped
`pgvector/pgvector:pg16` since 0.9.x; this only affects external/managed
databases pinned to older majors. The same migration also deduplicates
existing spend rows (staging observed ~69k → ~4.5k) and requires no manual
action.

### Added

- **OSS installer upgrades in place**: re-running the install command now upgrades an existing instance instead of half-reconfiguring it. Previously a bare `curl … | sh` re-run reset a public instance's `PRELOOP_URL` back to `localhost` (wiping the configured origin and CORS), left the TLS proxy/certbot containers orphaned while compose managed only the plain stack, and kept applying a `docker-compose.override.yaml` that compose loads implicitly. The installer now loads the existing `.env` as the baseline (URL, SMTP, TLS state, secrets all preserved), announces the version change, dumps the database to `~/.preloop-oss/backups/` before migrations run, pulls images before recreating containers, and passes `--remove-orphans` so services dropped by a new version are cleaned up. Certificate issuance stays idempotent (an existing certificate is never re-requested, avoiding Let's Encrypt rate limits).
- **OSS installer: public URL, automatic HTTPS and SMTP setup**: `install-oss.sh` now asks for (or takes from the environment) the instance's public URL and SMTP credentials. A public `https://` URL provisions a Let's Encrypt certificate with certbot — an nginx proxy terminates TLS in front of the stack, HTTP is served first so ACME can complete, and a sidecar renews every 12 hours. Certificates are only requested for public DNS names (`localhost`, bare IPs and `.local` are skipped); `PRELOOP_TLS_STAGING=1`, `PRELOOP_SKIP_TLS=1` and `PRELOOP_SKIP_SMTP=1` cover rehearsals, external TLS termination and unattended installs. `PRELOOP_URL`/`ALLOWED_ORIGINS` and the `SMTP_*` variables are now actually passed to the API and worker containers (previously they were unreachable from compose, so self-hosted instances could not send approval emails at all).
- **Question-aware push notifications**: `ask_user` requests are now pushed as questions rather than approvals — the payload carries `is_question`, `question`, `question_options`, and `allow_free_text`, the title reads "Agent question", and the APNs category is set per option count (`QUESTION_2_OPTIONS` … `QUESTION_4_OPTIONS`, or `QUESTION_REQUEST` for free-text-only / more than four options, since iOS notification categories are static and cap at four actions). Mobile clients use this to offer one-tap option buttons and a dictated inline answer straight from the notification. Approval payloads are unchanged.
- **Flow orchestration on sync workers**: Optional `FLOW_EXECUTION_WORKER_ENABLED` runs `FlowExecutionOrchestrator` on a dedicated JetStream worker pool (`execute_flow` / `resume_flow_execution`) with DB claim/heartbeat leases, ack-after-claim, periodic stale-claim reclaim, SIGTERM drain/redispatch, and API-side recovery gated when the flag is on. Helm pool `flow-execution` and compose `flow-worker` service included.
- **Durable Agent Control command persistence**: Operator commands are stored before delivery, with ack/delivery scoped to the target managed agent, batched redelivery marks, and enum CHECK constraints for command status / cost provenance markers.
- **Per-agent native-tool approval workflow**: Operators can pin an approval workflow on a managed agent from the Console agent detail view (Tools & Governance → Native tool approvals). The pin is stored in subject governance as `approval_workflow_id` and takes precedence over the account default when `POST /api/v1/agents/permission-check` resolves a workflow. Governance updates reject workflow IDs that are invalid or not in the account.
- **Default approval-workflow backfill**: The API startup repair pass now also seeds the account-default workflow (owner as approver) for active accounts that have none, covering signup-seed background tasks lost to restarts or transient failures.
- **Interactive approvals opt-in**: Discover-driven agent onboarding prompts for native tool-approval hooks on supported agents (default yes), matching `preloop agents onboard --approvals`. README documents the interactive path.
- **`ask_user` built-in tool**: Agents can ask the operator a question with multiple-choice `options` and/or a free-text answer and get the answer back, routed through the same approval workflow, notification, and audit pipeline as approvals. The question rides in the request's `tool_args` (surfaced as `is_question`/`question`/`question_options`/`allow_free_text`); the operator's reply is submitted via the existing decision endpoints, which now accept `selected_option`/`answer_text` (precedence `answer_text` > `selected_option` > `comment`). iOS and Android render options as buttons plus an answer field.
- **Deterministic default model pricing**: A vendored litellm price snapshot (`services/data/model_prices.json`, regenerated with `scripts/update_model_prices.py`) is loaded at startup so default per-model cost estimates are fixed per release instead of depending on the installed litellm version.
- **Multi-currency model price overrides**: Account model price overrides accept a `currency` and `fx_rate_to_usd`; non-USD overrides are converted to USD for all stored costs, preserving the original currency and unconverted prices (`original_currency`/`original_prices`) for display and audit.
- **Historical usage repricing**: A repricing service recomputes `ApiUsage.estimated_cost` from each row's stored token counts using the current price catalog and account overrides — filling rows recorded unpriced and applying a new/edited override retroactively (analytics-only; budget-spend buckets are not rewritten, and `subscription`-priced $0 rows are skipped).
- **Finer usage accounting**: `ApiUsage` gained `cache_read_tokens`, `cache_creation_tokens`, `reasoning_tokens`, `currency`, `cost_source`, `usage_source`, and `is_retry` columns for more accurate cost/token attribution, with streaming token-accuracy coverage.
- **Provider billing reconciliation (data model)**: `ProviderBillingConnection`/`ProviderBillingSnapshot` tables store a per-account link to a provider's billing/usage API and persist fetched actuals so estimated `ApiUsage` spend can be reconciled against what the provider actually billed. The tables ship in the OSS models package; the fetchers and endpoints live in the Enterprise billing plugin.
- **`preloop agents discover --json`**: The discover command's documented `--json` flag is now registered, emitting the discovered agents as JSON for scripting.

### Changed

- **Gateway summary opt-in for light cards**: Console Overview/Agents pass
  `include_breakdown=false` on `GET /api/v1/account/gateway-usage/summary` for
  faster first paint. The API default remains `true` so external consumers keep
  the historical full-breakdown response when the query param is omitted.
- **Default workflow owner resolution**: Seeding the account-default approval workflow prefers `Account.primary_user_id` over the oldest user in the account.
- **Live gateway validation throttling**: Upstream HTTP 429 during live validation is treated as proof the gateway credential and wiring work; onboarding no longer rolls back gateway config for throttled probes (hard `failed` still does).
- **CLI approvals list**: `preloop approvals list` table output now shows type, mode, default flag, approver summary, and timeout instead of the outdated tool-pattern / auto-approve / active columns.

### Fixed

- **Sync workers crash-looped when the dedicated flow pool was enabled**: the `tasks` JetStream stream uses WORKQUEUE retention, where consumer subject filters must not overlap — but the default worker subscribed to the `preloop.sync.tasks.*` wildcard while the new flow-execution pool filtered `execute_flow` / `resume_flow_execution`, so NATS rejected every flow-worker subscription (`filtered consumer not unique on workqueue stream`), the worker ended up with no subscriptions, and the container exited 0 into a restart loop (caught by the 0.11.0-rc.0 release smoke test). Worker pools now partition the stream: `preloop-sync worker --exclude-tasks <names>` enumerates the remaining subjects instead of using the wildcard (compose and the Helm default pool both set it), the worker refuses to start with zero subscriptions instead of exiting silently, and on upgrade it deletes the stale durable wildcard consumer that would otherwise keep blocking the filtered ones.
- **Agent Control WebSocket rejected durable credentials (endless 403 reconnect loop)**: managed-agent credentials are minted without a runtime-session binding, but both API-key auth layers hard-required a live bound session — every runtime plugin (OpenClaw, Hermes) enrolled after sessions became lazy was rejected on connect and the Console/mobile showed the agents offline forever. Runtime bearer auth now resolves — and reopens if ended — the agent's identity session (`allow_stale_runtime_session` on the shared API-key validator), so control connections survive re-onboarding, operator "end session", and session expiry.
- **Native-tool permission checks 500ed on the per-agent workflow pin**: the pin lookup bound the JSON path as `VARCHAR` (`json #>> character varying` has no operator), so `POST /api/v1/agents/permission-check` failed for every account and the client hook fail-closed denied all native tool calls. Rewritten with `json_extract_path_text` and covered by a real-database regression test (the previous tests mocked the DB and never executed the SQL).
- **`preloop agents onboard --yes` silently skipped native-tool approvals**: `-y` now accepts the approvals prompt's default (Yes) for supported agents (Claude Code, Codex CLI, Cursor) instead of onboarding without the hook.
- **Gateway cost summary on zero traffic**: Aggregations use `one_or_none()` with zero defaults so empty windows no longer 500 the cost summary / accounting health APIs.
- **Repricing commit batching**: Historical usage repricing commits in page-sized batches instead of once per row, avoiding partial multi-commit windows on crash.
- **Tracker credential backfill isolation**: Each tracker migrates in its own DB session so a single failure cannot poison later candidates.
- **Execution timeframe usage counts**: The fallback count path filters to `model_gateway` rows only.
- **Gemini gateway budget enforcement**: The Gemini-compatible gateway endpoints now inject the budget enforcer, so account/flow budget policies apply to Gemini traffic instead of being bypassable via that endpoint.
- **Streaming usage on client disconnect**: The Anthropic, chat-completions, and responses streaming paths now record a best-effort usage row when the client disconnects mid-stream (`GeneratorExit`), so already-consumed upstream tokens are still accounted and budgets don't drift.

### Security

- **Removed unauthenticated tracker debug endpoint**: `POST /api/v1/trackers/debug` echoed raw request bodies (including credentials) to the response and stdout.
- **Agent Control payloads sanitized**: Inbound agent event payloads are redacted and truncated before event-bus emit and activity persist.
- **OAuth refresh errors no longer store provider bodies**: `last_refresh_error` persists a status/code summary only; raw provider response bodies stay out of the DB.
- **Refresh tokens rejected as access tokens**: The refresh-token guard in the auth dependencies read a dict field that is never a dict (`decode_token` returns a model), so a 7-day refresh token was accepted anywhere an access token was expected. It now reads the flag correctly on both the REST and WebSocket/gateway paths.
- **MCP firewall & approval gate fail closed**: An exception during central policy evaluation or the approval check previously fell through to executing the tool. Both now block on error, matching the documented per-rule fail-closed posture.
- **Numeric access-rule bypass fixed**: A numeric rule such as `args.amount > 300` could be defeated by sending the value as a string; ordering comparisons now coerce numeric-looking operands.
- **Approval double-decide race & stale-approval expiry**: `approve`/`decline` now re-check, under the row lock, that a request is still pending and not past its deadline — preventing a resolved request from being flipped and an expired request from being approved via its token.
- **MCP-server OAuth authorize no longer exposes a reusable token**: The browser authorize redirect now uses a short-lived, server-scoped token minted by `POST /api/v1/mcp-servers/{id}/oauth/authorize-token` instead of the reusable access token in the URL (which could land in history, logs, and Referer).
- **Tracker credentials encrypted at rest**: Issue-tracker API keys and Jira webhook secrets are now stored via the Secret Service (`credentials_secret_id`/`webhook_secret_id` → `SecretReference`) instead of plaintext columns; a startup backfill migrates existing rows.
- **CLI credential files tightened to 0600**: Managed agent config files embedding the runtime bearer token, and the CLI token file, are no longer written world-readable.
- **Console XSS hardening**: Issue descriptions and the embedding-viewer tooltip (external tracker content) are now sanitized/escaped before rendering.
- **Removed a debug endpoint** that returned plaintext API keys for an arbitrary username with no scoping.

## [0.10.0] - 2026-07-10

### Overview

Version 0.10.0 evolves Preloop from an approval-and-gateway layer into a full **AI agent control plane**. It rolls up everything shipped in 0.10.0-rc.0 and 0.10.0-rc.1; the highlights across the release cycle:

- **Agent Control.** Talk to your live agents. A durable WebSocket control channel (`WS /api/v1/agents/control/ws`), operator command/prompt/voice endpoints, web console Talk composer with browser-native and server STT/TTS, and mobile/watch voice scaffolds turn managed agents into contactable, audited teammates. New standalone runtime plugins — `@preloop-ai/openclaw-plugin` (npm) and `preloop-hermes-plugin` (PyPI) — keep OpenClaw and Hermes connected from inside the agent process.
- **Native agent tool approvals.** Approval governance now reaches beyond MCP tools: `POST /api/v1/agents/permission-check` plus `preloop agents onboard --approvals` route Claude Code `Bash`/`Edit`, Codex CLI, and Cursor native tool calls to your phone, watch, or Slack before they run — with the requesting agent's identity on every approval card.
- **Cost analytics and session optimization.** A dedicated Console **Cost** area with Agents/Tools/Sessions/Users drill-downs and budget-health alerts in the open-source core; runtime session replay with a per-request timeline; and (Preloop Cloud / Preloop Enterprise) evidence-grounded session optimization with one-click applied actions, replay-measured savings, AI session titles, per-user budgets, and budget notification recipients.
- **A leaner context for every agent.** MCP tool output filters strip wasteful fields on the proxy hot path, gateway context optimization deduplicates repeated prompt prefixes and caps tool results before upstream dispatch, and per-tool usage stats expose which tool schemas are burning tokens.
- **Simpler ways in.** The landing page and README now offer two clear paths — Preloop Cloud or the self-hosted open-source stack — with a tabbed install widget, an installer that asks which instance to connect to (`PRELOOP_URL` supported throughout), a new self-hosting installation guide, and Antigravity and Devin onboarding adapters alongside the existing agents.
- **A release you can trust.** The OSS install crash loop from issue #53 (a NATS healthcheck that lame-ducked the server every 10 seconds) is fixed, and every release is now gated by an automated smoke test that boots the release compose stack, signs up a user, and fails on any restart loop before anything is published. CLI update notifications also work for the first time.

**Upgrade notes:** run `alembic upgrade head` (the approval-workflow name-uniqueness migration deduplicates existing rows automatically); review the raised `DATABASE_POOL_SIZE`/`DATABASE_MAX_OVERFLOW` defaults (20/40) if your PostgreSQL `max_connections` is small; `MODEL_GATEWAY_MAX_PREVIEW_CHARS` now defaults to 32768; and `PRELOOP_SERVICE_ROLE` (`all`/`api`/`gateway`) lets you split API and gateway deployments — the default remains combined.

## [0.10.0-rc.1] - 2026-07-10

### Added

- **CLI installer instance selection**: `install-cli.sh` now explains that the CLI connects to a control plane (Preloop Cloud at `https://preloop.ai` by default, or a self-hosted instance), honors a pre-set `PRELOOP_URL`, and interactively prompts for the instance URL before sign-in. Login, signup, and agent onboarding launched by the installer all target the chosen instance.
- **Landing page self-host path**: The hero install widget is now tabbed — "Install the CLI" (default) and "Install the full stack" (the OSS Docker Compose one-liner) — with the caption swapping per tab. The get-started card stays focused on agent onboarding: CLI-only, and on non-preloop.ai hosts its snippet targets the current instance via `PRELOOP_URL=<origin>`.
- **CLI identification**: The CLI now sends `User-Agent: preloop-cli/<version> (<os>; <arch>)` and `X-Client-Version` on requests to Preloop servers via `SetClientIdentityHeaders`, covering the API client, MCP client, auth token exchange, agent permission-check hooks, and version-check pings. Enables adoption metrics and better support diagnostics; no data beyond version and platform is transmitted.
- **CLI activity analytics**: When `INSTALLER_AUDIT_ACCOUNT_ID` is configured (hosted instances), daily CLI update-check pings are recorded as `cli_activity` audit events, and `GET /api/v1/admin/installer-downloads/stats` now reports active CLIs (24h/window), total check-ins, last-seen, and top CLI versions.
- **Updated default RBAC roles**: System roles now cover agents, runtime sessions, policies, approvals, cost/budgets, AI models, and audit. `GET /api/v1/auth/users/me` returns the caller's permission allow-list when RBAC is active; the Console hides inaccessible nav items and shows a permission-denied empty state instead of blank pages.
- **Free-tier hosted-model cap**: Card-free accounts with no subscription are subject to a calendar-month hard cap on built-in hosted model spend (`BILLING_FREE_HOSTED_MODEL_HARD_CAP_USD`, default `$1`) when entitlement enforcement is on, with a clear BYOK/upgrade denial message.
- **In-product upgrade UX**: Console `fetchWithAuth` surfaces HTTP 402 `upgrade_required` responses in an upgrade modal (feature-aware copy + checkout CTA). Shared `startCheckout` helper and Sessions title upsell hint support the card-free signup → upgrade-in-product flow when billing is present.

### Changed

- **Edition naming**: User-facing copy now consistently uses **Preloop** (the open-source edition), **Preloop Cloud** (the hosted service at preloop.ai; Teams is a Preloop Cloud plan), and **Preloop Enterprise** (the self-hosted commercial edition) across the README, architecture docs, landing/pricing pages, and the documentation guide.
- **README quickstart**: Restructured around the two-part model — control plane (Preloop Cloud or self-hosted OSS) plus CLI — with explicit Cloud and self-host paths, including how to point the CLI at your own instance (`preloop login --url` / `PRELOOP_URL`).
- **OSS installer next steps**: `install-oss.sh` now prints how to create the first user, install the CLI, connect it to the local instance, and onboard agents.
- **RBAC permission vocabulary**: Endpoint checks and seeded roles now share one `verb_resource` vocabulary (`create_projects`, `view_cost`, `decide_approvals`, …). Re-run `python scripts/init_system_roles.py` after upgrade to reconcile existing deployments.
- **Console Audit navigation**: Sessions and Approvals nest under an **Audit** sidebar section (All events / Sessions / Approvals). Cost stays top-level. The Audit section appears when any child is allowed for the user/edition; All events still requires the `audit_logs` feature flag.
- **Card-free signup path**: Landing, pricing, register, and header CTAs no longer force Stripe checkout at signup. New accounts register first; Teams upgrades happen in-product (logged-in pricing checkout or the upgrade modal).

### Fixed

- **CLI update notifications**: `GET /api/v1/version` now returns the `latest_version`/`min_version`/`download_url` keys the CLI update check parses. The CLI compared against a field the server never sent, so update prompts never fired.
- **Replay usage isolation**: `get_gateway_usage_for_execution` now applies the same `exclude_replay_usage_condition()` filter as the other gateway aggregations.
- **Permission decorator fail-closed**: `require_permission` now returns HTTP 500 when `current_user` or `db` is missing from the endpoint kwargs instead of silently skipping the RBAC check.
- **Code quality hardening**: Removed the unused `push_notifications.py` stub; tightened `retry_async` exhaustion handling; cleaned schema `__all__` / `model_config` merge; and fixed related dead assigns and string-concat style in policy generation and tracker helpers.

## [0.10.0-rc.0] - 2026-07-07

### Added

- **Native agent tool approvals**: Onboarded agents can route built-in tool calls (e.g. Claude Code `Bash`/`Edit`, Codex CLI, Cursor) through Preloop human approvals via `POST /api/v1/agents/permission-check`, authenticated with the agent's managed-runtime credential. The endpoint reuses the existing approval pipeline (create → notify mobile/watch → wait → allow/deny) and records `managed_agent_id`, `runtime_session_id`, and `managed_agent_name` on each request so operator surfaces show which agent is asking.
- **CLI approval hooks**: `preloop agents onboard --approvals` installs local permission hooks for Claude Code (PreToolUse), Codex CLI, and Cursor that call the permission-check API before mutating native tools. OpenClaw and Hermes runtime plugins ship matching tool-approval adapters with tests.
- **MCP tool output filters**: Account-scoped rules strip named top-level fields from MCP tool JSON results on the proxy hot path before they reach the calling agent, trimming wasted context tokens. Core model, CRUD, and proxy application live in OSS; Enterprise billing exposes `/api/v1/billing/cost/output-filters` CRUD and the Console tools editor includes a filter dialog.
- **Budget notification recipients**: Budget policies accept optional `notification_user_ids` and `notification_team_ids` so threshold alerts can target specific users and teams instead of only the policy owner.
- **Tool usage stats**: `GET /api/v1/tools/stats` aggregates per-tool call counts, schema-injection token estimates, and spend attribution across managed agents for the Console tools view.
- **Agent Control backend**: WebSocket control channel (`WS /api/v1/agents/control/ws`), operator command/prompt/voice-transcript endpoints, runtime adapter scaffolding, and mobile/web Talk UI foundations for audited operator messages to managed agents.
- **Audio endpoints**: `POST /api/v1/audio/transcriptions` (speech-to-text) and `POST /api/v1/audio/speech` (text-to-speech) backed by speech-capable `AIModel` rows, used as the server fallback for web/mobile Talk surfaces.
- **Manual tracker sync**: `POST /api/v1/trackers/{tracker_id}/sync` triggers an on-demand tracker scan without waiting for the scheduler.
- **Cost analytics (OSS)**: Dedicated Console Cost view with spend overview, grouped usage drill-downs, and budget-health alerts backed by `/api/v1/cost/*` endpoints. Enterprise billing plugin owns budget policy CRUD and enforcement.
- **Runtime session observer**: Shared session replay, timeline/chat views, gateway event inspection, and opt-in session summaries in the Console.
- **Runtime session request timeline**: `GET /account/runtime-sessions/{id}/requests` reads per-request `ApiUsage` rows (tokens, cost, status, tool schema attribution) to power a unified replay with turn/delta deduplication, sortable chat, cache-token visibility, and inline operator activity turns.
- **Runtime session titles**: Session list scheduling for background LLM-generated titles via the plugin service registry, with Enterprise billing providing the generator and a configurable daily spend cap (`billing_session_title_daily_cap_usd`).
- **Session optimization actions**: Core schemas, CRUD, and gateway averages for applied optimization actions; Enterprise billing exposes apply/list endpoints for scope_tools, set_budget, enable_compression, and cap_tool_results with measured outcomes.
- **Gateway context optimization**: Subject-scoped dedupe, noise stripping, and tool-result caps on the gateway hot path before upstream dispatch.
- **Standalone Agent Control runtime plugins**: New open-source runtime plugins under `runtime-plugins/` — `@preloop-ai/openclaw-plugin` (npm) and `preloop-hermes-plugin` (PyPI) — keep the Agent Control WebSocket connected from inside the agent process, advertise capabilities and presence, deliver operator/voice messages into the active session, and gate native tool calls through Preloop approvals (fail-closed by default). `PUBLISHING.md` documents lockstep versioning and marketplace submission.
- **CLI runtime installers**: `preloop agents install-plugin <agent>` delegates runtime-plugin installation to the agent's own marketplace installer, and `preloop agents install-runtime <hermes|openclaw>` installs the runtime locally and onboards it through Preloop in one step. `preloop agents validate --live` runs a live gateway probe on demand.
- **CLI agent adapters**: Antigravity (Google Gemini MCP tree) and Devin (Cognition) MCP-only onboarding adapters alongside existing managed runtimes.
- **Release compose migrate job**: `docker-compose.release.yaml` now runs schema migrations in a dedicated one-shot `migrate` service (`init_db.py --force`) that app services wait on, instead of migrating inside the API container's start script.
- **Helm health monitor**: Optional in-cluster health-monitor deployment (`healthMonitor.*`, enabled by default) polls `/api/v1/health` and logs alert lines after consecutive failures. New `computeBackend` values (KubeVirt/AWS/GCP) and CNPG lifecycle tuning values were added alongside it.
- **Automated release verification**: `scripts/release_smoke_test.sh` boots the release compose file with the tagged images, checks API/gateway/console health, exercises first-user sign-up and login, and fails on any container restart loop. The release workflow runs it as a `verify-oss-install` gate before the GitHub release and PyPI publish are created.
- **Deploy wizard**: Expanded console deploy wizard for guided agent onboarding.
- **Test coverage expansion**: Substantial backend endpoint, service, integration (gateway e2e), and frontend component test suites across the OSS core and Enterprise plugins.

### Changed

- **Approvals and session console UX**: Approval lists and detail views show managed-agent identity; budget policy editor adds recipient pickers and richer health cards; session replay, optimization panel, and agent detail views were refreshed for clearer operator workflows.
- **Enterprise cost features**: Moved model price override CRUD and runtime-session optimization recommendations into the billing plugin (`/api/v1/billing/cost/*`) with `model_price_overrides` and `session_optimization` feature flags gating the shared frontend.
- **Service role deployment modes**: API startup and route registration now respect `PRELOOP_SERVICE_ROLE` so API-only, gateway-only, and combined trial deployments can run the right surface area.
- **Release changelog generation**: AI-authored changelog drafting is now opt-in via `--generate-changelog-ai`, keeping deterministic release prep from depending on a local AI CLI.
- **README**: Restructured hero, quick-start, and capability messaging for the control-plane positioning.
- **Gateway runtime attribution**: Plugin-agent gateway traffic now attributes to the principal's latest open per-run session when available, improving per-run ROI for Hermes, OpenClaw, and similar runtimes without changing custom-agent `X-Preloop-Session-Id` behavior.
- **Gateway usage accounting**: Preserves prompt-cache token breakdown (`cached_tokens`, `cache_read_input_tokens`) for cache-aware cost estimates and session replay UI.
- **Database pool defaults**: `DATABASE_POOL_SIZE` default raised from 5 to 20 and `DATABASE_MAX_OVERFLOW` from 10 to 40 (up to 60 connections per worker). Deployments with many workers or a small PostgreSQL `max_connections` should set these explicitly.
- **Gateway preview size**: `MODEL_GATEWAY_MAX_PREVIEW_CHARS` default raised from 4096 to 32768 so session replay and optimization analysis see fuller conversation previews (increases stored payload size).
- **Approval workflow names**: Workflow names are now unique per account. The migration renames pre-existing duplicates in place (the oldest keeps its name; later duplicates get a short-id suffix) before creating the unique index.

### Fixed

- **OSS install NATS crash loop** ([#53](https://github.com/preloop/preloop/issues/53)): The release compose file's NATS healthcheck ran `nats-server --signal ldm`, which sent the lame-duck shutdown signal (SIGUSR2) to the server on every probe — gracefully stopping NATS every 10 seconds and leaving the stack in a restart loop after `curl … /install/oss | sh`. The healthcheck now probes the NATS monitoring endpoint (`wget --spider http://127.0.0.1:8222/healthz`) instead of signalling the process.
- **CNPG redeploy hangs**: Helm upgrades of multi-instance CloudNativePG clusters now use `switchover` for primary updates and only enable the PodDisruptionBudget when `instances > 1`, fixing redeploys that hung waiting on a primary restart.
- **OSS install failure reporting**: The OSS installer now exits non-zero when `docker compose up` fails and prints the log-inspection command instead of reporting success.
- **Gateway governance lookup performance**: Short-lived negative cache skips per-request account DB fetches when subject governance is unconfigured.

## [0.9.3] - 2026-05-19

### Fixed

- **List users N+1 query performance**: The `GET /api/v1/users` endpoint now batch-loads roles, team memberships, team roles, and teams with 4 strategic queries instead of per-user queries, eliminating N+1 overhead for user listings with team memberships.
- **Managed agent onboarding compatibility**: OpenClaw onboarding now writes and validates the `streamable-http` MCP transport expected by newer OpenClaw releases, and Hermes onboarding resolves provider-specific API key environment variables such as `DEEPSEEK_API_KEY` before falling back to generic OpenAI-style keys.
- **Gateway tool calls for agent execution**: OpenAI-compatible chat completions now preserve tool calls in both streaming and non-streaming responses, preventing OpenCode and other tool-capable clients from receiving `finish_reason="tool_calls"` without the tool-call payload they need to continue.
- **Gateway runtime-session stability**: Runtime-session activity touches are throttled and handled best-effort after gateway usage is recorded, reducing hot-row contention and preventing statement timeouts from failing otherwise-successful model requests.
- **Database connection cleanup**: Restored SQLAlchemy's default pool reset behavior so timed-out or rolled-back transactions are cleaned up before pooled PostgreSQL connections are reused.
- **OpenCode execution logging**: Fixed the generated OpenCode JSON log filter so newline splitting is escaped correctly inside the generated JavaScript, keeping the filter alive long enough for success sentinel detection.
- **Dynamic MCP tool wrappers**: Generated FastMCP wrapper signatures now keep required parameters before optional parameters, avoiding invalid Python function signatures for tools with mixed required and optional inputs.
- **GitLab review environments**: Review-app hostnames and Helm release names now use `CI_COMMIT_REF_SLUG`, keeping branch names with slashes or other DNS-unsafe characters from producing invalid deployment names.

### Security

- **SECRET_KEY hardening in tokens module**: `utils/tokens.py` now imports `SECRET_KEY` from `preloop.config.settings` (matching the `jwt.py` pattern) instead of using `os.getenv` with a hardcoded development fallback. This ensures email verification, password reset, and onboarding tokens are signed with a properly validated secret key in production.
- **Production SECRET_KEY validation**: Production configuration now rejects the development fallback secret instead of silently accepting it, and JWT helper paths use explicit `JWTError` handling with logging instead of broad exception swallowing.

## [0.9.2] - 2026-05-09

### Changed

- **Flow execution listings**: Lightened flow execution list responses to improve performance and reduce payload size for console views that do not need full execution detail.
- **Codex gateway routing**: Routed Codex-backed gateway traffic through the service endpoint so managed Codex model calls use the intended backend path.

### Fixed

- **MCP and gateway hardening**: Hardened production MCP and model-gateway paths for more reliable request handling and safer control-plane behavior.
- **Budget enforcement reliability**: Addressed security and reliability issues in budget CRUD paths, improving guardrail consistency for gateway budget checks.
- **Realtime events**: Improved NATS realtime event handling reliability.
- **CLI parsing**: Fixed CLI parsing edge cases that could break automation or managed-agent workflows.
- **OpenCode onboarding**: Captured OpenCode JSON output for sentinel detection so onboarding and validation can identify completion markers reliably.
- **OpenCode execution logs**: Switched the OpenCode JSON output filter to Node.js so flow execution sentinel detection works in the OpenCode container image without requiring Python.
- **Database pool cleanup**: Hardened SQLAlchemy pool/session cleanup so closed SSL sockets are invalidated quietly instead of surfacing noisy pool reset errors.
- **MCP client cleanup**: Reworked external MCP client pooling to avoid keeping streamable HTTP async generators open across request tasks, preventing cancel-scope cleanup errors.
- **Test stability**: Switched date-sensitive tests to relative dates to avoid failures when 30-day window filters move over time.

## [0.9.1] - 2026-04-30

### Fixed
- **CLI**: Registered the missing `--no-onboard-prompt` flag in `preloop agents discover` to prevent `unknown flag` errors during headless installation.
- **Testing**: Fixed a session filtering issue in `test_account_agent_detail_endpoint_returns_one_agent` that caused test assertions to fail.
- **Frontend**: Added explicit `uuid` dependency to resolve `package.json` resolutions.
## [0.9.0] - 2026-04-30

### Overview
Version 0.9.0 introduces major enhancements to Preloop's agent control plane. The most significant additions include the **AI model gateway**, robust support for **onboarding existing agents** (such as OpenClaw, Codex CLI, Hermes, OpenCode, Claude Code, and Gemini CLI), and comprehensive **cost tracking and budget governance**. These features allow organizations to securely route, monitor, and enforce policies on their AI traffic across diverse agent ecosystems.

### Added (since 0.9.0-rc.3)
- **API Key Details View**: Added a dedicated API key details view to manage subject-scoped governance.
- **Budget Controls**: Modernized the budget governance dashboard with clear spend alignment metrics.

### Fixed (since 0.9.0-rc.3)
- **Auth Session Refactoring**: Refactored authentication API routes to strictly use FastAPI dependency injection (`Depends(get_db_session)`), replacing legacy session iterators.
- **DynamicFastMCP Security**: Resolved an authorization bypass vulnerability for proxied MCP tool calls by enforcing strict internal re-entry checks.
- **Dashboard Stability**: Fixed budget spend alignment, gateway usage principal filtering, and applied proper time window filters to active agents and sessions.
- **UI Polish**: Persisted dismissed flow executions and prevented the budget dialog from unexpectedly closing upon selection changes.
- **Test Suite**: Resolved failing frontend UI tests for DashboardView, ToolsView, and RuntimeSessionsView.

## [0.9.0-rc.3] - 2026-04-21

### Changed

- **CLI Live Validation Now Runs By Default**: `preloop agents onboard` (and the discover-driven onboarding prompt) now runs an end-to-end live validation through the Preloop model gateway whenever the agent kind supports it (currently OpenClaw and Codex CLI). Previously `--live-validate` was opt-in *and* the interactive "Run live validation now?" prompt was suppressed for `--yes` / `--force` / `--all` / `PRELOOP_CONFIRM` and the entire discover-driven path, so any scripted re-onboard left supported agents stuck on **"Live check not run"** in the UI. The flag now defaults to `true` and a new `--skip-live-validate` flag (also exposed on `agents discover`) is the supported opt-out for automation that should never make a real model gateway request after onboarding. Live validation no longer depends on `SkipConfirmation` / `AutoApprove`, so `--all` batch onboards and discover-driven onboards now validate by default.
- **CLI Live Validation Now Covers Every Managed Agent Kind**: `preloop agents onboard` now ships an end-to-end live-validate probe for every kind of agent the CLI knows how to onboard, not just OpenClaw and Codex CLI. New runners send a real, account-bound model request through the Preloop gateway for **Hermes** (chat-completions via `/openai/v1/chat/completions`), **OpenCode** (chat-completions, with `preloop/<alias>` normalised back to the canonical model alias), **Claude Code** (Anthropic `/anthropic/v1/messages`, with token + alias resolved from either the new `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` env vars or the legacy `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_DEFAULT_*_MODEL` variants), and **Gemini CLI** (`/gemini/v1beta/models/<model>:generateContent`, with the qualified `google/<name>` alias recorded for the audit trail). This eliminates the misleading **"Live check not run / unsupported"** badges users were seeing for every kind except OpenClaw and Codex CLI after onboarding. The shared `runGatewayLiveValidation` helper unifies the per-agent boilerplate (base URL resolution, agent detail fetch, validation-token probe, gateway-usage search wait, canonical result map) so future kinds only need to declare their gateway endpoint and payload shape.
- **CLI Live Validation Runs in Parallel After Onboarding**: When onboarding multiple agents in one invocation (`preloop agents onboard --all`, the implicit "no args, multiple candidates" path, and the discover-driven onboarding prompt), live validation is now deferred to a single post-onboarding parallel phase instead of running serially after each agent. Onboarding itself stays strictly sequential — so state-mutating steps (config rewrites, backups, durable-credential creation) remain deterministic — but the live-validate wall clock collapses from O(N) to roughly the slowest single check. A clear summary is printed per agent (`✓ Codex CLI: live validation passed (450ms)` / `✗ OpenCode: live validation failed (210ms): ...`) and each outcome is persisted back to the corresponding managed enrollment so the UI surfaces the new status immediately. The interim "Live validation: pending" line in the per-agent onboard output communicates that the real check is in flight.

### Added

- **Audit Timeline — Full Approval Story**: The audit view now tells the complete lifecycle story for every approval-gated tool call in a single, expandable group. New audit event types `approval_notification_sent` and `approval_tool_executed` are persisted to `audit_log` and chained into the timeline via `correlation_id` / `approval_id`. Each notification fan-out records the channel (email, mobile push, webhook), the resolved recipient `user_ids`, and per-channel `sent_count` / `failed_count` / `skipped_count` (with a dedicated `no_devices` status when there are no registered mobile devices). Human approve/decline events now record the approver and reason. Post-approval tool executions in the async-poll path log status, duration, a result preview, and any error — and the group's overall outcome is promoted to that final execution status (e.g. `executed` / `failed`) so the timeline row reflects what actually happened, not just that an approval was requested.
- **Audit Timeline — Live Updates**: The audit page now subscribes to the per-account websocket topic and refreshes in real time as new entries land. A small "LIVE" pill in the header pulses on every incoming `audit_event` so users see immediate feedback as approvers act on requests, notifications fan out across channels, and tools execute. Refreshes are debounced (400 ms) so a burst of related events (notification fan-out + decision + execution) results in a single refetch, and live-refresh is suppressed when the user has paged back through history so the view doesn't shift under them.

- **Marketing & Positioning**: Positioned Preloop as an open-source AI agent control plane, added a native installation `curl | sh` command widget in the hero section, and added JSON-LD schemas for improved SEO.
- **CLI Onboarding Automation**: Enhanced `preloop agents onboard` and `preloop agents offboard` to support robust non-interactive automation via new `-y`, `--yes`, `-f`, `--force` flags and the `PRELOOP_CONFIRM` environment variable.
- **CLI Batch Operations**: The agent `onboard` and `offboard` CLI commands now support discovering and iterating through all matching agents when no arguments are provided. A new `--all` flag allows grouped confirmation prompts.

### Fixed

- **Claude Code OAuth Onboarding Regression**: Claude Code installs authenticated with Claude Code's native OAuth/subscription credentials are no longer rewritten to send model traffic through Preloop's generic Anthropic Messages gateway. Anthropic's public `/v1/messages` API explicitly rejects those OAuth tokens for third-party gateway use (`OAuth authentication is currently not supported` when sent as a bearer token, and `invalid x-api-key` when sent as an API key), so treating them like normal `sk-ant-api...` API keys broke Claude Code after onboarding with HTTP 401s. The CLI now only enables Claude Code model-gateway routing when it finds a real Anthropic API key (`sk-ant-api...`). OAuth-backed Claude Code still onboards managed MCP/tool traffic through Preloop, while model traffic remains on Claude Code's native direct OAuth path.
- **CLI Live Validation Prerequisite Skips**: Live validation now skips cleanly when an agent's managed model gateway is not configured (missing provider/base URL/token prerequisites) instead of attempting a request that can only fail. The parallel summary prints the skip reason, and the persisted validation result records `live_validation_status="not_run"` plus `live_validation_skip_reason`.
- **Hermes Onboarding Classification for Older CLI Records**: The account agents backend now recognizes Hermes' managed gateway config shape directly (`model.provider=custom`, `model.base_url` containing `/openai/v1`, durable key, and model alias) instead of relying solely on newer CLI validation flags. This keeps Hermes agents written by older CLI binaries from appearing incomplete when their local config and live check are already valid.
- **Authentication Flows**: Gracefully handle missing MCP servers during agent onboarding to prevent API 400 errors, and resolved an OAuth consent 401 and redirect flow loop.
- **Install Scripts**: Updated the CLI installation scripts with default `Y/n` prompt behavior and ensured proper standard input redirection for deeply nested interactive scripts.
- **OpenAI Gateway / Codex OAuth Models**: Routed Codex OAuth-backed models through the Codex backend on `/openai/v1/chat/completions` (both streaming and non-streaming). Previously the chat-completions paths only worked for the (non-streaming) responses-API endpoint, and any OpenAI-compatible client (e.g. Hermes via `provider: custom`) bound to a Codex OAuth model failed with `HTTP 400: Model credentials are not configured`. Codex Responses-API payloads are now transcoded into chat-completion shape (and faked-streamed as SSE chunks) so external clients receive the assistant text and tool calls correctly.
- **Proxied MCP Tool Access**: Fixed `Access denied: Tool '<name>' is not available` for every proxied MCP tool that has an access rule (e.g. `require_approval`). After the first call resolved the user-facing tool name and policy, FastMCP's dispatcher re-entered our `call_tool` override with the internal `account_<id>_<tool>` name, which `list_tools` strips out of the user-visible catalog. The override now short-circuits straight to the FastMCP base implementation on internal-name re-entry, so approvals and policy checks fire correctly and the tool actually runs against the upstream MCP server.
- **Hermes Onboarding Status**: Hermes agents that finish CLI onboarding successfully are now reported as `fully_onboarded` instead of `mcp_proxy_only`. The backend's onboarding flag derivation only matched the bespoke nested config shapes used by Codex, OpenCode, Claude, and Gemini; Hermes' `model.{provider,base_url,api_key,default}` layout slipped through. The detector now trusts the canonical `gateway_provider_ok` + `gateway_base_url_ok` validation flags emitted by every CLI adapter, so future agents are recognised automatically.
- **Live Validation Status Wording**: Replaced the misleading "Live check pending" badge for agents that were never run with `--live-validate` (a manually-triggered, opt-in step) with a neutral "Live check not run" indicator on both the agents list and the agent detail view.
- **Silent Approval Bypass**: Fixed a critical issue where a `require_approval` access rule with no `approval_workflow_id` (and no workflow on the tool config) caused the policy evaluator to return `(require_approval, None)` and the dynamic MCP wrapper to silently auto-approve the call. Calls that the user explicitly gated on approval would run without a workflow being created, no approval audit event, and no UI prompt — and the agent would then claim success even though no human ever approved. The policy evaluator now falls back to the account's default approval workflow whenever a `require_approval` rule does not pin a specific workflow, and the FastMCP override fails closed if no workflow can be resolved at all instead of silently allowing the tool through.
- **Default Approval Workflow Initialization**: Fixed two related bugs that left newly-created accounts with an unusable default workflow. (1) The seeded `Default Approval Workflow` was created with `approval_type="manual"` — a legacy synonym the dialog dropdown can no longer render — so the *Type* field appeared blank when the account owner opened the workflow editor. The seed now uses the canonical `"standard"` value, matching the dropdown's "Standard Human Approval" option. (2) When `complete_new_account_setup` was invoked without an explicit `user_id`, the default workflow was created with no approvers, making any default-routed approval request impossible to act on (the agent would receive `"Tool requires approval but no approval workflow is configured"` instead of triggering the human-approval flow). The service now falls back to looking up the account's first user (the owner) and seeds them as the default approver. A boot-time repair pass walks accounts whose existing default workflow still carries the legacy `manual` type and/or empty approver list, and heals them in place — so already-deployed accounts (e.g. `rearclaw` on staging) recover automatically on the next backend restart.
- **Approval Workflow Approver Selection**: Fixed a UI bug in `approval-workflow-dialog.ts` where clicking a user (or team) in the *Approvers* multiselect appeared to do nothing. The Shoelace `<sl-select>` `.value` was bound to bare UUIDs while its `<sl-option>` values were prefixed with `user:` / `team:`, so the controlled value never matched any option after a round-trip and selections never stuck. The dialog now renders the controlled value with the same prefixed form the options use, restoring multi-approver editing.
- **CLI Claude Code Live Validation — Opus / Sonnet / Haiku Families**: Fixed `preloop agents onboard` (and `preloop agents validate --live`) failing for every Claude Code agent bound to a model in the `claude-opus`, `claude-sonnet`, or `claude-haiku` family with HTTP 404 `{"type":"error","error":{"type":"not_found_error","message":"Requested model not found"}}` from the Preloop Anthropic gateway. `applyClaudeManagedGateway` writes the LITERAL Claude Code selection key (e.g. the bare string `"opus"`) into both `env.ANTHROPIC_MODEL` and the root `model` field whenever the model maps onto one of those three families — Claude Code's CLI then resolves that selection key through `ANTHROPIC_DEFAULT_OPUS_MODEL` / `_SONNET_MODEL` / `_HAIKU_MODEL` (the real gateway alias). The live-validate builder used to read `ANTHROPIC_MODEL` first, which sent the gateway the literal `"opus"` — a value that is correctly absent from the account's model registry, so `_resolve_requested_model` returned the catch-all 404. The builder now reads from `ANTHROPIC_CUSTOM_MODEL_OPTION` first (always populated unconditionally with the real alias by the apply path), falling back through `ANTHROPIC_DEFAULT_OPUS_MODEL` → `_SONNET_MODEL` → `_HAIKU_MODEL` → `ANTHROPIC_MODEL` → root `model` as defensive fallbacks for older / hand-edited configs. The optional `preloop/` provider prefix is also stripped on the way out — without it the gateway resolver's `alias.endswith("/" + requested)` rule never matches when the account stored the bare `anthropic/<model>` form, producing the same 404 from a different code path. Three new regression tests pin this down: `TestBuildClaudeCodeLiveValidationSpec_OpusFamily_PrefersCustomModelOptionOverSelectionKey` faithfully mimics the env block the apply path emits and asserts the builder picks the real alias instead of the selection key, `TestBuildClaudeCodeLiveValidationSpec_StripsPreloopPrefix` covers the prefix strip, and the existing `_ReadsTokenAndModelFromEnv` / `_FallsBackToAuthTokenAndPinnedModel` tests were updated to use the new canonical `ANTHROPIC_CUSTOM_MODEL_OPTION` field and to assert the normalised (prefix-stripped) alias shape.
- **CLI Live Validation for Hermes & Claude Code**: Fixed the two remaining live-validation regressions exposed once `preloop agents onboard` started exercising every kind end-to-end. (1) **Hermes** consistently failed with HTTP 400 `{"detail":"Unsupported parameter: temperature"}` because Hermes is bound to the Codex OAuth model `openai/gpt-5.4`, and the Preloop gateway routes Codex-backed chat-completions through the upstream Codex Responses backend — which rejects `temperature` / `max_tokens` / `max_output_tokens` outright (the same family of "Unsupported parameter" 400s already documented for `max_output_tokens` on the Responses path). The shared `buildChatCompletionsLiveValidationPayload` helper now sends only the canonical `model` + `messages` fields so the same probe works against both vanilla OpenAI-compatible upstreams (Google Gemini, ZAI, etc.) and the more restrictive Codex Responses backend without a per-model branch. (2) **Claude Code** consistently failed with HTTP 400 `{"type":"error","error":{"type":"invalid_request_error","message":"Missing anthropic-version header"}}` because the Preloop Anthropic gateway endpoint validates the upstream contract and *requires* an `anthropic-version` header on every request — but the CLI's `api.Client` had no way to attach extra headers, so every Claude Code probe fell out the bottom and timed out waiting for the gateway-usage search to index the validation token. A new `Client.PostWithHeaders` entry point now allows per-call extra headers (with the standard `Authorization` / `Content-Type` / `Accept` set always winning on conflict so callers cannot accidentally clobber auth or content negotiation), the `gatewayLiveValidationSpec` carries optional `Headers`, and the Claude Code builder pins `anthropic-version: 2023-06-01` (the long-standing GA value Anthropic recommends as the default for new integrations). New unit tests cover both regressions: `TestBuildChatCompletionsLiveValidationPayload_OmitsCodexIncompatibleFields` asserts the chat-completions probe never carries Codex-incompatible knobs, and `TestBuildClaudeCodeLiveValidationSpec_SetsAnthropicVersionHeader` plus `TestPostWithHeaders_AppliesExtraHeaders` / `TestPostWithHeaders_StandardHeadersWinOverExtras` cover the header plumbing end-to-end.
- **CLI Codex Live Validation Payload**: Fixed `preloop agents onboard` (and `preloop agents validate --live`) for managed Codex CLI agents failing with two consecutive HTTP 400s from the upstream Codex Responses backend: first `{"detail":"Instructions are required"}`, then (after the first fix landed) `{"detail":"Unsupported parameter: max_output_tokens"}` — both followed by `timed out waiting for gateway usage search to index validation token …`. The CLI was POSTing the Responses-API short-form `{"input": "...string..."}` body to `/openai/v1/responses`, which the Preloop gateway forwards almost verbatim to the upstream Codex Responses backend — and Codex (unlike vanilla OpenAI) strictly requires a non-empty `instructions` string, `store: false`, and `input` as an array of Responses-API items with `input_text` content, while *additionally* rejecting `max_output_tokens` outright (it is a valid OpenAI Responses-API field but Codex' chatgpt.com backend refuses it). The CLI now builds the validation payload in the shape Codex accepts (extracted into `buildCodexLiveValidationPayload` and covered by regression tests asserting both the required-field shape *and* the absence of `max_output_tokens` / `max_completion_tokens`), so live validation succeeds end-to-end against any Preloop-managed Codex CLI bound to a Codex OAuth model. Live-validate failures during `preloop agents onboard` are also no longer fatal — the failure is logged, surfaced in the UI as `Live check failed`, and the CLI continues onboarding subsequent agents (so a single Codex live-validate timeout no longer aborts the rest of `--all`); use `preloop agents validate <agent> --live` for the dedicated "exit non-zero on validation failure" semantics.
- **Codex Chat-Completions for Hermes**: Fixed Codex OAuth-backed models (e.g. `openai/gpt-5.4`) returning `HTTP 400: Instructions are required`, `HTTP 400: Store must be set to false`, `HTTP 400: Stream must be set to true`, `HTTP 400: Missing required parameter: 'tools[0].name'`, unknown-model errors, and *empty assistant turns on tool-call requests* when accessed via `/openai/v1/chat/completions` (the path Hermes uses). The Codex Responses backend now rejects every non-streaming request, rejects requests without an `instructions` field or with `store != false`, expects the upstream provider model identifier (e.g. `gpt-5-codex`) rather than the gateway alias, requires assistant text to use the `output_text` content type, expects tool calls/results to be encoded as `function_call` / `function_call_output` items rather than `role: assistant` / `role: tool` messages, and expects tool definitions in the flattened Responses-API shape (`{"type": "function", "name": ..., "parameters": ...}`) rather than the chat-completions nested shape (`{"type": "function", "function": {"name": ..., "parameters": ...}}`). The chat-to-Codex translator now lifts `system` messages into `instructions` (with a sane default), pins `store: false`, substitutes the bound model identifier, encodes the multi-turn tool history in the Responses-API shape, and flattens both `tools` entries and forced `tool_choice` selectors. The upstream call always sets `stream: true` and the gateway aggregates the resulting SSE event stream into a single response object by *incrementally rebuilding* it from `response.output_item.added/done`, `response.output_text.delta/done` and `response.function_call_arguments.delta/done` events (mirroring the official Codex CLI strategy) instead of trusting the giant `response.completed` event — which fixes silent empty responses on tool-only turns (e.g. Hermes asking ``pay $6 to Joe``) and also tolerates truncated `response.completed` events (cf. vercel/ai#14473). `response.failed` events now surface as `ModelGatewayAPIError` instead of being silently swallowed.

## [0.9.0-rc.2] - 2026-04-14

### Fixed

- **CLI OAuth Flow**: Fixed missing authorization header in the SPA consent submit request preventing a 401 Unauthorized error during authorization, and corrected the post-login routing context so the consent flow resumes automatically after a required sign-in or sign-up.
- **OAuth consent tests**: Updated test suite to validate the new SPA 307 temporary redirect flow for `GET /mcp/authorize/consent`, replacing obsolete Jinja template assertions.

## [0.9.0-rc.1] - 2026-04-14

### Added

- **Landing page UX**: Deployed SVG animation scrolltraps with a 20-second auto-scroll cycle to improve visual engagement.
- **CLI Onboarding**: Unified the terminal installation copy (`curl | sh`) and refined the CLI setup tabs to enhance the onboarding experience.

### Security

- **Dependabot**: Bumped `golang.org/x/crypto` in the CLI module to address upstream vulnerabilities.

## [0.9.0-rc.0] - 2026-04-13

### Added

- **Managed agent enrollment lifecycle**: Added durable enrollment validate/restore control-plane actions plus richer enrollment snapshots so CLI-driven onboarding can persist apply, validation, and rollback state per managed agent.
- **CLI agent enrollment workflow**: `preloop agents discover` is now inventory-first while `preloop agents enroll`, `status`, and `restore` handle backup-aware local MCP rewiring, durable credential bootstrap, and restore reporting for supported desktop/CLI agents.
- **OpenClaw managed enrollment adapter**: OpenClaw onboarding now uses an explicit adapter for `mcp.servers.preloop` config writes and validation, matching the documented `transport: "http"` plus bearer-header integration shape.
- **Subject-scoped governance**: Managed-agent and API-key subjects can now carry their own `allowed_models`, tool rules, and tool enable/disable overrides, with API-key scope taking precedence over the enrolled agent when both are present.
- **CLI release version reporting**: The `preloop version` command now reports the same release version as the rest of the shipped components by default instead of falling back to `dev` in local builds.
- **Responsive console sidebar**: Sidebar is now fully responsive with distinct behavior per breakpoint. On large screens (≥768px): sidebar is visible by default and stays visible while working in the main panel; hamburger toggle hides or shows it. On small screens: overlay behavior with backdrop; hamburger opens/closes the slide-in menu. Removed collapsed icon-only state in favor of fully visible or fully hidden.
- **AI Model Gateway foundations**: Flow executions now resolve models through explicit runtime transport settings and can hand gateway-enabled agents a Preloop gateway URL, short-lived bearer token, model alias, and provider adapter instead of raw provider credentials.
- **Preloop OpenAI-compatible gateway**: Added `/openai/v1/models`, `/openai/v1/chat/completions`, and `/openai/v1/responses` backed by LiteLLM, with bearer-token auth that preserves runtime API key context for attribution.
- **Anthropic-compatible gateway ingress**: Added `POST /anthropic/v1/messages` so Anthropic-format clients can route through the same Preloop gateway control plane, including a first-pass text-only streaming/SSE path.
- **Gateway streaming support**: Added SSE streaming support for `/openai/v1/chat/completions` and `/openai/v1/responses` so OpenAI-compatible clients can use streamed model output through the Preloop gateway.
- **Gateway usage ledger**: Model gateway requests are now recorded in `api_usage` with account, API key, flow, flow execution, model alias, provider, token usage, estimated cost, and runtime principal attribution.
- **Gateway budget controls**: Added preflight account-level and flow-level model gateway budget checks with soft-limit annotations and hard-limit denials.
- **Gateway reporting endpoints**: Added `GET /api/v1/account/gateway-usage/summary` and `GET /api/v1/flows/{flow_id}/gateway-usage/summary` to expose spend and token summaries from the gateway usage ledger.
- **Provider-agnostic secret references**: Added `SecretReference` plus a `SecretService` abstraction for AI model credentials, with a built-in `local_encrypted` backend.
- **Gateway runtime events**: Added normalized `model_gateway_call` execution events with redaction-aware request/response payload capture and flow execution log persistence.
- **Gateway event endpoint**: Added `GET /api/v1/flows/executions/{execution_id}/gateway-events` for execution-scoped inspection of normalized model gateway events.
- **Gateway events UI**: Flow execution detail now includes a dedicated Gateway Events tab that renders normalized model-call events, key spend/token metadata, and sanitized payload previews.
- **Gateway usage summaries UI**: The API usage page now renders real account-level gateway usage summaries with date filtering, budget state, and model/flow activity breakdowns.
- **Gateway session explorer UI**: The API usage page now includes a session/execution-oriented view so operators can inspect which flow executions and agent sessions have been using AI models.
- **AI model observability views**: AI model settings now expose per-model usage summaries, runtime-session drill-downs, and searchable captured interactions so operators can inspect one configured model in detail.
- **AI model fleet overview**: The AI model list now doubles as a fleet overview with 30-day spend, traffic, failure, and active-session signals for each configured model.
- **Gateway conversation previews**: `model_gateway_call` events now include a provider-neutral conversation preview plus capture-policy metadata describing redaction/truncation state.
- **Gateway search corpus foundation**: Added a dedicated `GatewayUsageSearchDocument` corpus keyed to `ApiUsage`, with normalized searchable text, content hashing, and a placeholder vector column for future semantic indexing.
- **Opt-in gateway interaction indexing**: Successful gateway requests, and failed requests when separately enabled, can now be automatically indexed into the `GatewayUsageSearchDocument` corpus. When content capture is disabled, indexing stays metadata-only.
- **Runtime session identity foundation**: Added a new `RuntimeSession` layer and `ApiUsage.runtime_session_id` so session browsing/search can evolve beyond flow-only execution identities while keeping current flow-backed paths intact.
- **Runtime session explorer APIs and UI**: Added account-scoped runtime session list/detail endpoints plus a dedicated console view for drilling into one managed session's model usage, model breakdowns, and captured gateway interactions.
- **Dashboard telemetry endpoint**: Added `GET /api/v1/account/telemetry/dashboard` to aggregate active runtime sessions, recent tool-call volume, daily spend, and success rate for the global operator dashboard.
- **Audit timeline session enrichment**: The grouped Audit timeline now includes runtime session lifecycle events, richer expandable metadata, and API token attribution on tool-policy activity so operators can trace session onboarding and guarded tool execution from the real Audit page.
- **Runtime session operator actions**: Operators can now end managed runtime sessions explicitly, with account events and managed-agent refreshes emitted from the same control-plane action.
- **Starter policy diff review**: MCP server onboarding now includes generated starter-policy diff previews and explicit review-before-apply flows in both the console and CLI.
- **Hash-only runtime API tokens**: Flow runtime API keys can now be stored and authenticated via hash/prefix fields without persisting the plaintext token.
- **Managed agent registry**: Added a durable `ManagedAgent` registry plus `GET /api/v1/agents` and `GET /api/v1/agents/{agent_id}` so onboarded external agents can be browsed independently from one runtime session.
- **Agents console surfaces**: Added `/console/agents` and `/console/agents/:agentId` so operators can inspect enrolled agents, linked MCP servers, session history, and recent runtime activity using the existing session drill-down surfaces.
- **Runtime session activity ledger**: Added normalized `RuntimeSessionActivity` records for MCP tool calls so runtime-session and managed-agent activity can be persisted beyond flow-backed execution logs.
- **Managed agent tool activity views**: Agent detail now includes historical model usage plus MCP server and tool activity breakdowns across all sessions owned by the same durable runtime principal.
- **ANSI log rendering**: Console execution logs now correctly parse and render ANSI color codes.

### Changed

- **Flow gateway usage summary**: `GET /api/v1/flows/{flow_id}/gateway-usage/summary` now loads the account through the account CRUD layer instead of an ad-hoc SQLAlchemy query.
- **Codex and OpenCode model transport**: Gateway-enabled executions now prefer Preloop gateway settings over direct-provider model credentials, while retaining compatibility fallbacks during rollout.
- **AI model credential storage**: New AI model credentials are stored via `SecretReference` instead of directly returning persisted plaintext API keys from the model record.
- **External secret backends**: AI models can now reference optional Vault/OpenBao-compatible KV v2 secrets through `credentials_backend_type` and `credentials_external_ref`.
- **Gateway client compatibility**: OpenAI-compatible and Anthropic-compatible ingress now return provider-native error envelopes for auth failures, validation errors, budget denials, and surfaced upstream gateway errors.
- **Agent identity model**: External-agent onboarding now separates durable `runtime_principal_id` from per-session `session_source_id`, allowing one enrolled agent to accumulate multiple runtime sessions over time.
- **Runtime session tenancy**: `RuntimeSession` source identity is now scoped by account so independently onboarded agents cannot collide across tenants.

### Security

- **Runtime token hardening**: Temporary flow runtime credentials are now revocable hash-only tokens rather than plaintext-only database entries.
- **Credential custody groundwork**: AI model secrets are now encrypted behind the secret-service abstraction, creating a clear path for external secret-manager backends without changing gateway callers.
- **Gemini fail-closed gateway behavior**: Gateway-enabled Gemini flows now error explicitly instead of falling back to direct provider traffic, preserving the requirement that managed model traffic must pass through Preloop.
- **Sensitive data redaction**: Centralized redaction of secrets and sensitive fields before logging, persisting to audit surfaces, or sending notifications. Tool arguments, approval payloads, and configuration changes are redacted in MCP execution logs, approval flows, flow execution logs, audit trail, and approval emails. See `preloop.utils.redaction` and ARCHITECTURE.md Redaction Policy.
- **Runtime session token scope validation**: Runtime-session token issuance now rejects caller-supplied scope escalation and only accepts account-authorized MCP server/tool restrictions.
- **Vault/OpenBao secret path hardening**: Secret reference validation now rejects traversal segments, encoded paths, and malformed external references before resolving secrets from Vault-compatible backends.

### Fixed

- **OpenClaw + Gemini onboarding**: Preloop AI models imported from OpenClaw now enable `meta_data.gateway` only when upstream provider credentials are actually stored (or already present on an existing model). This prevents gateway test calls from failing with “Model credentials are not configured” while the UI still showed gateway routing as enabled. OpenClaw `auth.profiles` entries with `mode: api_key` can now resolve inline or `${ENV}` API keys when the provider block does not expose `apiKey`.
- **AI model gateway controls in the console**: Adding or editing an AI model includes an explicit “route through Preloop gateway” option, and the model detail page can enable gateway routing when upstream credentials exist—addressing cases where Gemini (and other) models were configured with credentials but never received `meta_data.gateway.enabled`.
- **Dashboard telemetry query**: The account dashboard telemetry endpoint now filters gateway usage by `ApiUsage.timestamp`, restoring the intended active-session and daily-spend aggregation path.
- **Trial hosted-model denials**: Trial hosted-model hard-cap checks now use a consistent enforcement reason so direct budget-service callers return the intended BYOK guidance instead of a generic budget-exceeded error.
- **Runtime-session gateway inspection scoping**: When a `runtime_session_id` filter is present, gateway interaction search and per-model gateway totals now require matching `ApiUsage.runtime_session_id` rows only. Legacy rows attributed only to `flow_execution_id` with a null runtime session are no longer folded into session-scoped views (avoids mixing traffic across sessions that share execution lineage).
- **OpenCode gateway provider registry**: OpenCode `provider.*.models` keys now use a provider-local model id (with a single optional leading `{gateway_provider}/` stripped) so lookups stay aligned with the top-level `model` field after the gateway/provider refactor.
- **Gateway search performance**: Account interaction search now uses PostgreSQL full-text search plus a GIN index instead of broad `%...%` `ilike` scans on `GatewayUsageSearchDocument.searchable_text`.
- **AI model secret cleanup**: Deleting an AI model now removes its credential secret reference when no other model still depends on it.
- **Global default AI model seeding**: `scripts/init_db.py --force` can seed system-wide default AI models again by allowing global `SecretReference` rows without an account owner.
- **Gateway tool-call logging**: Anthropic payload normalization no longer emits raw LiteLLM tool-call argument payloads to debug logs, keeping the parsing fallback while aligning better with the branch's redaction posture.
- **Execution cancellation**: Restored the missing Cancel button for running executions.

## [0.8.0] - 2026-03-08

### Added

- **Async Approvals**: Tool calls can now return immediately with a `pending_approval` status when async approvals are enabled on a policy. Agents poll `get_approval_status` for the result instead of blocking, avoiding timeouts in CLI clients (Claude Code, Codex CLI). Approved tool results are cached for idempotent retrieval.
- **Per-Tool Justification Settings**: Configure `justification_mode` (`disabled`, `optional`, `required`) per tool via `ToolConfiguration`. When enabled, a `justification` parameter is injected into the tool schema and enforced server-side.
- **OpenCode Agent Support**: Added OpenCode as a supported agent type for flow execution alongside Codex, Gemini CLI, Aider, and OpenHands.

### Fixed

- **Async approval double-execution**: Concurrent poll requests could both execute an approved tool when `tool_result` was `None`. Fixed with `SELECT ... FOR UPDATE` row locking.
- **Approval remaining_seconds TypeError**: Subtracting a timezone-aware `datetime.now(timezone.utc)` from a naive `expires_at` column raised `TypeError`. Fixed to use consistent naive UTC datetimes.
- **Event timestamp serialization**: `event.timestamp.isoformat() + "Z"` produced invalid RFC 3339 when the timestamp already included a timezone offset. Fixed by stripping tzinfo before serialization.
- **Justification bypass**: `justification_mode=required` was only enforced via schema injection. Clients skipping schema validation could call tools without justification. Added server-side enforcement in `_call_tool`.
- **OSS 404 errors**: Frontend components (`approval-workflow-dialog`, settings views) unconditionally fetched `/api/v1/users`, `/api/v1/teams`, `/api/v1/roles` which don't exist in the open-source edition. Gated behind `advanced_approvals` and `user_management` feature flags.
- **Flow edit form empty values**: When editing an existing flow, select fields (model, tracker, tools) appeared empty until reference data loaded. Added loading spinners and parallelized API calls.

- **OAuth Sign-in/Sign-up**: Authenticate users via external OAuth providers (GitHub, Google, GitLab)
  - Plugin-based architecture: `plugins/oauth_signin/` with per-provider implementations
  - Auto-links OAuth identity to existing accounts by verified email
  - GitHub/GitLab sign-ups prompt for tracker installation after sign-in
  - Stripe checkout integration for new users when billing is enabled
  - Gated by `mcpOauth.enabled=true` Helm value; configure via `GOOGLE_OAUTH_CLIENT_ID/SECRET`, `GITLAB_OAUTH_CLIENT_ID/SECRET`, `GITHUB_APP_*` env vars
- **MCP OAuth 2.1 Authorization Server**: Full OAuth 2.1 server for MCP client authentication
  - Dynamic Client Registration (RFC 7591) at `POST /oauth/register`
  - Authorization Code + PKCE flow for MCP clients (Claude Desktop, etc.)
  - JWT token flow for CLI authentication (no PKCE)
  - Token revocation at `POST /oauth/revoke`
  - Discovery via `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource`

### Security

- **OAuth consent validation**: Validate `client_id` exists and `redirect_uri` is registered before issuing authorization codes
- **XSS prevention**: HTML-escape all user-controlled values in OAuth consent page template
- **PKCE enforcement**: Require `code_verifier` when authorization code was created with `code_challenge`
- **Token delivery**: Use URL fragments instead of query parameters for OAuth callback tokens to prevent leakage via browser history, server logs, and Referrer headers
- **Redirect URI validation**: Verify `redirect_uri` at token exchange matches the original authorization request

### Fixed

- **OAuth refresh tokens**: MCP clients can now refresh opaque OAuth tokens (previously only JWT refresh worked)
- **Codex custom models**: Properly generate `~/.codex/config.toml` with `model_provider`, `base_url`, `env_key`, and `wire_api` for non-OpenAI models

- **Policy-as-Code**: Define and manage policies declaratively via YAML files
  - `POST /api/v1/policies/import`: Import policy from YAML with validation and diff preview
  - `GET /api/v1/policies/export`: Export current configuration as YAML
  - `POST /api/v1/policies/validate`: Validate policy syntax without applying
  - `POST /api/v1/policies/diff`: Compare policy document against current state
  - Supports MCP servers, approval workflows, tool configurations, and access rules
- **Policy Versioning & Rollback**: Version control for policy configurations
  - `GET /api/v1/policies/versions`: List all policy versions
  - `POST /api/v1/policies/versions`: Create a snapshot of current policy state
  - `PUT /api/v1/policies/versions/{id}/tag`: Tag versions for identification (e.g., "production", "v1.0")
  - `POST /api/v1/policies/versions/{id}/rollback`: Rollback to a previous version with diff preview
  - `DELETE /api/v1/policies/versions/{id}`: Delete old versions (supports pruning by age)
  - Credential-safe rollbacks: MCP server credentials are preserved during rollback
- **AI-Driven Approvals**: New approval type where an AI model evaluates tool call requests
  - Configure approval workflows with `approval_mode: "ai_driven"`
  - Set AI model, custom guidelines, confidence threshold (0.0-1.0)
  - Fallback behavior when AI is uncertain: escalate to human, auto-approve, or auto-deny
  - Full audit logging of AI decisions with reasoning and confidence scores
- **Tool Access Rules**: Fine-grained access control for tools beyond approvals
  - Define multiple rules per tool with `allow`, `deny`, or `require_approval` actions
  - Priority-based rule evaluation (higher priority rules are checked first)
  - Condition expressions for parameter-based rules (e.g., `args.amount > 500`)
  - Replaces the simpler `tool_approval_conditions` table
- **Policy Analysis**: Analyze policies for potential issues
  - `POST /api/v1/policies/analyze`: Detect always-match, never-match, unreachable, or conflicting rules
  - Natural language policy authoring assistance via configured AI model
- **CLI Tool**: Go-based command-line interface for policy management (`preloop/cli/`)
  - `preloop auth login/logout/status`: Authentication management
  - `preloop policy import/export/validate/diff`: Policy operations
  - `preloop tools list/configure`: Tool management
  - Daily version check with update prompts
- **Flow Execution Retry**: Failed, stopped, timed out, or cancelled flow executions can now be retried via `POST /api/v1/flows/executions/{id}/retry`. The new execution is linked to the original via `retry_of_execution_id` and uses the same trigger event data. UI retry button available in the execution detail view.
- **update_comment Issue Comment Support**: The `update_comment` tool now supports PR conversation comments (issue comments) in addition to inline review comments. Use the optional `comment_type` parameter to specify the type, or let the tool auto-detect by trying review_comment first then issue_comment.
- **Pull Request/Merge Request MCP Tools**: New built-in tools for PR/MR management:
  - `get_pull_request`: Fetch PR/MR details including comments and diff
  - `update_pull_request`: Update PR/MR state, submit reviews (approve, request changes, comment), add/remove reactions
  - `add_comment`: Add comments to PRs/MRs (general, inline code comments, threaded replies)
  - `update_comment`: Update or resolve existing PR/MR comments
  - `create_pull_request`: Create new PRs/MRs with full metadata support
  - Works with both GitHub Pull Requests and GitLab Merge Requests
- **PR/MR Reactions**: `update_pull_request` now supports adding and removing emoji reactions (GitHub: +1, -1, laugh, confused, heart, hooray, rocket, eyes; GitLab: thumbsup, thumbsdown, smile, eyes, rocket, etc.)
- **Commit Status Updates**: Flow executions now appear as commit status checks in GitHub/GitLab, showing "pending" while running and "success"/"failure" on completion
- **Bot Event Filtering**: Flow trigger service now detects and ignores events triggered by Preloop's own actions to prevent infinite loops
- **Android Push Notifications (FCM)**: Native Firebase Cloud Messaging support for Android mobile app push notifications
- **Push Proxy**: Proxy endpoint allowing OSS instances to send push notifications via production infrastructure
- **Message-based WebSocket Authentication**: Secure WebSocket auth via message after connection (tokens no longer in URLs)
- **Periodic Version Checker**: Automatic daily version check against preloop.ai (configurable interval, opt-out available)
- **Admin Activity Monitor**: Click-to-navigate from session to user/account details

### Changed

- **Tool Access Control**: Replaced `tool_approval_conditions` table with `tool_access_rules` supporting multiple rules per tool with allow/deny/require_approval actions and priority-based evaluation
- **Approval Workflow Schema**: Added AI-driven approval fields (`approval_mode`, `ai_model`, `ai_guidelines`, `ai_context`, `ai_confidence_threshold`, `ai_fallback_behavior`, `escalation_workflow_id`)
- **[BREAKING CHANGE] Policy & Configuration Rename**: `approval_policies` and `approval_policy_id` properties in policy definition files and SDK API models have been renamed to `approval_workflows` and `approval_workflow_id` respectively. Ensure you update any exported/custom YAML policies and API client integrations. Backward compatibility responses are provided where applicable.
- **FCM Service**: Moved Firebase SDK calls to thread pool executor to avoid blocking the event loop
- **Session Manager**: Database writes now run in thread pool to prevent event loop blocking during connection spikes
- **WebSocket Endpoints**: Updated to support message-based authentication for browsers

### Deprecated

- **`get_merge_request` MCP Tool**: Use `get_pull_request` instead. Works with both GitHub PRs and GitLab MRs.
- **`update_merge_request` MCP Tool**: Use `update_pull_request` instead. Works with both GitHub PRs and GitLab MRs.

### Fixed

- **GitHub Assignees/Reviewers Clearing**: `update_pull_request` with `assignees=[]` or `reviewers=[]` now correctly clears all assignees/reviewers on GitHub (previously it did nothing because GitHub's POST endpoints only add). Consistent behavior with GitLab.
- **GitHub App Reaction Removal**: `remove_issue_reaction` now safely handles GitHub App installation tokens by checking for `app_slug` in connection_details. Previously it attempted to call GET /app which fails with installation tokens.
- **GitHub Inline Comment ID**: `add_comment` now returns the actual comment ID instead of the review ID for GitHub inline comments, enabling proper follow-up updates via `update_comment`
- **Thread Resolution Validation**: `update_comment` now properly validates that `thread_id` is required for resolving threads. GitHub requires a thread ID (format: `PRRT_...`), not a comment ID. Automatic GraphQL lookup added for GitHub.
- **Inline Comment Side Parameter**: `add_comment` no longer validates the `side` parameter for non-inline comments, fixing errors when `side` was passed for regular comments
- **GitLab Inline Comments**: Now properly returns 501 error explaining that inline diff comments require position data not available in this API, instead of creating non-anchored discussions
- **GitLab Assignees/Reviewers**: `update_pull_request` and `create_pull_request` now correctly look up user IDs from usernames for GitLab, with clear warnings when lookups fail
- **Review Comments Validation**: `update_pull_request` now validates that each item in `review_comments` has required fields (path, line, body), returning 400 with clear error instead of 500
- **Git Clone Fallback**: When `git_clone_config.enabled = true` but `repositories` is empty, now falls back to using the trigger project for cloning
- **Self-hosted GitLab URLs**: Fixed URL parsing for self-hosted GitLab instances (no longer requires "gitlab" in hostname)
- **Milestone Pagination**: GitHub milestone lookup now paginates through all milestones instead of only checking the first page
- **HTTPException Wrapping**: Fixed exception handlers that were incorrectly wrapping HTTPException in 502 errors
- **Event Loop Blocking**: FCM notifications and session DB writes no longer block the FastAPI event loop
- **WebSocket Middleware Paths**: Middleware now handles `/api/v1/ws` prefixed paths correctly
- **Telemetry Env Var**: Both `PRELOOP_DISABLE_TELEMETRY` and `DISABLE_VERSION_CHECK` now work to disable telemetry
- **Session Manager Thread Safety**: DB writes now use thread-local sessions to avoid SQLAlchemy thread-safety issues
- **WebSocket Auth Upgrade**: Anonymous users upgrading to authenticated are now properly registered for broadcast messages
- **OpenAI API Errors**: Issue duplicates endpoint now returns 503 for API auth/rate limit errors instead of 500

### Configuration

New environment variables (see `.env.example`):
- `FCM_CREDENTIALS_JSON` / `FCM_CREDENTIALS_PATH`: Firebase service account credentials
- `PUSH_PROXY_URL` / `PUSH_PROXY_API_KEY`: Push proxy configuration for OSS instances
- `PRELOOP_DISABLE_TELEMETRY`: Disable version check telemetry
- `VERSION_CHECK_INTERVAL`: Seconds between version checks (default: 86400 = 24h)

### Database

New migration `20260201_policy_engine_enhancements`:
- Creates `tool_access_rules` table (replaces `tool_approval_conditions`)
- Creates `policy_snapshot` table for policy versioning
- Adds AI approval columns to `approval_workflow` table
- Migrates existing `tool_approval_conditions` data to new schema
- Run `alembic upgrade head` after updating

### Flow publication integration preview

- Select repository PR templates and validate agent-authored title/body metadata;
  add execution attribution regardless of metadata fallback source.
- Add an opt-in trusted publisher with scoped GitHub App credentials, clean Git
  object import, concurrent-update protection and idempotent PR provenance.
  Controller verification integration and private runner publication are pending;
  unsupported isolated executions fail closed before publication.
