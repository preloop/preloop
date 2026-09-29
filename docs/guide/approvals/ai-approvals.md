# AI-Driven Approvals

Use AI models to automatically evaluate tool call requests and make approval decisions based on configurable guidelines.

!!! info "Availability"
    AI-driven approvals are available in both **Open Source** and **Enterprise** editions.

---

## Overview

Instead of requiring a human to review every approval request, you can configure an AI model to evaluate requests automatically:

- **Low-risk requests** → AI approves instantly
- **Uncertain requests** → Escalate to human reviewers
- **Clearly risky requests** → AI denies with reasoning

The AI evaluates each request against your guidelines and returns a confidence score. Requests above the threshold are auto-decided; requests below it follow the fallback behavior you configure.

---

## Configuration

### Via Policy YAML

```yaml
version: "1.0"
metadata:
  name: ai-safety-review

approval_workflows:
  - name: ai-reviewer
    approval_type: ai_driven
    ai_model: claude-sonnet-4-20250514
    ai_guidelines: |
      Approve routine operations (file reads, searches, list operations).
      Deny any destructive operations (delete, drop, truncate).
      Escalate anything involving production infrastructure or billing.
    ai_confidence_threshold: 0.8
    ai_fallback_behavior: escalate
    escalation_workflow: human-review

  - name: human-review
    timeout_seconds: 600
    approvals_required: 1
    approver_teams: [sre-team]

tools:
  - name: bash
    source: builtin
    approval_workflow: ai-reviewer
```

### Via Web UI

1. Navigate to **Approval Workflows** → **Create Policy**
2. Select **AI-Driven** as the approval type
3. Configure the AI model, guidelines, and threshold
4. Set the fallback behavior
5. Click **Save**

---

## Configuration Reference

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `approval_type` | `"ai_driven"` | `"standard"` | Must be `"ai_driven"` to enable AI evaluation |
| `ai_model` | string | `gpt-5.4-mini` | AI model to use (e.g., `claude-sonnet-4.7`, `gpt-5.4`) |
| `ai_guidelines` | string | — | Instructions for the AI when making decisions |
| `ai_context` | dict | — | Extra context merged into the evaluation — worked examples, domain knowledge |
| `ai_confidence_threshold` | float | `0.8` | Minimum confidence (0.0–1.0) for auto-decision |
| `ai_fallback_behavior` | string | `"escalate"` | Action when uncertain: `escalate`, `approve`, or `deny` |
| `escalation_workflow` | string | — | Name of the policy to escalate to (for `escalate` fallback) |

### `ai_context`

Beyond the guidelines prompt, you can attach structured context that gets merged into the AI's evaluation — useful for worked examples and domain knowledge that would clutter the guidelines text:

```yaml
approval_workflows:
  - name: ai-reviewer
    approval_type: ai_driven
    ai_guidelines: |
      Approve routine operations. Deny destructive operations.
    ai_context:
      examples:
        - request: "SELECT count(*) FROM users"
          decision: approve
        - request: "DROP TABLE users"
          decision: deny
      domain_knowledge: "Databases prefixed prod_ serve live customer traffic."
```

---

## Fallback Behaviors

| Behavior | When AI confidence is below threshold |
|----------|---------------------------------------|
| `escalate` | Forward to human reviewers via the `escalation_workflow` |
| `approve` | Auto-approve the request (use with caution) |
| `deny` | Auto-deny the request (conservative approach) |

---

## Writing Effective Guidelines

Your `ai_guidelines` string is the system prompt that tells the AI how to evaluate requests. Write them like instructions for a security reviewer:

### ✅ Good Guidelines

```yaml
ai_guidelines: |
  You are reviewing tool call requests for a production SaaS application.

  APPROVE if:
  - The operation is read-only (file reads, searches, list/get operations)
  - The target is a development or staging environment
  - The operation is idempotent and low-risk

  DENY if:
  - The operation deletes data or drops tables
  - The operation modifies production infrastructure
  - The operation involves secrets or credentials

  ESCALATE if:
  - You are unsure about the impact
  - The operation involves billing or payments
  - The operation affects customer-facing systems
```

### ❌ Poor Guidelines

```yaml
ai_guidelines: "Be careful with approvals"  # Too vague
```

---

## Audit Logging

Every AI decision is logged with full context:

- **Decision**: approve, deny, or escalate
- **Confidence score**: 0.0–1.0
- **Reasoning**: AI's explanation for the decision
- **Model used**: Which AI model evaluated the request
- **Guidelines applied**: The guidelines text at decision time

The audit log dashboard for browsing decision history is part of the Enterprise audit plugin.

---

## Examples

### Example 1: Conservative Production Guard

```yaml
approval_workflows:
  - name: prod-guard
    approval_type: ai_driven
    ai_model: claude-sonnet-4-20250514
    ai_guidelines: |
      Deny all write operations in production.
      Approve read-only operations.
      Escalate deployment requests.
    ai_confidence_threshold: 0.95
    ai_fallback_behavior: deny
```

### Example 2: Fast Development Flow

```yaml
approval_workflows:
  - name: dev-fast
    approval_type: ai_driven
    ai_model: gpt-5.4
    ai_guidelines: |
      Approve most development operations.
      Only deny clearly destructive operations like rm -rf or DROP TABLE.
    ai_confidence_threshold: 0.6
    ai_fallback_behavior: approve
```

### Example 3: Tiered with Human Escalation

```yaml
approval_workflows:
  - name: human-sre
    timeout_seconds: 300
    approvals_required: 1
    approver_teams: [sre-team]

  - name: ai-triage
    approval_type: ai_driven
    ai_model: claude-sonnet-4-20250514
    ai_guidelines: |
      Approve routine operations instantly.
      Escalate infrastructure changes to the SRE team.
    ai_confidence_threshold: 0.85
    ai_fallback_behavior: escalate
    escalation_workflow: human-sre
```

---

## Native Tool Approvals

New in 0.12.2: approvals are no longer limited to MCP tools. Onboarded agents can route their **native** tool calls — shell commands, file edits — through Preloop's approval pipeline:

- **Claude Code** via a `PreToolUse` hook — see [Claude Code](../clients/claude-code.md)
- **Codex CLI** via a `PermissionRequest` hook
- **OpenClaw** via the `@preloop-ai/openclaw-plugin` package (v0.1.1), which registers a `before_tool_call` hook — see [OpenClaw](../integrations/openclaw.md)
- **Hermes** via its runtime plugin

Each hook calls `POST /api/v1/agents/permission-check` with the agent's managed credential. The server evaluates your policies (including AI-driven workflows) and responds:

```json
{"decision": "allow", "reason": "…", "request_id": "…"}
```

The call blocks for up to ~300 seconds while a human decides, and is **fail-closed** by default — if Preloop is unreachable, the tool call is denied (the OpenClaw plugin exposes `tool_approval_fail_open`, default `false`, to invert this).

Toggle enforcement per subject with the governance setting `native_tool_approvals: enforce | off` (unset means enforce).
