# Runtime Sessions

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

A **runtime session** is Preloop's durable identity for one stretch of work by a managed runtime: a flow execution today, or an enrolled OpenClaw/Hermes desktop session going forward.

Sessions let operators answer: *who was running, what did they call, and what did it cost?*

---

## What a Session Represents

Each runtime session ties together:

- a **runtime principal** (the enrolled agent identity)
- optional links to a **managed agent** record and **flow execution**
- captured **gateway interactions**, tool activity, and operator lifecycle events
- spend and token totals derived from the `ApiUsage` ledger

One enrolled installation can accumulate many sessions over time while keeping one durable managed-agent identity for audit history.

---

## Where Sessions Appear

In the console you can inspect sessions from:

- **Agents**: live canvas and detail views for enrolled runtimes
- **Audit → Sessions**: account-scoped session explorer with transcript, replay, requests, and the [Optimize](../cost/session-optimization.md) tab
- **Cost**: spend grouped by session, model, agent, or API key
- **Models**: per-model usage with session-level drill-down

Operators can **end a session explicitly**. That updates runtime state, emits audit and realtime events, and refreshes managed-agent summaries.

---

## How Sessions Are Created

Common creation paths:

1. **Managed CLI onboarding**: `preloop agents discover` or `preloop agents onboard <agent>` mints a runtime credential bound to a session reference such as the local config path.
2. **Flow execution**: running a flow creates an execution-scoped session bridge.
3. **Agent Control**: when a runtime plugin connects to `WS /api/v1/agents/control/ws`, presence binds to the active managed agent and refreshes session activity.

---

## Prompt and turn timestamps

The session APIs accept reports of when a prompt was submitted
(`UserPromptSubmit`, Cursor `beforeSubmitPrompt`), when an agent finished a
turn (`Stop`) and when a session ended (`SessionEnd`). Preloop records each as
a session activity with a time and a kind only: the prompt text is never
stored (the first prompt still feeds the redacted 120-character session
title). A session end also marks the session ended.

Claude Code onboarding also installs `PostToolUse`, `PostToolUseFailure` and
`Notification` hooks, and the `Stop` hook reports the end of each turn. A tool
end is recorded with the tool's id, name, exit code, error flag and duration,
never its output. A `Notification` that says the agent is waiting for input is
recorded as the turn's end. These hooks run with a 5 second timeout and never
block the agent when Preloop is unreachable.

## Agent state

Each session carries one agent state, derived when these rows are written:

| State | Set by |
| --- | --- |
| `working` | a prompt, or a tool end while the turn is open |
| `waiting_for_approval` | a pending approval for the session, until the last one is resolved |
| `your_turn` | a turn end or a wait for input |
| `ended` | a session end (cleared again if the session is resumed) |

The session also keeps the current turn's start and end and whether the turn
was interrupted. Session and agent summaries expose them as `turn_state`,
`turn_started_at`, `turn_ended_at` and `turn_interrupted`. The console uses one
chip for this state everywhere: **Working**, **Needs approval**, **Your turn**,
**Idle** or **Ended**. "Active now" only means the session was seen recently.

## Presence

Optional Cloud and Enterprise extensions can derive presence from these
timestamps. This backend foundation does not add a console page. A person is **active** within 5 minutes of a prompt, an approval
decision or console use; **idle** up to 30 minutes, or up to 2 hours while one
of their agents worked in the last 15 minutes; **away** after that. An agent
is **working** within 60 seconds of a model request or tool call, **waiting for
approval** while an approval is open, and **waiting for a reply** after a turn
end that no prompt has followed, for up to 4 hours. Prompts are attributed
through the onboarding credential's user. Aggregate rates are visible to
anyone who can view runtime sessions; names, per-person states and reply
latency need the `view_users` permission, and everyone can see their own row.

---

## Session Explorer

Account-scoped APIs expose recent sessions plus captured gateway interactions. The console can drill from aggregate usage charts into one session timeline: model calls, tool calls, approvals, and operator actions on the same canvas.

Flow execution detail also includes a **Gateway Events** tab for execution-scoped model-call inspection.

---

## Related

- [What Preloop includes](../functionality.md)
- [AI Model Gateway](model-gateway.md)
- [Agent Control Runtime Adapters](../integrations/agent-control-runtime-adapters.md)
- [Quick Start: CLI onboarding](../quickstart-cli.md)

## Following live work and decisions

Talk, Conversation and Requests share a live activity line and inline approval
cards. `Model processing` describes an observed request with a stable request ID;
`Tool requested` describes a model-emitted call, and `Running` requires executor
start evidence. Transport reconnection is displayed separately. A stale unmatched
start becomes `Activity unavailable` rather than claiming continuous processing.

Conversation reads like the terminal: each turn shows the prompt, then agent
prose and one row per tool call in order, then a "Worked for" footer. A tool row
names the command, path or arguments, previews three lines of the result (`+ N
lines` shows the rest), renders a diff for edits and marks a failed exit with a
danger pill. Open "Arguments & result" on a row to inspect the formatted payloads. Missing, redacted and
truncated captures remain marked; a completed model request does not imply that
its requested tool ran successfully.

Pending approvals load for the selected session, including pending requests older
than the first history page. Use **Next pending approval** while reading older
history. Approve, Deny, question answers and schema forms stay in place; denial
still asks for confirmation. A recorded quorum vote can remain pending until the
remaining approvers decide. Resolved cards retain their outcome and attribution.
Viewing and deciding require the existing approval permissions, and the full
approval page remains available through **Approval details**.

The following desktop example uses synthetic session and approval data:

![Named tools and pending approval in a synthetic session](../../assets/screenshots/sessions/live-session-after-desktop.png)

Screenshot reproduction: run local Vite servers for the base and implementation
revisions, then from `frontend` run
`PRELOOP_DISABLE_TELEMETRY=true node scripts/capture-live-session.mjs http://127.0.0.1:5190 http://127.0.0.1:5189`.
The script intercepts all API reads and uses a fixed clock and generic fixtures.
