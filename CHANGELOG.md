# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`preloop agents refresh` (alias `sync`), `preloop models sync`, and `POST /api/v1/ai-models/sync`**: refresh rewrites managed model sections of onboarded agent configs from the account catalog; models sync (and the endpoint) pull newly released provider models into that catalog using stored credentials.
- **Opt-in scheduled model-catalog sync**: `MODEL_CATALOG_SYNC_SCHEDULED_ENABLED` (default false; helm `config.modelCatalogSync.*`) runs the same discovery as `preloop models sync` for every account, attributing audit events to the `model-catalog-sync` system actor.
- **`preloop usage hook` accepts harness-agnostic events**: stdin is
  auto-detected as Cursor hooks (unchanged), generic NDJSON
  (`preloop.usage.event.v1`), or Codex CLI session rollouts. Codex
  one-shot import uses `--from codex --file`. Guide:
  `docs/guide/usage-hooks.md` (old `cursor-usage-hooks.md` path kept as
  a stub).

### Changed

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

### Fixed

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
  silently opening an empty dialog when the export fails.
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

### Security

- **Model I/O ``text_sha256`` stays a SHA-256 prompt fingerprint**:
  CodeQL flagged the digest as password hashing because scanned prompts
  can contain secrets. It is an audit fingerprint (never the raw text),
  the same pattern as API-key lookup hashes. The algorithm is unchanged,
  so existing ``text_sha256`` rows keep matching.

- **Drop python-jose for PyJWT**: auth tokens, email/reset tokens, WebAuthn
  challenge state, MCP OAuth authorize codes, and APNs ES256 client
  assertions now use PyJWT. python-jose pulled unmaintained
  `python-ecdsa` (CVE-2024-23342, no patch). Auth is HS256; APNs ES256
  already uses `cryptography` when present. PyJWT was already in the
  tree via firebase-admin / MCP.

### Added

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

### Changed

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
- **GitHub CI backend tests run in parallel**: the backend unit suite is
  sharded across four GitHub Actions jobs with pytest-split, each with
  its own Postgres, so PRs are no longer gated on a single ~12-minute
  pytest process. Coverage from the shards is combined before the 60%
  floor is applied.
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

### Fixed

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
