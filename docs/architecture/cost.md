# Cost Analytics and Budgeting

Editions: OSS. Contributor documentation for this repository.

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

## Cost and cycle time per tracker issue

`preloop.services.issue_cost_rollup` rolls execution cost, tokens and pull
request cycle times up to the tracker issue, across flows. The per-flow Cost
page is unchanged.

*   **Tables:** `issue_cost_rollup` (one row per account, tracker and issue
    key), `issue_cost_execution` (one fact per execution id, so a replay or a
    rebuild upserts instead of double counting) and `issue_cost_pull_request`
    (publication, approval and merge times, the claiming issue and an
    ambiguity flag). Row sums are always recomputed from the facts.
*   **Attribution:** first match wins: issue lifecycle, resume lineage,
    delegated parent, retry parent, an issue trigger subject, a pull request
    already claimed by one issue, exactly one closing reference. Anything else
    is unassigned. A pull request claimed by two issues is marked ambiguous,
    and executions linked only through it move to the unassigned bucket.
*   **Write hooks:** the orchestrator terminal hook, the execution monitor's
    stale pass and the crashed local dispatch path (terminal statuses written
    outside the orchestrator), `record_opened_pr` (publication time),
    `process_webhook_event` (approval and merge times, never creating rows)
    and `sync_execution_cost_rollup` (repricing). Each runs in a savepoint
    and never fails its caller.
*   **PR opened time:** `issue_cost_pull_request.opened_at_source` says where
    `opened_at` came from. `forge` is the pull request's own `created_at`,
    read through the tracker's `list_open_pull_requests_by_source_branch` on the
    branch lookup bind path (GitHub, GitLab and Bitbucket) or from any later
    pull request webhook; it replaces a Preloop time even when that is
    earlier. `bind` is the time Preloop bound the pull request to the run and
    `run_end` the end of the publishing run; both only fill an empty value.
    The issue row carries the source as `pr_opened_at_source`.
*   **Rebuild:** `POST /api/v1/cost/by-issue/rebuild` records finished
    executions of a window of at most 92 days that have no fact yet, each in
    its own savepoint. It is the recovery path for executions that ended
    outside the orchestrator or whose hook failed.
*   **Scheduled rebuild:** the API role runs the same rebuild every
    `ISSUE_COST_REBUILD_INTERVAL_SECONDS` (default 3600) for executions that
    started in the last `ISSUE_COST_REBUILD_LOOKBACK_HOURS` (default 72), at
    most `ISSUE_COST_REBUILD_MAX_EXECUTIONS_PER_ACCOUNT` (default 500) per
    account per pass. Each account is rebuilt in its own transaction under a
    `pg_try_advisory_xact_lock`, so replicas skip an account another one is
    rebuilding. The pass then re-reads the estimate of recently active issues
    from their synced issue rows. `ISSUE_COST_REBUILD_ENABLED=false` turns it
    off. Older history still needs the rebuild endpoint.
*   **Estimate:** the human estimate as the tracker states it, never
    derived, for comparing AI cost with the estimate. Hours come from Jira
    Original Estimate (`timeoriginalestimate`) or GitLab `time_estimate`.
    Points, and hours on trackers without a native field, come from the
    tracker's `meta_data.issue_estimate` configuration: `points_field` (an
    issue field such as a Jira story points custom field or GitLab `weight`),
    `hours_label_prefix` and `points_label_prefix` (labels such as
    `estimate:4h` or `sp:3`; two labels with different values are no
    estimate). Set it with `PUT /api/v1/trackers/{id}`; `meta_data` is
    replaced as a whole, so send the existing keys too. Values are read from
    the trigger payload when it is about the issue and from the synced issue
    row (Jira and GitLab store the native fields in
    `meta_data.estimate_fields`), the synced row winning. A reading that
    states nothing never clears a stored estimate. Empty when the tracker has
    no estimate.
*   **Report:** `GET /api/v1/cost/by-issue` filters issues by first event
    time and shows their lifetime totals. The per-project and per-flow
    summaries are sums of the rows. `/unassigned/executions` lists the runs
    in the unassigned bucket for the same filter. `/export` returns CSV
    (issue grain plus one unassigned row) or JSON (with execution ids).
*   **Cost coverage:** `estimated_cost` is the subtotal of the runs that
    carry a cost, so on its own it cannot tell a free ticket from an
    unpriced one. Every issue row, every project and flow summary and the
    unassigned bucket therefore also report `cost_coverage`,
    `known_cost_run_count`, `unknown_cost_run_count` and `attributed_cost_usd`.
    Coverage is `complete` when every contributing run has a cost, `partial`
    when both kinds are present and `unknown` when none has one; an empty
    bucket is `unknown` with both counts zero, and a known zero counts as
    known. `attributed_cost_usd` is the subtotal only for `complete`
    coverage and null otherwise, so a partial or unknown bucket is never
    read as a total. The counts come from the same account-scoped fact
    aggregates as the sums, so they follow the report's filters (a flow
    filter prices only that flow's runs) without a per-execution usage
    query.
*   **What coverage is not:** it describes execution-cost availability, never
    invoice accuracy. A `complete` row is still an estimate priced from
    published model rates. The three other cost signals stay deliberately
    outside these numbers: the premium-request counts a host CLI run
    reports (`host_exec_usage`, shown on the execution), the daily GitHub
    Copilot import (`copilot_usage_import`, account level and never
    attributed to a ticket) and any per-seat subscription price. No daily
    import dollars are added to an issue total and no seat charge is
    inferred per ticket. Existing facts keep their stored cost: an explicit
    historical zero stays known unless its producer is independently shown
    to be wrong.
*   **Export columns:** CSV columns, in order: `tracker`, `issue_key`,
    `title`, `project`, `estimated_cost`, `total_tokens`, `run_count`,
    `failed_run_count`, `first_event_at`, `pr_opened_at`, `approved_at`,
    `merged_at`, `first_event_to_pr_opened_hours`,
    `pr_opened_to_approved_hours`, `approved_to_merged_hours`, `issue_url`,
    `pr_url`, `pr_opened_at_source`, `estimate_hours`,
    `estimate_hours_source`, `estimate_points`, `estimate_points_source`,
    then the appended `cost_coverage`, `known_cost_run_count`,
    `unknown_cost_run_count`, `attributed_cost_usd`.
    Blank means unknown, never zero. The JSON export's `issues[]` objects
    carry the same fields (null for unknown) plus `execution_ids`. Estimate
    sources are `jira:timeoriginalestimate`, `gitlab:time_estimate`,
    `<tracker type>:<points_field>` or `label:<prefix>`.

    The four coverage columns are appended, so an older consumer keeps
    reading the same names in the same order and `estimated_cost` keeps its
    type. What changes is the interpretation, not the shape: a consumer that
    adds the issue rows together now sees the priced subtotal only, which
    understates a ticket whose runs were subscription-backed.

    ```csv
    tracker,issue_key,title,project,estimated_cost,...,cost_coverage,known_cost_run_count,unknown_cost_run_count,attributed_cost_usd
    GitHub,example-org/example-repo#12,Add the export button,Example,2.0,...,partial,1,1,
    GitHub,example-org/example-repo#13,Subscription-backed work,Example,0.0,...,unknown,0,2,
    ```

    The second row is not a free ticket: two runs carry no per-run price, so
    the row reports no attributable total. A consumer that wants a total
    only where one exists sums `attributed_cost_usd` and treats the blanks as
    unknown.

## Spend outlier alerts

`preloop.services.spend_outliers` flags a developer or session whose spend
departs from the usual pattern. It reads gateway `ApiUsage` rows
(`action_type='model_gateway'`, replay validation excluded) grouped by user,
UTC day and model. It does not add `flow_execution.estimated_cost`, because
those calls are already usage rows.

*   **Daily spend:** spend on UTC day D is at least `daily_multiple` (default
    3) times the median of the days with spend among the previous 28. The rule
    needs `min_history_days` (default 7) such days, and a zero median never
    fires.
*   **Model mix:** one model matching a `top_tier_model_prefixes` entry (case
    insensitive, with or without a `provider/` prefix) is more than
    `top_tier_share` (default 0.5) of the developer's spend on both D and D-1.
*   **Session:** one runtime session costs more than
    `session_cost_threshold_usd`. The rule is off while that is null.

The daily rules run at 00:30 UTC for the day that just ended. The session rule
runs every 15 minutes over sessions active in the last two hours. Settings
live in `spend_outlier_settings`, one row per account, and are edited under
`/api/v1/attention/spend-outliers/settings` (`manage_budgets` to write,
`view_cost` to read).

**Fires once.** Each finding is a row in `spend_outlier_finding`, unique on
`(account_id, fingerprint)` and written with `ON CONFLICT DO NOTHING`, so a
rerun, a retry or two workers racing record it once. The attention item id is
stable per rule and developer (`spend:<rule>:<user_id>`, or
`spend:session_cost:<session_id>`). The fingerprint names the UTC day
(`<rule>|<user_id>|<YYYY-MM-DD>`, or `session_cost|<user_id>|<session_id>`).

**Dismissal.** Cards use the existing attention dismissals. A dismissal hides
the card while its fingerprint matches, so a developer who is still an outlier
on the next day gets a new card. A snooze is the exception: for spend cards an
unexpired snooze hides the card whatever the fingerprint, until the snooze
ends. The dismissal endpoints stamp `dismissed_at` on the matching finding,
and a restore clears it.

**Digest.** `build_spend_outlier_digest_section(db, account_id, now,
start=..., end=...)` returns the findings detected in one half-open
`[start, end)` window, one entry per fingerprint, each marked `dismissed` when
a dismissal or a snooze covers it. The two are read at different moments: a
dismissal is read as it stands, so a finding dismissed after the window closed
is still reported as dismissed, while a snooze is resolved as of the window
end, so one that only runs out afterwards still covers the window. Without
`start` and `end` the window is the seven days ending at `now`; with them it
is exactly that window, and both bounds are always applied, so a finding
detected at or after the end of the window is not in the section. The window is
reported back as `window_start` and `window_end`. Display names, session titles
and dismissals are resolved within the account, so a row that points at another
account's user or session shows no label rather than that account's. Every
entry keeps the numbers its rule recorded, including imported dollars;
overlapping findings are never summed into an account total. It is the section
for the weekly digest service, which is resolved through the plugin registry
and lives outside this repository.

**Imported spend.** Spend that does not pass through the gateway enters
through `register_imported_spend_source`. Cards and digest entries that
include such dollars say they are not metered by the gateway. The one
production source is `preloop.services.copilot_spend_source`, registered by
the `evaluate_spend_outliers` task before each pass (idempotent, no HTTP
router imported, no GitHub call). It reads stored Copilot premium-request
rows for the connection's current organization, keeps daily per-user rows in
USD whose login has a row in `copilot_user_mapping` (operator-written, one
active same-account user per login, several logins per user allowed), nets
signed amounts per user, day and model, and returns the positive nets as
`source='copilot'`. Seat rows, usage metrics, organization totals, the
unattributed residual, unmapped logins and rows without an amount are left
out and reported as counts by `GET /api/v1/cost/copilot/spend-coverage`.
Nothing is written back to `api_usage`, budgets or issue rollups.

**Replay and supersession.** Imported days arrive three days late and can be
corrected, so for accounts with an active Copilot connection the daily pass
evaluates the 28 most recent completed UTC days (`REPLAY_WINDOW_DAYS`) rather
than yesterday alone. `evaluate_days` loads the span once, judges each day
against its own 28-day history, and reconciles the stored findings for
exactly the two daily rules and those days: an unchanged finding is left
alone (dismissals and snoozes keep applying), changed evidence is written
onto the existing row with `detected_at` kept, and a day that no longer
qualifies gets `superseded_at` and `superseded_reason` and is filtered out of
the open list and the digest while its row remains. `superseded_at` and
`dismissed_at` are independent. When an imported source raises, nothing is
reconciled and only yesterday is judged from gateway spend; the pass reports
the account as `incomplete`.

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
