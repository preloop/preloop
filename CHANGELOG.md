# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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

### Added

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

### Changed

- **"Halt" is now "Pause" throughout the console**, rendered as a play/pause
  toggle in amber/warning tones. Red/danger styling stays reserved for the
  genuinely destructive offboard and remove actions, matching the fact that
  pausing is now reversible.

- **`identity.*` tags are hidden from the default agent tag chips** and shown
  instead under a collapsed "Identity history" disclosure on the agent detail
  view. These tags are server-written bookkeeping from agent re-keying, not
  operator labels; they are preserved unchanged when an operator edits tags.

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
