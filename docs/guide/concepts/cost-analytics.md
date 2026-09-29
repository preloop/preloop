# Cost Analytics & Budgets

Preloop turns model gateway telemetry into explainable spend. The console **Cost** view helps operators answer how much was spent, who or what spent it, and whether budgets are healthy.

---

## Canonical Ledger

`ApiUsage` is the source of truth for model calls. Each record can include:

- token counts and estimated cost, including cache-read, cache-creation, and reasoning token breakdowns
- provider and model alias
- runtime principal, managed agent, runtime session, flow, and API key attribution
- `currency`, `cost_source` (how the cost was derived), `usage_source`, and a retry flag
- timestamps for trend charts and drill-down

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

## Open Source vs Enterprise

| Question | Open source | Enterprise (billing plugins) |
|----------|-------------|-------------------------------|
| How much was spent? | Cost overview, trends, grouped breakdowns | Same, plus exports and FinOps workflows |
| Who spent it? | Attribution by model, agent, session, API key | Chargeback/showback and team views |
| Which budget applies? | Budget-health alerts from gateway limits | Configurable budget policies and enforcement |
| Why was it spent? | Session timeline + gateway event drill-down | LLM-assisted session value reviews |
| How to optimize? | [Full session-optimization loop](../cost/session-optimization.md): deterministic + BYOK analysis, one-click apply, replay verification | Metered hosted-model analysis, anomaly workflows |

The shared frontend gates Enterprise-only panels behind feature flags such as `billing`. Session optimization itself ships in the open-source core.

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

Denied calls never reach the provider. Enterprise plugins can add escalations, notifications, and post-hoc anomaly handling.

---

## Related

- [Session Optimization](../cost/session-optimization.md)
- [AI Model Gateway](model-gateway.md)
- [Runtime Sessions](runtime-sessions.md)
- [Enterprise Billing & FinOps](../integrations/enterprise-billing.md)
- [Architecture](../architecture.md)
