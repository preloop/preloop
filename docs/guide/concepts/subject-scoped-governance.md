# Subject-Scoped Governance

Preloop does not evaluate every request against account defaults alone. **Subject-scoped governance** applies the right tool rules, model limits, and budgets to the concrete subject making the call, usually an API key or an enrolled managed agent.

---

## Scope Resolution Order

When Preloop evaluates a tool listing, policy decision, or gateway budget check, it walks this chain:

1. **Active API key**: narrowest token-scoped overrides when present
2. **Linked managed agent**: per-agent tool visibility, access rules, and allowed models
3. **Account defaults**: broad fallback for the organization

The same subject context flows through MCP tool listing, approval evaluation, and model gateway preflight checks so one runtime token sees only the intended tools and models.

---

## What Can Be Scoped

Subject-scoped configuration can carry:

- **allowed_models**: restrict which AI models a runtime may call through the gateway
- **tool access rules**: ordered allow / deny / approval rules beyond account defaults
- **tool_enabled_overrides**: hide or expose specific tools for one enrolled agent
- **budget metadata**: per-subject spend limits where configured

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
