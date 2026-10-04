# Subject-Scoped Governance

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop does not evaluate every request against account defaults alone. **Subject-scoped governance** applies the right tool rules, model limits, and budgets to the concrete subject making the call, usually an API key or an enrolled managed agent.

---

## Scope Resolution Order

When Preloop evaluates a tool listing, policy decision, or gateway budget check, it walks this chain:

1. **Active API key**: narrowest token-scoped overrides when present
2. **Flow**: the per-flow override, only for traffic from an execution of that flow
3. **Linked managed agent**: per-agent tool visibility, access rules, and allowed models
4. **Account defaults**: broad fallback for the organization

The same subject context flows through MCP tool listing, approval evaluation, and model gateway preflight checks so one runtime token sees only the intended tools and models.

---

## What Can Be Scoped

Subject-scoped configuration can carry:

- **allowed_models**: restrict which AI models a runtime may call through the gateway
- **tool access rules**: ordered allow / deny / approval rules beyond account defaults
- **tool_enabled_overrides**: hide or expose specific tools for one enrolled agent
- **budget metadata**: per-subject spend limits where configured

---

## Overriding policies per flow

Flows are governed by the account policy by default. To govern one flow
differently, open the flow in the console and use its **Governance** card (on
the flow page and below the edit form), or call
`PUT /api/v1/account/governance/flows/{flow_id}` with the same config shape as
an agent override. `GET` returns the override plus the account defaults it
inherits; `DELETE` (the card's **Reset to account defaults** button) removes
the override. Reading needs `view_flows`; writing needs `edit_flows`.

Every execution of the flow runs with a short-lived credential that names the
flow, so these settings apply to that execution's traffic:

| Setting | Enforced at |
| --- | --- |
| `allowed_models` | model gateway preflight (request denied with 403) |
| `tool_rules` | MCP tool calls and native tool permission checks |
| `tool_enabled_overrides` | MCP tool listing and calls |
| `native_tool_approvals` | native tool permission checks (flow, then agent, then account default) |
| `approval_workflow_id` | workflow used for native tool approvals |

A flow that runs as a managed agent (an employee flow) uses the flow override
first and the agent's settings for anything the flow leaves unset. There is no
agent-type default in between: flow override, then managed agent if any, then
account defaults. Per-flow spend caps stay in the flow's budget settings; the
`model_budgets` field is stored with the override but, as for agents, is not
enforced by the gateway.

Note that the account default for native tool approvals now also applies to
flow executions that have no override, not only to managed agents.

### Allowed MCP Tools versus governance

The two settings answer different questions:

- **Allowed MCP Tools** on the flow decide which tools the flow's agent can
  see at all. A tool outside the list is never offered.
- The **governance override** decides how calls to the tools it can see are
  governed: allowed, denied, or sent for approval.

Example: a triage flow lists `search_issues` and `update_issue` in Allowed
MCP Tools, so the agent cannot even see `delete_issue`. The account policy
allows `update_issue` without approval. For this flow you add a governance
override with a `require_approval` rule on `update_issue`: the agent can still
call it, but each call now waits for a human, while every other flow keeps the
account behaviour.

---

## Primary Use Case

Grant a broad account-level tool catalog for human operators and automation flows, then enroll a desktop OpenClaw or Hermes runtime with a tighter subset of tools and models. The enrolled agent keeps its own audit trail and spend attribution without sharing one oversized API key across every machine.

Managed onboarding creates the managed-agent record, runtime credential, and local config rewrite; subject-scoped rules refine what that specific runtime may do afterward.

---

## Related

- [Safety Layer & Access Rules](safety-layer.md)
- [AI Model Gateway](model-gateway.md)
- [Runtime Sessions](runtime-sessions.md)
- [OpenClaw integration](../integrations/openclaw.md)
