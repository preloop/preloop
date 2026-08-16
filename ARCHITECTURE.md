# Preloop Architecture

## System Overview

Preloop is an open-source, responsible AI automation platform. It can proxy tools from MCP servers, optionally adding a human approval layer with configurable policies. It provides event-driven agentic flows to intelligently automate common tasks using agent frameworks like Claude Code, Codex CLI, OpenCode, Gemini CLI, Aider or OpenHands. It integrates with issue & code tracking systems like Jira, GitHub, GitLab, both for listening to events and for ingesting issues, comments, documentation and code. By leveraging vector-based similarity search, Preloop detects duplicate and overlapping issues, detects unmapped dependencies, evaluates compliance metrics, and offers intelligent suggestions to streamline workflows. The architecture now also includes Preloop-owned model-gateway surfaces so managed runtimes can route model traffic through a central enforcement point for telemetry, budgets, session observability, and secret custody. The architecture emphasizes flexibility, performance, and ease of integration, providing access via a REST API, a web UI, and an MCP server for various clients.

## High-Level Architecture

```mermaid
%%{init: {"flowchart": { "htmlLabels": false}} }%%
graph LR
    subgraph "External Systems"
        direction TB
        MCP_Clients["MCP Clients (e.g., Claude Code)"]
        Issue_Trackers["Issue Trackers (Jira, GitHub, GitLab)"]
        Browser["Browser"]
    end
    subgraph "Preloop Platform"
        subgraph "Main Repository"
            direction LR
            API["Preloop REST API"]
            Gateway["OpenAI-Compatible Model Gateway"]
            subgraph "Sub projects"
                direction LR
                preloop.models["Preloop Models (Data Layer)"]
                subgraph "Preloop Sync (Data Sync Service)"
                    Scheduler["Preloop Sync Scheduler"]
                    Worker["Preloop Sync Worker"]
                end
                PreloopConsole["Preloop Console (Frontend)"]
            end
        end
        subgraph "Services"
            direction RL
            DB["PostgreSQL + PGVector"]
            NATS["NATS (Internal Task Queue)"]
        end


    end
    Browser --> PreloopConsole
    preloop.models --> DB
    API --> Gateway
    Scheduler --> NATS
    NATS --> Worker
    Worker --> Issue_Trackers
    API --> Issue_Trackers

    MCP_Clients -- HTTP --> API
    PreloopConsole --> API
```

**Key Components:**

*   **Preloop REST API:** The core FastAPI application providing the HTTP interface.
    It now also exposes Preloop-owned model-gateway surfaces for managed runtime traffic.
*   **Preloop Models:** Handles database interactions, defining SQLAlchemy models, Pydantic schemas, and CRUD operations. Manages the PostgreSQL database connection and PGVector operations.
    This layer now includes `SecretReference` for provider credentials, `ApiUsage` gateway attribution for model usage, costs, and runtime principals, and account-scoped model pricing metadata used by cost analytics.
*   **Preloop Sync:** A service responsible for polling external issue trackers, processing data, generating embeddings, and storing/updating information in the database via `Preloop Models`. The preloop-sync cli can launch one-off scan operations, or start the scheduler process that adds polling tasks to the NATS queue. The NATS queue is consumed by the Preloop Sync worker process.
    *   **Preloop Sync Scheduler:** A process that adds polling tasks to the NATS queue.
    *   **Preloop Sync Worker:** A process that consumes tasks from the NATS queue and processes them.
    *   **Flow execution workers:** When `FLOW_EXECUTION_WORKER_ENABLED` is true, flow orchestration (`FlowExecutionOrchestrator`) runs on a dedicated sync worker pool (`execute_flow` / `resume_flow_execution`) instead of `asyncio.create_task` in the API or webhook worker. Workers claim a `flow_execution` row via a DB lease (`orchestrator_worker_id` + heartbeat), ack JetStream after claim, and publish `flow-updates.{id}` for console WebSockets. API replicas only create PENDING rows and dispatch; recovery re-publishes stale/unclaimed executions on the flow-execution pool boot.
*   **Preloop Console:** A web application built using Lit, Vite, TypeScript, and Material Web Components.
*   **PostgreSQL + PGVector:** The database storing metadata and vector embeddings.
*   **NATS:** An event bus used for both a reliable task queue (JetStream) and real-time streaming updates. It decouples the API from the background processing of events and flows.
    Gateway model-call events can be emitted into the same execution update channel used by flow runtime updates, and the same pattern should later support non-flow runtime sessions.
*   **External Systems:** Issue trackers and MCP clients interacting with the Preloop ecosystem.

## Frontend Architecture
The frontend is in the `frontend` directory.

```mermaid
graph TD
    subgraph "Browser"
        direction LR
        WebApp["Lit Web Application"]
        Shoelace["Shoelace Web Components"]
        WebApp -- Uses --> Shoelace
    end

    subgraph "Build & Dev Tools"
        direction LR
        Vite["Vite"]
        TypeScript["TypeScript"]
        WTR["Web Test Runner"]
    end

    subgraph "Backend"
        PreloopAPI["Preloop REST API"]
    end

    WebApp -- Bundled by --> Vite
    TypeScript -- Transpiled by --> Vite
    WebApp -- Makes API Calls to --> PreloopAPI
    WTR -- Runs Tests on --> WebApp

    style WebApp fill:#aef,stroke:#333,stroke-width:2px
```

### Technology Stack

*   **Framework:** [Lit](https://lit.dev/) - A simple library for building fast, lightweight web components. It provides reactive state, scoped styles, and a declarative templating system.
*   **Build Tool:** [Vite](https://vitejs.dev/) - A modern frontend build tool that provides an extremely fast development experience with features like Hot Module Replacement (HMR) and optimized production builds.
*   **Language:** [TypeScript](https://www.typescriptlang.org/) - A statically typed superset of JavaScript that enhances code quality and maintainability.
*   **UI Components:** [Shoelace](https://shoelace.style/) - A set of high-quality, standards-based web components.
*   **Testing:** [Web Test Runner](https://modern-web.dev/docs/test-runner/overview/) - A tool for testing web applications in a real browser, ensuring that components behave as expected in a live environment.

### Structure

The `Preloop Console` application is structured around a component-based architecture.

*   **`src/components/`**: This directory contains all the custom Lit components that make up the application. Each component is typically defined in its own file (e.g., `tracker-list.ts`) and may have a corresponding test file (e.g., `tracker-list.test.ts`).
*   **`src/api.ts`**: A dedicated module for handling communication with the Preloop REST API. It encapsulates fetch logic, authentication, and data transformation.
*   **`index.html`**: The main entry point for the application.
*   **`vite.config.ts`**: Configuration for the Vite build tool.
*   **`package.json`**: Defines project metadata, dependencies, and scripts for development, building, and testing.

#### Tracker Detail Page (`src/views/authed/tracker-detail-view.ts`)

The Tracker Detail page is the entry point for issue analytics. Clicking a tracker card in the Trackers list navigates to `/console/trackers/:trackerId`, which shows:

*   **Tracker metadata:** Name, type, connection status, creation/update dates, URL, and scope rules.
*   **Issue Analytics cards:** Conditional links to Similarity, Compliance, and Dependencies views, gated by feature flags (`issue_duplicates`, `issue_compliance`, `issue_dependencies`). Each link pre-filters to projects belonging to that tracker via `?projects=` query parameters.
*   **Projects list:** All projects synced under this tracker.

Issue analytics features are no longer accessible from the main sidebar — they are scoped to individual trackers via this detail page.

#### Tools Page (`src/views/authed/tools-view.ts`)

The Tools page has been redesigned from a card-based layout to a tree-style list view:

*   **Summary stats table:** Interactive statistics panel showing tool counts (total, available/unavailable, enabled/disabled, built-in/proxied, with rules/no rules, require approval/no approval, approval workflows). Each stat is a clickable filter link.
*   **Unified filter system:** Single active filter at a time, text search, and approval workflow filter dropdown.
*   **Tool groups:** Tools grouped by source — external MCP servers listed first, then HTTP tools, then built-in tools.
*   **Import/Export:** Full configuration export/import as YAML.
*   **Key components:**
    *   `tool-list-item.ts` — Individual tool row with expand/collapse, enable/disable toggle, rule summary badges, and drag-and-drop rule reordering.
    *   `tool-rule-editor.ts` — Dialog for creating/editing access rules with action selection (deny/require approval/allow), condition builder (simple or CEL), and approval workflow configuration (human or AI-driven).
    *   `approval-policy-dialog.ts` — Dialog for creating/editing approval workflows.
*   **Access rule UI semantics:** Actions use semantic icons and colors — Deny (red, `x-octagon-fill`), Require Approval (blue/primary, `shield-lock-fill`), Allow (green, `check-circle-fill`).

#### Cost Analytics Area (`src/views/authed/cost-*`)

The Console exposes a dedicated Cost section (`cost-view.ts`, sidebar "Cost") rather than scattering spend data across gateway, sessions, and settings pages. The shared frontend renders both OSS and Enterprise panels, gated by feature flags returned by the API (`billing`, `model_price_overrides`, `session_optimization`).

Core open-source subviews:

*   **Overview:** Date-range spend, token volume, request count, budget utilization, and budget-health cards, with sortable Agents / Tools / Sessions / Users tabs.
*   **Breakdown:** Groupable tables and charts by model, provider, managed agent, runtime session, flow, API key, and user, backed by `/api/v1/cost/*` and `GET /api/v1/tools/stats` (per-tool call counts, schema-injection token estimates, and spend attribution).
*   **Budgets:** OSS surfaces account/flow gateway limits and burn-rate health; budget policies support notification recipients (`notification_user_ids`, `notification_team_ids`). Enterprise billing plugin owns scoped budget policy CRUD, enforcement, and notification workflows.

Enterprise feature-flagged subviews (via `plugins/billing/`):

*   **Pricing:** Per-account model price overrides for input/output/cache tokens, fixed request costs, currency, effective date, and provider-specific metadata.
*   **Session Value:** LLM-generated summaries that explain what happened in a session, whether the outcome appears worth the spend, and which expensive attempts failed or retried.
*   **Optimization:** Recommendations for cheaper model routing, prompt compaction, caching, batching, retry suppression, or policy changes.
*   **Forecasting & Anomalies:** Burn-rate forecasts, unusual spend detection, alerts, chargeback/showback, and export workflows.

## Core Components

### Preloop API Server (Main Repository)
*   **Framework:** FastAPI-based RESTful API server.
*   **Authentication:** JWT authentication and authorization.
*   **MCP Server:** Includes integrated MCP tool endpoints under `/api/v1/mcp/` for direct communication with MCP clients over HTTP.
*   **Validation:** Request validation using Pydantic models (defined in `preloop.models`).
*   **Documentation:** Automatic API documentation with Swagger/ReDoc.
*   **Features:** Rate limiting, error handling, monitoring integration.
*   **Interaction:** Communicates with `preloop.models` for database operations, directly with Issue Tracker APIs for certain actions (e.g., creating/updating issues in real-time), and with LiteLLM/provider APIs through the model gateway path.

### OpenAI-Compatible Model Gateway
*   **Purpose:** Centralize model traffic from managed runtimes behind Preloop control.
*   **Ingress:** `GET /openai/v1/models`, `POST /openai/v1/chat/completions`, `POST /openai/v1/responses`
*   **Additional Client Compatibility:** `POST /anthropic/v1/messages` for Anthropic-format clients such as `Claude Code`
*   **Subscription-OAuth Passthrough:** Anthropic-protocol requests backed by a Claude Code subscription-OAuth credential bypass the LiteLLM transcode and are forwarded to Anthropic verbatim (system block array, `cache_control` markers, and client `anthropic-beta` flags preserved; merged with the OAuth beta flag). The upstream validates the structural shape of subscription-OAuth requests, and the transcode would destroy it. Budget preflight, governance tool-stripping, attribution, and usage accounting still run on this branch; API-key and OpenAI-protocol traffic keep the LiteLLM path.
*   **Claude Model-Family Fidelity:** Claude Code onboarding imports one gateway model per selectable family (opus/fable/sonnet/haiku) sharing a single credential secret, maps each to its `ANTHROPIC_DEFAULT_<FAMILY>_MODEL` env key, and leaves the subagent selector on the stock resolution chain — so `/model` switching, background fast-path requests, and subagents keep native UX while routing through Preloop. Context-window variant markers (`claude-fable-5[1m]`) resolve/price against the base model but are forwarded verbatim upstream on the OAuth passthrough. Unknown `claude-*` identifiers requested over a subscription-OAuth credential (e.g. new dated snapshots after a Claude Code update) are lazily auto-registered against the same credential and bound to the requesting managed agent (`model_gateway_claude_family_autoregister_enabled`, default on); Anthropic remains the authorization boundary for what the subscription may use, and subject-scoped `allowed_models` checks still apply.
*   **Streaming:** Supports SSE streaming for chat completions and responses.
*   **Authentication:** Reuses short-lived runtime bearer tokens while preserving `ApiKey` context and runtime-principal metadata.
*   **Accounting:** Persists token usage and estimated cost in `ApiUsage`, including first-class cache-read/cache-creation/reasoning token columns, `currency`, and provenance markers (`cost_source`: override | model_config | provider | catalog | subscription | reconciled | unpriced; `usage_source`: provider | estimated | partial). When the upstream reports the request's actual cost in its usage payload (OpenRouter usage accounting: `usage.cost` / `usage.cost_details.upstream_inference_cost`; the gateway requests it via `usage: {"include": true}` on OpenRouter-bound requests), that figure is authoritative over catalog estimates and the row is tagged `cost_source='provider'`. Historical rows that predate usage accounting can be repriced from the provider's daily activity ledger via `scripts/backfill_openrouter_ledger.py` (`OPENROUTER_ACTIVITY_KEY` env var, dry-run by default): each day's ledger total is allocated across that day's unpriced rows proportionally by tokens and tagged `cost_source='reconciled'` with an audit marker in `meta_data` — labeled approximations, deliberately distinct from per-request `provider` figures. Streaming requests always request the provider's final usage chunk (`stream_options.include_usage` is injected upstream; the synthetic chunk is stripped from clients that did not opt in); usage payloads split across chunks are merged, client disconnects record partial usage at status 499, and a local tokenizer fallback (`litellm.token_counter`) estimates tokens when a provider reports none.
*   **Budget Enforcement:** Applies account-level, flow-level, and subject-scoped allowed-model checks before upstream dispatch. Preflight input estimation uses a real tokenizer with a chars/4 fallback; OAuth-subscription-credentialed models preflight at $0.
*   **Pricing:** Default prices come from a vendored, provenance-stamped snapshot of litellm's price map (`services/data/model_prices.json`, registered via `litellm.register_model` at startup) so estimates are deterministic per release. The snapshot is kept small on purpose — Preloop-routed providers only, chat/responses modes only (no image/audio/embedding models), past-deprecation entries dropped, fields stripped to pricing essentials; `scripts/update_model_prices.py` is the standardized refresh path (fetch → review diff → commit, with a `--check` CI staleness gate). Models missing from the snapshot self-heal at runtime: an `unpriced` usage row triggers a one-shot background lookup against the live upstream map (`model_price_catalog.schedule_price_lookup`, gated by `model_price_live_lookup_enabled`) that registers the price and re-prices the triggering row; the downloaded map is cached in-process (~6h), failed downloads back off (~15m), and unknown model names are negative-cached (~24h) so the same model never triggers repeated lookups. Model-name resolution normalizes Bedrock region prefixes (`us.`/`eu.`/…) and trailing date stamps. Account-scoped price overrides win over the catalog (resolved once, via `services/pricing_overrides.py`, for the record path, budget preflight, execution metrics, and tool stats) and support per-token-type prices, fixed request fees, discounts, prepaid balances, effective-date ranges, and non-USD currencies via an explicit `fx_rate_to_usd` (all stored costs remain USD). Requests routed over subscription OAuth credentials (Claude Code Max, ChatGPT/Codex) record $0 spend with the API-equivalent value kept in metadata. Unknown models record `NULL` cost tagged `unpriced` and are surfaced in the Cost overview; the Enterprise reprice endpoint (`POST /api/v1/billing/cost/reprice`) re-derives historical costs from stored tokens against current prices (analytics-only; budget spend is never rewritten).
*   **Rate-Limit Telemetry:** Provider rate-limit headers observed on gateway upstream responses (`Retry-After`, `anthropic-ratelimit-*`, `x-ratelimit-*`) are parsed by `services/rate_limit_telemetry.py` and persisted on `ApiUsage` (`rate_limit_retry_after_ms` column plus a verbatim header snapshot in `meta_data["rate_limit"]`). 429s are subtyped transient vs quota-exhausted (delegating to the shared upstream-error taxonomy when available, with a labeled heuristic fallback). `GET /account/gateway-usage/rate-limits` aggregates 429 counts, provider-advised blocked time, and the latest per-model headroom snapshots for the Console's Rate Limits & Headroom panel; every reported number echoes an observed provider response, nothing is estimated.
*   **Context Optimization:** Subject-scoped request-context optimization on the hot path (`services/context_optimization.py`) — repeated-prefix dedupe, noise stripping, and tool-result caps applied before upstream dispatch, with evidence-grounded savings attribution.
*   **Observability:** Emits normalized model-call events with redaction-aware request/response payload capture, provider-neutral conversation previews (up to `MODEL_GATEWAY_MAX_PREVIEW_CHARS`, default 32768), prompt-cache token breakdowns, and optional indexing into a gateway search corpus.
*   **Debug Surface:** Flow execution-scoped gateway events can already be queried via the flows API, runtime-session explorers can query recent session activity directly, and the operator dashboard can aggregate active sessions, recent tool calls, and daily model spend.
*   **Managed Agent Onboarding:** External agents such as OpenClaw can be enrolled so local model traffic is rewritten onto this gateway while local MCP configuration is narrowed to Preloop-managed proxy access.

### Subject-Scoped Governance
*   **Purpose:** Apply governance decisions against the concrete subject using the platform, not just the parent account.
*   **Scope Chain:** Resolution currently walks the active API key first, then the linked managed agent, and finally falls back to account defaults.
*   **Configuration Surface:** Subject-scoped governance can carry `allowed_models`, per-model budget metadata, ordered tool access rules, and `tool_enabled_overrides`.
*   **Enforcement Points:** The same subject context is propagated through MCP tool listing, policy evaluation, and model gateway budget checks so one runtime token sees only the intended tools and models.
*   **Primary Use Case:** Managed agent owners can grant a broad account-level tool catalog while restricting one enrolled desktop/CLI runtime to a tighter set of tools and models.

### Security Screen Scoring (QM Proxy Contract)
*   **Purpose:** Let external agent platforms delegate content security screening to Preloop through a documented HTTP contract, starting with QM's `securityScreen: { backend: "proxy" }` deployment option.
*   **Endpoint:** `POST /api/v1/security-screen/score` (`api/endpoints/security_screen.py`) accepts `{text, hook, metadata}` with the caller's token in `x-api-key` (Bearer fallback) and returns `{score, threshold, primary_outcome}`; `primary_outcome` is omitted for benign content. Auth reuses the model gateway's `authenticate_bearer_token`, so a standard Preloop API key is the routed credential.
*   **Scoring:** `services/security_screen.py` is a pure, deterministic rule engine: compiled case-insensitive regex categories (`prompt_injection`, `destructive_command`, `destructive_sql`, `secret_exfiltration`) with max-match-wins scoring. No I/O, no model calls, no persistence on the scoring path; the threshold comes from `PRELOOP_SECURITY_SCREEN_THRESHOLD` (default 0.7, clamped).
*   **Privacy:** Screened text is never logged or stored. Flagged chunks log score, outcome, matched rule names, and caller chunk coordinates only.
*   **Rollout Semantics:** Shadow vs enforce and fail-closed error handling live on the caller's side (per QM's contract); Preloop only scores.

### Runtime Session Identity
*   **Purpose:** Provide a shared identity layer for browsing, auditing, and searching managed runtime sessions across both flows and onboarded external agents.
*   **Current Implementation:** A new additive `RuntimeSession` layer now uses flows as the first session source, bridged through `runtime_session_id`, `flow_execution_id`, runtime-principal metadata, and optional `agent_session_reference`.
*   **Current Explorer Surface:** Account-scoped runtime session list/detail endpoints now expose recent managed sessions plus their captured gateway interactions so the console can drill from aggregate usage into one session timeline. `GET /account/runtime-sessions/{id}/requests` reads per-request `ApiUsage` rows (tokens, cost, status, tool-schema attribution) to power a unified session replay with turn/delta deduplication, sortable chat view (newest-first by default, message order following the turn sort), cache-token visibility with the re-sent prompt-cached prefix collapsed inside full request context, and inline operator activity turns (`session-replay-panel`, `preloop-session-observer` in the Console). Operator ergonomics on top: keyboard navigation over turns (`j`/`k`/arrows, `Home`/`End`, `Enter`/`o` to expand), clickable summary-bar stats that jump to the most-expensive or first-failed turn, relative turn timestamps with absolute time on hover, a session list that collapses into a compact picker bar (animated, reduced-motion aware) once a session is chosen, and a deep-linkable replay mode via `?replay=` where the host view opts in (`syncModeToUrl`).
*   **Summaries & Titles:** Opt-in LLM-generated session summaries (`POST /account/runtime-sessions/{id}/summaries`) and background session titles are scheduled through the plugin service registry; Enterprise billing provides the generators, each bounded by a per-account daily spend cap (`billing_session_optimization_daily_cap_usd`, `billing_session_title_daily_cap_usd`).
*   **Auxiliary Model Credentials & Fallback:** Non-interactive auxiliary generations (approval summaries, session titles, policy generation, issue compliance/duplicates/dependencies) resolve model credentials through the secret service via `services/model_credentials.resolve_model_call_credentials`, so vault-backed (`credentials_secret_id`), legacy plaintext, OAuth, and ambient credentials all work; routing (`api_base`) is preserved even when credential resolution fails. Approval summaries and session titles additionally retry once against the system-wide default model when the account's model fails, bounded by a per-account, per-UTC-day cap (`PRELOOP_AUX_FALLBACK_DAILY_CAP`, default 50, enforced per process). The main gateway/completion path never falls back.
*   **Session Identity On The Wire (public semantics):** A gateway request is bound to a conversation by the FIRST of these that is present, highest precedence first:
    1.  `X-Preloop-Session-Id` — Preloop's own explicit override. Always wins, works on every gateway ingress (OpenAI, Anthropic, Gemini), and is the documented answer for any client that wants deterministic control.
    2.  A **vendor-native session signal** the agent already sends, read only when the credential's `runtime_principal.type` identifies that agent: Claude Code's `X-Claude-Code-Session-Id` / `metadata.user_id`, Codex's `Session-Id` / `Thread-Id`, OpenCode's `X-Session-Id`. These are deliberately gated on the principal type because `Session-Id` and `X-Session-Id` are *generic* names that any intermediate proxy, load balancer, or CDN may stamp; a wrong session boundary is unrecoverable after the fact, since boundaries can never be re-derived from stored rows.
    3.  `prompt_cache_key` on the OpenAI-shaped ingress. OpenAI splits the old `user` field into `safety_identifier` (stable principal) and `prompt_cache_key` (per-conversation, and explicitly "replaces the `user` field"). Agents populate it for their own cache hit rate, which makes it a de-facto convergence point. It ranks below the two above because it is a *cache* key, not an identity key: a client may legitimately share it across conversations with identical prefixes or rotate it on compaction.
    4.  Nothing. Signal-less sources (Gemini CLI, Hermes, OpenClaw's Anthropic transport) fall through to the inactivity closer below.
    Anything read here is normalized, and a hostile or unusable value degrades to source-keying rather than being trusted.
*   **Published Vocabulary:** For telemetry we align with OpenTelemetry GenAI's `gen_ai.conversation.id`, the only real cross-vendor standard for this concept. It is a telemetry attribute rather than a request field, so it is what we *emit and document*, while the wire-level intake is the precedence chain above.
*   **Inactivity Closer (signal-less fallback):** Without a native id, session identity would derive solely from the runtime principal, which for a durable managed-agent credential is machine-scoped and never changes — so every conversation on that machine appends forever to one row that is never `ended_at`. `runtime_session_idle_timeout_minutes` (default 720) bounds this: when the newest generation's last activity is older than the window, that row is closed **at its own last activity** (never at "now", so history is not rewritten) and the next request opens a new generation keyed `<principal>:idle-<epoch>`. It is strictly a safety net — a native session id always wins, so agents from levels 1-3 above are unaffected — and setting it to `0` disables it entirely. This is deliberately preferred over prompt-prefix inference, which was measured and rejected: two unrelated Codex sessions were 99.6% byte-identical (false merge) while one OpenCode session's consecutive requests shared zero messages (false split), i.e. it fails in both directions at once.
*   **Operator Actions:** Operators can end a session explicitly, which updates runtime state, emits audit and runtime-session events, and refreshes managed-agent summaries derived from the same principal.
*   **Target Direction:** Introduce a runtime-wide session abstraction that can represent flow executions, independent CLI/desktop agent sessions, and later enrolled workforce entities without making `flow_execution` the universal long-term session model.

### Cost Analytics and Budgeting
*   **Purpose:** Turn model usage telemetry into explainable spend, enforceable budgets, and optimization guidance.
*   **Canonical Ledger:** `ApiUsage` remains the source of truth for model call tokens, estimated cost, provider, model, runtime principal, API key, flow, managed agent, and runtime-session attribution.
*   **Idle Cache Expiry:** `preloop.services.context_analysis` extends `CacheProfile` with `CacheIdleExpiryEvent` rows when consecutive content-stable gateway calls are separated by more than the provider idle TTL (Anthropic 5m, OpenAI 10m, Gemini 1h, DeepSeek 2h) and ApiUsage shows a cache_read collapse plus cache_creation spike. Extra cost is `(write_price_per_1k - read_price_per_1k) * rewritten_tokens` from the vendored catalog; optimize/replay surfaces only measured, per-session figures.
*   **Accounting Self-Check:** `GET /api/v1/cost/health` verifies the accounting chain end-to-end per account over a lookback window (gateway traffic seen → streaming requests record tokens → costs priced → provider-reported usage share → audit events present), so silent accounting breakage (like streaming rows recording 0 tokens) is caught immediately instead of weeks later.
*   **OSS API Surface:** Core endpoints should provide aggregate summaries, grouped breakdowns, raw usage drill-downs, and budget-health alerts derived from gateway account/flow limits. Core endpoints also provide runtime-session optimization recommendations, one-click apply, and replay verification (`preloop/api/endpoints/session_optimization.py`), with hosted-model analysis gateable via the `preloop.services.optimization_gating` authorizer hook. Enterprise billing plugin endpoints provide budget policy CRUD, enforcement, and model price override CRUD behind feature flags.
*   **OSS UX Boundary:** Open source should answer "how much was spent?", "who or what spent it?", and "which budget applies?" with enough drill-down to inspect the related session timeline.
*   **Enterprise UX Boundary:** Enterprise should answer "why was it spent?", "was it worth it?", and "how could it be optimized?" at scale with LLM-assisted reviews, anomaly detection, forecasting, showback/chargeback, credits/promotions, exports, and workflow automation.
*   **Default AI Model Use:** Enterprise session-value analysis should call the account's default AI model through the Preloop Gateway, producing an auditable meta-usage record for the evaluation itself. The analysis should reference redacted session summaries, gateway events, tool calls, approvals, and final outcomes rather than unrestricted raw prompts.
*   **Plugin Boundary:** Backend features beyond OSS summaries and budget-health tracking must live in Enterprise plugins under `./plugins/`, likely extending `plugins/billing/` for budget policy enforcement, pricing overrides, FinOps, credits, promotions, forecasting, exports, and value-review jobs. The shared frontend should gate those panels with feature flags.
*   **Budget Actions:** Core enforcement should continue to block or warn before upstream dispatch. Enterprise plugins can add escalations, Slack/mobile notifications, approval requirements for expensive calls, and post-hoc anomaly workflows.

### Agent Control
*   **Purpose:** Agent Control gives autonomous agents such as OpenClaw and Hermes a single, audited channel for online presence, operator messages, status updates, interruption, and future voice-originated contact.
*   **Implemented Today:** Backend Agent Control exposes `WS /api/v1/agents/control/ws` for runtime-credential agent connections and `POST /api/v1/agents/{agent_id}/control/commands` for authenticated operator text commands. It authenticates the runtime principal, binds presence to the managed agent and runtime session, publishes command envelopes through NATS when available, falls back to local delivery, emits account-scoped realtime events, and accepts heartbeat/status/presence/event envelopes from agents. Operator commands are now persisted BEFORE delivery in the `agent_control_command` table (state machine: pending → delivered → acked, with failed/expired side states, TTL via `agent_control_command_ttl_seconds`); reconnecting agents receive undelivered commands in order with their original `command_id`s (runtime plugins should dedupe on `message_id`), and inbound `command_ack`/`command_result`/`command_error` envelopes mark acknowledgement.
*   **Related Implemented Surfaces:** Browser, mobile, and console clients can use account-scoped realtime topics over WebSocket. Runtime sessions, managed-agent records, model-gateway usage, approval events, and operator lifecycle actions already share account-scoped event routing.
*   **Scaffolded Today:** `account_realtime` defines normalized topics such as `runtime_sessions`, `managed_agents`, `gateway_activity`, `budget_health`, and `audit`; the WebSocket manager can filter broadcasts by account and topic; frontend runtime-session and managed-agent views subscribe to those topics. Mobile/watch clients have native voice UI scaffolds that can create operator text turns, but the end-to-end user experience still depends on runtime adapters and production hardening.
*   **Runtime Plugins (shipping):** Standalone open-source runtime plugins live in `runtime-plugins/` — `@preloop-ai/openclaw-plugin` (npm, TypeScript) and `preloop-hermes-plugin` (PyPI, Python) — and implement the `preloop.agent_control.v1` protocol: they read `preloop.control.control_ws_url`, connect with the durable runtime bearer token, own reconnect/backoff behavior, keep the WebSocket open, send heartbeat/status events, advertise capabilities, receive `send_message` command envelopes, acknowledge delivery, map operator messages into their own interactive runtime, and gate native tool calls through Preloop approvals (fail-closed by default). `preloop agents install-plugin <agent>` delegates installation to the runtime marketplace; `PUBLISHING.md` covers lockstep versioning. Existing enrollment can rewrite MCP and model traffic even when the runtime plugin is absent, but Agent Control is not enabled until that plugin is running inside the agent process.
*   **Target Protocol:** A managed agent opens a durable WebSocket using its runtime credential, sends runtime principal/session metadata, subscribes to account and agent-specific command topics, publishes heartbeat/status updates, and acknowledges command delivery. Server-side commands should be persisted and audited before delivery so reconnecting agents can recover missed instructions.
*   **Session Prompt Semantics:** Operator text sent through Agent Control is an auditable user/operator turn for the selected runtime session. It is not a hidden system prompt, policy override, or privileged tool instruction. Runtime adapters should inject it as the next user-facing instruction in the agent's normal conversation model, preserve the current session context when possible, record the originating surface in metadata, and continue routing any resulting tool calls or model calls through the MCP firewall, model gateway, and approval policies.
*   **In-Session Question Delivery:** When the `ask_user` or `request_approval` builtin raises a pending approval and the asking session's runtime has a live Agent Control connection, `preloop.services.ask_user_inband` also delivers the question into that session as an audited `send_message` turn (persisted in `agent_control_command`, logged as `agent_control_message` activity with `kind=preloop_question_notice`). The in-session turn is a notice with token-free deep links (`/approval/<id>` web, `preloop://approve/<id>` mobile) — never an answer channel: anything returned over the control WebSocket is agent output and is not accepted as the human's answer. Answers enter only through the governed approval endpoints, so the single approval record and its quorum/first-answer-wins semantics are preserved across surfaces.
*   **Security Boundary:** The channel uses the same runtime principal, subject-scoped governance, and API-key revocation model as MCP and gateway traffic. Commands that trigger tool use, model calls, or local side effects still flow through the MCP firewall, model gateway, or explicit approval policy rather than bypassing enforcement.

```mermaid
sequenceDiagram
    participant Agent as OpenClaw/Hermes Adapter
    participant WS as Preloop Agent WebSocket
    participant Runtime as RuntimeSession
    participant Operator as Console/Mobile/Watch
    participant Policy as MCP/Gateway/Policy

    Agent->>WS: Connect with managed runtime credential
    WS->>Runtime: Bind or refresh runtime session
    Operator->>WS: Send Agent Control command or voice-originated message
    WS->>Runtime: Persist audited command event
    WS-->>Agent: Deliver command
    Agent->>Policy: Execute governed tool/model path
    Policy-->>WS: Emit account realtime update
    WS-->>Operator: Stream status/result
```

### Managed CLI/Desktop Agent Enrollment
*   **Discovery Entry Point:** `preloop agents discover` can stay read-only (`--json`, `--no-onboard-prompt`) or hand off interactively into managed enrollment, with `--yes` available for auto-onboarding.
*   **Shared Enrollment Engine:** `preloop agents enroll <agent>` and discovery-triggered onboarding both create or reuse a managed runtime identity, import representable MCP servers, mint a durable credential, back up the local config, and rewrite supported local endpoints to Preloop-managed MCP and gateway URLs. For Agent Control, the CLI writes the `preloop.control` contract and can delegate installation to runtime-native plugin managers, but it does not itself own the long-lived Agent Control WebSocket or execute operator commands.
*   **OpenClaw Coverage:** The current OpenClaw adapter supports legacy and newer config locations, JSON5 parsing, gateway-backed model rewrites, and conservative import of command-backed MCP entries such as `mcporter` when an upstream URL can be inferred safely.
*   **Hermes Coverage:** The Hermes adapter discovers `~/.hermes/config.yaml`/`.yml` or installed-but-unconfigured Hermes markers, preserves existing `mcp_servers`, adds a managed `preloop` HTTP MCP server, rewrites supported model configuration to Preloop's `/openai/v1` gateway, and can import provider-specific environment keys or ChatGPT/Codex OAuth material when present.
*   **Credential Boundary:** OpenClaw model credentials may be declared inline under `models.providers` or indirectly through `auth.profiles`; the enrollment path imports model metadata either way, but profile-backed provider secrets may still require manual configuration inside Preloop.
*   **Durable Identity:** `ManagedAgent.agent_kind` is now stored alongside `session_source_type` so operator UX and reporting do not depend on an active runtime session to recover the agent family.
*   **Explicit Model Association:** Onboarding now persists direct managed-agent to AI-model bindings instead of inferring one configured model indirectly from `AIModel.meta_data`.

### Mobile and Watch Voice Contact
*   **Implemented Today:** iOS, watchOS, and Android clients are documented and implemented around approval review, push notifications, QR pairing, and WebSocket-driven approval updates.
*   **Implemented Web Voice:** The web console Agent Control composer prefers browser-native `SpeechRecognition` and `speechSynthesis`, then falls back to server STT/TTS endpoints backed by speech-capable `AIModel` rows when browser audio APIs are unavailable.
*   **Scaffolded Native Voice:** iOS/watchOS and Android contain native STT/TTS or dictation scaffolds that can capture a user turn and call the Agent Control command surface, but production behavior depends on backend availability, managed-agent lookup, and OpenClaw/Hermes runtime adapters being online.
*   **Planned Voice Path:** Mobile/watch voice should start as a native app feature using vendor STT/TTS APIs, then post normalized operator messages into the same runtime session and Agent Control channel used by the console. The server remains the source of truth for transcript, command intent, approval requirements, and delivery state.
*   **Siri Constraints:** Siri Shortcuts and App Intents can launch a predefined Preloop action, capture structured parameters, and hand the user into the app. They should not be treated as a general always-listening background transport for arbitrary agent conversations.
*   **Google Assistant Constraints:** Google Assistant/App Actions can deep link into Android flows and pass structured intent data where supported, but arbitrary background agent chat or cross-app streaming is not a dependable control surface. Android should hand off to the Preloop app before sending audited commands.

### Secret Service
*   **Purpose:** Provider-agnostic custody and resolution of model credentials.
*   **Built-in Backend:** `local_encrypted` for encrypted-at-rest credentials stored in Preloop-managed storage.
*   **External Backends:** Optional Vault/OpenBao-compatible KV v2 references via `SecretReference.external_ref`.
*   **Runtime Boundary:** Gateway-enabled runtimes receive Preloop gateway tokens instead of provider API keys.

### preloop.models (`./backend/preloop/models`)
*   **Purpose:** Data modeling and database interaction layer.
*   **Current Agent/Model Shape:** `AIModel` remains the durable flat row for provider, model identifier, endpoint, and credential reference, while `ManagedAgentAIModelBinding` carries explicit per-agent config slots and primary/default selection.
*   **Deferred Normalization:** Full provider-profile normalization is intentionally deferred until agent UX and policy semantics for many-model agents stabilize; the current migration keeps compatibility fields and avoids a broader schema split.
*   **Cost Analytics Shape:** `ApiUsage` records the measured usage event. Account-scoped pricing metadata should be stored separately from `AIModel` so the same provider/model can have different cost estimates per account, contract, currency, effective date, or self-hosted deployment. Credits, promotions, invoice-grade adjustments, and chargeback rules belong in Enterprise plugin models.
*   **Technology:** SQLAlchemy for ORM, Pydantic for data validation/schemas.
*   **Database:** Defines schema for PostgreSQL, including tables for organizations, projects, issues, embeddings, etc.
*   **Vector Store:** Integrates with PGVector for storing and querying issue embeddings.
*   **Operations:** Provides CRUD (Create, Read, Update, Delete) functions for all database entities.
*   **Migrations:** Uses Alembic for database schema evolution.

### Preloop Sync ( `./backend/preloop/sync`)
*   **Purpose:** Data synchronization and embedding generation service.
*   **Functionality:**
    *   The `preloop.sync` CLI can launch one-off scan operations or start a persistent scheduler.
    *   **Scheduler:** Periodically adds polling tasks for each configured tracker to the NATS queue. The same daemon also reconciles native flow schedules (`sync/services/flow_schedules.py`): one APScheduler job per enabled, non-preset flow with `trigger_event_source='schedule'`. `flow.schedule_config` is a typed union — raw cron (`{"type": "cron", "expr": <5-field crontab>}`; the legacy `{"cron": ...}` shape is still accepted) or the friendly forms `interval` (`every`/`unit`, bounded between 5 minutes and 366 days), `daily` (`at: "HH:MM"`), and `weekly` (`days` + `at`), each with an optional IANA `timezone` (default UTC); the minimum 5-minute interval is enforced at the API for all forms. `POST /api/v1/flows/schedule/preview` validates a config without saving and returns its type, human description, and next run times. Each tick only publishes a `run_scheduled_flow` NATS task; the worker side (`FlowTriggerService.run_scheduled_tick`) re-checks state and enforces the policies — paused flows never fire, and overlapping ticks are skipped while a previous execution is still running (recorded as `flow_schedule_tick_skipped` audit events). Flow API responses expose the derived `schedule_state` (incl. next fire time).
    *   **Worker:** Consumes tasks from the NATS queue. Multiple, specialized worker groups can be deployed, each subscribing to a specific subset of tasks (e.g., polling, webhooks). This allows for independent scaling and monitoring of different task types.
*   **Execution:** Runs as two distinct, long-running processes (scheduler and worker) or as a one-off CLI command.


### Issue Tracker Clients (within Preloop Sync)
*   **Location:** Implementations reside within Preloop Sync.
*   **Structure:** Abstract base classes define common interfaces (`get_issue`, `create_issue`, etc.).
*   **Implementations:** Concrete classes for each supported tracker (Jira, GitHub, GitLab).
*   **Features:** Handles authentication, API specifics, rate limiting, and error mapping for each tracker.

### Backend Project Structure

The backend codebase is organized to separate concerns between the data models, synchronization logic, and the API server.

*   **`backend/preloop/models/`**:
    *   **`models/`**: SQLAlchemy models defining the database schema (e.g., `issues.py`, `projects.py`).
    *   **`schemas/`**: Pydantic models for data validation and API I/O.
    *   **`crud/`**: Database access operations (Create, Read, Update, Delete).
    *   **`db/`**: Database connection and session management.
    *   **`alembic/`**: Database migration scripts.

*   **`backend/preloop/sync/`**:
    *   **`scanner/`**: Core logic for polling trackers and processing data.
    *   **`trackers/`**: Client implementations for different issue trackers (Jira, GitHub, GitLab).
    *   **`embeddings/`**: Logic for generating vector embeddings from issue text.
    *   **`scheduler/`**: Task scheduling logic for regular synchronization.
    *   **`worker/`**: Worker process logic for consuming tasks from NATS.

*   **`backend/preloop/api/`**:
    *   **`endpoints/`**: API route definitions grouped by resource.
    *   **`auth/`**: Authentication logic and router.
    *   **`app.py`**: FastAPI application entry point.

### Tracker Scope Rules

For detailed rules on how Organizations and Projects limit scope during syncing and searching, see the [Tracker Scope documentation](https://docs.preloop.ai/admin/tracker-scope).

### Database (PostgreSQL + PGVector)
*   **Role:** Central data store for metadata and vector embeddings.
*   **Managed by:** `preloop.models` module.
*   **Key Features:** Relational data storage, efficient vector similarity search via PGVector.

## Data Flow

### REST API Flow (e.g., Searching Issues)
1.  **Client Request:** An HTTP client sends a `GET /api/v1/issues/search` request to the Preloop API server.
2.  **API Server:**
    *   Authenticates the request (JWT).
    *   Validates query parameters (using Pydantic models from `preloop.models`).
    *   Calls the appropriate service function.
3.  **Service Layer (API):**
    *   Generates an embedding for the search query.
    *   Calls a function in `preloop.models` to perform a vector similarity search in the PostgreSQL/PGVector database, potentially with metadata filters.
4.  **preloop.models:**
    *   Constructs and executes the SQL query against the database.
    *   Retrieves matching issue data.
5.  **API Server:** Formats the results and returns the HTTP response to the client.

### Data Synchronization Flow (Preloop Sync)
1.  **Trigger:** `preloop.sync scan all` command is executed.
2.  **Preloop Sync Service:**
    *   Retrieves tracker configurations using `preloop.models`.
    *   For each configured tracker:
        *   Uses the appropriate Issue Tracker Client to poll the external API (e.g., Jira API) for new/updated issues since the last scan.
        *   Processes the fetched issues.
        *   Generates vector embeddings for new/updated issue text.
        *   Calls functions in `preloop.models` to insert or update issue data and embeddings in the database.
3.  **preloop.models:** Interacts with the PostgreSQL database to persist changes.

### MCP Flow (Integrated HTTP)
1.  **MCP Client Request:** An MCP client (e.g., Claude Code) sends a tool request using streamable HTTP transport to the MCP server (e.g., `/mcp/v1`). The request includes the standard MCP payload and an `Authorization: Bearer <token>` header.
2.  **Preloop API Server:**
    *   Authenticates the request using the JWT token.
    *   Routes the request to the appropriate MCP tool endpoint.
    *   Validates the incoming MCP parameters against the Pydantic schema for that tool.
    *   Executes the tool logic, interacting with other Preloop services and `preloop.models` as needed.
    *   Formats the result into the standard MCP JSON response format.
3.  **MCP Client:** Receives the HTTP response containing the tool's output.

The `preloop tools list|describe|exec` CLI commands reuse this same `/mcp/v1` surface, so the backend remains the single source of truth for tool visibility and policy enforcement.

## Database Schema (Managed by preloop.models)

The detailed schema is defined using SQLAlchemy models within the `preloop.models` directory. Key tables include:

*   **Organizations:** Stores organization metadata, settings, and potentially user associations.
*   **Projects:** Contains project details, tracker configurations (type, API URL, credentials), and links to organizations.
*   **Trackers:** Holds specific tracker instance details and encrypted credentials.
*   **Issues:** Stores core issue data (ID, title, description, status, labels, etc.) synchronized from trackers.
*   **Issue Embeddings:** Contains vector embeddings (using PGVector `vector` type) linked to issues, used for similarity search.
*   **Other Metadata:** Tables for comments, users, API keys, etc., as needed.

Schema migrations are managed using Alembic within `preloop.models`.

## Technical Decisions

### REST API Implementation
Preloop implements a RESTful HTTP API using FastAPI, which provides:
- High performance with Starlette and Pydantic
- Automatic OpenAPI documentation generation
- Type annotation-based parameter validation
- Native async/await support
- Dependency injection system
- Middleware for authentication, logging, etc.

### MCP Implementation
The MCP server is implemented directly within the FastAPI application using a custom
extension of FastMCP. This provides several advantages:
- **HTTP Transport:** Natively supports HTTP-based MCP clients via StreamableHTTP,
enabling secure remote access.
- **Unified Authentication:** Leverages the same JWT authentication as the rest of the
API.
- **Code Reusability:** Directly calls internal services and CRUD operations, reducing
code duplication.
- **Scalability:** Benefits from the same deployment and scaling infrastructure as the
main API.
#### Dynamic Tool Filtering
The MCP server implements per-user dynamic tool filtering using `DynamicFastMCP`, a
custom subclass of FastMCP:

**Implementation Details:**
- **`DynamicFastMCP`** (`preloop/services/dynamic_fastmcp.py`): Extends FastMCP and
overrides `_list_tools()` and `_mcp_call_tool()` methods
- **Tool Visibility:** Default tools (get_issue, create_issue, update_issue, search,
estimate_compliance, improve_compliance) are only visible when the authenticated
account has one or more trackers configured
- **User Context Propagation:** Uses Python's `ContextVar` for async-safe user context
storage across request boundaries
- **Authentication:** `PreloopBearerAuthBackend` validates JWT tokens and injects user
context into the request scope
- **Middleware:** `UserContextMiddleware` extracts authenticated user info and stores
it in a ContextVar for access during tool listing and execution
- **StreamableHTTP Transport:** Uses FastMCP's proven `http_app
(transport="streamable-http")` implementation for bidirectional streaming
- **Endpoint:** Mounted at `/mcp/v1` with full authentication and lifespan management

**Tool Registration:**
All built-in tools are registered in `preloop/services/initialize_mcp.py` using
FastMCP's `@mcp.tool()` decorator, then filtered at runtime based on user context.

**Benefits:**
- Zero performance overhead for tool registration (happens once at startup)
- Dynamic filtering happens only during tool list requests
- Full compatibility with FastMCP's StreamableHTTP implementation
- Backward compatible with existing authentication infrastructure

### Tool Configuration and Approval Workflow

Preloop includes comprehensive infrastructure for managing tool configurations and implementing human-in-the-loop approval workflows for sensitive tool operations.

#### Tool Configuration Management

**Database Models:**
- **`ToolConfiguration`**: Defines which tools are enabled for an account, their configuration parameters, and approval requirements
  - Links to an optional `ApprovalWorkflow` for tools requiring human approval
  - Supports both default (built-in) and proxied (external MCP server) tools
  - Stores tool-specific configuration in JSONB format

- **`ApprovalWorkflow`**: Defines rules for when and how tool executions require approval
  - Configurable approval modes: manual, auto-approve, auto-reject
  - Optional webhook integration for external approval systems
  - Supports workflow-specific settings (e.g., timeout duration, required approvers)

#### Approval Workflow Architecture

```mermaid
graph TD
    subgraph "MCP Client"
        Client["MCP Client (Claude Code, etc.)"]
    end

    subgraph "Preloop API"
        MCPEndpoint["MCP Endpoint (/mcp/v1)"]
        DynamicMCP["DynamicMCPServer"]
        ApprovalCheck["Approval Check"]
    end

    subgraph "Approval System"
        ApprovalService["ApprovalService"]
        ApprovalDB["ApprovalRequest (DB)"]
        WebhookNotifier["Webhook Notifier"]
    end

    subgraph "External Systems"
        Slack["Slack/Mattermost"]
        CustomWebhook["Custom Approval System"]
    end

    Client --> MCPEndpoint
    MCPEndpoint --> DynamicMCP
    DynamicMCP --> ApprovalCheck

    ApprovalCheck -->|Requires Approval| ApprovalService
    ApprovalService --> ApprovalDB
    ApprovalService --> WebhookNotifier

    WebhookNotifier --> Slack
    WebhookNotifier --> CustomWebhook

    CustomWebhook -->|Approve/Decline| ApprovalService
    Slack -->|Approve/Decline| ApprovalService

    ApprovalService -->|Approved| DynamicMCP
    ApprovalService -->|Declined| Client
```

**Approval Flow:**
1. MCP client initiates a tool call through the `/mcp/v1` endpoint
2. `DynamicMCPServer` checks if the tool requires approval via `_check_approval_required()`
3. If approval is required:
   - `ApprovalService.create_and_notify()` creates an `ApprovalRequest` record
   - Webhook notifications are sent to configured channels (Slack, Mattermost, custom endpoints)
   - The service waits for approval with configurable timeout
4. Approver reviews request and responds via:
   - Public approval API endpoint (`/approval/{request_id}/decide`)
   - Direct API call to Preloop
5. On approval, tool execution proceeds; on decline, error is returned to client

**API Endpoints:**
- `GET /api/v1/tool-configurations` - List all tool configurations for account
- `POST /api/v1/tool-configurations` - Create new tool configuration
- `PUT /api/v1/tool-configurations/{id}` - Update tool configuration
- `DELETE /api/v1/tool-configurations/{id}` - Delete tool configuration
- `GET /api/v1/approval-workflows` - List approval workflows
- `POST /api/v1/approval-workflows` - Create approval workflow
- `GET /api/v1/approval-requests` - List approval requests
- `GET /approval/{id}/data` - Public endpoint for getting approval request details (token-based)
- `POST /approval/{id}/decide` - Public endpoint for approval responses (token-based)
- `POST /api/v1/agents/permission-check` - Lets an onboarded agent raise an approval for one of its **native/built-in** tool calls (not just MCP tools), authenticated with the agent's managed-runtime credential. It reuses `ApprovalService.create_and_notify` → `wait_for_approval` and blocks until decided, returning `{"decision":"allow"|"deny","reason","request_id","timed_out"}` (deny is the safe default). `timed_out: true` marks a deny that is only the expiry of an unanswered approval request — not a human decision — so hook adapters whose host has a native "ask" verdict (e.g. Claude Code PreToolUse hooks) can hand the prompt back to the agent's local UI instead of hard-denying; adapters without an ask verdict keep the fail-closed deny. The request's non-sensitive originating adapter travels as a `_preloop_source` marker inside `tool_args` so approver surfaces can distinguish e.g. a Cursor-originated `Write` from a Claude Code one without a schema migration.

**Agent questions (`ask_user`).** Beyond allow/deny gating, the built-in `ask_user` MCP tool lets an agent ask the operator a question with multiple-choice `options` and/or a free-text answer, routed through the same approval workflow, notification, and audit pipeline. The question payload (`is_question`, `question`, `options`, `allow_free_text`) rides in the approval request's `tool_args` JSONB (no schema migration) and is surfaced on `ApprovalRequestResponse` as computed fields. The operator's reply is submitted via the same decision endpoints, where `ApprovalDecision` now accepts `selected_option`/`answer_text` (precedence: `answer_text` > `selected_option` > `comment`); the resulting text is returned to the agent as the tool result. Mobile/watch render options as buttons plus an answer field.

**Managed-agent linkage:** `ApprovalRequest` carries optional `managed_agent_id`, `runtime_session_id`, and `managed_agent_name` fields, populated from the runtime token context so approval surfaces can show which agent is asking. The endpoint and these identity columns are part of the open-source core. The per-agent native-tool interception adapters (Claude Code, Codex CLI, Cursor, OpenClaw, Hermes) and any future central per-agent/global policy UI live in Preloop Enterprise / the CLI.

**Workflow resolution.** Every account gets a default approval workflow seeded at signup with the account owner as approver (a startup repair pass heals legacy defaults and seeds accounts that missed it). Operators can additionally pin a specific approval workflow per managed agent from the Console's agent detail view (Tools & Governance → Native tool approvals); the pin is stored in the agent's subject-governance config (`approval_workflow_id`) and wins over the account default when the permission-check endpoint resolves a workflow.

**Account governance defaults.** `GET/PUT /api/v1/account/governance-defaults` stores account-wide native tool-approval defaults in the account's subject-governance metadata. Per-agent settings resolve through an explicit chain: explicit per-agent value → account default → enforce (fail-closed). Overrides are bidirectional — an agent can opt out of a permissive account default or relax a strict one — and the defaults response lists the per-agent override ids so the Console (Tools view: account panel; agent detail: inherit/override controls) can render effective state without N+1 lookups.

**Local decision mirroring.** The CLI permission hooks compute a `client_decision` that mirrors — never widens — the host agent's own policy before raising an approval. Claude Code mirroring follows Claude's precedence (bypassPermissions → deny rules → ask rules → acceptEdits → allow rules → safe reads → ask); workspace `Write`/`Edit` in default permission mode deliberately stays "ask" because stock Claude Code prompts for them, so auto-allowing would swallow approvals the operator expects to see. Cursor keeps its own workspace-edit auto-allow because auto-applying edits *is* Cursor's default behavior; slash-rooted paths are treated as absolute on every host OS (Windows `filepath.IsAbs` alone would misroute `/etc/passwd` down the workspace-local branch).

#### Access Rules System

The tool configuration system has been expanded with a **ToolAccessRule** model that replaces the simpler ToolApprovalCondition approach.

**ToolAccessRule Model** (`backend/preloop/models/models/tool_access_rule.py`):

| Field | Description |
|-------|-------------|
| `action` | "allow", "deny", or "require_approval" |
| `condition_expression` | CEL expression for conditional evaluation (e.g., `args.environment == "production"`) |
| `condition_type` | "simple" or "cel" |
| `priority` | Integer for rule ordering (evaluated in priority order, first match wins) |
| `description` | Human-readable description (for deny rules, returned as denial message to the agent) |
| `is_enabled` | Toggle individual rules on/off |
| `approval_workflow_id` | Links to an ApprovalWorkflow for "require_approval" rules |

**Evaluation:** Rules are evaluated at runtime in `DynamicFastMCP._evaluate_policy()` — the first matching enabled rule determines the action. If no rules match, the tool call is allowed by default (but audited in EE).

**Access Rule API Endpoints:**
- `POST /api/v1/tool-configurations/{config_id}/access-rules` - Create access rule
- `PUT /api/v1/access-rules/{rule_id}` - Update access rule
- `DELETE /api/v1/access-rules/{rule_id}` - Delete access rule

#### Tool Output Filters

Account-scoped `ToolOutputFilter` rules strip named top-level fields from MCP tool JSON results on the proxy hot path before results reach the calling agent, trimming wasted context tokens. The model, CRUD layer, and proxy application live in the OSS core; the Enterprise billing plugin exposes `/api/v1/billing/cost/output-filters` CRUD and the Console tools editor provides a filter dialog (also reachable from session-optimization suggestions). Persistent per-tool cost findings are tracked as `ToolCostFlag` rows and surfaced in the agent detail view.

### Language and Framework
Python is chosen as the primary language due to its strong ecosystem for machine learning and data processing, which is essential for similarity search and embedding generation. FastAPI is used for the REST API due to its performance, type safety, and automatic OpenAPI documentation generation.

### Database
PostgreSQL with the PGVector extension is used. The `preloop.models` module encapsulates all database interaction logic, providing a clean separation from the API and synchronization services. This allows for centralized data management and schema evolution.

### Authentication & Authorization

Preloop implements authentication and multi-tenancy:

**Authentication:**
- JWT-based authentication for REST API and MCP endpoints
- Token-based authentication with refresh token support
- Email verification for new user accounts
- Integration points for SSO and OAuth providers (future)

**Multi-User Architecture:**
- **Account Model:** Represents an organization/company
- **User Model:** Represents individual users within an account
- All data is scoped by `account_id` for multi-tenancy isolation

**Security Features:**
- Password hashing with industry-standard algorithms
- Account-level data isolation (all queries filtered by `account_id`)
- User invitation system with secure token-based email verification

**Plugin System:**
- Extensible plugin architecture for adding custom functionality
- Plugins can provide services, API routes, middleware, and dependencies
- Built-in plugins: Argument-based condition evaluator for approval workflows
- Plugin discovery via module paths or file system paths
- Lifecycle hooks: `on_startup()` and `on_shutdown()`

> **Enterprise Features**: Preloop Cloud and Preloop Enterprise add RBAC with 7 system roles, fine-grained permissions, team management, and comprehensive audit logging. Contact sales@preloop.ai for more information.

### Deployment
The system is designed to be containerized using Docker, enabling easy deployment in various environments including Kubernetes clusters. Stateless components enable horizontal scaling under load.

*   **Service roles:** `PRELOOP_SERVICE_ROLE` (`all` | `api` | `gateway`, default `all`) gates which subsystems boot in a given container — API-only deployments skip gateway-only surfaces and gateway-only deployments skip the MCP server, NATS WS consumer, execution monitor, plugins, and approval-repair passes. The release compose file runs separate `api` and `gateway` services from the same image.
*   **Migrations:** `docker-compose.release.yaml` runs schema initialization in a dedicated one-shot `migrate` service that app services wait on (`service_completed_successfully`); Helm deployments run Alembic via their own lifecycle.
*   **Health monitoring:** The Helm chart ships an optional in-cluster health-monitor deployment (`healthMonitor.*`, enabled by default) that polls `/api/v1/health` and logs alert lines after consecutive failures.
*   **Release verification:** `scripts/release_smoke_test.sh` boots the release compose file with tagged images and verifies HTTP health, first-user sign-up/login, and restart-loop-free stability; the release workflow runs it as the `verify-oss-install` gate before publishing a GitHub release.

## Security Considerations

- [x] All API requests authenticated via JWT tokens
- [x] Multi-tenant data isolation (all queries scoped by account_id)
- [x] User invitation system with secure token-based verification
- [x] Password hashing with industry-standard algorithms
- [x] Input validation for all parameters via Pydantic models
- [x] Issue tracker credentials encrypted at rest via the Secret Service (`credentials_secret_id`/`webhook_secret_id` → `SecretReference`; a startup backfill migrates legacy plaintext rows)
- [x] Sensitive data masked in logs (see Redaction Policy below)
- [ ] Rate limiting to prevent abuse (partial implementation exists)
- [ ] 2FA/MFA support for user accounts
- [ ] Session management and token revocation
- [ ] Regular security audits and dependency updates

> **Enterprise Security**: Preloop Cloud and Preloop Enterprise add RBAC and comprehensive audit logging. Contact sales@preloop.ai for more information.

### Redaction Policy

Preloop redacts sensitive data before logging, persisting to audit surfaces, or sending notifications. The centralized redaction module (`preloop.utils.redaction`) provides:

- **`redact_dict(data)`**: Recursively replaces values for sensitive field names (e.g. `password`, `api_key`, `token`, `secret`, `credential`) with `***REDACTED***`.
- **`redact_for_log(data)`**: Produces a safe JSON string for log messages, with sensitive fields redacted and output truncated.

**Redaction is applied in:**
- MCP tool execution logs (tool arguments)
- Approval flow logs and notifications (tool args, approval URLs)
- Flow execution MCP usage logs (persisted to DB)
- Audit trail (configuration changes, tool executions)
- Approval request emails and Slack/Mattermost messages

**Known exceptions:** Approval URLs are not logged in full (replaced with `[sent via notification]`). Progress tokens and request context metadata are not logged. Tracker credentials and AI model API keys are not logged when present in payloads.

**Tests:** `tests/utils/test_redaction.py` asserts that representative secrets never appear in redacted output.

## Real-Time Communication

Preloop uses WebSocket connections for real-time updates:

### Unified WebSocket Architecture

Single WebSocket connection per client with pub/sub message routing:

**MessageRouter** (`backend/preloop/services/message_router.py`):
- Routes messages to topic-based subscribers
- Supports wildcard subscriptions (`'*'` topic)
- Optional per-subscriber filter functions
- Topics: `flow_executions`, `approvals`, `system`

**Benefits:**
- Single WebSocket reduces connection overhead
- Scalable pub/sub pattern
- Easy to add new message types/topics
- Clear separation of concerns

> **Enterprise Features**: Preloop Cloud and Preloop Enterprise add RBAC and approval workflows with quorum, escalations and AI gates. Contact sales@preloop.ai for more information.

## Event-Driven Agentic Flows

For detailed architecture on the Flow subsystem, including the Trigger Service, Flow Orchestrator, NATS queue, Agent infrastructure, and data flows, see the [Flows Architecture documentation](https://docs.preloop.ai/flows/architecture).

### Matrix / Batch Fan-Out

One flow definition can drive an agent-harness × model evaluation grid without cloning the flow per combination:

*   **Trigger:** `POST /api/v1/flows/{flow_id}/trigger` reserves the top-level `matrix` key in the trigger body. When present, it must be a list of up to 25 `{"agent_type"?, "ai_model_id"?}` cells; the trigger fans out to one execution per cell (an empty object `{}` runs the flow defaults). Validation is all-or-nothing (allowed keys, agent types from the agent factory registry, account-visible models), and all execution rows are committed before any cell is dispatched. **Note for existing users:** `matrix` is now a reserved key in trigger bodies — it is stripped before template-variable resolution and never reaches `{{trigger_event...}}` placeholders; rename any pre-existing `matrix` field in custom trigger payloads.
*   **Batch identity:** all cells share a `batch_id` (indexed column on `flow_execution`). Per-cell overrides are persisted on each execution under the reserved `_matrix` key of `trigger_event_details`, making every cell self-describing. All paths that (re)build an agent executor — initial run, resume, background monitor, crash recovery — resolve the effective agent type through a single shared helper (`resolve_matrix_agent_selection`), so an interrupted cell is always handled by its own harness rather than the flow default.
*   **Observation:** `GET /api/v1/flows/batches/{batch_id}/executions` lists a batch (account-scoped, sorted by matrix index) with a rollup of status counts, tokens, tool calls, and estimated cost, so an eval matrix can be observed as a unit.
*   **Response shape:** non-matrix triggers are wire-identical to before; matrix triggers return `batch_id` plus per-cell execution references.
**Eval / observe runs (structured result artifact).** The `Observe / Eval` preset (`backend/presets/003-observe-eval.yaml`) establishes a run → capture → serve contract: the agent writes its final report to `/workspace/result.json` (`preloop.eval.result/v1`: `status`, `summary`, `metrics`, `checks`, optional `artifacts`), the orchestrator captures it first-class on every terminal path (success, failure, stop, timeout) via the Docker archive API — no log scraping or sentinels — and persists it to `flow_execution.result` (JSONB, size-capped; malformed or unfetchable artifacts are recorded as wrapped `{"error": ...}` objects so failures stay visible). It is served on `GET /api/v1/flows/executions/{id}` and `GET /api/v1/flows/executions/{id}/result` (404 when the run reported nothing); list payloads exclude it. Preloop transports and meters the artifact; scoring is owned by the customer's own verifier. Known gap: capture is not yet implemented for the Kubernetes backend (a completed pod's filesystem needs a sidecar or shared-volume reader).
