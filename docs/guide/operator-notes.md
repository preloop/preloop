# Operator notes

An operator note is a short instruction from an identified human to a running
agent. You type it in the console or the API; the agent receives it at
its next turn boundary; the note is recorded as a human decision, with who sent
it, when it landed and on which turn.

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
| Claude Code | `PreToolUse`, installed by `preloop agents onboard --approvals` | `operator_note` on the permission-check response |
| Claude Code (channels) | An MCP channel server you run | `POST /agents/notes/pending`, then push `channel_event` as `notifications/claude/channel`. The harness wraps our block in its own `<channel source= severity=>` tag |
| Claude Code (cross-session messaging) | A bridge process holding `CLAUDE_CODE_MESSAGING_TOKEN`, with `crossSessionInbound: accept` on headless `-p` workers | `POST /agents/notes/pending`, then post `text` to the session inbox socket. The harness delivers it between tool calls |
| Codex CLI | `PermissionRequest` | `operator_note` on the permission-check response |
| Cursor CLI | `beforeShellExecution`, `beforeMCPExecution` | `operator_note` on the permission-check response |
| OpenCode | `tool.execute.before`, via `@preloop-ai/opencode-plugin` | `operator_note` on the permission-check response |
| OpenClaw, Hermes | Gateway path only, no hook needed | Trailing message in the model request |

For the Claude Code transports, Preloop supplies the text, the identity and the
record; the harness supplies the last hop.

This PR ships the server side of every row above. The harness-side rendering
(the CLI hook putting `operator_note` into `additionalContext`, and the channel
and inbox bridges) is a follow-up: those depend on per-harness output schemas
that have to be verified against a running harness, and guessing at a schema on
a governance path is worse than shipping the endpoint and wiring it next.

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
(or per session, when the target has no managed agent). Note
bodies are stored in the clear, exactly as approval comments are, because both
are operator text that has to be readable in the audit trail and in the
timeline. Do not put secrets in a note; use the credential store.

Two audit actions, both written in the same transaction as the thing they
describe:

| Action | Written when |
| --- | --- |
| `agent.note_sent` | Before the API tells the author it worked |
| `agent.note_delivered` | Before the request carrying the note leaves Preloop |

The same two arrive as webhook events, `agent.note_sent` and
`agent.note_delivered` (see [webhooks](webhooks.md)), and each delivery is
written to the runtime session timeline where it landed, with its channel and
turn index, so the execution view shows the note in the stream of what the
agent was doing when it arrived.
