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
token counts (reported or estimated), billed amounts when the source
provided them, and small metadata. For Cursor it also sends a session
title (the first line of the first prompt) and a short summary (the last
assistant paragraph, 280 characters) so the runtime sessions list is
readable; transcript text itself is shipped only when you opt in with
`--store-transcript`. It never sends file paths, shell commands, or
workspace contents.

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
| `id` or `external_id` | no | Dedupe key with `source`. If omitted: `{event_type}:{conversation_id}:{timestamp}:{n}`, where `n` is the 0-based index in that invocation. Same-type events in one batch therefore stay unique even when timestamp is omitted. |
| `timestamp` | no | RFC3339. If omitted, the shipper's receive time. Omitting it on a batch of same-type events no longer collapses keys. |
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
| `metadata` | no | Small JSON object. Capped by the ingest API (8 KiB serialized). Oversize metadata is dropped with a warning so the rest of the event can still ship. |

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
Preloop cannot meter that traffic itself; Cursor's agent hooks are the
only signal. Wired into them, this command stores every conversation as
a runtime session and ships a per-generation token and cost estimate.

Hook payload fields referenced below come from Cursor's hooks reference
(https://cursor.com/docs/agent/hooks, checked 2026-09-05).

For headless cursor-agent runs, the [`preloop cursor` launcher](cursor-cli.md)
captures print-mode output and ships estimated usage without any hook
configuration.

### What Cursor reports, and what is estimated

Cursor hook payloads carry no token counts and no billed amounts. Two
sources fill the gap:

- Transcript-derived estimate. Every agent hook names the conversation
  transcript (`transcript_path`; the process environment also carries
  `CURSOR_TRANSCRIPT_PATH`), and `subagentStop` names the subagent's own
  transcript (`agent_transcript_path`). At `stop`, `sessionEnd` and
  `subagentStop` the command reads the transcript from the last shipped
  byte offset, splits the new text by role, and ships
  `input_tokens = ceil(context characters / 4)` and
  `output_tokens = ceil(assistant characters / 4)`. The context is the
  whole transcript before the generation plus the generation's own prompt
  and tool results, because Cursor re-sends the conversation each turn;
  the output is the assistant text plus its tool calls. The divisor is
  the same chars-per-token heuristic the gateway budget preflight uses
  (`billing_budget_chars_per_token`, default 4), so client and server
  numbers agree. Each record's `metadata.token_estimate` names the method
  (`transcript_chars`), the divisor, the bytes read and the input source.
- Cursor's own context count. `preCompact` reports `context_tokens`, the
  only real token figure any Cursor hook carries. The command ships it in
  the compaction record's metadata and uses it as `input_tokens` for the
  next generation (`input_source: pre_compact_context_tokens`).

The estimate also biases the other way after a compaction: the transcript
keeps growing while Cursor's real context shrank, so chars-based input is
overstated until the next `preCompact` replaces it with Cursor's own
count.

The server prices estimated records that carry tokens through the model
pricing catalog. Cursor's version-first Claude spellings are mapped onto
catalog keys (`claude-4.5-sonnet` prices as `claude-sonnet-4-5`). The
amount lands on the `estimated` basis with `cost_source: catalog`; models
the catalog does not know (for example `composer` or `auto`) stay
unpriced, never $0. A later reconciled import (a Cursor dashboard Usage
export via `preloop usage import`) supersedes the estimates for the same
conversation and is never summed with them.

Per-conversation offsets live in
`~/.preloop/agents/cursor/transcripts/<conversation_id>.json` (offset,
last generation, running totals, pending context tokens, title). Files
untouched for 30 days are pruned at the next `sessionStart`. Reads are
bounded to the delta since the last offset (8 MiB per hook process).

Transcript format, checked 2026-09-05 against 169 local files under
`~/.cursor/projects/<workspace>/agent-transcripts/<conversation_id>/`
(subagents under `subagents/<subagent_id>.jsonl`): JSONL, one
`{"role": "user" | "assistant", "message": {"content": [...]}}` object per
line with `text` and `tool_use` content blocks, plus role-less control
lines such as `{"type": "turn_ended", "status": "success"}`. Tool output
is not recorded. Half-written trailing lines are left for the next read.
The parser also accepts a plain-text transcript: everything counts as
input except lines under an `Assistant:` marker.

### Sessions

Every conversation is stored as a runtime session (source type `cursor`,
source id `conversation_id`, principal = the managed Cursor agent the
records are attributed to). It appears in the runtime sessions explorer
next to gateway-metered sessions:

- `sessionStart` opens the session with the title
  "Cursor conversation <short id>".
- `beforeSubmitPrompt` keeps the first line of the first prompt locally.
  Nothing is posted for this event and the prompt is otherwise ignored.
  The next `stop` sends that line as the session title.
- `stop` advances the session's last activity and sets its summary to
  the last assistant paragraph (280 characters).
- `sessionEnd` closes the session.

By default only counts, the title and that short summary leave the
machine, matching the Claude Code plugin's summaries-only design. Full
transcript text is opt-in: `preloop usage hook --store-transcript` (or
`preloop agents onboard Cursor --store-transcript`, which puts the flag
in `hooks.json` and records `store_transcript: true` in the hook
credential file) ships the new transcript text with each `stop` as
`transcript_message` activities on the session, capped at 50 messages
and 64 KiB per record.

### Prerequisites

1. The Preloop CLI installed and logged in (`preloop login`), with a user
   allowed to import usage.
2. A managed Cursor agent to attribute events to:
   `preloop agents onboard Cursor`.

### Configure Cursor

`preloop agents onboard Cursor` installs the usage hooks in
`~/.cursor/hooks.json` for `sessionStart`, `sessionEnd`, `subagentStart`,
`subagentStop`, `stop`, `preCompact` and `beforeSubmitPrompt`, each running
`<absolute path to preloop> usage hook --from cursor` with a 5 second
timeout. The install is idempotent, keeps entries it does not own, and
`preloop agents offboard Cursor` removes exactly its own entries. Flags:

- `--no-usage-hooks` skips the usage hooks (approval hooks are unaffected).
- `--store-transcript` opts into transcript storage (see Sessions).
- Re-run `preloop agents onboard Cursor` to change either choice.

To wire the hooks by hand instead, create or extend `hooks.json` (user
wide at `~/.cursor/hooks.json` or per project at
`<project>/.cursor/hooks.json`), using the absolute path of the binary
so Cursor's PATH does not matter:

```json
{
  "version": 1,
  "hooks": {
    "sessionStart": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "sessionEnd": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "subagentStart": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "subagentStop": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "stop": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "preCompact": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }],
    "beforeSubmitPrompt": [{ "command": "/usr/local/bin/preloop usage hook --from cursor", "timeout": 5 }]
  }
}
```

Cursor watches hook config files and reloads them automatically. The same
command handles every event; it reads the hook payload from stdin and
decides from `hook_event_name` what to do.

### Event mapping

| Cursor hook event | Shipped as | Notes |
| ----------------- | ---------- | ----- |
| `sessionStart` | `session_start` | Opens the runtime session with the default title. |
| `sessionEnd` | `session_end` | Closes the session. End reason in metadata; any transcript text not yet counted is estimated here. |
| `subagentStart` | `subagent_start` | Carries the documented `parent_conversation_id`, which powers thread nesting in the rollup. |
| `subagentStop` | `subagent_stop` | Ships the documented `message_count` and `tool_call_count`, plus a token estimate from `agent_transcript_path`. |
| `stop` | `response` | Token estimate, session title and summary. Fires when the agent loop finishes responding. |
| `preCompact` | `compaction` | `context_tokens`, `context_window_size`, `context_usage_percent`, `messages_to_compact` and `is_first_compaction` in metadata; `message_count` as a column. |
| `beforeSubmitPrompt` | not shipped | The first prompt line is kept locally for the session title; the prompt is otherwise ignored. Answers `{"continue": true}` on stdout, the decision JSON this event's contract expects. |
| any other event | not shipped | Permission, file, and thought hooks would inflate event counts without adding cost signal. The command acknowledges them and exits 0. |

Deduplication: the server deduplicates on `(source, external_id)`.
Records are keyed by conversation id (`sessionStart`/`sessionEnd`),
`generation_id` plus `loop_count` (`stop`), or `subagent_id`
(`subagentStart`). `subagentStop` and `preCompact` payloads document no
per-fire id, so those records get a receive-time suffix; they stay unique
across parallel workers but do not deduplicate on replay. The command
makes a single delivery attempt, so duplicates cannot originate from it.
The transcript offset file is the second guard: a replayed `stop` finds
nothing new in the transcript and ships no token fields.

### Subagent conversations

When Cursor spawns a subagent (Task tool), the `subagentStart` payload
reports `parent_conversation_id`, and the rollup nests the record under
that parent thread automatically. `subagentStop` reads the subagent's own
transcript, so its tokens are counted once, separately from the parent's.

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
printf '%s\n' \
  '{"role":"user","message":{"content":[{"type":"text","text":"hello"}]}}' \
  '{"role":"assistant","message":{"content":[{"type":"text","text":"hi there"}]}}' \
  > /tmp/preloop-cursor-test.jsonl
echo '{"conversation_id":"test-conv","generation_id":"test-gen-1","hook_event_name":"stop","status":"completed","loop_count":0,"model":"claude-4.5-sonnet","transcript_path":"/tmp/preloop-cursor-test.jsonl"}' \
  | preloop usage hook --from cursor
```

Then open Cost analytics in the console. The "Imported usage" section
shows a "Conversations" rollup listing `test-conv` with one event,
2 input tokens ("hello" is 5 characters), 2 output tokens ("hi there" is
8 characters) and an estimated amount from the catalog price of
`claude-sonnet-4-5`. The runtime sessions list shows a `cursor` session
`test-conv` with the summary "hi there". Delete
`~/.preloop/agents/cursor/transcripts/test-conv.json` afterwards if you
want to replay the check.

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
ingest API can attribute events to the onboarded Codex agent. Every
shipped record carries the thread's conversation id, so importing a
historical rollout also registers the thread as a runtime session (source
type `codex`) and populates the sessions explorer.

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
- stdin hooks write nothing to stdout, except Cursor's
  `beforeSubmitPrompt`, which answers `{"continue": true}` so Cursor
  never treats the hook as failed. `--file` prints a one-line shipment
  summary

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
