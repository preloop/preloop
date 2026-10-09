# Cost Analytics & Budgets

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop turns model gateway telemetry into explainable spend. The console **Cost** view helps operators answer how much was spent, who or what spent it, and whether budgets are healthy.

---

## Canonical Ledger

`ApiUsage` is the source of truth for model calls. Each record can include:

- token counts and estimated cost, including cache-read, cache-creation, and reasoning token breakdowns
- provider and model alias
- runtime principal, managed agent, runtime session, flow, and API key attribution
- `currency`, `cost_source` (how the cost was derived), `usage_source`, and a retry flag
- timestamps for trend charts and drill-down

The gateway normalizes cache and reasoning counts from three provider usage shapes: OpenAI Chat Completions (`prompt_tokens_details.cached_tokens`, `completion_tokens_details.reasoning_tokens`), OpenAI Responses (`input_tokens_details.cached_tokens`, `output_tokens_details.reasoning_tokens`) and Anthropic Messages (top-level `cache_read_input_tokens` / `cache_creation_input_tokens`). An explicit `0` is recorded as zero; a missing, negative, boolean or malformed count is recorded as unknown (`NULL`), so an empty cache column never means "no caching" by itself. The raw provider payload is kept in `meta_data.usage_details`. Responses rows recorded before this was supported can be repaired from that payload with `python scripts/repair_usage_token_details.py` (dry run by default, `--apply` writes, `--reprice` re-prices the repaired rows).

Spend summaries and breakdowns in open source are derived from this ledger plus gateway budget state.

---

## How model spend is priced

Estimated cost on each `ApiUsage` row is computed from the row's token counts and a price for that provider/model, resolved in this order:

1. **Account price override**: if the account has an active override for the model (see below), its per-token prices and fixed request fee apply.
2. **Vendored price catalog**: otherwise Preloop uses a snapshot of public model prices vendored with the release (`services/data/model_prices.json`). Pinning the snapshot per release keeps default pricing deterministic instead of depending on whichever pricing library version is installed. Maintainers regenerate it with `scripts/update_model_prices.py`.

### Per-account price overrides and currency

An account can override input/output/cache-token prices, a fixed request fee, and an effective-date range for a specific model. Overrides carry a `currency` and, when the currency is not USD, an `fx_rate_to_usd` conversion rate: Preloop stores all costs in USD but preserves the original currency and unconverted prices for display and audit. Configuring overrides is an Enterprise billing feature; see [Enterprise Billing & FinOps](../integrations/enterprise-billing.md).

### Repricing historical usage

Because prices change and overrides can be added after the fact, Preloop can recompute `estimated_cost` for past `ApiUsage` rows from their stored token counts using the current catalog and overrides. Repricing fills rows that were recorded unpriced (e.g. a model not yet in the catalog) and applies a new or edited override retroactively. It is **analytics-only**: budget-spend buckets charged at request time are not rewritten, and rows priced under a flat `subscription` (whose $0 cost is correct) are skipped.

---

## OSS vs Cloud and Enterprise

| Question | OSS | Cloud and Enterprise (billing and RBAC plugins) |
|----------|-----|-------------------------------------------------|
| How much was spent? | Cost overview, trends, grouped breakdowns | Same, plus provider billing reconciliation |
| Who spent it? | Attribution by model, agent, session, API key | Same, plus per-user and per-team budgets |
| Which budget applies? | Account, flow, API-key and managed-agent budgets with hard limits | Same, plus limit notifications and negotiated price overrides |
| Why was it spent? | Session timeline and gateway event drill-down | Same |
| How to optimize? | [Full session-optimization loop](../cost/session-optimization.md): deterministic and BYOK analysis, one-click apply, replay verification | Same, plus metered hosted-model analysis |

The shared frontend gates Cloud and Enterprise panels behind feature flags such as `billing`. Session optimization itself ships in the open-source core.

---

## Console Surfaces

- **Cost overview**: spend, tokens, and request volume over selectable ranges
- **Breakdowns**: by model, provider, managed agent, runtime session, or API key
- **Budget health**: soft warnings and hard-limit state from gateway budgets
- **Session drill-down**: jump from a cost row into the runtime session timeline

Enterprise accounts can additionally configure budget policies and model price overrides from the same area when the billing plugin is enabled. See [Enterprise Billing & FinOps](../integrations/enterprise-billing.md).

---

## Budget Enforcement

Core enforcement happens **before upstream dispatch**:

- account-level and flow-level gateway budgets
- subject-scoped allowed-model checks

Denied calls never reach the provider.

!!! cloud "Cloud and Enterprise"
    Per-user and per-team budgets and soft and hard limit notifications need the RBAC plugin.

---

## Related

- [Session Optimization](../cost/session-optimization.md)
- [AI Model Gateway](model-gateway.md)
- [Runtime Sessions](runtime-sessions.md)
- [Enterprise Billing & FinOps](../integrations/enterprise-billing.md)
- [What Preloop includes](../functionality.md)
