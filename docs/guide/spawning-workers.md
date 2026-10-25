# Spawning workers

Name each worker when you start it so concurrent runs are easy to find and steer.
The SessionStart hook reads `PRELOOP_SESSION_ALIAS` and
`PRELOOP_PARENT_SESSION_ID`. Install the hooks first:

```sh
preloop agents onboard "Claude Code" --approvals
```

The worker needs its own installed runtime credential. An alias is a label,
not a credential or a permission grant. Codex and Cursor registration hooks,
and the Claude and Codex runtime plugins, also read these environment variables.

## Start a named worker

Run this from your conductor's shell. Its SessionStart hook exports its own
runtime session id as `PRELOOP_PARENT_SESSION_ID` through `CLAUDE_ENV_FILE`,
so child processes can record it as their parent. Outside that shell, set the
variable to the conductor's full runtime session id from `preloop sessions list`.

```sh
: "${PRELOOP_PARENT_SESSION_ID:?Set this to the conductor runtime session id}"
worker_session_id=$(python3 -c 'import uuid; print(uuid.uuid4())')
PRELOOP_SESSION_ALIAS=brief-api \
PRELOOP_PARENT_SESSION_ID="$PRELOOP_PARENT_SESSION_ID" \
nohup claude -p --session-id "$worker_session_id" \
  'Inspect the API tests and report the next change.' \
  >worker-api.log 2>worker-api.stderr &
worker_pid=$!
printf 'worker pid=%s harness session=%s\n' "$worker_pid" "$worker_session_id"
```

Use a different alias for each brief. Aliases use `A-Z`, `a-z`, `0-9`, `.`,
`_` and `-`, with a maximum of 64 characters. Other characters become `-`.
The alias is write-once and fills an empty title. Active aliases are unique
within an account and parent session, or within the runtime principal when
there is no parent. A clash gets `-2`, `-3`, and so on; the base is shortened
when necessary to keep the result within 64 characters. The hook reports the
final value as `preloop session_alias=brief-api-2` on stderr and in the handoff file.
Ended sessions release aliases for new runs. If a name has been reused,
resuming the original session cannot rename its write-once alias: registration
returns a conflict until the other run ends.

## Find the runtime id and parent

PR #1256 already provides two id handoffs. In `claude -p` mode the hook prints
`preloop runtime_session_id=<id>` once on stderr. It also writes
`~/.preloop/sessions/<external_session_id>.json`, keyed by the harness session id.
Some harness versions suppress successful hook stderr from the captured worker
stderr. The JSON handoff remains available in that case:

```sh
cat worker-api.stderr
cat "$HOME/.preloop/sessions/$worker_session_id.json"
preloop sessions list --parent "$PRELOOP_PARENT_SESSION_ID" --wide
preloop sessions list --alias brief-api --json
```

The JSON contains `runtime_session_id`, `external_session_id`, `started_at`,
`agent_kind`, `cwd`, the recorded `parent_session_id`, and the final `alias`.
Registration is best effort. Wait for the handoff file or a list entry before
sending a note. If there is no entry, inspect the hook errors on stderr and
check that onboarding installed SessionStart hooks and a valid credential.
A generic fallback title alone does not prove registration failed: a first
prompt might not have arrived, and older sessions might predate the hooks.

## Steer one worker or the live children

```sh
preloop notes send --alias brief-api 'Stop after the API tests and report.'
preloop notes send --external-session-id "$worker_session_id" 'Report progress.'
preloop notes send --children latest 'Report the next step.'
preloop notes send --children all 'Pause at the next safe boundary.'
```

Name exactly one target. The CLI uses `PRELOOP_PARENT_SESSION_ID` to identify
the parent for `--children`; `all` selects at most 20 live children. `--session`,
`--agent`, and `--execution` remain available. `--json` returns note ids and the
resolved targets. The MCP `send_note` tool accepts `alias`, `external_session_id`
and `children: latest|all`; `list_sessions` returns aliases and accepts an
`alias` filter. MCP alias lookup prefers the caller's own principal and then
refuses multiple matches as `target_ambiguous`. Human CLI lookup refuses
ambiguous aliases too. Use a runtime session id to disambiguate.

Each agent-authored note still passes the existing note scope check. Parent
lineage enables the default descendant scope; an alias widens no access.
Cross-account lookups return not found.

A successful send means the note is queued. See [Operator notes](operator-notes.md)
for delivery channels. Delivery inspection is tracked in
[#1550](https://github.com/preloop/preloop/issues/1550). A short run can finish
before another turn boundary and leave its note undelivered; timing changes
are tracked in [#1551](https://github.com/preloop/preloop/issues/1551).

## Codex and Cursor conductors

Use the spawn wrapper when the harness cannot export environment variables from
SessionStart. It reads the conductor's registered handoff file and sets
`PRELOOP_PARENT_SESSION_ID` and `PRELOOP_SESSION_ALIAS` for the child:

```sh
preloop spawn --alias api-worker -- claude -p 'Inspect the API tests.'
preloop spawn --session "$conductor_harness_session_id" --alias ui-worker -- codex exec 'Inspect the UI tests.'
```

The wrapper uses `CODEX_THREAD_ID`, `CURSOR_CONVERSATION_ID`, or
`PRELOOP_CURRENT_SESSION_ID` to select the current handoff. Use `--session` when
that environment is unavailable. It refuses an unknown session instead of
choosing the newest file, which could belong to another concurrent conductor.
The default alias is `worker` when `--alias` is omitted. It forwards standard
input, output, errors and the exit status and waits for the child; use the
shell's background operator when needed. Children still need registration hooks.

The PreToolUse approval hook adds `preloop_caller_session_id` to Preloop
`send_note` and `list_sessions` calls through `updatedInput`. The server resolves
this harness id only under the authenticated credential's runtime principal.
A session-bound credential always retains its bound session. This field cannot
claim another principal's lineage, and the target alias grants no access.

Every account receives editable `send_note` rules in the Tools console: allow
self and descendants, ask the sender's human for other runs that human can
control, and deny the rest. Approval uses the standard push and email channels,
respecting that human's notification preferences. Async workflows return
`pending_approval`; poll `get_approval_status` with the request id to execute
the approved note. Declined or expired requests deliver nothing. The approval
records the resolved target and scope facts so an alias change cannot redirect it.
