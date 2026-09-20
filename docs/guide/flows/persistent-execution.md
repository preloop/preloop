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
* Agent Control is not verified on that agent
* the agent's control heartbeat is stale (it is offline)

The flow form shows each target's Agent Control state and disables agents that
are not online. If you save an offline target, start still fails until that
agent reconnects.

## What happens at run time

1. The orchestrator renders the flow prompt the same way as the ephemeral path.
2. Preloop persists one `send_message` command, then delivers it to the target.
3. The execution stays `RUNNING` while the command is pending, delivered, or
   acked without a result.
4. A successful `command_result` marks the execution succeeded. A
   `command_error`, expiry, or stop marks it failed.
5. If the flow timeout budget expires, Preloop interrupts the session and
   stops waiting.

See [Flow execution on a persistent agent](../../architecture/agent-control.md#flow-execution-on-a-persistent-agent)
for the envelope, binding, and status table.

## What this does not do yet

Persistent mode does not check out the trigger repository onto the agent host,
does not apply preset workspace assumptions, and does not add Codex to the
Agent Control allow-list. Those are separate contracts. Until they exist, pick
persistent only when the target agent already has whatever files and tools the
prompt needs.
