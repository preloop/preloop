# Importing Claude Code Usage from Anthropic

Editions: OSS, Cloud, Enterprise. Everything on this page ships in OSS.

After this page you can bring Claude Code usage that never passed through the Preloop gateway into Cost analytics: developers signed in with a subscription seat, Anthropic keys used outside Preloop, and sessions from before rollout.

Preloop meters model spend by sitting in the request path. Claude Code traffic that goes straight to Anthropic is invisible to it. Anthropic reports that traffic in its organization-level Claude Code Analytics API, which Preloop reads once a day with an Admin API key and stores as imported, estimated daily aggregates.

---

## What is imported

One row per developer (or API key), per UTC day, per model, from `GET /v1/organizations/usage_report/claude_code`:

| Field | Source |
| --- | --- |
| Actor | `actor.email_address` for people who sign in with OAuth, or `key:<api_key_name>` for API key users |
| Model | each `model_breakdown` entry |
| Tokens | input, output, cache read and cache creation tokens |
| Estimated cost | `estimated_cost.amount` (reported in cents, stored in dollars), marked as an estimate |
| Activity counters | sessions, lines added and removed, commits, pull requests, tool accept and reject counts |
| Context | terminal type, customer type, subscription type, report date |

Nothing else is stored. The API carries no prompt or transcript text.

The rows land in the provider billing snapshot table with provider `anthropic_cc` and usage source `imported`. They create no sessions and no gateway usage rows, and they never feed budgets or ingestion quota. The Cost page shows them in the **Copilot and Claude Code** tab under the "Not metered by the gateway" marker.

## Never counted twice

Usage on an Anthropic key that Preloop itself uses as an upstream credential was already metered by the gateway. Preloop finds those keys by matching each Anthropic model's configured key against the organization's key list (`GET /v1/organizations/api_keys`, by `partial_key_hint`). You can also list key names on the connection. Rows for those keys are stored with `metered_by_gateway: true`, shown separately as "Already metered by the gateway", and left out of every total. Listed names are read from the connection each time the Cost page loads, so adding or removing a name re-classifies days that are already imported without a re-import.

Rows for people signed in with OAuth (subscription or console sign in) are counted as usage outside the gateway, the same treatment Copilot rows get.

## Which key to use

Create an **Admin API key** in the Claude Console (Settings, Admin keys) and dedicate it to Preloop, so you can revoke it without affecting anything else. Workspace API keys do not work. Treat the key as read only: Preloop only calls the two report and listing routes above, but Anthropic Admin keys are not scoped, so keep it out of every other tool.

The key is stored encrypted as a secret reference. It is never logged and never returned by the API; the console shows only its last four characters.

## Connect

In the console, open **Cost**, the **Copilot and Claude Code** tab, and fill in the Anthropic form. Or use the API:

```bash
curl -X PUT "$PRELOOP_URL/api/v1/anthropic-usage/connection" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"admin_key": "'"$ANTHROPIC_ADMIN_KEY"'", "gateway_key_names": ["preloop-gateway"]}'
```

| Route | Purpose |
| --- | --- |
| `GET /api/v1/anthropic-usage` | Summary for a window (`start_date`, `end_date`) |
| `PUT /api/v1/anthropic-usage/connection` | Create or update; omit `admin_key` to keep the stored one |
| `DELETE /api/v1/anthropic-usage/connection` | Remove the connection and its key; imported history stays |
| `POST /api/v1/anthropic-usage/connection/test` | Test the key with the cheapest read (one key listed) |
| `POST /api/v1/anthropic-usage/sync` | Queue an import now |
| `GET`, `PUT /api/v1/anthropic-usage/mappings`, `DELETE /api/v1/anthropic-usage/mappings/{actor}` | Map an actor to a Preloop user |

Writes need the `manage_budgets` permission; reads need `view_cost`.

## Schedule

The scheduler queues `ingest_anthropic_usage` once a day. It imports up to yesterday (UTC), because the report lags activity by up to about an hour, and catches up at most seven missed days after downtime. Rate limits (HTTP 429) are retried a bounded number of times, honouring `Retry-After`. A failure, such as a revoked key (401), is recorded on the connection and shown in the console; the next run resumes after the last imported day. Re-importing a day overwrites that day's rows, it never adds to them.

Set `ANTHROPIC_USAGE_SYNC_ENABLED=false` to turn the schedule off. Without a connection the task does nothing.

## Who is who

An actor's email maps to an active Preloop member with the same email, and the matching gateway subject (if one exists) is shown alongside. An explicit mapping wins over the email match, and is the only way to attribute an API key actor. Nothing is created: unknown emails stay unmapped.

## What is not attributable

- Claude Enterprise and Team seats on claude.ai (web, Desktop and Cowork chat usage) are reported by a separate Enterprise Analytics API with a different key type. They are not imported.
- The Claude Code Analytics API covers Claude Code on the Claude API only. Usage through cloud provider platforms is not included.
- Imported rows are daily aggregates. Per-request tokens, sessions and transcripts are never invented, and imported rows never feed budgets or ingestion quota.
- Priority Tier and code execution costs are not represented.

## Related

- [Copilot usage import](../copilot-usage-import.md)
- [Importing usage from Cursor](importing-cursor-usage.md)
