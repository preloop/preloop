# Gateway activity storage

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Every model call that goes through the Preloop gateway inside a runtime
session is stored as one row in the `runtime_session_activity` table. The
row holds usage, cost and timing, plus the conversation content that the
session views show.

A model call made by a flow run is stored instead as a `model_gateway_call`
row in `flow_execution_log`, the execution's log, with the same payload. The
flow execution page builds its Transcript tab from these rows. Everything
below about what a row stores applies to both tables.

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

### When a call is larger than the cap

`MODEL_GATEWAY_ACTIVITY_MAX_STORED_BYTES` (default 8388608, 8 MB) is a guard
against runaway payloads, not a size budget. A row already holds only what
the call added, so a normal call is stored whole, untruncated. Only a call
whose stored form would exceed the cap is shortened. Short of the final
fallback described below, Preloop never removes:

- the model response: its text, the tool calls it made, the finish reason
  and usage,
- the messages this call added (usually tool results and file contents),
- the last user message.

It shortens in this order and stops as soon as the row fits:

1. Tool definitions are reduced to their names.
2. `tool_activity` is dropped. It is derived from the request and response,
   which stay.
3. The `tools` entries are dropped (also derived), and long text in the
   request outside its new messages, such as the system prompt, is cut in
   the middle.
4. Long text in the new messages is cut in the middle, longest first, so
   the largest tool outputs go first. The first and last
   `MODEL_GATEWAY_ACTIVITY_TRUNCATE_KEEP_CHARS` characters (default 2048)
   are kept, with a marker in between: `[... truncated 12345 chars ...]`.
5. Only then is long response text cut, middle out, keeping four times as
   much at the start (8192 characters by default).

Tool-call arguments and tool-call inputs are never cut, because a cut
argument is no longer valid JSON.

A cap far below the size of the call can need more: text is cut harder (down
to 64 characters at each end), then only the newest new message and the last
user message are kept, then the response text is cut harder. If even that
does not fit (a cap smaller than the bare structure of the call), the
contents of `request` and `response` are replaced with
`{"omitted_for_size": true}`, the preview is removed and the marker records
`content_omitted_for_size`. The keys stay so a reader can tell the content
was removed, not absent. With the default cap this only happens for a
single call larger than 8 MB whose tool arguments alone exceed the cap.

The session views show a **Truncated** badge on every message or tool
result that was cut.

`body_compaction` on the row records exactly what happened:

| Field | Meaning |
| --- | --- |
| `original_bytes`, `stored_bytes` | Size of the call before and after |
| `request_history_start` | Index of the first new message; earlier ones were replayed history |
| `tool_definitions_omitted` | Tool definitions were reduced to names |
| `keys_omitted` | Derived keys that were dropped (`tool_activity`, `tools`) |
| `request_context_shortened`, `messages_shortened`, `response_shortened` | `strings` cut, `chars_removed`, and the characters kept (`keep_chars` or `head_chars`) |
| `messages_shortened_further`, `delta_messages_elided`, `response_shortened_further` | The last-resort steps above, when they ran |
| `content_omitted_for_size` | The final fallback ran: request and response contents replaced with `{"omitted_for_size": true}` |

### Why the cap is large

Because a row keeps only the call's own new messages and response, its size
no longer grows with the length of the session. On a synthetic workload of
2,000 long-session calls (a median of 148 replayed messages, up to 1,200,
original calls averaging about 700 KB), the stored row is 107 KB at the
median and 171 KB at p95 before any cap. The 8 MB default therefore leaves
normal calls alone and only stops a single runaway turn, such as a tool
that returns a whole build log or a binary file.

Rows live in Postgres, so there is still a limit. Storing arbitrarily large
single turns with no limit at all would need an object storage backend for
call bodies, which Preloop does not have today.

### Rows stored by earlier releases

Rows written while the default cap was 32768 can be missing content: their
`body_compaction` shows `keys_omitted` containing `request` and `response`,
or `strings_capped_at` (text cut to that many characters from the start).
That content was never written, and the maintenance pass does not touch rows
that already carry `body_compaction`, so it cannot be restored. Token counts,
cost and timing on those rows are complete.

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
  nothing; the next pass continues with the rows still left. Each worker
  remembers how far it got, so once the backlog is drained an hourly pass
  only looks at rows it has not seen. After a worker restart the first pass
  walks the table once more and finds nothing to rewrite.
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

### Flow execution logs

Flow log rows written by older releases are compacted by the same pass when
`FLOW_LOG_GATEWAY_COMPACTION_ENABLED=true`. It is off by default because it
removes replayed history from existing rows; new rows are stored slim
whatever this setting says. It uses the same batch settings, skips
executions under legal hold, and leaves every other log type (agent log
lines, tool calls, status updates) untouched.

```bash
# See what compacting flow logs would do, without writing anything
python -m preloop.services.gateway_activity_maintenance --flow-logs --no-compaction --dry-run

# Compact up to 500 batches of flow log rows now
python -m preloop.services.gateway_activity_maintenance --flow-logs --no-compaction --max-batches 500
```

The summary reports flow log rows under `flow_log_compaction`. Body
retention below does not apply to flow logs.

## Retention of conversation content

With `GATEWAY_ACTIVITY_BODY_RETENTION_ENABLED=true`, the same hourly pass also
removes conversation content from every gateway call row older than the
account's plan history window, whatever its size. Usage, tokens, cost and timing stay, so reports over
old periods still add up. Accounts with unlimited history are skipped, and
so are sessions under legal hold. This is off by default because removed
content cannot be restored. Run it by hand with `--retention`.

Deleting whole sessions and their records is a separate job, the retention
purge (`RETENTION_PURGE_ENABLED`, periods under **Settings > Records**).

## Stored payload size

Each row records the size of its `metadata` in `metadata_bytes`, set whenever
the row is written. Readers that page gateway payloads under a byte budget use
it instead of serialising every value to measure it. Rows written before the
column existed are filled in by the same hourly pass
(`GATEWAY_ACTIVITY_SIZE_BACKFILL_ENABLED`, on by default), one bounded UPDATE
per batch; until then, readers measure those rows the old way. Skip it in a
manual run with `--no-size-backfill`.

## Settings

| Setting | Default | Effect |
| --- | --- | --- |
| `MODEL_GATEWAY_ACTIVITY_MAX_STORED_BYTES` | `8388608` | Guard against runaway gateway call rows (bytes); `0` disables the cap |
| `MODEL_GATEWAY_ACTIVITY_TRUNCATE_KEEP_CHARS` | `2048` | Characters kept at each end of text cut to fit the cap |
| `GATEWAY_ACTIVITY_COMPACTION_ENABLED` | `true` | Hourly compaction of rows from older releases |
| `FLOW_LOG_GATEWAY_COMPACTION_ENABLED` | `false` | Hourly compaction of flow log model call rows from older releases |
| `GATEWAY_ACTIVITY_BODY_RETENTION_ENABLED` | `false` | Hourly removal of content older than the plan history window |
| `GATEWAY_ACTIVITY_SIZE_BACKFILL_ENABLED` | `true` | Hourly fill of `metadata_bytes` on rows from older releases |
| `GATEWAY_ACTIVITY_MAINTENANCE_BATCH_SIZE` | `200` | Rows per transaction |
| `GATEWAY_ACTIVITY_MAINTENANCE_MAX_BATCHES` | `50` | Batches per pass |
