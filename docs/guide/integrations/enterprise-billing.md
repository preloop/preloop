# Enterprise Billing & FinOps

Preloop Cloud and Preloop Enterprise extend the open-source **Cost** experience
with billing plugins that add budget policy management, negotiated model
pricing, session optimization, subscriptions, and commercial FinOps workflows.

Open source still owns the canonical `ApiUsage` ledger, OSS cost summaries, and
gateway budget-health signals. Enterprise features are delivered through the
`billing` plugin and gated in the shared console by feature flags.

---

## Feature flags

When the billing plugin is enabled, the console exposes additional panels when
these flags are true:

| Flag | Enables |
|------|---------|
| `billing` | Budget policy CRUD, billing subscription UI, enterprise Cost panels |
| `model_price_overrides` | Account-scoped negotiated pricing for gateway cost estimates |
| `session_optimization` | Always `true` since 0.12.0 — [session optimization](../cost/session-optimization.md) ships in the open-source core. Cloud meters hosted-model analysis at request time (HTTP 402 via the authorizer hook), never by hiding the UI. |

Self-hosted Enterprise deployments link EE plugins with
`./scripts/link_ee_plugins.sh` before installing Preloop.

---

## OSS vs Enterprise boundaries

| Capability | Open source | Enterprise billing plugin |
|------------|-------------|---------------------------|
| Spend summaries & trends | `GET /api/v1/cost/summary` | Same data, richer UI filters |
| Budget health from gateway limits | Yes | Yes, plus configurable policies |
| Budget policy CRUD | No | `GET/POST/PUT/DELETE /api/v1/budget/policies` |
| Model price overrides | No | `/api/v1/billing/cost/pricing-overrides` |
| Session optimization | Yes — analyze / apply / replay at `/api/v1/billing/cost/runtime-sessions/{id}/...` ([details](../cost/session-optimization.md)) | Same, plus metered hosted-model analysis |
| Subscriptions & Stripe | No | `/api/v1/billing/subscription`, checkout, portal |
| Session value reviews (AI) | Planned | Planned under `/api/v1/billing/cost/value-reviews` |
| Forecasting & anomalies | Planned | Planned under `/api/v1/billing/cost/forecast`, `/anomalies` |

Enterprise analysis jobs route their own model calls through the Preloop Gateway
so meta-usage is auditable and respects account budgets.

---

## Budget policies

Enterprise budget policies attach spend limits to subjects such as accounts,
API keys, flows, or managed agents. The **EnterpriseBudgetEnforcer** applies
them at gateway preflight time alongside OSS gateway limits.

Configure policies from the console **Cost** view when `billing` is enabled, or
via:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://preloop.example.com/api/v1/budget/policies
```

Open-source deployments use a no-op enforcer: gateway account/flow limits still
apply, but configurable policy CRUD is unavailable.

---

## Model price overrides

Negotiated or self-hosted contract pricing can diverge from public provider
defaults. Overrides store per-account rates (per token type, optional request
fees, currency, effective dates) so `ApiUsage` estimated cost reflects your
contract rather than list price.

**Non-USD contracts:** set the override `currency` and an `fx_rate_to_usd`
conversion rate. Preloop keeps all stored costs in USD (so budgets and
cross-account analytics stay comparable) while preserving the original currency
and unconverted prices for display and audit. Editing an override can be applied
retroactively via [usage repricing](../concepts/cost-analytics.md#repricing-historical-usage).

Manage overrides at `/api/v1/billing/cost/pricing-overrides` or from the Cost view
when `model_price_overrides` is enabled.

---

## Provider billing reconciliation

Estimated `ApiUsage` cost is only ever an estimate. Provider billing
reconciliation closes the loop by pulling each provider's own billing/usage
actuals and comparing them against Preloop's estimates so you can see and
explain estimate-vs-actual drift.

- A per-account **billing connection** links to a provider's billing/usage API;
  the admin credential is stored via the Secret Service, never in plaintext.
- Fetched actuals are persisted as idempotent **snapshots** so re-fetching the
  same period does not double-count.
- Snapshots are reconciled against estimated spend for the same window.

The shared `ProviderBillingConnection`/`ProviderBillingSnapshot` tables live in
the open-source models package (one alembic tree), while the fetchers and API
endpoints ship in the Enterprise billing plugin.

---

## Session optimization

Since 0.12.0 the full optimization loop — analyze, one-click apply, replay
verification — ships in the open-source core and is documented at
[Session Optimization](../cost/session-optimization.md). What Cloud/Enterprise
add on top:

- **Metered hosted-model analysis** — LLM-assisted analysis on operator-hosted
  built-in models is compute the operator pays for, gated at request time via
  the `optimization_gating` authorizer hook (denials return HTTP 402).
  Deterministic and BYOK analysis are never gated.
- Session value reviews and anomaly workflows (planned surfaces above).

---

## Subscriptions and usage metering

Hosted Preloop and Enterprise self-hosts can enable Stripe-backed plans defined
in the billing plugin's `plans.yaml`. The plugin tracks monthly usage meters,
enforces plan limits, and exposes checkout and customer-portal flows.

This is separate from gateway spend analytics: subscriptions gate product features
while `ApiUsage` records model consumption.

---

## Related

- [Cost Analytics & Budgets](../concepts/cost-analytics.md)
- [AI Model Gateway](../concepts/model-gateway.md)
- [Subject-Scoped Governance](../concepts/subject-scoped-governance.md)
- [API Reference](../api.md)

!!! info "Enterprise access"
    Contact [sales@preloop.ai](mailto:sales@preloop.ai) for Preloop Enterprise
    licensing, or enable the enterprise plugins in your self-hosted deployment.
