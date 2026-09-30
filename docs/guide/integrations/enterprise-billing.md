# Enterprise Billing & FinOps

Editions: Cloud, Enterprise. Nothing on this page ships in OSS unless it says so.

Preloop Cloud and Preloop Enterprise extend the OSS **Cost** views with a
billing plugin that adds negotiated model pricing, provider billing
reconciliation, metered hosted-model analysis, subscriptions, and notifications
on budget limits.

OSS owns the `ApiUsage` ledger, cost summaries, budgets and their enforcement.
The billing plugin is gated in the shared console by feature flags.

---

## Feature flags

When the billing plugin is enabled, the console exposes additional panels when
these flags are true:

| Flag | Enables |
|------|---------|
| `billing` | Plan and subscription UI (**Settings > Plan**) and the Cloud and Enterprise Cost panels |
| `model_price_overrides` | Account-scoped negotiated pricing for gateway cost estimates |
| `session_optimization` | Always `true`: [session optimization](../cost/session-optimization.md) ships in OSS. Cloud meters hosted-model analysis at request time (HTTP 402 via the authorizer hook), never by hiding the UI. |

---

## OSS vs Cloud and Enterprise

| Capability | OSS | Cloud and Enterprise |
|------------|-----|----------------------|
| Spend summaries and trends | `GET /api/v1/cost/summary` | Same |
| Budgets on the account, flows, API keys and managed agents | `GET/POST/PUT/DELETE /api/v1/budget/policies` | Same |
| Budgets on users and teams, soft and hard limit notifications | No (HTTP 402) | Yes |
| Repricing historical usage | `POST /api/v1/cost/reprice` | Same, plus pricing overrides |
| Model price overrides | No | `/api/v1/billing/cost/pricing-overrides` |
| Provider billing reconciliation | No | `/api/v1/billing/provider-billing/...` |
| Session optimization | Yes: analyze / apply / replay at `/api/v1/billing/cost/runtime-sessions/{id}/...` ([details](../cost/session-optimization.md)) | Same, plus metered hosted-model analysis |
| Subscriptions and Stripe | No | `/api/v1/billing/subscription`, checkout, portal |

Analysis jobs route their own model calls through the Preloop gateway, so
that usage is recorded and respects account budgets.

---

## Budget policies

Budget policies attach spend limits to a subject: the account, a flow, an API
key or a managed agent in every edition, plus users and teams on Cloud and
Enterprise. The gateway checks them before dispatch; a priced request that
would cross a hard limit gets HTTP 403.

Configure them from an agent's page in the console, or via:

```bash
curl -H "Authorization: Bearer $TOKEN" \
  https://preloop.example.com/api/v1/budget/policies
```

With the billing plugin, crossing a soft or hard limit also sends a
notification, and hosted models are priced from their tariff.

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

The full optimization loop (analyze, one-click apply, replay
verification) ships in OSS and is documented at
[Session Optimization](../cost/session-optimization.md). What Cloud/Enterprise
add on top:

- **Metered hosted-model analysis**: LLM-assisted analysis on operator-hosted
  built-in models is compute the operator pays for, gated at request time via
  the `optimization_gating` authorizer hook (denials return HTTP 402).
  Deterministic and BYOK analysis are never gated.

---

## Subscriptions and usage metering

Preloop Cloud and Enterprise self-hosts can enable Stripe-backed plans. The
billing plugin tracks usage against plan limits and exposes checkout and
customer-portal flows.

This is separate from gateway spend analytics: subscriptions gate product features
while `ApiUsage` records model consumption.

---

## Related

- [Cost Analytics & Budgets](../concepts/cost-analytics.md)
- [AI Model Gateway](../concepts/model-gateway.md)
- [Subject-Scoped Governance](../concepts/subject-scoped-governance.md)
- [API Reference](../api.md)

Contact [sales@preloop.ai](mailto:sales@preloop.ai) for Preloop Enterprise
licensing.
