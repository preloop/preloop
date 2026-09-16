# Operator notes

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

Nothing when there is no note. The agent never polls, never decides to check an
inbox, never spends a token asking. Delivery is a piece of work Preloop is
doing anyway: the gateway is already assembling the outbound request body, and
the permission hook is already making a round trip for the tool call. A session
with no pending note reads one indexed row and appends nothing.

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
non-zero. The CLI sends notes and does not read them: listing a note's
delivery state and cancelling one stay in the console and the API.

API:

```bash
curl -X POST https://your-preloop/api/v1/operator-notes \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"agent_id": "0b0d...", "text": "Deploy to eu-west-1, not us-east-1."}'
```

Name exactly one target: `agent_id`, `runtime_session_id` or `execution_id`. A
note addressed to an agent with no live session waits for the next session that
agent opens, which is how you brief a run before it starts. `expires_in_seconds`
overrides the 24 hour default; a note nobody delivered in a day is stale advice,
and expiring visibly beats rotting silently.

- `GET /api/v1/operator-notes?agent_id=...` (or `runtime_session_id`,
  `execution_id`) lists notes newest first with their delivery state.
- `POST /api/v1/operator-notes/{note_id}/cancel` withdraws one that has not
  been delivered. Cancelling is a state, never a delete. A delivered note
  cannot be unsent, so cancelling one returns it unchanged and you can see why.

Mobile is out of scope for now. The iOS and Android clients already hold an
account session, so they need no new backend: the three endpoints above are the
whole surface.

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

## Which agent may note which target

A note from an agent is text a model will read on its next turn, so the blast
radius of this channel is the other agent's behaviour, not just its transcript.
The default is therefore narrow enough that turning `send_note` on is not
itself a decision that needs a threat model.

**The default scope is descent.** An agent may note the runs its own run
started, directly or transitively, and nothing else:

| The target | In the default scope |
| --- | --- |
| A run this run started | Yes |
| A grandchild, or deeper | Yes. Delegation is transitive, or inserting a middleman would widen the reach |
| A sibling (another run of the same parent) | No |
| The run that started this one | No. Descent has a direction |
| An agent running nothing of this run's | No |
| Anything in another account | No, and not as a scope question: a foreign id is simply not found |

Lineage is the key because it is the only relationship here that Preloop can
verify instead of accepting from the caller. `parent_execution_id` is written
by the platform when a child run is created; "we are on the same team" is a
claim an agent could simply make. The tool takes no lineage argument: the
calling run is read from the authenticated context.

**A caller with no lineage can note nothing.** A top level run that started no
children, and an enrolled agent that is not running inside an execution at all,
both have an empty descendant set, so every target is refused. That is the case
that makes the default safe: no lineage is no reach, rather than no restriction.

A refusal is a structured answer the model can act on, naming the scope the
call would have needed, and each one has its own code:

| Reason code | What happened |
| --- | --- |
| `note_scope_out_of_scope` | The target exists in the account but does not descend from the calling run |
| `note_scope_no_lineage` | The call carries no execution lineage, so it reaches nothing |
| `note_scope_denied_by_rule` | A tool access rule denied it |
| `note_scope_grant_unavailable` | The rule that would have granted it asked for approval, or could not be evaluated. Scope widening fails closed |

Every refusal writes one `agent.note_scope_denied` audit row carrying that code,
the target, the calling run and the rule that decided, so "my agent says it
cannot reach that run" is answerable from the record.

### Granting a wider scope

Wider than descent is a grant, not a setting. When the default refuses, the
call is put to the same tool policy path every tool call already goes through,
as a rule evaluation on `send_note`, and **only a rule that explicitly allows it
is a grant**: the default allow that a tool with no rules returns is not, or
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

Write the `has()` guard. The scope facts below exist only at this evaluation,
and a CEL expression that reads a key the plain tool call does not carry fails
that call closed. A `simple` condition needs no guard: a missing key is a
non-match there.

These bindings are what a rule sees. Every one of them is what the platform
worked out, never anything the calling agent asserted:

| Binding | Value |
| --- | --- |
| `args.note_scope` | `account`, the scope being asked for |
| `args.note_default_scope` | `descendants` |
| `args.note_target_relation` | `self`, `ancestor`, `same_tree`, `unrelated` or `no_lineage` |
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
gateway path. Call it from the hook or bridge process, never from the model.
Claiming is the delivery: what it returns is marked delivered, audited and
evented before it leaves.

| Harness | What fires | How to pick the note up |
| --- | --- | --- |
| Claude Code | `PreToolUse`, installed by `preloop agents onboard --approvals` | `operator_note` on the permission-check response, written into `hookSpecificOutput.additionalContext` |
| Claude Code (channels) | An MCP channel server you run | `POST /agents/notes/pending`, then push `channel_event` as `notifications/claude/channel`. The harness wraps our block in its own `<channel source= severity=>` tag |
| Claude Code (cross-session messaging) | A bridge process holding `CLAUDE_CODE_MESSAGING_TOKEN`, with `crossSessionInbound: accept` on headless `-p` workers | `POST /agents/notes/pending`, then post `text` to the session inbox socket. The harness delivers it between tool calls |
| Codex CLI | `PreToolUse` and `PermissionRequest` | `operator_note` on the permission-check response, written into the `PreToolUse` `hookSpecificOutput.additionalContext`. `PermissionRequest` has no field for it, so a note claimed there rides the next `PreToolUse` |
| Cursor CLI | `beforeShellExecution`, `beforeMCPExecution`, `preToolUse` | `operator_note` on the permission-check response, written into the `preToolUse` `additional_context` on allow and deny. The two `before*` hooks have no field for it, and on this build `preToolUse` collects `additional_context` only on allow and deny, so a note claimed on `ask` also rides the next carrying call |
| OpenCode | `tool.execute.before`, via `@preloop-ai/opencode-plugin` | `operator_note` on the permission-check response |
| OpenClaw, Hermes | Gateway path only, no hook needed | Trailing message in the model request |

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

A note whose harness fires no tool call and makes no model call is not
delivered, and its state stays `pending` until it expires. There is no path
that pushes into a process that is not asking Preloop anything.

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

Two audit actions, both written in the same transaction as the thing they
describe:

| Action | Written when |
| --- | --- |
| `agent.note_sent` | Before the API tells the author it worked |
| `agent.note_delivered` | Before the request carrying the note leaves Preloop |
| `agent.note_scope_denied` | Instead of a note, when an agent's target was out of [its scope](#which-agent-may-note-which-target) |

The same two arrive as webhook events, `agent.note_sent` and
`agent.note_delivered` (see [webhooks](webhooks.md)), and each delivery is
written to the runtime session timeline where it landed, with its channel and
turn index, so the execution view shows the note in the stream of what the
agent was doing when it arrived.
