# Gateway activity storage

Every model call that goes through the Preloop gateway inside a runtime
session is stored as one row in the `runtime_session_activity` table. The
row holds usage, cost and timing, plus the conversation content that the
session views show.

## What one row stores

A model call sends the whole conversation so far. Older releases stored all
of it on every row, so call 200 of a session repeated calls 1 to 199, and
the table grew with the square of session length. Long agent sessions
wrote several hundred kilobytes per model call.

Rows are now stored slim:

- The messages new to this call: what the caller added after the previous
  response (usually the latest tool result or user message), and the
  response. The last user message is always kept because the session views
  title a call by it. Older messages are already on the session's earlier
  rows, and the console stitches the conversation back together by message
  position.
- The request parameters, including tool definitions, as before.
- Tool calls and results that belong to this call.
- Usage, tokens, cost, model, outcome and timing, unchanged.

If a call is still larger than `MODEL_GATEWAY_ACTIVITY_MAX_STORED_BYTES`
(default 32768), Preloop reduces tool definitions to their names, then drops
the raw request messages (the readable preview stays), then shortens long
text until the row fits. The row records what was removed under
`body_compaction`.

The live view of a running session is not affected: live events still carry
the full call.

## Compacting rows written by older releases

Rows written before this change keep their full history until they are
compacted. A maintenance pass does this in the background:

- It runs hourly from the sync scheduler (task `maintain_gateway_activity`)
  while `GATEWAY_ACTIVITY_COMPACTION_ENABLED` is true (the default).
- Each pass rewrites at most `GATEWAY_ACTIVITY_MAINTENANCE_MAX_BATCHES`
  batches (default 50) of `GATEWAY_ACTIVITY_MAINTENANCE_BATCH_SIZE` rows
  (default 200). Each batch is its own transaction, so stopping a pass loses
  nothing; the next pass continues with the rows still left.
- It is not part of a schema migration, so it never holds up an upgrade.
- Sessions under legal hold are skipped.

To drain a backlog faster, or to see what a pass would do, run it by hand
in an API or sync container:

```bash
# Count what would change without writing anything
python -m preloop.services.gateway_activity_maintenance --dry-run

# Rewrite up to 500 batches now
python -m preloop.services.gateway_activity_maintenance --max-batches 500
```

Each batch is logged with the number of rows rewritten and the timestamp it
reached. The command prints a JSON summary with `compacted_rows`,
`bytes_before`, `bytes_after` and `exhausted` (true when no rows were left).
Run it again until `exhausted` is true.

Compaction frees space inside the table for new rows. The table file on disk
does not shrink until Postgres rewrites it (`VACUUM FULL` or `pg_repack`),
which you schedule in your own maintenance window.

## Retention of conversation content

With `GATEWAY_ACTIVITY_BODY_RETENTION_ENABLED=true`, the same hourly pass also
removes conversation content from gateway call rows older than the account's
plan history window. Usage, tokens, cost and timing stay, so reports over
old periods still add up. Accounts with unlimited history are skipped, and
so are sessions under legal hold. This is off by default because removed
content cannot be restored. Run it by hand with `--retention`.

Deleting whole sessions and their records is a separate job, the retention
purge (`RETENTION_PURGE_ENABLED`, periods under **Settings > Records**).

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `MODEL_GATEWAY_ACTIVITY_MAX_STORED_BYTES` | `32768` | Byte cap for one stored gateway call row; `0` disables the cap |
| `GATEWAY_ACTIVITY_COMPACTION_ENABLED` | `true` | Hourly compaction of rows from older releases |
| `GATEWAY_ACTIVITY_BODY_RETENTION_ENABLED` | `false` | Hourly removal of content older than the plan history window |
| `GATEWAY_ACTIVITY_MAINTENANCE_BATCH_SIZE` | `200` | Rows per transaction |
| `GATEWAY_ACTIVITY_MAINTENANCE_MAX_BATCHES` | `50` | Batches per pass |
