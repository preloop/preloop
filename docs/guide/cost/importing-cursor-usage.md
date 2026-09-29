# Importing Usage from Cursor

After this page you can take spend that never went through the Preloop model gateway, Cursor's bundled Composer and Auto models being the usual case, and get it into Cost analytics: from the CLI in one command, or from the API if you are scripting it.

Preloop measures model spend by sitting in the request path. Clients that let Preloop rewrite their model configuration route through the [gateway](../concepts/model-gateway.md) and are metered exactly. Cursor's bundled models are the opposite case: they are billed by Cursor, served by Cursor, and never touch your gateway, so Preloop cannot observe them live. Importing closes that gap after the fact using the CSV Cursor already exports.

---

## Before you start: attribute the import to an agent

Every imported record is attributed to a managed agent. If you do not name one, the account's managed Cursor agent is used, and that agent only exists if it was created through the CLI:

```bash
preloop agents onboard cursor
```

Skip this and the import fails with HTTP 422:

```json
{"detail":"No managed 'cursor' agent found. Onboard one with `preloop agents onboard cursor` or pass agent_id explicitly."}
```

This trips people up because an agent created through the console or through `POST /api/v1/agents` does **not** satisfy it. Those are created with kind `custom`; default resolution looks for kind `cursor`, which only `preloop agents onboard cursor` produces. If you already have the agent you want, pass its id explicitly instead (`--agent-id` on the CLI, `agent_id` in the API) and the prerequisite does not apply.

If the account has more than one Cursor agent, default resolution also refuses rather than guessing, and asks you to pass an id.

## Export the CSV from Cursor

In the Cursor dashboard, go to **Usage** and use **Export CSV**. The export has a row per model call, with columns along these lines:

```text
Date, Kind, Model, Max Mode, Input (w/ Cache Write), Input (w/o Cache Write),
Cache Read, Output Tokens, Total Tokens, Cost
```

Header matching is case-insensitive and order-independent, and the `User` and `Max Mode` columns that appear in newer and team exports are handled, so a plain export needs no configuration. Only `Date` and `Model` are strictly required; without them the import is rejected with a message naming the headers it did find.

Rows whose `Cost` reads `Included` are the bundled-model rows. They are imported with their token counts and no charged amount, which is the honest representation: you used the tokens, the subscription paid for them.

## Import with the CLI

```bash
preloop usage import cursor-usage.csv
```

```text
✓ Imported 412 usage records from cursor-usage.csv
  Agent:      Cursor (ea7d00c9-adfc-4fef-a543-bac00fd83da2)
  Source:     cursor
  Duplicates: 0 skipped
```

The command takes a `.csv` (a Cursor Usage export) or a `.json` file (normalized events, see below) and picks the right endpoint from the extension.

| Flag | Default | Purpose |
|------|---------|---------|
| `--agent-id` | the onboarded Cursor agent | Managed agent to attribute the records to |
| `--source` | `cursor` | Origin label stored on each record, for filtering later |
| `--column-map` | none | Maps your CSV's headers onto the expected fields (CSV only) |

Rows the parser cannot use are reported rather than silently dropped:

```text
✓ Imported 410 usage records from cursor-usage.csv
  Agent:      Cursor (ea7d00c9-adfc-4fef-a543-bac00fd83da2)
  Source:     cursor
  Duplicates: 0 skipped
  Rows the parser could not use: 2
    - line 88: unparseable date '2026-13-45'
```

## Re-importing is safe

Every imported record carries a fingerprint derived from its timestamp, model, token counts, charged amount, session id, and target agent. Re-importing a file you already imported reports the overlap as duplicates instead of counting the spend twice:

```text
✓ Imported 0 usage records from cursor-usage.csv
  Agent:      Cursor (ea7d00c9-adfc-4fef-a543-bac00fd83da2)
  Source:     cursor
  Duplicates: 412 skipped
```

Dedupe is enforced by a unique index in the database, not only by a pre-insert check, so two concurrent imports of the same file cannot race past each other. In practice this means you can export a fresh CSV covering an overlapping period every week and just import it: only the new rows land.

Two genuinely distinct calls that happen to share a timestamp and model still both import, as long as any measured quantity differs.

## Imported spend never touches your budgets

This is the guarantee that makes the feature safe to use on a live account. Imported records are stored with a different action type from gateway-metered records, and every gateway aggregation (cost summaries, budgets, spend caps) filters on the gateway type. Budget accumulation is deliberately not performed for imported records.

The practical consequences:

- Imported spend does **not** count toward model budgets or spend caps. An import cannot trip a budget or throttle your agents.
- Imported spend is **not** added into `estimated_cost` in the cost summary. It is reported as its own `imported_usage` block, with its own event count, token total, cost, and per-model breakdown.

So the two numbers stay separately auditable: what Preloop metered itself, and what you told it about.

## Doing it from the API

Both endpoints require the `import_usage` permission.

### Normalized events

`POST /api/v1/usage/import` takes events you have already normalized, which is the path to use when you are pulling from something other than a Cursor CSV export.

```bash
curl -sS -X POST "$PRELOOP_URL/api/v1/usage/import" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "source": "cursor",
    "agent_id": "'"$AGENT_ID"'",
    "events": [
      {"timestamp": "2026-07-31T10:00:00Z", "model": "composer",
       "prompt_tokens": 1500, "completion_tokens": 850, "cost_usd": 0.42},
      {"timestamp": "2026-07-31T10:05:00Z", "model": "claude-4.5-sonnet",
       "total_tokens": 240, "charged_cents": 125, "session_id": "sess-1"}
    ]
  }'
```

```json
{"imported":2,"skipped_duplicates":0,"agent_id":"ea7d00c9-adfc-4fef-a543-bac00fd83da2","agent_display_name":"Cursor","source":"cursor"}
```

Per event, `timestamp` and `model` are required, and at least one measured quantity must be present: some token count, or a charged amount. Optional fields are `prompt_tokens`, `completion_tokens`, `total_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `session_id`, `kind`, `max_mode`, and a free-form `meta` object. Cost is given as either `cost_usd` or `charged_cents`, never both.

A request carries at most 5000 events. The CLI splits larger JSON files for you.

### CSV upload

`POST /api/v1/usage/import/csv` takes the Cursor export directly as a multipart upload.

```bash
curl -sS -X POST "$PRELOOP_URL/api/v1/usage/import/csv" \
  -H "Authorization: Bearer $PRELOOP_TOKEN" \
  -F "file=@cursor-usage.csv" \
  -F "agent_id=$AGENT_ID"
```

```json
{"imported":2,"skipped_duplicates":0,"agent_id":"ea7d00c9-adfc-4fef-a543-bac00fd83da2","agent_display_name":"Cursor","source":"cursor","parsed_rows":2,"skipped_rows":0,"skipped_row_reasons":[]}
```

Form fields are `file`, plus the optional `agent_id`, `source` (default `cursor`), and `column_map`. The upload is capped at 10 MiB and 10,000 data rows; split larger exports and import them in batches, which is safe precisely because the import is idempotent.

## When the headers do not match

If Cursor changes its export, or you are importing a CSV from somewhere else, map the columns yourself instead of waiting for a release. `column_map` is a JSON object from logical field name to the exact header in your file:

```bash
preloop usage import export.csv \
  --column-map '{"date":"Timestamp","model":"Model Name","cost":"Cost to You"}'
```

The logical fields you can map are `date`, `kind`, `model`, `max_mode`, `input_with_cache_write`, `input_without_cache_write`, `cache_read`, `output_tokens`, `total_tokens`, and `cost`. Any field you do not map falls back to the built-in matcher. An unknown field name is rejected outright rather than ignored, so a typo does not silently produce a half-parsed import.

## Verifying the result

```bash
curl -sS "$PRELOOP_URL/api/v1/cost/summary" \
  -H "Authorization: Bearer $PRELOOP_TOKEN"
```

The `imported_usage` block in the response holds the imported totals and the per-model breakdown, next to (and never inside) the gateway-metered figures.

## Related

- [Cursor, Claude Desktop & Other MCP Clients](../clients/other-mcp-clients.md): what is governed and metered per client
- [Cost Analytics & Budgets](../concepts/cost-analytics.md): how metered spend is reported and capped
- [AI Model Gateway](../concepts/model-gateway.md): the path that meters spend live
- [CLI Reference](../cli.md#usage-import)
