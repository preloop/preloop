# Spike: talking to a running Codex TUI through the app-server daemon

Recorded against Codex CLI 0.163.0, local only, with an isolated `CODEX_HOME`
whose `config.toml` points `model_provider` at a local mock Responses server.
No model traffic left the machine.

## Result

A second client on the shared `codex app-server` daemon can drive an
interactive `codex` TUI thread. The TUI renders the injected input and the
reply exactly as if they had been typed.

| Step | What was done | What happened |
|---|---|---|
| 1 | Daemon running, `codex` in tmux with one typed prompt. Second client: `thread/resume {threadId, excludeTurns: true}`, then `turn/start` "say hi from second client". | The TUI showed `› say hi from second client`, then the reply and `Worked for <1s`. The client received `turn/started`, `item/agentMessage/delta`, `item/completed`, `thread/tokenUsage/updated`, `turn/completed`. |
| 2 | `turn/start` on a slow stream, then `turn/steer {expectedTurnId}` 3 s later. | `turn/steer` returned the active turn id. The TUI showed both lines; the in-flight model request was cut and a new one carried the steered text. |
| 3a | TUI started without an explicit `daemon start`. | Not embedded: 0.163.0 starts the daemon itself (feature `DaemonAutoStart`), so the thread was loaded and reachable. |
| 3b | `codex --no-daemon`, then `daemon start`. | `thread/loaded/list` returned `[]`: an embedded TUI cannot be reached. |
| 4 | `turn/start` with `approvalPolicy: "untrusted"` and a model reply that runs a command. | The daemon sent `item/commandExecution/requestApproval` to the second client while the TUI showed its own approval prompt. |
| 5 | Same, second client answers `{decision: "accept"}`. | `serverRequest/resolved` was broadcast, the TUI prompt closed and the command ran. First answer wins. |
| 6 | `turn/interrupt {threadId, turnId}` during a slow stream. | The TUI showed `■ Conversation interrupted`. |

## Wire facts the driver relies on

1. `codex app-server proxy` pipes bytes to the control socket at
   `$CODEX_HOME/app-server-control/app-server-control.sock`, and the socket
   speaks WebSocket (HTTP/1.1 Upgrade, then JSON-RPC in text frames). Frames
   sent before `101 Switching Protocols` arrives get no answer, so the client
   waits for the upgrade before sending `initialize`.
2. `initialize` needs `capabilities.experimentalApi: true` for the v2 thread
   and turn methods, followed by the `initialized` notification. The result's
   `userAgent` carries the server version (`codex-tui/0.163.0 ...`); the
   driver refuses anything older than 0.163.0.
3. `thread/loaded/list` also returns ephemeral helper threads (one per turn,
   used for title generation). Only threads whose `thread/read` result has
   `ephemeral: false` are sessions. The TUI thread reports
   `source: "vscode"`, so `source` is not a usable filter.
4. A thread that is not loaded is never resumed: that would load its rollout
   from disk and make the sidecar a second writer on the user's session.

## Decisions taken from the spike

- Approvals: the driver does not answer `requestApproval`. Codex's own prompt
  and the Preloop approval hook stay in charge; answering from the web is a
  separate product decision.
- `preloop codex sidecar enable --app-server-daemon` starts the daemon for
  builds or setups where the TUI does not start it itself. It is never
  started without the flag.
