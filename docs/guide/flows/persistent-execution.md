# Persistent flow execution

Use **Persistent (Govern persistent agent node)** when the work should run on
an already-enrolled managed agent that is connected to Agent Control, instead
of provisioning a short-lived container.

Typical reasons to pick it:

* The agent already has the repository, tools, or session context on its host.
* You want the run governed through the same Agent Control audit trail as
  console and mobile operator messages.
* An ephemeral clone would be slower or would not see local state.

## Fail-fast rule

Start does **not** fall back to an ephemeral container. The execution fails
immediately when:

* the flow has no `target_agent_id`
* the target is missing, inactive, or not an Agent Control kind
* the target only accepts text on an already-open session (currently Pi
  and DeepSeek); persistent start always opens a new session
* Agent Control is not verified on that agent
* the agent's control heartbeat is stale (it is offline)

The flow form shows each target's Agent Control state and disables agents that
are not online. If you save an offline target, start still fails until that
agent reconnects. Connected Pi and DeepSeek targets can still appear in the
picker; start refuses them because they cannot open a new session.

## What happens at run time

1. The orchestrator renders the flow prompt the same way as the ephemeral path.
2. Preloop persists one `send_message` command, then delivers it to the target.
3. The execution stays `RUNNING` while the command is pending, delivered, or
   acked without a result.
4. A successful `command_result` marks the execution succeeded. A
   `command_error`, expiry, or stop marks it failed.
5. If the flow timeout budget expires, Preloop interrupts the agent's
   **current** session (`session_mode: current`) and stops waiting. That is
   usually this flow's turn; it is not a guarantee if another session started.

See [Flow execution on a persistent agent](../../architecture/agent-control.md#flow-execution-on-a-persistent-agent)
for the envelope, binding, and status table.

## Checkout and clone-less

Persistent execution sends a `workspace` object on the `send_message`.

* **Checkout** (`workspace.mode` is `persistent_checkout`): the flow has git
  clone enabled and the trigger names a repository. The sidecar keeps
  `<workspace_root>/<repository_slug>` on the agent host, clones it once
  with the host's own git credentials, then fetches and checks out later
  runs. No tracker token is sent to the sidecar.
* **Clone-less** (`workspace.mode` is `clone_less`): git clone is disabled,
  no repository could be resolved, or the flow lists more than one
  repository. The preset must not run git. The pull request reviewer
  reads the diff from the tracker and says so in the review.
* **Ephemeral** runs are unchanged. Their prompt renders
  `workspace.mode` as `ephemeral` and the container still clones into
  its own workspace.

Presets declare `supports_persistent`. The marker means the prompt was
checked against persistent modes. It does not mean the host captures
container result files. When Persistent is selected, the flow form
disables presets that do not support it. The pull request reviewer
supports it. Presets that write a container result path, or that still
assume an ephemeral checkout, do not.

## What this does not do yet

Persistent mode does not forward git credentials, and it does not add
Codex to the Agent Control allow-list. The Codex sidecar is a separate
contract that should follow the same `workspace` metadata.
