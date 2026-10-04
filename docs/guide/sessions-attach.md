# Attaching to a session from the terminal

`preloop sessions attach` follows one running session as it happens, one line
per event, and lets you talk back: a typed line becomes an operator note, and
`a` or `d` decides a pending approval. It is the terminal equivalent of the
console's live session view, for a shell next to the agents you are running.

```bash
preloop sessions list --active          # find the session
preloop sessions attach 5a3e0c1d        # short id is enough
```

```text
-- attached to 5a3e0c1d-... (Release worker, claude_code alpha 2026-10-02 11:50:07Z)
-- notes are delivered at the agent's next tool or model call; type a line and press Enter to send one
-- Ctrl-C detaches; the session keeps running
11:52:20 tool     claude_code/Read  allowed  {file_path:8B}  79ms
11:52:21 approval pending Bash: command=git status  [3b3eb5f2]
         decide: a (approve) or d (decline), then Enter [3b3eb5f2]
a
-- approval 3b3eb5f2 approved
11:52:23 approval approved Bash: command=git status  [3b3eb5f2]
11:52:23 tool     claude_code/Bash  approved  {command:12B}  2060ms
Stop refactoring the tests, ship the fix.
-- note ed7cc2e6 queued; notes are delivered at the agent's next tool or model call
11:52:34 note     from Admin User delivered: Stop refactoring the tests, ship the fix.
```

## Two tiers

**Any governed agent: follow, notes, approvals.** Every agent under Preloop
control (native hooks, the model gateway, a runner, a sidecar) produces the
events attach shows: model requests with model, tokens and cost; tool calls
with server, tool, outcome, an argument summary and duration; approvals with
their decision prompt; operator notes with their author; the end of the
session. Attach works the same for all of them. What you can send back is a
note and a decision, and a note is read at the agent's next tool or model
call. A hook-governed agent such as `claude -p` has no terminal to type into,
so there is no "send a keystroke to the agent" and there will not be.

**Managed agents: commands.** An agent connected over Agent Control (Hermes,
a Claude workspace, the Codex sidecar, any kind listed under Agent Control)
can be given a new turn, not only a note at the next one. When the attached
session belongs to such an agent and its control connection is live, attach
switches the input to command mode: a typed line starts a new turn. See
[Command mode](#command-mode-for-managed-agents).

## What you see

| Kind | Line |
| --- | --- |
| `model` | model alias, HTTP status, tokens in and out (or total), cost, duration |
| `tool` | `server/tool`, outcome (`allowed`, `denied`, `approved`, `declined`, `timed_out`, or the MCP outcome), argument key names with their sizes, decision time |
| `approval` | `pending` with the tool and its redacted arguments or summary, then the outcome |
| `note` | author, `delivered`, the note text |
| `command` | an operator command that started a new turn: who sent it and the text |
| `reply` | the agent's reply to a command, with its result status |
| `end` | the session ended; attach exits |

Argument values are never shown for tool calls: the server keeps only key
names and sizes for the timeline, the same rule MCP tool calls follow.
Approval prompts show the redacted arguments the approver already sees in the
console.

`--json` prints one JSON object per line in the shapes the console reads, and
nothing else on stdout: live events as the websocket delivered them (they
carry `type`), replayed timeline items as `GET /api/v1/runtime-sessions/{id}/activity`
returns them (they carry `activity_type`), and pending approvals as
`GET /api/v1/approval-requests` returns them (they carry
`approval_workflow_id`). Notices go to stderr.

## Talking back

- A line and Enter sends an operator note to this session, with you as the
  author. It is the same request as `preloop notes send --session <id>`, so it
  has the same permission check, rate limit and audit.
- While an approval is pending, `a` or `d` and Enter approves or declines the
  oldest one; `a <id>` or `d <id>` picks one by its short id. It is the same
  request as `preloop approvals approve`, with the same permission check and
  audit as a console decision. With nothing pending, `a` is just a note.
- `--read-only` sends nothing: typed lines are answered with `read-only: not
  sent`, and no decision prompt is shown.

## Command mode for managed agents

On attach the CLI asks the server which mode applies
(`GET /api/v1/runtime-sessions/{id}/control`) and prints it:

```text
-- attached to 31aeec9d-001e-4bb5-8d17-7bc5156cc5e9 (Hermes warehouse, hermes 2026-10-04 00:11:46Z)
-- mode: command. A line starts a new turn for Hermes warehouse through Agent Control; /note <text> sends a note instead
-- Ctrl-C detaches; the session keeps running
list the goods-receipt workflows for nord
-- command 8f579255 delivered
02:13:14 command  from admin: list the goods-receipt workflows for nord
-- command 8f579255 started
02:13:15 model    preloop-fake  200  tokens in=11 out=9  $0.0000  0.0s
02:13:15 tool     warehouse/list_workflows  succeeded
02:13:15 tool     warehouse/get_transcript  succeeded
-- command 8f579255 finished
/note after this one, wait for my review
-- note 9817bd7a queued; notes are delivered at the agent's next tool or model call
```

- A line and Enter is sent as an operator message to the agent, addressed to
  the attached session. It is the console's command box request
  (`POST /api/v1/agents/{agent_id}/control/prompts` with `target_session_id`),
  so it needs the same `control_managed_agent` permission and is stored with
  you as its author and `cli_attach` as its source. It appears on the session
  timeline (a `command` line here, an operator message in the console) as
  soon as it is sent, and the agent's reply, when it sends one, as a `reply`
  line.
- Its delivery is shown inline, in the states the command row goes through:
  `queued` (stored, not yet on the agent's connection), `delivered` (sent on
  the connection), `started` (the agent acknowledged it), then `finished`,
  `failed`, `expired` or `cancelled`. The model and tool events of the new
  turn stream into the same terminal like any other event.
- `/note <text>` sends a plain note instead, read at the agent's next tool or
  model call. `/mode` asks the server again and prints the current mode.
- Approvals work as before: `a` and `d` decide while something is pending.

Command mode needs all of: the session is open, it belongs to a managed agent
(by its source or its runtime principal), the agent kind takes Agent Control
commands, the agent is active, its Agent Control plugin is verified, and the
agent sent a control heartbeat recently. Otherwise the mode is `note` and the
reason is printed, for example for a hook-governed `claude -p` session:

```text
-- mode: note (claude -p (local) is governed through hooks or the gateway and has no verified Agent Control plugin; a line is a note read at its next tool or model call ('preloop agents install-plugin' adds command mode))
```

A session no managed agent owns says so the same way ("this session is
governed through hooks or the gateway, not run by an Agent Control agent").

If a command is refused because the agent went offline, attach says so,
re-reads the mode and does not resend the line as a note; the next line
follows the new mode. A server without the mode endpoint leaves attach in note
mode.

### Attaching by agent

`preloop agents attach <agent-id|name>` attaches to the agent's most recently
active open session, or waits for its next one (`--no-wait` exits instead).
It takes `--read-only`, `--since` and `--json` like `sessions attach`.

```bash
preloop agents attach "Hermes main"
```

## Replay, reconnects and the end

On attach, the last `--since` of the timeline (default `10m`) is replayed,
then new events stream. A dropped connection is retried with backoff (1s
doubling to 30s); once it is back, the timeline is read again and anything
that happened while it was down is printed, followed by `reconnected, N
events missed`. Events already printed are not printed twice.

Attaching to a session that has already ended replays it and exits. A session
that ends while you watch prints `session ended` and attach exits. Ctrl-C
detaches; the session keeps running.

`--execution <id>` follows a flow execution: it also streams the execution's
own events, and finds the session when you do not name one.

## Permissions and audit

| Action | Needs |
| --- | --- |
| attach | session read (`view_runtime_sessions`) on a session in your account |
| see approvals | approval read (`view_approvals`); without it approvals are withheld from the stream |
| send a note | `control_managed_agent`, the same as `notes send` |
| send a command (command mode) | `control_managed_agent`, the same as the console's command box |
| decide | `decide_approvals`, the same as a console decision |

A refused note or decision is printed as the server's sentence and attach
continues; a refused attach prints the reason and exits. Every
attach and detach is an audit event (`runtime_session.attached`,
`runtime_session.detached`) with the user, the session, the execution and
whether the attach was read-only.

## How it is wired

Attach uses `GET /api/v1/runtime-sessions/{id}/activity` and
`GET /api/v1/approval-requests` for the replay and the session-scoped
websocket `/api/v1/ws/runtime-sessions/{id}` for live events. The socket
authenticates `Authorization: Bearer`, checks session read, filters the
account's realtime events down to that session (or execution), and is
receive-only: notes, commands and decisions go through their REST endpoints.
Command delivery is read from
`GET /api/v1/agents/{agent_id}/control/commands/{command_id}`, which returns
the `delivery_state` described above.

A hook-governed agent whose credential names the agent but no session (the
durable credential `preloop agents onboard` writes) is attributed to the
agent's current open session, the one `preloop notes send --agent` resolves
to. Each native tool call its hook checks is recorded on that session's
timeline and broadcast, so it appears in attach within a second of the hook
event.
