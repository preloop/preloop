# `preloop cursor`: spawn Cursor Agent and record estimated usage

`preloop cursor` starts the Cursor Agent CLI (`cursor-agent`) from the
Preloop CLI. v1 does two things: pass through an interactive session
unchanged, and optionally capture headless print-mode output so estimated
usage can show up in Cost analytics.

Runs bill the user's own Cursor account. Preloop records estimated
usage, not Cursor billing. Quantities `cursor-agent` did not report are
omitted and display as "not reported". They are never stored as 0 or
$0.00. Billed amounts enter Preloop only through a reconciled import
(for example a Cursor dashboard Usage export via `preloop usage import`).

This command is not Agent Control. There is no sidecar, remote takeover,
or pairing flow. For Claude Code, see `preloop claude`. For editor-side
conversation lifecycle (no token counts), see
[Cursor usage hooks](cursor-usage-hooks.md).

## Install cursor-agent

The Cursor Agent CLI is a separate install from the Cursor editor and
from the Preloop CLI:

```bash
# macOS, Linux, WSL
curl https://cursor.com/install -fsS | bash
```

Confirm it is on your `PATH`:

```bash
cursor-agent --version
```

If `preloop cursor` cannot find it, the error names `PATH` and the usual
`~/.local/bin` fallback, and repeats the install command above. Official
install notes live at https://cursor.com/docs/cli/overview.

## Interactive passthrough (no capture)

```bash
preloop cursor
preloop cursor "refactor the auth module"
preloop cursor --plan --model gpt-5
```

Every argument after `cursor` is passed to `cursor-agent`. stdin, stdout,
and stderr are the user's TTY, so the interactive session behaves as if
you had run `cursor-agent` directly.

Structured usage is not available in this mode. Cursor's CLI documents
`--output-format` as valid only with `--print` (non-interactive). v1
does not try to scrape the TUI.

To record editor chats as they happen, wire `preloop usage hook` instead.

## Headless capture: `preloop cursor run`

```bash
preloop login --token <token>
preloop cursor run "summarize this repository"
preloop cursor run --agent-id <managed-agent-uuid> --force "fix the tests"
```

`run` injects `--print --output-format stream-json` unless those flags
are already present, tees stdout so scripts still see the JSON stream,
parses session and (when present) token fields, and POSTs records to
`/api/v1/usage/ingest` with `source=cursor` and `cost_basis=estimated`.

Auth and API URL follow the rest of the CLI: `--token` / `PRELOOP_TOKEN`
/ `~/.preloop/config.yaml`, and `--url` / `PRELOOP_URL` / config / the
default `https://preloop.ai`. Put global flags before `cursor`:

```bash
preloop --url https://preloop.example.com --token "$PRELOOP_TOKEN" \
  cursor run "review these changes"
```

`--agent-id`, `--source`, and `--parent-conversation-id` are Preloop
flags on `run`. Everything else is passed to `cursor-agent`. If the
child already passed `--print` or `--output-format`, Preloop does not
duplicate them. `--output-format json` is accepted and parsed as a
single result object.

### What is captured

Verified against `cursor-agent --help` (2026.01.23-916f423) and Cursor's
CLI output-format reference (https://cursor.com/docs/cli/reference/output-format.md):

| Field | Source | Shipped as |
| ----- | ------ | ---------- |
| `session_id` | `system` init and terminal `result` events | `conversation_id` |
| `model` | `system` init (display name) | `model`, when present |
| `request_id` | terminal `result`, optional | metadata |
| `duration_ms` | terminal `result` | metadata |
| `usage.inputTokens` / `outputTokens` / `cacheReadTokens` | optional on `result`; not in the public result schema, accepted when present | `input_tokens`, `output_tokens`, `cache_read_tokens` |
| `usage.cacheWriteTokens` | same optional object | metadata only (ingest has no cache-write column) |

Prompt text, assistant text, and tool payloads are parsed only enough
to skip them. They are never sent to Preloop.

A `system`/`init` event becomes `event_type=session_start` with
`external_id=sessionStart:<session_id>` (the same key `preloop usage hook`
uses, so a chat observed both ways deduplicates). The terminal `result`
becomes `event_type=usage` when a model name and at least one token
count are present, otherwise `event_type=response`. Missing token
fields are omitted, not zeroed.

`parent_conversation_id` is never inferred from `--resume`. Set it with
`--parent-conversation-id` or `PRELOOP_PARENT_CONVERSATION_ID` when you
know this run was spawned from another conversation.

### Failure behavior

- If `cursor-agent` is missing, the command exits 1 with an install hint.
- The process exit code is the child's exit code (2, 130, and so on),
  not remapped to 1. Scripts and CI can branch on it. Ingest happens
  after wait, so a failed run still ships a record when a `session_id`
  was observed.
- Ingest errors (unreachable server, expired login, bad `--agent-id`)
  print `preloop cursor: ... (usage not recorded)` on stderr and do not
  change the child's exit status.
- No `session_id` in the captured stream means nothing is POSTed.

## Related commands

- `preloop usage hook` ships editor hook lifecycle events (no tokens).
- `preloop usage import` records billed Cursor dashboard exports as
  imported spend. Reconciled per-conversation amounts supersede these
  estimates in Cost analytics summaries; the two bases are never summed.
