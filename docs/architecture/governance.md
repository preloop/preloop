# Subject-Scoped Governance

Editions: OSS. Contributor documentation for this repository.

Governance is applied to the concrete subject using the platform, not only the parent account. This chapter covers subject-scoped configuration, tool access rules, and tool output filters.

## Subject-Scoped Governance
*   **Purpose:** Apply governance decisions against the concrete subject using the platform, not just the parent account.
*   **Scope Chain:** Resolution currently walks the active API key first, then the linked managed agent, and finally falls back to account defaults.
*   **Configuration Surface:** Subject-scoped governance can carry `allowed_models`, per-model budget metadata, ordered tool access rules, and `tool_enabled_overrides`.
*   **Enforcement Points:** The same subject context is propagated through MCP tool listing, policy evaluation, and model gateway budget checks so one runtime token sees only the intended tools and models.
*   **Primary Use Case:** Managed agent owners can grant a broad account-level tool catalog while restricting one enrolled desktop/CLI runtime to a tighter set of tools and models.

## Access Rules System

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

**Evaluation:** Rules are evaluated at runtime in `DynamicFastMCP._evaluate_policy()`: the first matching enabled rule determines the action. If no rules match, the tool call is allowed by default and audited in every edition.

**Access Rule API Endpoints:**
- `POST /api/v1/tool-configurations/{config_id}/access-rules` - Create access rule
- `PUT /api/v1/access-rules/{rule_id}` - Update access rule
- `DELETE /api/v1/access-rules/{rule_id}` - Delete access rule

## Tool Output Filters

Account-scoped `ToolOutputFilter` rules strip named top-level fields from MCP tool JSON results on the proxy hot path before results reach the calling agent, trimming wasted context tokens. The model, CRUD layer, and proxy application live in the OSS core; the Enterprise billing plugin exposes `/api/v1/billing/cost/output-filters` CRUD and the Console tools editor provides a filter dialog (also reachable from session-optimization suggestions). Persistent per-tool cost findings are tracked as `ToolCostFlag` rows and surfaced in the agent detail view.

## Weekly governance review

The OSS store for the weekly governance review is five account-scoped tables: `governance_policy_document`, `governance_review_settings`, `governance_session_digest`, `governance_review_run`, and `governance_finding`. The Enterprise plugin performs the review. The OSS scheduler only publishes the work, and each worker task no-ops when no Enterprise handler is registered.

`GOVERNANCE_REVIEW_ENABLED` (default true) registers two cron jobs. A scheduler restart does not fire either one:

*   `governance_session_digest_sweep` at minute 0 of every hour, UTC.
*   `governance_weekly_review` on Monday at 07:00 UTC.

The weekly report is enabled by default and runs deterministic checks for every account, including accounts with no review settings row. Account report opt-outs and recipient preferences still apply. Quiet exception weeks send no email.

Model analysis is off by default in 1.0.0 and is planned to return in 1.1.0. The global `GOVERNANCE_REVIEW_MODEL_ANALYSIS_ENABLED` setting defaults to `false`. Each account's `governance_review_settings.model_analysis_override` is nullable: `true` enables analysis, `false` disables it, and `null` inherits the global default. An account value wins even when the global default differs. Account admins choose **Model analysis for this account** on the review settings page; platform admins use the same selector on the account details page. This permits testing on one account while the global default remains off.

When the effective value is off, no analysis model is resolved, no model budget is reserved, and no session work is queued. Deterministic reports are complete and say “Model analysis is not enabled.” The same gate covers session digests, semantic policy findings and follow-up checks, the AI usage policy summary, and optional exception-email synthesis. The latter retains its separate account opt-in and caps. When analysis is enabled, existing model selection, consent, allowance admission and daily/weekly limits apply; unknown allowances refuse summaries, and exhausted allowances do not prevent deterministic findings.

The hourly task stays scheduled so an account override can enable it even when the global default is off. Disabled accounts are skipped before session work is queued. See the [settings reference](../weekly-report-v3.md).

Operators: see the [weekly governance review guide](https://docs.preloop.ai/guide/governance-review/) for what is sent to the analysis model, the cost rules, and the Helm values.

## Policy draft simulation

`POST /api/v1/policies/evaluate` accepts one sample tool call and a stored
policy, unsaved tool rule, or draft YAML. It uses the firewall's shared rule
evaluator with recording disabled and reports the ordered checks, winning
rule, overlapping rules and condition errors. Model text is an optional separate
sample; model I/O and sensitive-data evaluators also disable their audit and
notification hooks. Simulation does not dispatch tools, create approvals or
record usage. The console's `policy_simulation` capability exposes draft testing
in the rule dialog and YAML editor. Paths are evaluated as submitted, without
normalization, so operators can test traversal and repeated-slash samples.

Console monetary values use the shared USD formatter, with exact precision in tooltips; non-USD provider invoices retain their denomination through shared currency helpers. Server timestamps use the UTC-aware date utilities. Wide tables use the shared `table-scroll` stylesheet in both document CSS and Lit shadow roots so content scrolls within its container at phone widths. Full list-controller migration remains incremental.

Model-price override edits and provider-price fetches require `edit_ai_models`; repricing and budget controls require `manage_budgets`. User role assignment requires `assign_roles`, independently of user management. Console capability copy is based on plan availability, separately from viewer permissions.

## Governance findings console

Weekly report signals count human requests and decisions separately from automatic
policy approvals. Report evidence projects account-scoped gateway metadata through
CRUD, retaining daily spend (including registered imports), linked contributing
sessions, and per-tool schema costs with unknown tariffs represented as null.
`GET/PUT /api/v1/weekly-report/preferences` controls independent account and member
email delivery, defaults on, and audits changes in the same transaction. Account
changes require account administration; members can unsubscribe themselves without
changing approval notifications. The Enterprise digest enforces both switches.
The Enterprise automatic sender is a factual exception digest: it sends only
newly actionable or changed gateway failure and approval facts and sends
nothing in a quiet or unchanged week. Its ledger lives in
`models.crud.exception_digest` (migration `20261121_exception_digest`): one
claim per recipient and week, a committed dispatch claim before SMTP,
per-member material history, and an account-week synthesis reservation capped
at $0.02, two calls, 12,000 tokens and 20 seconds. `POST
/api/v1/weekly-report/deliveries/{delivery_id}/acknowledge/{exception_key}`
acknowledges a fact only for the member it was sent to and only while its
material state is current. Implicit governance analysis is off on every
edition until an account opts in explicitly.


The `governance_review` capability registers lazy `/console/governance` and
`/console/governance/findings/:findingId` views. They read the Enterprise plugin's
account-scoped runs and findings, with kind, severity, developer and week filters.
Coverage is explicit even before the first summary; partial runs show their stop
reason. Evidence links to sessions and individual audit events. Dismissal and
restore require `manage_policies` and visibility of the finding's kind.

The core governance CRUD supplies paged run history, permission-filtered finding
pages and segment counts over each run's half-open window. Segment availability
and the earliest retained summary date are derived from stored segments; reviewed
session counts and stop reasons come from the persisted run. No analyser is
required to browse fixture or existing findings.

The capability-gated runner policy console consumes the Enterprise provider’s `/account/runner-policy`, paginated `/account/runner-compliance`, and member-only `/account/runner-compliance/me` APIs. Policy editing lives in Account settings, the roster in Runners, and the member warning in the console shell. OSS without `runner_requirement_policy` makes no requests to these routes; server permissions remain authoritative.

Runner-bound local model endpoints use the existing authenticated runner websocket
and internal Core NATS routing between API and gateway workers. Provider I/O stays
inside gateway authorization, policy and audit processing. See
[the transport design](runner-local-models.md) and
[the operator guide](../guide/runner-local-models.md).

Native host execution additionally supports explicit private profiles for
Codex (`codex_cli`), OpenCode (`opencode`) and DeepSeek CLI (`deepseek`, using
the `dsh` executable). These remain dual-runtime agent types: without a host
profile they retain Docker routing. Capability matching, local `model_map`
selection and the `host_exec` completion protocol keep native leases separate
from Docker launch payloads. Credentials remain local; no implicit hosted
fallback is introduced. These new native adapters support fresh flows only,
with no initial native resume or publication. See the
[native profile guide](../guide/runners/quickstart-linux.md#codex-opencode-and-deepseek-native-profiles)
for governance requirements and acceptance limits.

Tool data policies also support CEL predicates on `tool.args` using `args`,
`tool.name` and `tool.server`. These reuse the sensitive-data policy store,
notice delivery and approval dispatch. Evaluation errors skip notify rules;
deny and approval rules fail closed. A bounded CEL program cache shares compiled
expressions between validation and evaluation without retaining request bindings.
The [back-office policy pack](../guide/policies/back-office-ai.md) demonstrates
portable server allowlists, write approvals and bulk-export checks.

The optional weekly outcome ranking uses account-scoped projections in
`models/crud/weekly_value.py` and a nullable forge evidence cache on
`IssueCostPullRequest` (`value_evidence`, `value_observed_at`). Migration
`20261023_weekly_value_cache` adds those fields without changing existing issue
attribution. The shared console's weekly outcome component is gated by the
`weekly_work_value` server capability and View cost permission. Attribution,
forge fetching, scoring and the API live in the commercial plugin; an OSS
server does not advertise or call them.

Weekly report settings live in core Account fields and the audited
`models.crud.weekly_report` transaction. `/api/v1/weekly-report/preferences`
keeps individual unsubscribe separate from independent account controls for
owner/admin delivery, member delivery, member policy findings and the optional
admin-only synthesis switch and model (account models only). The console no
longer shows the member policy findings switch because the factual digest
carries no policy findings; the API field remains for explicit previews. An unset
member-delivery field uses an optional edition service for its effective plan
default. The capability-gated Lit settings card lives in OSS. Report queries in
`models.crud.weekly_report_data` apply account and user attribution before
aggregation; report orchestration and delivery remain in the commercial plugin.
See [the settings contract](../weekly-report-v3.md).
