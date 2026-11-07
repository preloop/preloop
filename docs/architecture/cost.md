# Cost Analytics and Budgeting

Editions: OSS. Contributor documentation for this repository.

Cost analytics turns gateway telemetry into explainable spend and budget health. This chapter covers the `ApiUsage` ledger, OSS API/UX boundaries, and the Enterprise plugin split.

## Progressive reporting

The Cost console requests one `GET /api/v1/cost/summary` for the headline,
previous-period comparison, initially selected tab and imported details.
The default Agents tab asks for `breakdown=sessions&breakdown=flows&breakdown=imported`.
Settings and model metadata load separately and do not block that summary.
Totals-only API readers can still set `include_breakdown=false`.

Callers can select details with repeated `breakdown` parameters: `models`,
`flows`, `sessions`, `tools`, `days`, or `imported`. For example,
`?breakdown=sessions&breakdown=flows` loads the Agents tab without computing
tool costs or the daily timeseries. With no new parameters, the endpoint
retains its full historical response. `include_breakdown=false` takes precedence
over a selection. Unselected arrays are empty because they were not requested;
clients must distinguish this from a loaded section with no data.

Agents and Users share the recent-session breakdown within the current view.
Sessions uses server paging; when it remains selected on a range reload, page
one rides in the initial summary. A paged answer never substitutes for the
recent-session window. Later tabs and user ownership load when opened. Imported
details share the initial summary, avoiding another totals query. Each
section has its own loading/error/retry state. Range changes invalidate loaded
sections and reject late responses, then reload the tab that remains selected.
All details use the effective period returned with the initial totals.
The console does not persist previous-period results across account sessions.

This changes request scheduling and selected query execution only. Account
isolation, history policies, ledger accounting, attribution, reporting limits,
and full-query ordering remain unchanged. It adds no rollups.

The headline totals request also carries the comparison range:
`compare_start_date` and `compare_end_date` (sent together, 422 otherwise)
return that window's totals, without breakdowns, as `previous`. The Spend
page no longer bundles everything into one request: it first asks for the
window's totals only (`include_breakdown=false`) and paints them, then asks
for the comparison window's totals and the visible tab's breakdowns in their
own requests, so the slowest section never holds the page. It rounds its clock
up to the minute, so reloads within a minute ask the same window.

Exact-window summaries are cached per worker, with at most 512 entries. A
window that ended more than an hour ago (`CLOSED_WINDOW_GRACE`) is closed and
kept for six hours (`CLOSED_SUMMARY_CACHE_TTL_SECONDS`); any other window is
kept for 60 seconds (`SUMMARY_CACHE_TTL_SECONDS`). Keys include account,
account metadata, effective dates, principal, retry policy, selected
breakdowns and session paging. Concurrent identical misses share one
computation. Permission and current history policy checks run before cache
hits; responses are copied before adding comparisons. Failures are not cached.

A closed window changes only through a reprice, a ledger backfill or a usage
import. Each of those writes bumps the account's `cost_cache_revision` in
`account.meta_data` (`crud_account.bump_cost_revision`, one atomic UPDATE).
The metadata is part of every key and is read fresh on each request, so every
worker and replica stops serving older aggregates at once; the worker that
handled an OSS reprice or CSV reconciliation also drops its entries,
including in-flight ones. EE repricing that does not go through these paths
may take up to the TTL to appear.

The console keeps `GET /cost/summary` and `GET /cost/timeseries` answers for
the browser session when the window is closed and for 60 seconds otherwise.
Any write from the tab and signing out clear that cache.

Each worker also keeps the `GET /cost/health` self-check
per (account, window) for 60 s, because the Spend page asks it on every load
and it changes slowly. A non-dry-run reprice drops that account's entries, and
a check computed while a reprice ran is not stored.

## Query shape

Session breakdowns aggregate raw `api_usage` rows by session and model first.
Session name, agent, flow, and principal labels are joined onto that aggregate.
The response limit applies after the full aggregation, so a capped session list
still carries complete totals for each returned group. Daily series aggregate
in a materialized day bucket, then sort those buckets. Per-user windows filter
`runtime_principal_id` through `ix_api_usage_account_principal_id_ts`
(`account_id`, `runtime_principal_id`, `timestamp` for `model_gateway` rows).
`ix_api_usage_account_principal_ts` still leads with principal type. Accounting
rules exclude replay-validation and every `governance_` purpose; retries stay included
unless the caller sets `exclude_retries`, and there is no daily rollup. A summary
with no unpriced requests skips the unpriced-model aggregate.

The headline totals read the newest rows through the covering partial index
`ix_api_usage_customer_totals`. That read stays index-only only for pages that
VACUUM has marked all-visible. `api_usage` therefore has its own autovacuum
thresholds (migration `20261015_api_usage_autovacuum`): about 1% new or
changed rows, instead of the server default of 20% (#1583). Without them, the
newest unvacuumed rows, which are exactly the Spend window, pushed the planner
to scan the whole table. A reprice rewrites its whole range and had the same
effect until the next vacuum.

The Sessions breakdown walks session/run keys newest first and counts the
window's distinct sessions. Both read `ix_api_usage_customer_session_keys`
(`account_id`, `timestamp DESC`, including `runtime_session_id` and
`flow_execution_id`, partial on the customer predicate plus "belongs to a
session or a run"; migration `20261203_session_keys_idx`). Before that index
the planner walked the global `ix_api_usage_timestamp` and discarded every
other tenant's rows, so the cost grew with the whole table, not the account.

## Cost Analytics and Budgeting
*   **Purpose:** Turn model usage telemetry into explainable spend, enforceable budgets, and optimization guidance.
*   **Canonical Ledger:** `ApiUsage` remains the source of truth for model call tokens, estimated cost, provider, model, runtime principal, API key, flow, managed agent, and runtime-session attribution.
*   **Idle Cache Expiry:** `preloop.services.context_analysis` extends `CacheProfile` with `CacheIdleExpiryEvent` rows when consecutive content-stable gateway calls are separated by more than the provider idle TTL (Anthropic 5m, OpenAI 10m, Gemini 1h, DeepSeek 2h) and ApiUsage shows a cache_read collapse plus cache_creation spike. Extra cost is `(write_price_per_1k - read_price_per_1k) * rewritten_tokens` from the vendored catalog; optimize/replay surfaces only measured, per-session figures.
*   **Spend Over Time:** `GET /api/v1/cost/timeseries?start_date&end_date&granularity=day|week|month` feeds the Spend chart: gateway spend and requests per UTC bucket (weeks start Monday), keyed by bucket start. One request covers at most 366 days (422 above that) and runs on `ix_api_usage_account_action_ts`. The chart defaults to the page's window so its total matches the cards.
*   **Accounting Self-Check:** `GET /api/v1/cost/health` verifies the accounting chain end-to-end per account over a lookback window (gateway traffic seen → streaming requests record tokens → costs priced → provider-reported usage share → audit events present), so silent accounting breakage (like streaming rows recording 0 tokens) is caught immediately instead of weeks later. Two more checks cover #1401: `token_details_normalized` warns when the raw `meta_data.usage_details` carries a cache or reasoning count that the normalized `cache_read_tokens` / `cache_creation_tokens` / `reasoning_tokens` column is missing or contradicts, and `subscription_billing_coverage` warns whenever subscription-covered rows exist, because the API-equivalent cost is an estimate and billed subscription dollars are not tracked. Check and overall status is `pass`, `warn`, `fail` or `skip`; a warning is never reported as healthy.
*   **Subscription Workload:** `GET /api/v1/cost/summary` returns `subscription_usage` as a separate block (requests, tokens, the summed `api_equivalent_cost` labelled as an estimate, and `coverage`, the share of subscription rows that carry that estimate). `billed` is always null and `billed_available` false: billed subscription dollars and provider credits are never derived from tokens. Subscription rows add $0 to `estimated_cost`.
*   **Effective Price Read-Back:** `GET /api/v1/ai-models/{id}/pricing` (`view_ai_models`) answers what one model is priced at right now and where that number came from, resolving in the gateway's own order: an account price override, then the pricing configured on the model, then the vendored catalog, then `source="none"`. `POST /api/v1/ai-models/{id}/pricing/fetch` (`edit_ai_models`) reads a provider's published price (OpenRouter's public model list, or Alibaba Cloud Model Studio's native catalog on USD sites) and never writes it: a fetched number is confirmed by a person through the price override endpoints before it changes what spend means. Both are account-scoped through the model, and a malformed stored price reads as unpriced rather than failing the page.
*   **Priced, Unpriced, and Zero:** each `usage_by_model` row carries `unpriced_request_count` (tokens spent with no price at all, so that cost is missing from every total), `zero_priced_request_count` (a price was applied and it was exactly zero, so nothing is missing), `failed_request_count`, and `last_request_at`. The split exists because a $0.00 total means two different things, and only one of them is an accounting hole. `unpriced_request_count` reuses the `get_gateway_usage_summary` condition, so the per-model counts sum to the account total.
*   **OSS API Surface:** Core endpoints should provide aggregate summaries, grouped breakdowns, raw usage drill-downs, and budget-health alerts derived from gateway account/flow limits. Core endpoints also provide runtime-session optimization recommendations, one-click apply, and replay verification (`preloop/api/endpoints/session_optimization.py`), with hosted-model analysis gateable via the `preloop.services.optimization_gating` authorizer hook. Enterprise billing plugin endpoints provide budget policy CRUD, enforcement, and model price override CRUD behind feature flags.
*   **OSS UX Boundary:** Open source should answer "how much was spent?", "who or what spent it?", and "which budget applies?" with enough drill-down to inspect the related session timeline.
*   **Enterprise UX Boundary:** Enterprise should answer "why was it spent?", "was it worth it?", and "how could it be optimized?" at scale with LLM-assisted reviews, anomaly detection, forecasting, showback/chargeback, credits/promotions, exports, and workflow automation.
*   **Default AI Model Use:** Enterprise session-value analysis should call the account's default AI model through the Preloop Gateway, producing an auditable meta-usage record for the evaluation itself. The analysis should reference redacted session summaries, gateway events, tool calls, approvals, and final outcomes rather than unrestricted raw prompts.
*   **Plugin Boundary:** Backend features beyond OSS summaries and budget-health tracking must live in Enterprise plugins under `./plugins/`, likely extending `plugins/billing/` for budget policy enforcement, pricing overrides, FinOps, credits, promotions, forecasting, exports, and value-review jobs. The shared frontend should gate those panels with feature flags.
*   **Budget Actions:** Core enforcement should continue to block or warn before upstream dispatch. Enterprise plugins can add escalations, Slack/mobile notifications, approval requirements for expensive calls, and post-hoc anomaly workflows.

## Cost per work item

`GET /api/v1/cost/work-items` (`view_cost`, console: the Work items tab on Spend, `/console/cost/spend?tab=work-items`)
reports spend per issue and pull request over a window, most expensive first.

*   **Contributors:** flow runs attributed by `issue_cost_execution` (assigned
    links only) and runtime sessions or runs with classified `work_ref` tool
    calls. A contributor's gateway spend in the window (customer predicate,
    served by `ix_api_usage_customer_session_keys`) is split evenly between
    the work items it touched, so no request counts twice.
*   **Grouping:** an issue joins the pull requests that close it (`work_ref`
    `closes`), the pull request the issue cost attribution tied to it, and a
    flow run that worked on both. An unnumbered create's closing references
    join a pull request only when the same contributor named exactly one in
    that repository.
*   **Drill-down:** per group the issues and pull requests (with state), and
    the agents, flows, models and people that drove the spend. A flow run's own
    principal is shown as one "Flow runs" row.
*   **Caps:** 20,000 rows per link source and 50,000 usage slices; reaching a
    cap sets `truncated`. `limit` (at most 200) and `offset` page the groups;
    totals always cover every group.

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
*   **Cycle-time definitions:** the report measures from the first agent run
    to the merge, never from ticket creation.
    *   `first_event_at` is the earliest start of any execution attributed to
        the issue, across all flows. It is not the ticket's creation time:
        time a ticket waited before its first run is not measured.
    *   `pr_opened_at` is when the linked pull request was opened, with the
        provenance in `pr_opened_at_source` (`forge`, else `bind`, else
        `run_end`; see below).
    *   `approved_at` is the earliest recorded approval event (GitHub review
        with state approved, GitLab `merge_request_approved`, Bitbucket Cloud
        `pullrequest:approved`) at the time the forge reported for it. It is
        not verified mergeability: required checks, approval counts and
        branch restrictions are not evaluated, so a pull request can be
        approved and still not mergeable.
    *   `merged_at` is the earliest recorded merge event (Bitbucket Cloud
        `pullrequest:fulfilled` uses the pull request's `updated_on`).
    *   `first_event_to_pr_opened_hours`, `pr_opened_to_approved_hours` and
        `approved_to_merged_hours` are elapsed UTC hours between those
        milestones, rounded to two decimals, blank when a milestone is missing
        or the two are out of order. No interval runs from ticket creation,
        and approval-to-merge is not a time-to-mergeable figure. Ticket
        creation to an observed ready-for-merge state is a separate,
        explicitly scoped metric.
    *   Redelivered or out-of-order webhooks never move a milestone: the
        earliest approval and merge times win, and approval and merge events
        are never counted as executions.
*   **Report:** `GET /api/v1/cost/by-issue` filters issues by first event
    time (`start_date` inclusive, `end_date` exclusive) and shows their
    lifetime totals. An issue whose first run started before the period is
    excluded even when its merge falls inside it. A flow filter restricts
    cost, tokens, runs and execution ids to that flow's executions; the
    milestones and intervals stay those of the whole issue. The per-project and per-flow
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
    `unknown_cost_run_count`, `attributed_cost_usd`, then the readiness
    columns and, last, `failure_categories`: failed runs of the issue per
    `failure_category` as `category:count` pairs joined by `;` (for example
    `result_schema_invalid:2;timeout:1`). The JSON export carries the same
    counts as a `failure_categories` object, and execution drill-down rows
    carry each run's `failure_category`.
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
*   **Worked example (synthetic):** a Jira ticket `REC-7` created at 08:00
    UTC, a Bitbucket Cloud pull request and five runs, as recorded by the
    reconciliation fixture `backend/tests/issue_cost_reconciliation.py`:

    | Time (UTC) | Event | Cost |
    | --- | --- | --- |
    | 08:00 | Ticket created (not measured) | |
    | 09:00 | Implementation run starts and fails | 1.00 |
    | 09:20 | Distinct retry publishes the pull request | 0.25 |
    | 10:00 | Bitbucket `created_on` (Preloop bound it at 10:05) | |
    | 10:30 | Review run, triggered by `pullrequest:created` | 0.50 |
    | 11:00 | Repair turn resuming the review | 0.10 |
    | 11:30 | Seat-backed review run, no per-run price | null |
    | 12:00 | `pullrequest:approved` (delivered twice, after the merge) | |
    | 13:00 | `pullrequest:fulfilled` (delivered twice) | |

    The issue row reports `first_event_at` 09:00, `pr_opened_at` 10:00 with
    source `forge`, intervals 1, 2 and 1 hours, `estimated_cost` 1.85 from
    four priced runs, `run_count` 5, `failed_run_count` 1, `cost_coverage`
    `partial` and a null `attributed_cost_usd`. A missing Jira original
    estimate stays blank, and a story points field set to 0 reports 0. The
    08:00 to 12:00 span is never reported: it would claim a mergeable time
    the report does not observe.
*   **Reconciliation procedure:** for one issue, export JSON and CSV with the
    same filter. Check that the JSON `execution_ids` are exactly the runs you
    expect (one per unique execution; a retry is its own run), that
    `estimated_cost` equals the sum of their known costs and the run counts
    match, that the per-flow and per-project summaries add up to the rows,
    and that the unassigned bucket holds the runs that could not be tied to
    one issue. Compare the four timestamps (UTC offsets included), the three
    intervals and the blank or null cells between the two exports. A run
    whose `estimated_cost` is null is counted in `unknown_cost_run_count`,
    not as zero.

## Spend outlier alerts

`preloop.services.spend_outliers` flags a developer or session whose spend
departs from the usual pattern. It reads gateway `ApiUsage` rows
(`action_type='model_gateway'`, replay validation and governance analysis excluded) grouped by user,
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
The weekly governance report runs deterministic checks by default without analysis charges. Optional model analysis follows the account override or the global default (off in 1.0.0, planned to return in 1.1.0); see the [settings reference](../weekly-report-v3.md) and [governance review guide](https://docs.preloop.ai/guide/governance-review/).

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


Governance analysis remains visible through purpose-targeted reads for its own
allowance and the weekly policy section. It does not consume customer budget
buckets, monthly usage observations or ingest quotas. The customer covering
indexes apply that exclusion in their partial predicate. The older replay-only
indexes cannot provide an index-only customer read because evaluating the
additional governance filter requires `meta_data`, which they do not include.

[Reviewed price feeds](../guide/model-price-refresh.md) update the generic model
map and Alibaba's dedicated regional tariff store in each API, gateway, and
worker process. Alibaba estimates retain input tiers and distinct implicit,
explicit, and cache-creation rates. Native discovery keeps credentials on their
documented host; workspace routes can use reviewed regional prices without a
cross-host credential transfer. Weekly review produces a PR with source evidence
and verification gates. Feed ingestion never rewrites historical usage.

## Analytics history and reporting reads

Cloud analytics history is resolved through a billing plugin service at reporting boundaries. Physical retention preserves longer subscription promises through account-locked CRUD updates and remains separate from audit/evidence policy and live governance. See [Cloud analytics history and stored records](../guide/flows/evidence-storage.md#cloud-analytics-history-and-stored-records).

Customer-facing gateway reports exclude replay validation and every
`governance_` purpose, including account-funded analysis. Explicit purpose-scoped
budget reads retain analysis spend. Dedicated customer covering indexes preserve
index-only cost and model aggregates; the weekly policy section reports review
cost separately from customer activity.

The Cost console shares one summary between totals, the initially selected
tab and imported usage; settings and later tabs load independently. Exact-window
summary aggregates use a bounded 60-second cache per worker. Selective reporting
queries and per-section loading states are described in
[Progressive reporting](#progressive-reporting).
Console catalogue lists use typed `/tools/summary` and `/flows/summary` views
instead of downloading tool schemas and flow prompts/configuration. Full
`/tools` and `/flows` responses retain their existing defaults for editors and
API clients. Flow summary statistics are opt-in (`include_stats=true`) and use
the same period and owned-flow aggregates as the full list, plus each flow's
`last_run` (id, status, times, subject) so the Flows list links the run without
fetching executions. Policies reads `/tools/rules`, each tool's identity and
access rules without descriptions or schemas, and the Tools list asks
`/tools/stats?include_agents=false`, which skips the per-agent breakdown and
its query. Both account gateway
and Cost summaries support selecting repeated `breakdown` sections; callers
needing only totals set `include_breakdown=false`, which skips the unused
aggregations. The grouped audit timeline resolves correlations and approval
lifecycle through account-scoped partial JSON-expression indexes. Their
migration builds indexes concurrently and repairs interrupted invalid builds.
Explicit concurrent-index blocks have bounded lock and statement budgets and
restore the migration session's short blocking-DDL timeout afterwards. Operation
durations and cancellation diagnostics identify contention without extending
the deployment deadline. See [schema migrations](../operations/schema-migrations.md).

## Cost per issue coverage

Cost and cycle time per tracker issue are rolled up across flows into their own
tables. Each issue row, summary and unassigned bucket also states `cost_coverage`
(`complete`, `partial`, `unknown`) and how many of its runs carry a cost, so an
unpriced subscription-backed run is never read as a free ticket; see
[Cost per issue](#cost-and-cycle-time-per-tracker-issue).

## Hosted model visibility and request billing attribution

The console loads entitled hosted models and durable allowance balances through
the capability-gated `/account/hosted-models` API. Models, Cost, and Plan share
the allowance display, including outstanding holds and the monthly reset date;
one-time credit has no reset. Subscription details live on Plan, while Account
links to that view. Gateway events preserve the billing path and actual resolved
model row captured at request time, so later alias changes cannot relabel history.

Prepaid credit is a separate ledger (`hosted_credit_account`,
`hosted_credit_ledger`, `hosted_credit_purchase`) with one lot per purchase.
`crud.hosted_spend.reserve` accepts optional `credit_available` and
`price_multiplier` arguments. The default `credit_available=None` reads and
writes no credit rows, so existing callers retain the allowance-only path and
the same exhaustion error. Missing or empty credit wallets also retain that
behavior. Consumption is inert until credit grants exist and the caller opts in;
the Enterprise plugin owns activation, purchases and refusal messages. An absent
`hosted_credit_topup_setting` row means auto top-up is off.

With credit enabled, reserve takes the account row lock and refreshes the credit
balance after acquiring it. A caller's earlier balance snapshot cannot authorize
spending. Monthly allowance pays first at provider cost; overflow is held in
customer dollars at the supplied multiplier. The allowance counters, credit hold
and reservation split share one transaction. A hold reserves aggregate credit,
not a particular purchase lot. Insufficient funds raise `HostedSpendExceededError`
before any reservation writes. The one-time Free allowance check still runs first.

Settlement takes the same account lock. Allowance absorbs up to the recorded
allowance reservation; the remaining actual cost uses the recorded multiplier.
Credit consumption walks lots by `(granted_at, id)`, oldest first, with one ledger
entry per lot carrying its purchase ID, provider cost and multiplier. Provider
cost shares sum to the full credit-funded provider cost at ledger precision.
Unused holds return to both buckets. Unknown provider cost keeps both holds in
`recovery_required`; held credit cannot be spent or expired. The expiry worker
writes off expired lots through CRUD, so they no longer contribute to the balance.

An actual charge above its reservation is recorded in full, including lotless
debt when purchases are exhausted. Credit balance may reach -$1.00. Any amount
below that floor is immediately compensated with a system `adjustment` noted
`below credit floor, absorbed`; this clears debt without creating a purchase lot.
The next grant clears negative balance first. Account totals reconcile to ledger
movements, and purchase remainders sum to the balance whenever it is nonnegative.
New credit-funded requests stop when available credit cannot cover them; provider
calls already in progress are never interrupted for balance exhaustion.
Above the floor, negative credit does not block a request fully covered by its
allowance. At or below the floor, the explicit hard stop still applies, including
after the monthly allowance resets. Reservation-linked credit holds require a
recorded multiplier. A legacy unpriced hold releases without a price when no
credit charge is due, including proven non-dispatch. For a positive credit charge,
an operator supplies the verified historical multiplier through
`recover_verified_cost(..., price_multiplier=..., evidence=..., apply=True)`.
Preview changes nothing; apply records the price and evidence with settlement in
one transaction. An already recorded multiplier cannot be changed.

CRUD only flushes. The caller commits reserve and dispatch marking before
provider I/O, then commits settlement in a separate transaction, or rolls back
all changes on failure. Lock order is account, credit wallet, then purchase lots
in FIFO order. Holds, releases, consumption, floor compensation and terminal
reservation status remain within these transaction boundaries. Repeated operation
keys cannot redispatch; repeated settlement cannot debit twice.

`preloop models list` separates your models from Preloop-hosted models and reports
the allowance when supported. Before adding a system alias, operators can run
`preloop models check-hosted-alias ALIAS` for aggregate collision warnings.
The lookup exposes no account identifiers and does not change model routing.

## Cache-inclusive input tokens

`ApiUsage.prompt_tokens` is cache inclusive for every provider: it counts the
input read from and written to the prompt cache as well as the uncached part,
so `uncached = prompt - cache_read - cache_creation` holds per row and in every
aggregate. Anthropic reports `input_tokens` without the cache, so the gateway
adds `cache_read_input_tokens` and `cache_creation_input_tokens` back when it
records a raw Anthropic payload (`usage_token_details.anthropic_cache_input_tokens`);
litellm's OpenAI-shaped usage is already inclusive and is left alone. The raw
provider usage stays in `meta_data.usage_details`, and cost is priced from it,
so cached input keeps the cache rate. The remaining clamp at zero only absorbs
provider rounding.

Existing rows were backfilled rather than guarded at read time
(`20261106_anthropic_cache_input`): aggregates sum `prompt_tokens` from
covering indexes that never read `meta_data`, so a read-time guard could only
be a heuristic over sums, which a mix of Anthropic and other traffic defeats.
The backfill touches only rows whose raw usage is the Anthropic shape and whose
`prompt_tokens` still equals the raw `input_tokens`, and records what it added
in `meta_data.input_cache_backfill` for an exact downgrade. Because the
migration runs before the new pods while the previous release keeps serving,
each API replica runs the same conversion once more over the last day of rows
15 minutes after startup (`services/anthropic_cache_input_repair.py`); an
advisory lock lets one pass run at a time and the update re-checks each row,
so concurrent replicas never convert a row twice.
`estimated_cost` is not repriced: catalog-priced rows were already priced from
the raw usage, while rows priced by a configured price or the DeepSeek native
tariff keep the old undercount of their uncached input. Activity rollup
buckets and budget windows written before the change keep their original
token counts.
