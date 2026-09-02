# Usage hooks: live conversation tracking in Cost analytics

`preloop usage hook` ships conversation lifecycle and usage events to
`POST /api/v1/usage/ingest` so chats appear in the Cost analytics
conversation rollup in near real time. It is harness-agnostic: the same
command accepts a documented generic event schema, Cursor's agent hooks,
and OpenAI Codex CLI session rollouts.

By default the command auto-detects the payload:

- a Cursor hook object (`hook_event_name`) is handled as before
- newline-delimited generic events (`schema: preloop.usage.event.v1`)
  are the contract third-party harnesses should target
- Codex CLI session JSONL (`type` plus `payload`, typically starting
  with `session_meta`)

Override detection with `--from cursor`, `--from generic`, or
`--from codex`. Read from a file with `--file` instead of stdin.

Every shipped record is labeled with a `cost_basis`. That basis is
`estimated` unless the event explicitly carries a billed amount from a
provider ledger (`charged_cost` / `cost_usd` and `cost_basis=reconciled`).
Quantities the source never reported are omitted (shown as "not reported"
in the console), never as 0 or $0.00. Estimated and reconciled amounts
are always displayed separately and are never summed together.

Privacy: the shipper sends ids, event names, the reported model name,
token counts, billed amounts when the source provided them, and small
metadata. It never sends prompt text, task descriptions, file paths,
shell commands, or workspace contents.

## Generic event schema (`preloop.usage.event.v1`)

This is the contract homemade harness hooks should emit. Pipe
newline-delimited JSON (NDJSON) to `preloop usage hook`:

```bash
echo '{"schema":"preloop.usage.event.v1","conversation_id":"conv-1","event_type":"usage","model":"gpt-5","input_tokens":12,"output_tokens":4}' \
  | preloop usage hook --source my-harness
```

Pretty-printed single objects are also accepted. Unknown fields are
ignored and never fatal. An unsupported `schema` value skips that event
and continues. Malformed lines are reported on stderr; the command still
exits 0.

### Fields

| Field | Required | Notes |
| ----- | -------- | ----- |
| `schema` | no | Set to `preloop.usage.event.v1`. Omitted events are treated as v1. Other values are skipped. |
| `conversation_id` | yes | Harness conversation / thread id. |
| `parent_conversation_id` | no | Parent thread for subagent rollup. Flag `--parent-conversation-id` and env `PRELOOP_PARENT_CONVERSATION_ID` are fallbacks, in that order. Never guessed. |
| `id` or `external_id` | no | Dedupe key with `source`. If omitted: `{event_type}:{conversation_id}:{timestamp}`. |
| `timestamp` | no | RFC3339. If omitted, the shipper's receive time. |
| `event_type` | no | `session_start`, `session_end`, `subagent_start`, `subagent_stop`, `response`, `compaction`, or `usage`. Default: `usage` when any token/cost field is present, otherwise `response`. Unknown types are skipped. |
| `model` | for `usage` | Source-reported model name. |
| `input_tokens` | no | Non-negative int. Alias: `prompt_tokens`. Null or omitted means not reported, never 0. An explicit `0` is passed through. |
| `output_tokens` | no | Non-negative int. Alias: `completion_tokens`. |
| `cache_read_tokens` | no | Cache-read tokens only. Do not put cache-write counts here. |
| `message_count` | no | Growth tripwire. |
| `tool_call_count` | no | Growth tripwire. |
| `charged_cost` | no | Billed amount in USD from a provider ledger. Alias: `cost_usd`. Do not send model-card estimates here. |
| `cost_basis` | no | `estimated` (default) or `reconciled`. `reconciled` is honored only when `charged_cost` / `cost_usd` is present. |
| `source` | no | Recorded in metadata as `event_source`. The ingest request source is `--source` (default `generic` in this mode). |
| `metadata` | no | Small JSON object. Capped by the ingest API (8 KiB serialized). |

Example with a billed ledger amount:

```json
{
  "schema": "preloop.usage.event.v1",
  "id": "invoice-line-88",
  "conversation_id": "conv-1",
  "timestamp": "2026-09-01T12:00:00Z",
  "event_type": "usage",
  "model": "gpt-5",
  "input_tokens": 1200,
  "output_tokens": 80,
  "charged_cost": 0.042,
  "cost_basis": "reconciled"
}
```

## Cursor

Cursor's bundled models never pass through the Preloop model gateway, so
Preloop cannot meter that traffic itself. Wired into Cursor's hooks, this
command ships conversation lifecycle events as they happen.

Hook payload fields referenced below come from Cursor's hooks reference
(https://cursor.com/docs/agent/hooks, checked 2026-08-27).

For headless cursor-agent runs, the [`preloop cursor` launcher](cursor-cli.md)
captures print-mode output and ships estimated usage without any hook
configuration.

Cursor hook payloads carry no token counts and no billed amounts. This
integration therefore records lifecycle facts only:

- which conversations were active, and when
- session, response, subagent, and compaction lifecycle events
- parent and child conversation links (`parent_conversation_id` from
  `subagentStart` payloads)
- message and tool-call counters where Cursor reports them
  (`subagentStop`), as growth tripwires

Billed amounts enter the system only through a reconciled import (for
example a Cursor dashboard Usage export via `preloop usage import`).

### Prerequisites

1. The Preloop CLI installed and logged in (`preloop login`), with a user
   allowed to import usage.
2. A managed Cursor agent to attribute events to. Either onboard one with
   `preloop agents onboard cursor`, or pass `--agent-id` explicitly.

### Configure Cursor

Cursor reads hook configuration from `hooks.json`, either user wide at
`~/.cursor/hooks.json` or per project at `<project>/.cursor/hooks.json`.
Create or extend it:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "preloop usage hook" }],
    "sessionEnd": [{ "command": "preloop usage hook" }],
    "subagentStart": [{ "command": "preloop usage hook" }],
    "subagentStop": [{ "command": "preloop usage hook" }],
    "stop": [{ "command": "preloop usage hook" }],
    "preCompact": [{ "command": "preloop usage hook" }]
  }
}
```

Cursor watches hook config files and reloads them automatically. The same
command handles every event; it reads the hook payload from stdin and
decides from `hook_event_name` what to do.

### Event mapping

| Cursor hook event | Shipped as | Notes |
| ----------------- | ---------- | ----- |
| `sessionStart` | `session_start` | Fires when a new conversation is created. |
| `sessionEnd` | `session_end` | End reason recorded in metadata. |
| `subagentStart` | `subagent_start` | Carries the documented `parent_conversation_id`, which powers thread nesting in the rollup. |
| `subagentStop` | `subagent_stop` | Ships the documented `message_count` and `tool_call_count`. |
| `stop` | `response` | Fires when the agent loop finishes responding. |
| `preCompact` | `compaction` | Context compaction marker. |
| any other event | not shipped | Permission, file, prompt, and thought hooks would inflate event counts without adding cost signal. The command acknowledges them and exits 0. |

Deduplication: the server deduplicates on `(source, external_id)`.
Records are keyed by conversation id (`sessionStart`/`sessionEnd`),
`generation_id` plus `loop_count` (`stop`), or `subagent_id`
(`subagentStart`). `subagentStop` and `preCompact` payloads document no
per-fire id, so those records get a receive-time suffix; they stay unique
across parallel workers but do not deduplicate on replay. The command
makes a single delivery attempt, so duplicates cannot originate from it.

### Subagent conversations

When Cursor spawns a subagent (Task tool), the `subagentStart` payload
reports `parent_conversation_id`, and the rollup nests the record under
that parent thread automatically.

One documented ambiguity: Cursor's reference does not state whether the
common `conversation_id` field on subagent events refers to the worker's
own conversation or the parent's. The command ships exactly what Cursor
reports and never rewrites it; if both ids are equal, the record simply
lands on the parent thread and per-thread totals stay correct.

For orchestrations you drive yourself (a wrapper that launches worker
sessions on behalf of a parent), you can set the parent id explicitly:
`preloop usage hook --parent-conversation-id <id>` in that wrapper's hook
configuration, or export `PRELOOP_PARENT_CONVERSATION_ID` in the worker's
environment. Precedence: payload field, then flag, then environment
variable. When none is present the field is omitted; it is never guessed.

### Verify the wiring

```bash
echo '{"conversation_id":"test-conv","generation_id":"test-gen-1","hook_event_name":"stop","status":"completed","loop_count":0}' \
  | preloop usage hook
```

Then open Cost analytics in the console. The "Imported usage" section
shows a "Conversations" rollup listing `test-conv` with one event and its
tokens and costs marked "not reported" (hooks report neither).

## Codex CLI

Codex CLI does not expose Cursor-style live hooks. This adapter reads a
session rollout file (JSONL) and maps a small subset of lines onto the
same ingest API. It is a one-shot import. There is no follow/watch mode.

### What was verified

Checked 2026-09-02 against:

- openai/codex `codex-rs/protocol/src/protocol.rs` on GitHub `main`
  (`SessionMeta`, `TokenUsage`, `TokenCountEvent`, `ThreadSource`,
  `SubAgentSource`)
- OpenAI docs: `CODEX_HOME` defaults to `~/.codex`
  (https://developers.openai.com/codex/environment-variables)
- 26 local rollout files from Codex CLI 0.144.4 and 0.151.0 under
  `~/.codex/sessions`

Verified layout and fields:

- Root: `$CODEX_HOME/sessions`, default `~/.codex/sessions`
- Sharding: `YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`
- Each line is `{timestamp, type, payload}` and, in 0.151.0, `ordinal`
- First line is `session_meta`. `payload.id` is this thread's id.
  `payload.session_id` is the root thread id (equal to `id` for root
  sessions; for subagents it is the parent)
- `payload.parent_thread_id` and
  `payload.source.subagent.thread_spawn.parent_thread_id` identify the
  parent of a spawned subagent. `thread_source` is `"subagent"` on those
  files
- `turn_context.payload.model` is the model for the upcoming turn
- `event_msg` / `token_count` carries
  `payload.info.last_token_usage` (this request) and
  `payload.info.total_token_usage` (session cumulative), each with
  `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`,
  `output_tokens`, `reasoning_output_tokens`, `total_tokens`
- Local files contained no billed USD / cost fields on those usage
  objects
- `response_item` and `world_state` lines contain prompts, tool output,
  and workspace text. They are never shipped

### What is assumed

- All `token_count` lines after the first `session_meta` in a file are
  attributed to that file's thread (`payload.id`). A child rollout can
  embed an inherited parent `session_meta`; later headers with a
  different id are ignored. Inherited parent `token_count` lines, if
  present, may still be attributed to the child. `subagent_history_start_ordinal`
  exists in protocol.rs but was not present on the local 0.144.4 child
  rollout we inspected
- `last_token_usage` is the per-request figure to ingest. Cumulative
  `total_token_usage` is used only to skip a `token_count` whose totals
  did not advance, matching openai/codex#14489 (rate-limit snapshots
  re-emitting the previous last usage). That skip was not reproduced in
  the local 26 files
- `event_msg` type `context_compacted` maps to `compaction` if present.
  It was not observed in the local files (protocol.rs defines
  `ContextCompacted`)
- `task_complete` / `turn_complete` / `turn_aborted` map to `response`.
  There is no reliable `session_end` marker; we do not invent one
- Watch/follow of a growing rollout is out of scope

### Import a session

Onboard a Codex agent (or pass `--agent-id`), then:

```bash
preloop usage hook --from codex --file ~/.codex/sessions/2026/08/31/rollout-2026-08-31T02-17-48-01a054f7-010e-7d42-bc41-a1355bacf7ef.jsonl
```

Piping stdin also works for small files:

```bash
preloop usage hook --from codex < ~/.codex/sessions/2026/08/31/rollout-....jsonl
```

Stdin is capped at 1 MiB (the same bound as live hooks). Use `--file`
for larger rollouts. `--source` defaults to `codex` in this mode so the
ingest API can attribute events to the onboarded Codex agent.

### Event mapping

| Codex line | Shipped as | Notes |
| ---------- | ---------- | ----- |
| `session_meta` (first in file, no parent) | `session_start` | `conversation_id` = `payload.id`. |
| `session_meta` (first in file, with parent) | `subagent_start` | Parent from `parent_thread_id`, else `source.subagent.thread_spawn.parent_thread_id`, else `forked_from_id`. |
| later `session_meta` | not shipped | Inherited parent header. |
| `turn_context` | not shipped | Model remembered for later usage records. |
| `event_msg` / `token_count` | `usage` | Tokens from `last_token_usage`. `cached_input_tokens` maps to `cache_read_tokens`. Cache-write and reasoning tokens stay in metadata. `cost_basis=estimated`, no `charged_cost`. |
| `event_msg` / `task_complete` (and aliases) | `response` | Turn finished. |
| `event_msg` / `context_compacted` | `compaction` | If present. |
| `response_item`, `world_state`, other types | not shipped | Content, not cost signal. |

## Failure behavior

The command is fail-open by design:

- stdin ingest requests are capped at 3 seconds (editor hooks must not
  stall). File imports use the CLI's default API timeout
- any error (server unreachable, expired login, malformed payload) is
  printed to stderr and the command still exits 0
- stdin hooks write nothing to stdout. `--file` prints a one-line
  shipment summary

A failed shipment means missing events, nothing more.

## Reconciling billed amounts later

When you have actual billed figures, import them separately:

- `preloop usage import <cursor-usage-export>.csv` records the dashboard
  export as imported spend (totals and per-model figures; the export
  carries no conversation ids).
- A billing source that does report per-conversation amounts can push
  them as generic events with `charged_cost` and `cost_basis=reconciled`,
  or to `POST /api/v1/usage/ingest` directly. Reconciled records then
  supersede hook-derived estimates for that conversation in summary
  totals, and the conversation rollup shows both figures side by side.
