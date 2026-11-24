# Interactive hosted conversations

Start a conversation from a flow's **Start conversation** action. It opens the
existing Talk page on that flow's hosted OpenCode runtime. No chat connection is
needed. The execution and Sessions pages link back to the same durable thread;
reloading or opening another tab does not create another conversation.

## Configuration and defaults

Choose OpenCode and the model already configured on your account, such as a
DeepSeek flash model. Model calls use the Preloop gateway. DeepSeek **Harness**,
Pi and other harnesses cannot enable this setting. Codex is not included in this
initial transport.

The flow stores the setting in `agent_config`:

```json
{
  "interactive": {
    "enabled": true,
    "max_turns": 30,
    "idle_timeout_seconds": 600
  }
}
```

Interactive mode defaults **off**. An enabled flow defaults to **30 user turns**
and **600 seconds of idle time**. Both numbers are configurable positive integers;
there is no additional conversation-turn ceiling. These defaults are initial
product choices, open for revision. The first flow prompt counts as a turn even
if no optional message was supplied. The flow's existing model-request iteration
limit is separate: one user turn can make several model requests and tool calls.

Existing token, spend and wall-clock limits cover the entire conversation,
including later containers. The execution limit settings and deployment defaults
still apply. The existing default wall-clock allowance is 3,600 seconds when the
flow does not set `timeout_seconds`. Startup, work and idle runtime count toward
it; time while the container is stopped does not. A resume receives the remaining
allowance, not a new budget.

Self-hosted installations must enable `FLOW_ARTIFACT_DIRECT_UPLOAD` and configure
the existing encrypted artifact storage. Workspace snapshots cannot be disabled
for interactive flows. This path captures the workspace even when it is clean,
so native conversation state is paired with the same repository checkout.

## Live turns and resume

OpenCode is pinned to **1.18.29**. Its documented `serve` process stays alive
inside the container; `run --attach` submits each turn to that server with an
explicit session ID. The server's `/event` stream supplies assistant text deltas.
The control plane persists answer snapshots and clients poll the shared API.
There is no browser-owned agent connection.

After an answer, Talk shows **Waiting for you** and the time remaining before the
container stops. A message during that time uses the same server and workspace.
Messages during **Starting** or **Working** receive HTTP 409 with an explanation;
they are **not queued**. Retrying the same accepted request ID cannot add a second
turn. When idle time expires, input is briefly refused while the runtime saves
its checkpoint. Only a successfully saved native session and workspace earn the
**Stopped (resumable)** state.

A message to a stopped conversation starts a new execution/container and restores
the explicit native session and repository workspace. Every resume, including
later stop/resume cycles, uses the working branch recorded in that conversation's
workspace checkpoint. It keeps the saved checkout, commits and uncommitted files
even if the flow's source branch has moved on the remote. It does not update or
rebase the checkout onto the latest source branch. A different restored branch
still fails the checkpoint guard. Flows without a repository do not enter Git branch planning or this guard.
A separate limitation remains in the pinned OpenCode transport: the local
no-repository check reaches the model endpoint but the attached CLI omits
completion events, leaving the first turn Failed before it can save a resumable
checkpoint.

Editing the flow prompt does not replay it into an existing conversation:
follow-up turns contain only the new message. The conversation keeps the turn
limit and idle timeout selected when it started. Other execution settings and
current authorization policies are evaluated again for each new container;
model/harness identity must remain compatible with the saved native session.
Changing the flow name or target-branch setting does not rename the saved branch.
Changing repository paths so they no longer match the checkpoint refuses resume;
start a new conversation for a different repository configuration.

The states shown are:

| State | Meaning |
| --- | --- |
| Starting (cold start) | A new container is starting, including on resume. |
| Working | The agent is answering; wait before sending another message. |
| Waiting for you | The server remains live until the displayed idle deadline. |
| Stopped (resumable) | The container is gone; the next message restores its checkpoint. |
| Ended | You ended it or it reached its user-turn limit. Start a new conversation. |
| Failed | A runtime, limit or checkpoint failure prevents continuation; the notice explains the next step. |

Native resume restores OpenCode's durable conversation records, recorded tool
calls/results and related session metadata. Workspace recovery separately
restores repository files, commits and uncommitted changes. Neither restores
running subprocesses, sockets, in-memory tool/plugin state or prior permission
grants. Tools and approvals remain governed by the current Preloop configuration.
Checkpoint archives exclude credentials. The session ID, version and artifact
identity must match. Missing, expired or incompatible checkpoints fail visibly
and offer a new conversation; they never silently start a fresh session.

Resume requires both artifacts to remain available. The existing workspace and
native-session retention settings apply independently (OSS defaults: workspace
24 hours, native session 168 hours). The shorter available lifetime governs
whether this conversation can resume.

## Ownership, limits and idle cost

One user owns each conversation. Other users with permission to view the flow
may read it; only its owner may send messages or end it. Audit entries identify
that user, and gateway calls on every turn use runtime credentials attributed to
that user. Flow authorization, MCP policies, approvals, the kill switch and hosted
capacity checks remain in the normal execution path.

Interactive containers wait in place for tool approvals rather than using the
separate approval-park/resume controller. Approval decisions, policies and windows
are unchanged; that waiting time occupies capacity and consumes the conversation
wall-clock allowance. Delegated child work remains observable through the existing
child-status tools; it does not park this conversation behind its owner’s back.

An idle container is still a running execution and occupies the account's hosted
concurrency allowance. Idle time counts toward execution duration and the conversation wall-clock
allowance. Waiting by itself makes no model request and adds no model-token charge.
This change adds no idle-compute billing meter; the operator continues to pay for
the container infrastructure under their hosting arrangement. Model and tool calls that are still finishing retain
their normal charges. Stopping the container releases capacity; restoring it
requires capacity again. No separate per-message charge is introduced here.

## Shared API

All routes use the existing authenticated account API. Writes require
`execute_flows` and ownership. The browser, mobile apps and future chat follow-ups
can use the same contract:

| Method and path | Operation |
| --- | --- |
| `POST /api/v1/flows/{flow_id}/conversations` | Start with `{ "request_id": "<UUID>", "message": "optional first message" }`. |
| `POST /api/v1/flow-conversations/{id}/messages` | Send with `{ "request_id": "<new UUID>", "message": "follow-up" }`; resume if stopped. |
| `GET /api/v1/flow-conversations/{id}` | Poll state, deadline, execution ID and ordered messages/streamed answers. |
| `GET /api/v1/flow-conversations/{id}?after_turn=N` | Read the next page after turn N (up to 100 turns). Re-read the current page while its answer is streaming. |
| `POST /api/v1/flow-conversations/{id}/end` | End the conversation. |
| `GET /api/v1/flow-conversations?flow_id={id}&offset=0` | List visible conversations, 50 per page; `flow_id` is optional. |

The start request ID is the stable conversation ID. Use the same UUID when
retrying a request whose response was lost; generate a new UUID for new intent.
The optional first message replaces `{{conversation.message}}` in the flow's
initial prompt. If that placeholder is absent, the message is appended. Follow-up
messages go directly to the same native session without replaying the initial
prompt. HTTP 409 states why input was refused; HTTP 410 reports an unavailable
checkpoint. The returned `notice` is suitable for display to users.

A failed resume currently leaves the conversation **Failed**, even when a
startup guard refused it before the agent ran and the previous checkpoint is
still intact. Retrying that checkpoint after a deployment fix needs a future
recovery transition that retains the failed turn and its runtime accounting while
binding the next attempt to the last successful checkpoint.

The `/runtime` endpoints are internal, execution-scoped capability endpoints;
account credentials cannot impersonate a runtime. They are not part of the
mobile/chat client contract.

The driver receives its capability in the container environment and removes it
from OpenCode child environments. This prevents accidental inheritance, but is
not a security boundary against tools running as the same UID: on Linux they can
read the original container environment through `/proc/1/environ` (or another
same-UID process) and obtain the capability. Such a tool could overwrite its own
conversation's answer or request runtime shutdown. The capability remains scoped
to that conversation and execution; it does not grant access to other threads.
Stronger isolation requires a separate driver UID and protected capability
transport. The current hosted runtime does not provide that isolation.

[Chat connections](../../chat-connections.md#interactive-flow-conversations)
use these same routes. On a flow with both a chat alias and interactive enabled,
`ask ALIAS TEXT` starts a conversation owned by the linked user. A durable binding
keeps its ID on the bot DM or Slack channel thread, and the durable chat worker
polls complete answers and state notices. The console lists it alongside
console-started conversations and Talk shows its chat origin. Slack and
Mattermost bot DMs accept plain follow-ups; Slack channel replies must mention
the bot in the original thread. Discord follow-ups use `/preloop message:TEXT`.
Use `end` and `status` (Discord: `/preloop message:/end` and
`/preloop message:/status`). End the active conversation before another `ask`.
Flows without interactive enabled retain separate one-shot runs.

## Validation boundary

Automated tests use synthetic upstreams with telemetry disabled. A real hosted
DeepSeek run requires a local model credential, a repository clone and a local
Preloop gateway. Synthetic transport tests do not establish provider latency or
prove a real model retained context. Record cold-start-to-first-token, live
follow-up and resumed-turn latency from that real run before using those numbers
for performance claims.

The opt-in `test_conversation_repository_integration.py` test uses local
PostgreSQL, pinned OpenCode 1.18.29 and a synthetic model endpoint. With
`OPENCODE_CONVERSATION_SMOKE=1` and that OpenCode version on `PATH`, it exercises
idle stop and two resumes using a real bare Git upstream, checkpoint capture and
restore, native history and persisted answers/turn counts. It also covers a
conversation without a repository as an expected failure for the transport
limitation above. The ordinary branch regression tests execute
the generated clone and restore shell, including the branch guard.

OpenCode references: [server API](https://opencode.ai/docs/server/),
[CLI](https://opencode.ai/docs/cli/). The pinned executable's `serve --help`,
`run --help` and `/doc` schema are the authority for this transport's protocol.
