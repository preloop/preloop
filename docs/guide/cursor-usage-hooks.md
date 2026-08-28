# Cursor hooks: live conversation tracking in Cost analytics

Cursor's bundled models never pass through the Preloop model gateway, so
Preloop cannot meter that traffic itself. The `preloop usage hook` command
closes part of that gap: wired into Cursor's hooks, it ships conversation
lifecycle events to `POST /api/v1/usage/ingest` as they happen, so your
chats and their subagent conversations appear in the Cost analytics
conversation rollup in near real time.

Hook payload fields referenced below come from Cursor's hooks reference
(https://cursor.com/docs/agent/hooks, checked 2026-08-27).

## What this does and does not record

Cursor hook payloads carry no token counts and no billed amounts. This
integration therefore records lifecycle facts only:

- which conversations were active, and when
- session, response, subagent, and compaction lifecycle events
- parent and child conversation links (`parent_conversation_id` from
  `subagentStart` payloads)
- message and tool-call counters where Cursor reports them
  (`subagentStop`), as growth tripwires

Every shipped record is marked with the `estimated` cost basis and carries
no cost figure. In the console, quantities the source never reported show
as "not reported", never as 0 or $0.00. Billed amounts enter the system
only through a reconciled import (for example a Cursor dashboard Usage
export via `preloop usage import`, or a billing feed pushed to the ingest
API with `cost_basis=reconciled`). Estimated and reconciled amounts are
always displayed separately and are never summed together.

Privacy: the shipper sends ids, event names, statuses, the reported model
name, and counters. It never sends prompt text, task descriptions,
subagent summaries, file paths, or shell commands, even though hook
payloads contain them.

## Prerequisites

1. The Preloop CLI installed and logged in (`preloop login`), with a user
   allowed to import usage.
2. A managed Cursor agent to attribute events to. Either onboard one with
   `preloop agents onboard cursor`, or pass `--agent-id` explicitly in the
   hook command.

## Configure Cursor

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

## Event mapping

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

## Subagent conversations

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

## Failure behavior

The command is fail-open by design:

- the ingest request is capped at 3 seconds
- any error (server unreachable, expired login, malformed payload) is
  printed to stderr and the command still exits 0
- nothing is written to stdout; all shipped events are observational for
  Cursor, and Cursor treats hook exit codes other than 2 as fail-open

A failed shipment means one missing lifecycle event, nothing more.

Timestamps are the shipper's receive time: Cursor hook payloads carry no
event timestamp, and hooks fire at the moment of the event, so the skew
is at most process startup plus queueing.

## Verify the wiring

Simulate a hook invocation from a shell:

```bash
echo '{"conversation_id":"test-conv","generation_id":"test-gen-1","hook_event_name":"stop","status":"completed","loop_count":0}' \
  | preloop usage hook
```

Then open Cost analytics in the console. The "Imported usage" section
shows a "Conversations" rollup listing `test-conv` with one event and its
tokens and costs marked "not reported" (hooks report neither).

## Reconciling billed amounts later

When you have actual billed figures, import them separately:

- `preloop usage import <cursor-usage-export>.csv` records the dashboard
  export as imported spend (totals and per-model figures; the export
  carries no conversation ids).
- A billing source that does report per-conversation amounts can push
  them to `POST /api/v1/usage/ingest` with `cost_basis=reconciled` and
  the `conversation_id`; reconciled records then supersede hook-derived
  estimates for that conversation in summary totals, and the conversation
  rollup shows both figures side by side.
