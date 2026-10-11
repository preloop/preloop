# Remote sessions on your runners

A runner on your machine can host interactive sessions with the agent CLIs
installed there, for example GitHub Copilot CLI. You start the session from
the Preloop console; the agent runs on your machine with your own installed,
unmodified CLI and your own sign-in. Preloop never reads, copies or forwards
the agent's credentials.

Wave 1 (0.17.0) supports GitHub Copilot CLI. Other harnesses follow.

## What the host owner controls

Nothing is reachable until the person at the machine opts in:

- `preloop runner sessions enable copilot_cli` turns on remote sessions for
  one harness. The server cannot turn it on.
- `preloop runner dirs add <path>` authorizes a directory and gives it a label.
  The console only ever sees the label, never the path.
- When a session starts, the runner writes a log line and shows a desktop
  notification: "Preloop: <actor> started a <harness> session in <label>".

## Start a session from the console

1. Open **Sessions** and click **New session**, or open **Runners** and click
   **New session** on the runner's row.
2. Pick the runner (when you started from Sessions), the agent and its model.
   Agents that are installed but not ready show why: signed out, disabled, or
   remote sessions not enabled on the host.
3. Pick the workspace:
    - **Authorized directory**: one of the labels the host owner added for
      this agent.
    - **Repository checkout**: a GitHub or Bitbucket Cloud repository from a
      tracker connected to the account. The runner clones it into a temporary
      directory with a short-lived credential that is never written to disk.
      Checkouts are not available yet in 0.17.0; the console says so.
4. Optionally type a first message, then click **Start session**.

The console then opens the session view. Operator notes, approvals and the
audit trail work as for any other governed session.

### Who may start a session

The runner owner (the user who registered it) and account admins. Everyone
else gets `403 not_runner_owner`. Team sharing of a runner is an Enterprise
extension.

### Why a start can be refused

| Code | Meaning |
|---|---|
| `runner_offline` | The runner is not connected. |
| `harness_not_enabled_for_sessions` | Remote sessions are off for this agent on the host. |
| `harness_signed_out` | The agent CLI is signed out on the host. |
| `harness_disabled` | The agent is disabled in the runner configuration. |
| `workspace_not_authorized` | The directory is not authorized for this agent. |
| `max_concurrent_reached` | The runner already runs its maximum of remote sessions (2 by default). |
| `checkout_not_available` | Repository checkouts are not available on this server yet. |
| `remote_sessions_unavailable` | This server does not run the session service yet. |

Every start, refusal, message and stop is written to the audit log
(`runner_session.*` events) with the actor, runner, host, agent, model and
workspace label. Message text is never stored in the audit log, only its
length.

## API

The console uses the same endpoints the mobile apps will use. All paths are
under `/api/v1`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/runners/{runner_id}/session-options` | Agents, authorized directories, checkout sources and limits for this runner |
| POST | `/runners/{runner_id}/sessions` | Start a session: `{"harness","model","workspace","first_prompt","title"}`, answers `202` with `session_id` |
| GET | `/runners/{runner_id}/sessions` | Recent remote sessions on the runner |
| POST | `/runner-sessions/{session_id}/turns` | Send the next message: `{"text"}`; `409 turn_in_progress` while one runs |
| POST | `/runner-sessions/{session_id}/stop` | `{"mode":"graceful"}` or `{"mode":"kill"}` |

Workspaces:

```json
{"kind": "authorized_directory", "id": "dir_9f2c"}
{"kind": "tracker_checkout", "tracker_id": "<uuid>", "repository": "example/app", "ref": "main"}
```

Refusals use the body `{"detail": {"code": "...", "message": "..."}}`.
