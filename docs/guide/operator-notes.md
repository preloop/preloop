# Operator notes

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

An operator note is a short instruction from an identified human to a running
agent. You type it in the console, the CLI or the API; the agent receives it at
its next turn boundary; the note is recorded as a human decision, with who sent
it, when it landed and on which turn. An agent can author one too, through an
opt-in tool, and the record says so; see
[A note from an agent](#a-note-from-an-agent).

It exists because the alternative in an unattended run is to kill the agent and
start again. A note is the cheaper correction: "the staging cluster is the one
in eu-west-1", "stop refactoring the tests, ship the fix", "the customer changed
the deadline". It is a steer, not a policy override. A note cannot approve a
tool call, lift a kill switch, or grant anything the sender does not already
have.

## What it costs

No extra model input tokens when there is no note. The agent never polls, never
decides to check an inbox, never spends a token asking. Delivery is a piece of
work Preloop is doing anyway: the gateway is already assembling the outbound request body, and
the permission hook is already making a round trip for the tool call. A session
with no pending note reads one indexed row and appends nothing.

Native model-input plugins also use bounded note API requests at the model
boundary. These add a network round trip even when the inbox is empty, but no
extra model input tokens. Session registration is cached for the current run.

The turn that carries a note is a cache miss. Providers serve cached input only
for a prefix that is byte-identical to a previous request, and a note is
appended after the last cached block, so the turn that delivers it pays uncached
input pricing for the segment it lands in and writes a new cache entry for the
turns after it. On Anthropic, where the note becomes one more text block on the
trailing user message, that means the final cache segment; on OpenAI, where
caching is prefix-automatic, it means the tail of the prompt. One turn, once
per note batch. It is the same cost as the human having typed the sentence
themselves, which is exactly what happened.

## Sending one

Console: the **Operator notes** card on the agent page and on a running flow
execution. Enter sends, Shift+Enter is a newline. Each note shows its state, and
an undelivered note can be withdrawn from the same card.

CLI, which is where an operator running an agent already is:

```bash
preloop notes send --agent 0b0d... "Deploy to eu-west-1, not us-east-1."
preloop notes send --session 5a3e... "Stop refactoring the tests, ship the fix."
preloop notes send --execution 9f2b... "The deadline moved to Friday."
```

Name exactly one of `--agent`, `--session` or `--execution`; naming none or
naming two is refused before any request is made. The body is the argument,
and with no argument it is read from standard input, so a multi line note can
be piped or written in a heredoc:

```bash
cat <<'NOTE' | preloop notes send --agent 0b0d...
Two things:
  1. the cluster is the one in eu-west-1
  2. do not touch the tests
NOTE
```

`--expires-in 2h` overrides the 24 hour default. `--json` emits the note id
and the target and nothing else, for scripts. A refusal prints the server's
reason, including an unresolvable target and the rate limit, and exits
non-zero. Track a receipt or list the notes for a target:

```bash
preloop notes status <note-id> --json
preloop notes list --session <session-id> --state claimed
preloop notes list --agent <agent-id> --json
preloop notes list --execution <execution-id>
preloop notes send --session <session-id> --wait 5m "Run the final checks."
```

`status` prints state, channel, creation time and delivery or terminal time.
`list` takes exactly one target and optionally filters by `--state`. Both accept
`--json` for the receipt fields, including the author and transition timestamps.
Against an older server, a status-route 404 prints "server does not support note
status, or the note was not found in this account"; the same response cannot
distinguish a missing note from an unsupported route.

`send --wait` polls only that note's status, every two seconds, for at most the
positive duration. Exit codes are 0 for delivered (or acknowledged), 3 for
undeliverable or expired, and 4 for timeout. Other errors exit 1. With `--json`,
waiting returns the final receipt as one JSON document; without waiting it
keeps the note-id-and-target output. Delivery means the carrying output was
written, not that the model followed the instruction.

### Finding the session to steer

`--session` needs a session id. `preloop sessions list` shows them, most
recently active first, with the short id, the agent, when it started and last
did something, whether it is live, idle or ended, its tool and model call
counts, how many approvals are waiting on a human, and a title:

```bash
preloop sessions list --active
```

```text
ID        AGENT   STARTED  LAST ACTIVITY  STATE  TOOLS  MODEL  PENDING  TITLE
aaaaaaaa  Worker  9m ago   30s ago        live   7      12     1        claude_code alpha 2026-10-02 11:50:07Z
bbbbbbbb  Worker  9m ago   5m ago         idle   2      3      0        claude_code beta 2026-10-02 11:50:07Z
Steer: preloop notes send --session <id>. Watch: preloop sessions attach <id>.
```

A session the server has not titled yet is labelled with its agent kind, the
working directory its hook last reported and its start time, so two workers
started in the same second are still two different rows. `--wide` prints the
full id that `notes send --session` takes, and `-o id` prints only full ids,
one per line, for a pipe:

```bash
preloop sessions list --agent "Release worker" --active -o id | head -1 \
  | xargs -I{} preloop notes send --session {} "Ship the fix, leave the tests."
```

Narrow the list with `--agent <id|name>`, `--kind claude-code`, `--since 2h`,
`--parent <session-id>` (the sessions a conductor spawned) and
`--execution <id>`. The server applies every filter across the account. See
[the CLI reference](cli.md#sessions). To watch the session and steer it from
the same terminal, attach to it:
[Attaching to a session from the terminal](sessions-attach.md).

API:

```bash
curl -X POST https://your-preloop/api/v1/operator-notes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id": "0b0d...", "text": "Deploy to eu-west-1, not us-east-1."}'
```

Name exactly one target: `agent_id`, `runtime_session_id` or `execution_id`. A
note addressed to an agent with no live session waits for the next session that
agent opens, which is how you brief a run before it starts. A durable enrollment
identity is not a live harness conversation: notes sent with `agent_id` stay
agent-scoped until an actual conversation claims them. Use `runtime_session_id`
(or `notes send --session`) to pin a note to one particular run. `expires_in_seconds`
overrides the 24 hour default; a note nobody delivered in a day is stale advice,
and expiring visibly beats rotting silently.

To brief a future run while other sessions are still open, send
`{"agent_id": "<agent UUID>", "next_session": true, "text": "Run the checks."}`.
Only a session started after the note was sent can claim it. `next_session`
requires `agent_id`; omitting it preserves current-or-next delivery. The receipt
keeps `next_session: true` after delivery and names the session that claimed it.

- `GET /api/v1/operator-notes?agent_id=...` (or `runtime_session_id`,
  `execution_id`) lists notes newest first with their delivery state and receipt
  timestamps. Add `state=pending` (or another public state) to filter before
  the result limit is applied.
  Combine `agent_id` and `runtime_session_id` to filter an agent's history to
  one conversation; `limit` and `offset` page the results with a matching `total`.
- `GET /api/v1/operator-notes/{note_id}` reads one receipt with the same account
  and permission checks; foreign and missing ids both return 404.
- `POST /api/v1/operator-notes/{note_id}/cancel` withdraws one that has not
  been delivered. Cancelling is a state, never a delete. A delivered note
  cannot be unsent, so cancelling one returns it unchanged and you can see why.

Mobile is out of scope for now. The iOS and Android clients already hold an
account session, so they can use the same account-scoped endpoints above.

## A note from an agent

An agent can leave a note for another agent through the `send_note` builtin
tool, so a hand off between two runs stops going through a person or a file
nobody sweeps. It is off by default and has to be enabled per agent or per
flow, like any other builtin: an agent that was never given the tool does not
see it in its tool list and is refused if it calls it anyway.

```json
{"text": "The migration is applied; run the backfill.", "agent_id": "0b0d..."}
```

Same one-target rule, same store, same delivery rail, same 4096 character and
20-per-hour ceilings, and the same account boundary: the target is resolved in
the calling agent's account, so an id from another account is simply not found.
Naming no target or two is a structured refusal the model can correct on its
next turn, not an exception, and no note row is written.

What differs is only the author. The row records the calling managed agent
rather than a user, the envelope carries `"authMethod": "agent"` and a display
name suffixed `(agent)`, and the audit row for the send names the agent as the
actor. The delivered block's framing sentence follows the authors inside it:
an all-agent delivery says the named agent does not hold the permission to
stop this run, and a mixed human-plus-agent delivery names both authorities
instead of wrapping the sibling's text in the human-stop sentence. The
per-note `from` and `auth` attributes still name the author either way. The
note still grants nothing: every action taken because of it goes through the
firewall, the gateway and the approval policy as before.

An agent cannot note whatever it likes inside the account. What it can reach is
[the runs it started](#which-agent-may-note-which-target).

### Steering the runs you started

A conductor agent that starts workers from a shell (`claude -p "..." &`) can
steer them with `send_note` under the default scope, with no grant and no
lookup through the API. Three pieces make that work once the Preloop hooks are
installed (`preloop agents onboard`):

1. **Lineage.** The SessionStart hook registers each run and exports its
   runtime session id to the commands the run starts, as
   `PRELOOP_PARENT_SESSION_ID`. A child whose own SessionStart hook sees that
   variable records the spawner as its `parent_session_id`, so the child is a
   descendant of the conductor exactly like a child flow execution. The
   parent must be in the same account, and the field is write-once.
2. **The id at spawn.** SessionStart writes
   `~/.preloop/sessions/<external_session_id>.json` with the
   `runtime_session_id`, `started_at`, `agent_kind` and `cwd`, and in
   non-interactive mode (`claude -p`) prints one line to stderr:
   `preloop runtime_session_id=<id>`.
3. **Discovery.** The `list_sessions` builtin (off by default, enable it like
   `send_note`) returns the caller's live children with no arguments: id,
   start time, agent kind, cwd, parent, title and whether it is active now.
   Listing another session's children, or the whole account
   (`parent_session_id: "any"`), needs the same operator grant as
   `search_sessions` scope `account` and is refused by name without it.

The spawn recipe, from inside the conductor's Bash tool:

```bash
SID=$(uuidgen)
nohup claude -p --session-id "$SID" "Fix the flaky test in api/" \
  >"/tmp/worker-$SID.log" 2>&1 &
sleep 5
jq -r .runtime_session_id ~/.preloop/sessions/"$SID".json
```

Then any of these reaches that worker at its next turn boundary, recorded with
the conductor as author:

```json
{"text": "Skip the docs, only the test.", "runtime_session_id": "<id from the file>"}
{"text": "Skip the docs, only the test.", "external_session_id": "<SID>"}
{"text": "Wrap up and push.", "children": "all"}
```

`external_session_id` is the harness's own session id (the Claude Code
`session_id`, which is also its transcript file name). It resolves inside your
account and is refused as `target_ambiguous` if more than one run carries it.
`children` takes `latest` (the newest live run your session started) or `all`
(one note each, at most 20, each checked and rate limited on its own). Exactly
one target field is still the rule.

The hook also sends the first prompt once, so the server sets a redacted title
of at most 120 characters and a principal name of agent kind plus working
directory. Two workers started in the same second show distinct titles in the
console, `preloop sessions list` and `GET /api/v1/runtime-sessions`.

`send_note` knows which session called it from the credential, or, for a
durable enrolled-agent key, from the harness session header on the MCP request
matched against that key's own runs. A call that cannot be tied to a session
is refused with `note_scope_no_lineage`; name the target by
`runtime_session_id` from a session that can be, or use
`preloop notes send --session` as the operator.

## Which agent may note which target

A note from an agent is text a model will read on its next turn, so the blast
radius of this channel is the other agent's behaviour, not just its transcript.
The default is therefore narrow enough that turning `send_note` on is not
itself a decision that needs a threat model.

**The default policy allows self and descendants.** New accounts and existing
accounts whose startup backfill succeeds receive editable tool access rules for
`send_note`. A failed account is logged separately and retried on the next
startup; its existing rules remain in effect until seeding succeeds:

| Target | Default decision |
| --- | --- |
| The calling run, a child, or a deeper descendant | Allow |
| Another run the sender's human can control from the console | Require that human's approval |
| A run the sender's human cannot control | Deny |
| Anything in another account | Not found |

Preloop resolves execution and runtime session lineage before evaluating these
rules. The PreToolUse hook supplies `preloop_caller_session_id`; the server only
accepts a session owned by the calling credential's principal. A session-bound
credential cannot switch its session. See [Spawning workers](spawning-workers.md)
for the hook and `preloop spawn` setup.

Approval uses the existing workflow service and push/email notification channels.
An asynchronous workflow returns `pending_approval`; `get_approval_status` delivers
the note once approved. Decline and expiry write no note. The request records the
resolved target, relation and human permission facts in its rule context.
The request's token link authorizes that bound human's approve or decline
decision without requiring sign-in. Automatic approval bypasses do not decide
these requests. A sender can queue at most three live approvals per target;
pending or declined requests do not spend the hourly note delivery allowance.

A refusal is a structured answer the model can act on, naming the scope the
call would have needed, and each one has its own code:

| Reason code | What happened |
| --- | --- |
| `note_scope_out_of_scope` | The target exists in the account but does not descend from the calling run |
| `note_scope_no_lineage` | The call carries no execution lineage, so it reaches nothing |
| `note_scope_denied_by_rule` | A tool access rule denied it |
| `note_scope_grant_unavailable` | The approval workflow is unavailable, or policy could not be evaluated. Scope widening fails closed |
| `note_approval_rate_limited` | Three live note approvals from this sender are already pending for the target |

Every refusal writes one `agent.note_scope_denied` audit row carrying that code,
the target, the calling run and the rule that decided, so "my agent says it
cannot reach that run" is answerable from the record.

### Granting a wider scope

Every target is evaluated by the standard tool policy path after resolution.
An explicit allow rule can grant wider access without approval: the default allow that a tool with no rules returns is not, or
enabling the tool would quietly mean "note anyone". Add the rule on the
`send_note` tool in the console, or through the tool access rule API:

```
tool:       send_note
condition:  has(args.note_scope) && args.note_scope == "account"
type:       cel
action:     allow
priority:   20
```

Rules are evaluated in priority order and the first match decides, exactly as
for every other tool, so a `deny` placed above a grant wins. Disabling or
deleting the grant takes the reach away on the next call: there is nothing to
restart and no cache to clear.

The scope facts below are available on every `send_note` policy evaluation.
A `has()` guard also makes exported rules safe in simulations without scope facts.

These bindings are what a rule sees. Every one of them is what the platform
worked out, never anything the calling agent asserted:

| Binding | Value |
| --- | --- |
| `args.note_scope` | `account`, the scope being asked for |
| `args.note_default_scope` | `descendants` |
| `args.same_owner` | Author and target agents have the same human owner |
| `args.caller_user_can_steer_target` | The sender's human passes the console operator-note permission check |
| `args.note_target_relation` | `self`, `descendant`, `ancestor`, `same_tree`, `unrelated` or `no_lineage` |
| `args.note_author_managed_agent_id`, `args.note_author_execution_id` | Who is asking, and from which run |
| `args.note_target_managed_agent_id`, `args.note_target_runtime_session_id`, `args.note_target_execution_id` | What it wants to reach |
| `args.text`, `args.agent_id`, `args.runtime_session_id`, `args.execution_id` | `text` is the caller's. On the grant path the id keys are the target as the platform resolved it, under the names the tool uses, and can be present even when the caller never named them. Key on the `note_*` facts, which name author and target unambiguously. |

The grant evaluation uses the same subject context as the preceding `send_note`
call: the caller's `api_key_id`, the caller's `runtime_session_id`, and the rest
of that chain. Target identity lives only in the `note_*` facts, so an
API-key-scoped rule is not skipped on the grant path and a rule against
`runtime_session_id` still means the caller. Top-level `execution_id` in the
rule context is the author run only on grant evaluation. On a plain
`send_note` call it is unbound.

So a grant can be narrower than "anyone". `args.note_target_relation ==
"same_tree"` lets runs in one delegation tree note each other and nothing
outside it; `args.note_author_managed_agent_id == "..."` grants one coordinator
the account and leaves every other agent on the default.

There is no scope wider than `account`. The account boundary is resolved before
scope is considered and no rule can widen past it.

### The honest part

A delivered note is untrusted input rendered into another agent's turn. The
`(agent)` suffix and the `auth="agent"` attribute tell a reader where it came
from, but a label is not a boundary against a model: a sufficiently confused
agent can be talked into acting on a note it should have ignored. What the
scope model buys is that only a run you started can put the words there, that
the grant to do more is a rule somebody wrote and can revoke, and that both the
send and the refusal are in the audit trail. What stops the note from being
consequential is unchanged: it grants no permission, and every action the
reader then takes still goes through the MCP firewall, the gateway and the
approval policy.

## How it is delivered

Two paths. Both put the note at a turn boundary, never inside a tool result and
never mid-stream.

**Gateway (primary, any harness).** Every governed model call passes through
the gateway, which already knows the account, the managed agent and the runtime
session. Immediately after the request policy runs, and before anything is sent
upstream, a pending note is appended to the end of the conversation in the
protocol's own shape:

| Protocol | Where the note lands |
| --- | --- |
| OpenAI chat completions | A trailing `user` message |
| OpenAI Responses | A trailing `input` entry with an `input_text` part, and the normalized message list |
| Anthropic messages | One more `text` block on the trailing `user` message, after any `tool_result` blocks, or a new `user` turn when the conversation ends on the assistant. User/assistant alternation is preserved |
| Gemini | Through the Responses path it already translates to |

Streaming and non-streaming entry points behave identically: the note goes in
before the stream opens.

**Hook (agents that bypass the gateway).** The permission hook your harness
already runs carries the note back with the decision, so a note costs no extra
round trip: `POST /api/v1/agents/permission-check` answers with an
`operator_note` field holding the rendered block, or `null` when there is none,
which is almost always. Preloop stamps `delivery_channel = hook`; the text, the
label and the note ids are the same ones the gateway path delivers.

The second hook route is `POST /api/v1/agents/notes/pending`. It authenticates
with the managed-agent runtime bearer token and returns the claimed notes three
ways at once: the raw envelopes, the rendered block, and a ready
`notifications/claude/channel` event. `channel` is one of `hook`,
`claude_channel` or `claude_message`; `gateway` is recorded only by the
gateway path. `event` names the harness lifecycle point that asked
(`session_start`, `stop` or `pre_tool_use`) and is stamped on the delivery, so
a Stop rescue is distinguishable from the ordinary next-tool-call path. Call it
from the hook or bridge process, never from the model. Gateway and carrying
hooks record claim and delivery together. A non-carrying hook uses
`defer_delivery=true` and gets a `claimed` receipt. After writing the block into
carrying output, it posts `/api/v1/agents/notes/ack` with `note_ids` and the
harness `session_id`. Receipt failures do not change the tool decision. Foreign,
unclaimed and already finished ids are silently ignored.

| Harness | What fires | How to pick the note up |
| --- | --- | --- |
| Claude Code | `PreToolUse`, installed by `preloop agents onboard --approvals` | `operator_note` on the permission-check response, written into `hookSpecificOutput.additionalContext` |
| Claude Code (session start / stop) | `SessionStart`, `Stop` and `SessionEnd`, installed by `preloop agents onboard --approvals` | `SessionStart` (and the first `UserPromptSubmit`) pulls `/agents/notes/pending` with `event=session_start` and returns `hookSpecificOutput.additionalContext`, so a note sent before the run starts lands before the first tool call. `Stop` pulls with `event=stop` and, when something is pending, blocks the stop once with the rendered block as the continuation reason, so the model reads it and may act. `SessionEnd` posts `/agents/session-end`, which marks a note whose only session ended `undeliverable` instead of letting it expire |
| Claude Code (channels) | An MCP channel server you run | `POST /agents/notes/pending`, then push `channel_event` as `notifications/claude/channel`. The harness wraps our block in its own `<channel source= severity=>` tag |
| Claude Code (cross-session messaging) | A bridge process holding `CLAUDE_CODE_MESSAGING_TOKEN`, with `crossSessionInbound: accept` on headless `-p` workers | `POST /agents/notes/pending`, then post `text` to the session inbox socket. The harness delivers it between tool calls |
| Codex CLI | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PermissionRequest`, `Stop`, `SessionEnd` | `operator_note` on the permission-check response, written into the `PreToolUse` `hookSpecificOutput.additionalContext`. `PermissionRequest` has no field for it, so a note claimed there rides the next `PreToolUse`. Codex 0.162.1 supports the same startup context, bounded Stop continuation and SessionEnd receipt shapes as Claude Code |
| Cursor CLI | `sessionStart`, `beforeSubmitPrompt`, `beforeShellExecution`, `beforeMCPExecution`, `preToolUse`, `stop`, `sessionEnd` | `operator_note` on the permission-check response, written into the `preToolUse` `additional_context` on allow and deny. The two `before*` hooks have no field for it, and on this build `preToolUse` collects `additional_context` only on allow and deny, so a note claimed on `ask` also rides the next carrying call. Startup uses `additional_context`; stop uses `followup_message` on completed turns with a maximum of three continuations, respecting `loop_count`. `beforeSubmitPrompt` registers the session without claiming notes; `sessionEnd` reports the native session id |
| OpenCode | `experimental.chat.system.transform`, `session.deleted`, via `@preloop-ai/opencode-plugin` | OpenCode 1.18.32 supplies a session id and mutable `system` array before each model request. The plugin registers the session, claims with `defer_delivery=true`, appends the rendered block and acknowledges its `note_id` values only after append succeeds. Failed acknowledgments are retried before another pull. `session.deleted` reports an end; `session.idle` remains resumable |
| Copilot CLI | `sessionStart`, `sessionEnd`; native `preToolUse` is non-carrying | Session lifecycle commands register native `sessionId` and report its end alongside usage hooks. Native tool checks leave notes pending; no verified model-visible context or continuation field is installed |
| Devin CLI | `SessionStart`, `UserPromptSubmit`, native permission hooks | Session registration only. Permission hooks have no verified note carrier; notes stay pending. Managed runner termination supplies end receipts |
| Pi, Nanobot | Gateway model request; native lifecycle plugins | Native tool gates leave notes pending. Notes can arrive on the next model request routed through Preloop; the plugins supply native session identity on model requests |
| DeepSeek Harness | Native pre-step middleware and `llm/stream` observer | The plugin registers the native session and appends notes as SDK-supported logged user messages. It acknowledges only when the exact note appears in the frozen history admitted to a model call. Its awaited teardown effect reports session end. Native tool gates leave notes pending; gateway routing alone does not supply per-request native session headers |
| Hermes | `pre_llm_call`, `pre_api_request`, optional `on_session_finalize` | Turn-prologue context carries notes; an exact witness in the sanitized API request confirms admission. Notes arriving mid-turn wait for the next turn. Per-turn `on_session_end` releases unadmitted claims without ending the session; only supported `on_session_finalize` reports a hard close |
| OpenClaw | `before_prompt_build`, `llm_input`, diagnostic model-call events, `session_end` | Notes enter `prependContext`; exact input and matching actual-call diagnostics confirm admission. Preflight rejection cannot acknowledge delivery. Native notes require SDK prompt-injection and conversation-access permissions and diagnostic hooks; hosts without them keep notes pending. Only authoritative new/reset/deleted ends terminalize receipts; idle, compaction and shutdown remain resumable |

The native capabilities above are checked against Codex CLI 0.162.1 embedded
hook schemas, Cursor's [hook reference](https://cursor.com/docs/agent/hooks)
(Cursor Agent version `2026.10.01-e373342` observed), and OpenCode 1.18.32's
plugin SDK, DeepSeek Harness SDK 0.1.5-rc.2, Hermes revision
`8f817911723e3ec13e7f47fc022fdcc9e2149354`, and OpenClaw v2026.6.11.
Fixture and loopback harness tests establish protocol behavior;
real provider smoke results are tracked in #1637. Re-onboard after updating the
CLI or runtime plugin to install the lifecycle commands. Usage-hook removal
preserves the independently installed lifecycle commands; offboarding removes
both.

Other supported onboarding targets have narrower transport capabilities:

| Target | Operator-note boundary |
| --- | --- |
| Gemini CLI | Next model request routed through the managed gateway; no native lifecycle note hook is installed |
| VSCode / Copilot | A manually configured BYOK gateway route can carry notes; MCP onboarding alone does not inject context |
| Claude Desktop | An explicitly configured supported gateway route can carry notes; MCP onboarding alone does not inject context |
| Cursor/Windsurf IDE | Cursor native hooks are covered above where emitted; Windsurf has no installed note carrier |
| Antigravity | MCP firewall integration only; no model route or native note carrier |
| Browser-use, Skyvern | Import/observation adapters; importing activity does not inject instructions into a running vendor session |

A gateway route must be used by the actual model request to deliver a note.
MCP tool access, an approval decision, and a usage event alone do not place text
in the model context. A managed runner's authoritative termination and the
server's reconciliation of ended sessions supply terminal receipts independently
of native hook support (#1636). A silent or idle live session is never treated
as ended solely to produce a receipt. Remaining native carriers and gateway
session-identity limitations are tracked in #1638 and the adapter work
#1489/#1494.

For the Claude Code transports, Preloop supplies the text, the identity and the
record; the harness supplies the last hop.

The permission hook renders the block itself for Claude Code, Codex CLI and
Cursor CLI: nothing to install beyond `preloop agents onboard --approvals`, and
a turn with no pending note produces exactly the response it produced before.
Each field above was read from the installed harness, which is why they differ:
only some hook events have a field that reaches the model at all. Codex accepts
`additionalContext` on `PreToolUse` and rejects any unknown field on a
`PermissionRequest` response; Cursor keeps `additional_context` for
`preToolUse` on allow and deny and strips it from `beforeShellExecution` and
`beforeMCPExecution`. On this build `preToolUse` collects `additional_context`
only on allow and deny, so a note claimed on `ask` also rides the next carrying
call. A note claimed by one of those hooks is held for its session and written
into the next tool call's carrying hook, once, which is one tool call later and
still a turn boundary. The versions each shape was captured from are recorded
in `cli/internal/cmd/testdata/operator-notes/`.

The channel and inbox bridges remain a follow-up: they need a process the
operator runs, not a hook Preloop already installs.

A note whose harness fires no tool call and makes no model call would never be
delivered. On Claude Code two more hooks close that gap: a note sent before the
run starts rides `SessionStart`, and a note that arrives after the last tool
call is rescued by `Stop`, which keeps the headless run alive for one more step
so the model can read it. At most three Stop blocks are spent per session, so a
stream of notes cannot pin a worker in a loop; past that cap the note waits and
`SessionEnd` marks it `undeliverable` rather than letting it sit `pending` until
its 24h expiry and look like the agent ignored it. A note addressed to the
*agent* with no session is untouched by a session ending: it keeps waiting for
the agent's next run.

The lifecycle hooks are installed for Claude Code only. Codex CLI 0.162 has
`Stop`, `SessionStart` and `SessionEnd` hook events too, but `onboard
--approvals` does not wire them yet, so a Codex note still relies on
`PreToolUse` and, if the session ends first, expires after 24h. Cursor CLI is
unchanged. The Claude Code input and output shapes for these events are pinned
in `cli/internal/cmd/testdata/operator-notes/claude-code-lifecycle.json`.

A Stop block is only spent when `stop_hook_active` is false, which Claude Code
sets on the stop that follows a block. A headless `claude -p` run therefore
continues for at most one extra model step per stop, and a note that arrives
during that step waits for `SessionEnd`. If the hook names a run the caller's
credential never registered, `session-end` changes nothing. There is still no path that pushes into a process that is
not asking Preloop anything.

## Delivery receipts

| State | Meaning | Receipt time |
| --- | --- | --- |
| `pending` | Waiting for a carrying call or hook | `created_at` |
| `claimed` | A hook has spooled the block or a native carrier is awaiting model-input confirmation | `claimed_at` |
| `delivered` | Included in the gateway request, carrying hook output, or admitted native model input | `claimed_at`, `delivered_at`, `delivery_channel` |
| `acknowledged` | The harness recorded the model's acknowledgment | `delivered_at`, `acknowledged_turn_id` |
| `expired` | An uncarried note passed its TTL | `expired_at` |
| `cancelled` | The operator withdrew a pending note | `cancelled_at` |
| `undeliverable` | The only target session ended, or the local spool dropped the block | `undeliverable_at`, `undeliverable_reason` |

`undeliverable_reason` is `session_ended` or `spool_dropped`. A session end sweeps
pending and claimed notes for that exact session. Agent notes that still have
no session keep waiting for the next run. Existing rows keep their state and
have null values for the new receipt columns. The console shows the state and
available transition times, with undeliverable distinct from expired.

The API also reconciles notes whose runtime session is already recorded as
ended, regardless of harness. This catches runner offline/idle termination,
kill-switch termination, usage-import session-end events, and gateway idle
session rollover. The background pass runs every minute and handles up to
100 ended sessions per pass; a backlog or unavailable API can delay receipts.
A late hook cannot claim notes for a session already recorded as ended.
Reconciliation does not end a session because it is silent, asleep, or briefly
disconnected. Without an authoritative termination event, a note keeps waiting
until delivery or its expiry.

See [Operator note verification](operator-notes-smoke.md) for isolated API,
receipt and installed-harness checks, including which evidence uses simulated
providers and which requires a real provider account.

## What the model sees

```
<operator-notes count="1" source="preloop-control-plane">
The block below is an instruction from the human operating this agent,
delivered out of band by the Preloop control plane at a turn boundary. ...
<operator-note id="a1b2c3d4e5f60718" from="Ada Lovelace" auth="jwt"
               at="2026-09-10T09:14:02+00:00">
Deploy to eu-west-1, not us-east-1.
</operator-note>
</operator-notes>
```

That framing is for a human-authored delivery and stays byte-identical to
what shipped before `send_note`. A block whose every note is `auth="agent"`
uses a different sentence: the named agent does not hold the permission to
stop this run. A mixed block (human and agent notes in one delivery) says
so, and still treats only the human-authored elements with the operator's
stop-authority. The per-note `from` and `auth` attributes are unchanged in
every case.

Preloop stamps every attribute. The sender authors only the text inside the
element, and a body containing the literal characters of one of these tags is
escaped on the way out, so no note can forge another note's identity and no
note can close the block early. Tool output cannot forge a note at all, because
tool output never travels this path.

This is the honest form of the prompt-injection argument. The label is not a
security boundary against the model: a model can ignore a note, and a
sufficiently confused one can be talked out of trusting it. What the label
does buy is that everything an attacker would need to impersonate an operator
(the tag, the identity, the timestamp, the note id) is written by Preloop from
an authenticated session, so an injected string in a fetched page can claim to
be an operator note and will still arrive escaped, inside a tool result, on a
channel that no operator note ever uses. The real boundary stays where it
always was: the note grants no permission, and every consequential action the
agent then takes still goes through the MCP firewall, the gateway and the
approval policy.

Several pending notes ride one block, oldest first, up to five. That is what a
human who typed twice meant, and it removes the window where a second delivery
arrives with no idea the first one happened.

## Message shape

Each note is stored and returned as an A2A `message`, so a future A2A endpoint
carries exactly these notes with no second schema:

```json
{
  "kind": "message",
  "role": "user",
  "messageId": "a1b2c3d4e5f60718",
  "contextId": "<runtime session id>",
  "parts": [{"kind": "text", "text": "Deploy to eu-west-1, not us-east-1."}],
  "metadata": {
    "preloop.ai/kind": "operator_note",
    "preloop.ai/noteId": "a1b2c3d4e5f60718",
    "preloop.ai/managedAgentId": "0b0d...",
    "preloop.ai/author": {"userId": "...", "display": "Ada Lovelace", "authMethod": "jwt"},
    "preloop.ai/createdAt": "2026-09-10T09:14:02+00:00",
    "preloop.ai/expiresAt": "2026-09-11T09:14:02+00:00"
  }
}
```

`role: user` is deliberate: A2A reserves `user` for the client side of a task,
and an operator note is a human turn, not the agent's own output.

## Exactly once

A note is marked delivered before the request that carries it leaves Preloop,
by an update guarded on the note still being pending. Two concurrent turns
cannot both claim it; the loser delivers nothing rather than sending the
instruction twice. A retried upstream attempt re-enters nothing, and a client
that replays the whole request finds no pending note.

The failure this trades away is a note marked delivered into an upstream call
that then failed. That is the better failure of the two. A second delivery
would arrive with no idea the first one happened, and the sender can see the
delivery state either way and resend deliberately.

## Who can send, and what is recorded

Sending, listing and cancelling take `control_managed_agent`, the same
permission that lets the caller stop the agent. If you can kill it, you can
steer it and see the notes; if you cannot kill it, you cannot put words in
its context or read them. Account owners and superusers hold it implicitly.
Viewers get 403 with the required permission named, on Enterprise and on the
open-source build alike.

A note never crosses an account. Every target is resolved with an
account-scoped query, so an id from another account is a 404 and can never
become a delivery, and the candidate query used at delivery time is itself
bounded by the account.

Limits: 4096 characters per note, 20 notes per author per agent per hour
(or per session, when the target has no managed agent). An agent author counts
against the same ceiling, keyed on the authoring agent. Note
bodies are stored in the clear, exactly as approval comments are, because both
are operator text that has to be readable in the audit trail and in the
timeline. Do not put secrets in a note; use the credential store.

Four audit actions, all written in the same transaction as the thing they
describe:

| Action | Written when |
| --- | --- |
| `agent.note_sent` | Before the API tells the author it worked |
| `agent.note_delivered` | Before the request carrying the note leaves Preloop |
| `agent.note_scope_denied` | Instead of a note, when an agent's target was out of [its scope](#which-agent-may-note-which-target) |
| `agent.note_undeliverable` | When session-only notes can no longer arrive (`session_ended`), or a local spool drops a claimed block (`spool_dropped`) |

Three arrive as webhook events, `agent.note_sent`, `agent.note_delivered` and
`agent.note_undeliverable` (see [webhooks](webhooks.md)), and each delivery is
written to the runtime session timeline where it landed, with its channel and
turn index, so the execution view shows the note in the stream of what the
agent was doing when it arrived.

For spawn-time aliases, parent lineage, and session id handoffs, see
[Spawning workers](spawning-workers.md).

The console discovers live note targets with
`GET /api/v1/operator-notes/session-targets?agent_id=<agent UUID>`. The response
contains `sessions` and `total`, paged with `limit` and `offset`. Each target
includes its ID, alias, harness (`session_source_type`) and start time.
Enrollment identity placeholders and ended sessions are excluded. Sending a
note still resolves its target again with the same account and permission
checks as `send_note`.
