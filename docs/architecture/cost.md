# Cost Analytics and Budgeting

Cost analytics turns gateway telemetry into explainable spend and budget health. This chapter covers the `ApiUsage` ledger, OSS API/UX boundaries, and the Enterprise plugin split.

## Progressive reporting

The Cost console requests `GET /api/v1/cost/summary?include_breakdown=false`
for its first paint and previous-period comparison. This keeps account totals,
budget, pricing/unpriced context, and separately reported imported totals, but
skips the grouped breakdown queries. Settings and model metadata load separately
and do not block those totals.

Callers can select details with repeated `breakdown` parameters: `models`,
`flows`, `sessions`, `tools`, `days`, or `imported`. For example,
`?breakdown=sessions&breakdown=flows` loads the Agents tab without computing
tool costs or the daily timeseries. With no new parameters, the endpoint
retains its full historical response. `include_breakdown=false` takes precedence
over a selection. Unselected arrays are empty because they were not requested;
clients must distinguish this from a loaded section with no data.

Agents, Sessions and Users share an in-flight/session breakdown within the
current view. Tools and user ownership load when their tabs are opened. Imported
details load separately when imported totals identify visible content. Each
section has its own loading/error/retry state. Range changes invalidate loaded
sections and reject late responses, then reload the tab that remains selected.
All details use the effective period returned with the initial totals.
The console does not persist previous-period results across account sessions.

This changes request scheduling and selected query execution only. Account
isolation, history policies, ledger accounting, attribution, reporting limits,
and full-query ordering remain unchanged. It adds no rollups or response cache.

## Query shape

Session breakdowns aggregate raw `api_usage` rows by session and model first.
Session name, agent, flow, and principal labels are joined onto that aggregate.
The response limit applies after the full aggregation, so a capped session list
still carries complete totals for each returned group. Daily series aggregate
in a materialized day bucket, then sort those buckets. Per-user windows filter
`runtime_principal_id` through `ix_api_usage_account_principal_id_ts`
(`account_id`, `runtime_principal_id`, `timestamp` for `model_gateway` rows).
`ix_api_usage_account_principal_ts` still leads with principal type. Accounting
rules are unchanged: replay-validation rows stay excluded, retries stay included
unless the caller sets `exclude_retries`, and there is no daily rollup or
response cache.

## Cost Analytics and Budgeting
*   **Purpose:** Turn model usage telemetry into explainable spend, enforceable budgets, and optimization guidance.
*   **Canonical Ledger:** `ApiUsage` remains the source of truth for model call tokens, estimated cost, provider, model, runtime principal, API key, flow, managed agent, and runtime-session attribution.
*   **Idle Cache Expiry:** `preloop.services.context_analysis` extends `CacheProfile` with `CacheIdleExpiryEvent` rows when consecutive content-stable gateway calls are separated by more than the provider idle TTL (Anthropic 5m, OpenAI 10m, Gemini 1h, DeepSeek 2h) and ApiUsage shows a cache_read collapse plus cache_creation spike. Extra cost is `(write_price_per_1k - read_price_per_1k) * rewritten_tokens` from the vendored catalog; optimize/replay surfaces only measured, per-session figures.
*   **Accounting Self-Check:** `GET /api/v1/cost/health` verifies the accounting chain end-to-end per account over a lookback window (gateway traffic seen → streaming requests record tokens → costs priced → provider-reported usage share → audit events present), so silent accounting breakage (like streaming rows recording 0 tokens) is caught immediately instead of weeks later.
*   **Effective Price Read-Back:** `GET /api/v1/ai-models/{id}/pricing` (`view_ai_models`) answers what one model is priced at right now and where that number came from, resolving in the gateway's own order: an account price override, then the pricing configured on the model, then the vendored catalog, then `source="none"`. `POST /api/v1/ai-models/{id}/pricing/fetch` (`edit_ai_models`) reads a provider's published price (OpenRouter's public model list, or Alibaba Cloud Model Studio's native catalog on USD sites) and never writes it: a fetched number is confirmed by a person through the price override endpoints before it changes what spend means. Both are account-scoped through the model, and a malformed stored price reads as unpriced rather than failing the page.
*   **Priced, Unpriced, and Zero:** each `usage_by_model` row carries `unpriced_request_count` (tokens spent with no price at all, so that cost is missing from every total), `zero_priced_request_count` (a price was applied and it was exactly zero, so nothing is missing), `failed_request_count`, and `last_request_at`. The split exists because a $0.00 total means two different things, and only one of them is an accounting hole. `unpriced_request_count` reuses the `get_gateway_usage_summary` condition, so the per-model counts sum to the account total.
*   **OSS API Surface:** Core endpoints should provide aggregate summaries, grouped breakdowns, raw usage drill-downs, and budget-health alerts derived from gateway account/flow limits. Core endpoints also provide runtime-session optimization recommendations, one-click apply, and replay verification (`preloop/api/endpoints/session_optimization.py`), with hosted-model analysis gateable via the `preloop.services.optimization_gating` authorizer hook. Enterprise billing plugin endpoints provide budget policy CRUD, enforcement, and model price override CRUD behind feature flags.
*   **OSS UX Boundary:** Open source should answer "how much was spent?", "who or what spent it?", and "which budget applies?" with enough drill-down to inspect the related session timeline.
*   **Enterprise UX Boundary:** Enterprise should answer "why was it spent?", "was it worth it?", and "how could it be optimized?" at scale with LLM-assisted reviews, anomaly detection, forecasting, showback/chargeback, credits/promotions, exports, and workflow automation.
*   **Default AI Model Use:** Enterprise session-value analysis should call the account's default AI model through the Preloop Gateway, producing an auditable meta-usage record for the evaluation itself. The analysis should reference redacted session summaries, gateway events, tool calls, approvals, and final outcomes rather than unrestricted raw prompts.
*   **Plugin Boundary:** Backend features beyond OSS summaries and budget-health tracking must live in Enterprise plugins under `./plugins/`, likely extending `plugins/billing/` for budget policy enforcement, pricing overrides, FinOps, credits, promotions, forecasting, exports, and value-review jobs. The shared frontend should gate those panels with feature flags.
*   **Budget Actions:** Core enforcement should continue to block or warn before upstream dispatch. Enterprise plugins can add escalations, Slack/mobile notifications, approval requirements for expensive calls, and post-hoc anomaly workflows.

## Reviewed price publication

After an initial rollout and explicit configuration, each API, dedicated gateway,
and worker polls the same trusted HTTPS price artifact. A reviewed publication
updates supported flat token rates, native DeepSeek UTC peak/off-peak tariff
revisions, and dedicated Alibaba USD regional token tiers without deploying application code. Unknown policy structures require
an estimator change, boundary tests, and a deployment. Refresh validates evidence,
effective dates, model scope, and historical tariff continuity before replacing
the current map; existing usage records, account overrides, and provider-reported
costs are unchanged. On failure it retains last-known rates as potentially stale
estimates and logs the failure. The public weekly model-price review preset and idempotent installer bind the
account's existing model and repository, prepare an evidence-backed PR and
regional feed, and report providers or cache policies that could not be verified.
Alibaba prices land in the dedicated region store of each process; scoped regional
allowlists can admit newly reviewed SKUs, while freshness and effective dates
prevent stale native overlays or newer tariffs from corrupting historical estimates. See [configuration and publication](../guide/model-price-refresh.md).
