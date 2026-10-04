# GitHub Copilot usage import

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Copilot requests go from the editor straight to GitHub, so the gateway never
sees them. This import reads what GitHub reports about seats, premium-request
spend and per-user usage, and shows it in the **Copilot** tab of the Cost
page. Which Copilot surfaces this import covers, and which ones the gateway
meters instead, is in [Copilot coverage](copilot.md).

Every figure in that tab carries the marker **Not metered by the gateway**.
Imported Copilot rows never count toward gateway usage, budgets, budget
alerts, ingestion quota or provider reconciliation drift.

## What these numbers do not say

GitHub reports seats and premium-request spend per user and per day. That
is the finest grain this import has, and it is a billing report, not an
activity log:

- **Not per ticket.** Nothing here is joined to a flow execution, a
  trigger event or a session. A row cannot be read as "this ticket cost
  X". A Copilot CLI flow on a private runner records the premium-request
  count Copilot reported for the run, and this import records what
  GitHub billed the seat that day; the two are not the same number and
  are not reconciled. See [Copilot coverage](copilot.md) for what each
  surface meters.
- **Not per request.** `netAmount` is a daily total per user and model.
  It is not a price for one prompt, one edit or one model call.
- **Not a budget for a run.** Nothing here is enforced. Only gateway
  usage feeds budgets and alerts.
- **Unknown is not zero.** A day GitHub has not settled yet, a seat that
  reports no premium requests, an organization that returns no per-user
  data, and a connection that has not been synced are all *unknown*.
  The tab shows that state, and "no data imported" for a range with
  nothing imported, rather than `$0`. The optional seat price behaves
  the same way.

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

## Spend alerts from imported premium requests

Premium-request rows are stored per day, per user and per model with the
billed `netAmount`, so the [spend outlier rules](../architecture/cost.md#spend-outlier-alerts)
can judge imported spend without calling GitHub again. Two things have to be
true before an imported dollar reaches a rule: the GitHub login must be
mapped to a Preloop user, and the day must be inside the replay horizon.

### Map GitHub logins to Preloop users

Imported rows name GitHub logins; the rules evaluate Preloop users. Nothing
is inferred from usernames, email addresses, seat lists or OAuth identities.
An operator writes each mapping explicitly, and only mapped logins count.

- A mapping belongs to the account and to the organization the connection
  points at when it is written. Change the connection to another
  organization and the old mappings stay stored but no longer apply; switch
  back and they apply again.
- A login maps to exactly one active user of the same account. Several
  logins may map to the same user; their spend is summed.
- Logins are compared the way GitHub compares them: trimmed and lowercased.
  Writing `Alice` after `alice` updates the one mapping.
- A deactivated user, a deleted user, a paused connection and a deleted
  connection all stop contributing immediately. Deleting the connection
  deletes its mappings; imported history stays.

Reads take `view_cost`; writes take `manage_budgets`, like budget limits.

```bash
# What is mapped for the connected organization
curl "$PRELOOP_URL/api/v1/cost/copilot/mappings" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"

# Map a login to a user of this account (repeat to change the user)
curl -X PUT "$PRELOOP_URL/api/v1/cost/copilot/mappings" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" -H "Content-Type: application/json" \
  -d '{"github_login": "jane-doe", "user_id": "<preloop user uuid>"}'

# Remove a mapping (any letter case)
curl -X DELETE "$PRELOOP_URL/api/v1/cost/copilot/mappings/jane-doe" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"

# How much of the stored spend the rules can see, and why the rest is left out
curl "$PRELOOP_URL/api/v1/cost/copilot/spend-coverage?start_day=2026-09-01&end_day=2026-09-28" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"
```

A `user_id` that is not an active user of your account is refused with one
fixed message, whether the id is unknown, deactivated or belongs to another
account. There is no console form for mappings yet; the API above is the
supported way to configure them.

### What the rules read, and what they do not

The rules see the **net billed amount** GitHub reported for each day, user
and model (`cost_basis = reconciled`), netted per user, day and model so a
credit lowers that day rather than being dropped. They never multiply a
request count by a list price. Only positive nets are evaluated; a day
billed at exactly `$0` is a known zero and cannot be a spike.

Everything else stays on the Cost page and never reaches a rule:

| Stored data | Why it is excluded |
| --- | --- |
| Seat snapshots, seat summary, the monthly seat estimate | Not premium-request spend |
| Usage metrics (request counts, editors, features) | Adoption data, not dollars |
| The organization total stored when GitHub refused per-user answers | Cannot be attributed to a user |
| **Not matched to a current seat** (the unattributed residual) | Cannot be attributed to a user, and is never assigned to the account owner |
| Rows for logins without a mapping | Unknown user |
| Rows without an amount, in a currency other than USD, or with a non-finite amount | Unknown is not zero |

`GET /api/v1/cost/copilot/spend-coverage` reports these as counts per reason
(`unmapped`, `unknown_amount`, `unsupported_currency`, `nonfinite_amount`,
`aggregate_only`, `unattributed`, `not_daily`) together with the mapped and
unmapped logins, so an operator can see which mappings are missing. It
returns no tokens and no stored payloads. `mapped_net_amount` is the sum of
the positive per user, day and model nets, which is exactly what the rules
evaluate; it is `null` when nothing was mapped, never `$0`. A user, day and
model whose credits exceed its charges nets at or below zero, reaches no
rule, and is reported apart as `credited_net_amount`.

### Replay horizon and delayed days

GitHub settles a day up to two full days later, so the newest day this
import stores is three days old, and a day can be corrected by a later
import. The daily spend outlier pass therefore does not stop at yesterday
for accounts with an active Copilot connection: it re-judges the **28 most
recent completed UTC days** (yesterday included) on every run. A day first
imported three days late is evaluated on the next pass. The finding keeps
that day as its spend day; its detection time is when the pass ran.

Replaying the same day again and again records one finding per rule, user
and day, with its first detection time preserved; a re-import with the same
numbers changes nothing. When a corrected import, a removed mapping or a
mapping moved to another user changes the result:

- a day that still qualifies has its evidence updated on the existing
  finding, keeping its fingerprint, so a dismissal or snooze keeps applying;
- a day that no longer qualifies is marked **superseded**. It leaves the
  Attention page and the weekly digest, and the row stays as an audit record
  with the dismissal state it had. If a later correction makes the day
  qualify again, the same finding comes back.

Imports older than 28 days are summary-only: they show on the Cost page but
raise no alert. If the adapter fails to read the stored rows, the pass keeps
every earlier finding, reconciles nothing, judges only yesterday from
gateway spend, and logs the source and the error type (never a token).

Daily reports cannot identify a session, so imported spend never feeds the
expensive-session rule. Imported amounts are never written to gateway
usage, budgets, quota, execution costs or per-issue cost rollups.
