# GitHub Copilot usage import

Copilot requests go from the editor straight to GitHub, so the gateway never
sees them. This import reads what GitHub reports about seats, premium-request
spend and per-user usage, and shows it in the **Copilot** tab of the Cost
page.

Every figure in that tab carries the marker **Not metered by the gateway**.
Imported Copilot rows never count toward gateway usage, budgets, budget
alerts, ingestion quota or provider reconciliation drift.

## What is imported

| Data | GitHub route | Stored |
| --- | --- | --- |
| Seat count and plan | `GET /orgs/{org}/copilot/billing` | Seat total (`seat_breakdown.total`) and plan type, once per day |
| Assigned seats | `GET /orgs/{org}/copilot/billing/seats` | Login, `last_activity_at`, `last_activity_editor`, `created_at`, `pending_cancellation_date`, plan type. Names and emails are dropped. |
| Premium-request spend | `GET /organizations/{org}/settings/billing/premium_request/usage` (and `/enterprises/{enterprise}/...` as a fallback) | One row per day, user and model: `netAmount` as the cost, plus quantities, price per unit and SKU |
| Usage metrics | `GET /orgs/{org}/copilot/metrics/reports/users-1-day` | Per user and day: interaction and code-generation counters, editors, features, and counters per model |

All calls send `Accept: application/vnd.github+json` and
`X-GitHub-Api-Version: 2026-03-10`. The legacy `/orgs/{org}/copilot/metrics`
route is not used. No code path reads prompt or completion text: the usage
metrics report is reduced to an allowlist of counters before it is stored,
and its signed download links are fetched without the GitHub token.

Rows land in the provider billing snapshot table with
`provider = copilot`, `usage_source = imported` and, for premium-request
spend, `cost_basis = reconciled` (it is GitHub's billed `netAmount`, not an
estimate).

## Connect

Open **Cost > Copilot** and enter:

- **GitHub organization**: the organization login.
- **Organization token**: a token from an organization owner. A classic token
  needs `manage_billing:copilot` or `read:org`. Seats need an organization
  owner. Premium-request spend on the organization route needs an
  organization administrator. The usage metrics report needs an owner or a
  role with **View Organization Copilot Metrics**, and the enterprise
  **Copilot usage metrics** policy must be enabled.
- **Enterprise slug and enterprise billing token** (optional): used only when
  the organization route refuses per-user premium-request data, which is
  common for organizations whose billing is owned by an enterprise. The
  enterprise call is filtered to your organization.
- **Seat price per month** (optional): your contract price. GitHub does not
  return a seat price, so none is fetched. With a price, the tab shows
  `price x seats` as a monthly seat estimate. Leave it empty (or clear it) and
  the tab shows seats with no dollar seat line, never `$0`.

Tokens are stored encrypted and are never returned by the API.

The same can be done with the API (`manage_budgets` permission):

```bash
curl -X PUT "$PRELOOP_URL/api/v1/cost/copilot/connection" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" -H "Content-Type: application/json" \
  -d '{"organization": "example-org", "token": "<github token>", "seat_price_monthly": 19}'
curl -X POST "$PRELOOP_URL/api/v1/cost/copilot/connection/sync" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"
curl "$PRELOOP_URL/api/v1/cost/copilot?start_date=2026-09-01T00:00:00Z" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"
```

`DELETE /api/v1/cost/copilot/connection` removes the connection and its
tokens. Imported history stays.

## Schedule and freshness

The scheduler queues `ingest_copilot_usage` once a day
(`COPILOT_USAGE_SYNC_ENABLED`, default on). GitHub can take up to two full
UTC days to settle a day's usage, so the newest day imported is three days
before the current UTC date. A run after a gap catches up at most seven days.
Each day's rows replace that day's earlier rows, so running the import again
(or pressing **Sync now**) never duplicates spend. Seats are a snapshot of
the day the import ran.

GitHub rate limits (429, or 403 with `Retry-After` or
`x-ratelimit-remaining: 0`) are retried up to three times, waiting what GitHub
asks for (at most 60 seconds per wait). If the limit outlasts the retries,
the run fails with that message and the next run resumes from the last fully
imported day.

Setting `is_active` to `false` on the connection pauses scheduled imports.
**Sync now** is then refused with 409 and the tab offers **Resume imports**.
Saving the connection without `is_active` keeps its current state.

## When per-user data is not available

Per-developer premium-request spend is tried in this order:

1. The organization route, one call per seated user whose seat was active on
   or after that day (seats never active are skipped). The organization total
   for the day is then read once, and spend no queried user explains (for
   example a developer whose seat was removed before the import) is kept as
   **Not matched to a current seat**, so the day still adds up to GitHub's
   bill. If that total is refused, the tab shows a warning instead.
2. On 403 or 404, the enterprise route for the same users, when an
   enterprise slug is configured.
3. Otherwise the organization total without a user filter. The tab then
   shows the organization total and the reason per-developer spend is
   missing. Spend is never split across developers by guesswork.

If none of these can be read, the import fails with an explicit message on
the tab (which route refused, and which role it needs). A missing usage
metrics report does not fail the import: the tab explains why it is missing
and the model mix then has no request-count fallback for that window.

When GitHub reports more seats than the seat list returned, the import still
succeeds and the tab shows a warning; those developers' spend lands in
**Not matched to a current seat**. The tab only shows data for the
organization currently connected; rows from an earlier organization are kept
but not mixed in.

## Model mix

The model mix per developer is the share of premium-request `netAmount` per
model. When a developer has no billed premium requests in the window, the
share of user-initiated interactions per model from the usage metrics report
is shown instead, labelled as based on request counts.

## Stored for spend alerts

Premium-request rows are stored per day, per user and per model with the
billed `netAmount`, so a later spend alert can compare developers and models
without calling GitHub again. The alert itself is not part of this import.
