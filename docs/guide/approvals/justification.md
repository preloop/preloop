# Per-Tool Justification

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Require or optionally request agents to explain *why* a tool is being called before approval.

---

## Overview

When an AI agent calls a tool, it may not always be clear *why*. Per-tool justification adds a `justification` parameter to tool calls, allowing you to:

- **Require** agents to provide a reason before the tool can execute
- **Optionally** accept a reason if the agent provides one
- **Log** the justification in audit trails and approval requests

Reviewers see the agent's reasoning in the approval notification, making it easier to make informed decisions.

---

## Configuration

### Via Policy YAML

Set the `justification` field on a tool definition:

```yaml
version: "1.0"
metadata:
  name: justified-tools

tools:
  - name: "bash"
    source: mcp
    justification: required        # Agent MUST explain the call
    approval_workflow: "ops-review"

  - name: "create_issue"
    source: mcp
    justification: optional        # Agent MAY explain (logged if provided)
```

| Value | Behaviour |
|-------|-----------|
| `required` | Tool call is **rejected** if no justification is provided |
| `optional` | Justification is accepted and logged but not enforced |
| *(omitted)* | No justification parameter is injected |

### Via Web UI

1. Navigate to **Tools** → select a tool
2. Under **Justification**, choose **Required** or **Optional**
3. Save

---

## How It Works

### 1. Parameter Injection

When a tool has justification configured, Preloop dynamically injects a `justification` parameter into the tool's schema. The agent sees it in the tool definition:

```json
{
  "name": "bash",
  "description": "Execute a shell command",
  "parameters": {
    "properties": {
      "command": {"type": "string"},
      "justification": {
        "type": "string",
        "description": "Provide your reasoning and context for why this tool is being called."
      }
    },
    "required": ["command", "justification"]
  }
}
```

### 2. Agent Provides Reasoning

The agent includes its reasoning in the tool call:

```json
{
  "tool": "bash",
  "arguments": {
    "command": "kubectl rollout restart deployment/api",
    "justification": "The API pods are returning 503 errors after the config change. Restarting to pick up the corrected config."
  }
}
```

### 3. Server-Side Enforcement

- **`required` mode:** If `justification` is missing or empty, the tool call is rejected with an error message.
- **`optional` mode:** The tool call proceeds regardless, but any provided justification is logged.

### 4. Approval Integration

When the tool also requires approval, the justification appears as `agent_reasoning` in:

- **Approval notifications** (email, Slack, mobile push)
- **Approval request detail page** in the web UI
- **Audit logs** for compliance review

---

## Justification in Approval Requests

The justification text flows through the system as follows:

1. Agent includes `justification` in tool arguments
2. Preloop strips it from arguments and stores it as `agent_reasoning`
3. Approval request is created with the reasoning attached
4. Reviewers see the reasoning in their notification:

!!! note "Truncation"
    `agent_reasoning` is truncated to **500 characters** in approval summaries. Coach agents toward one or two tight sentences, not an essay.

```
🔔 Approval Required: bash
Agent reasoning: "The API pods are returning 503 errors after the config change."
Arguments: {"command": "kubectl rollout restart deployment/api"}
[Approve] [Decline]
```

---

## Audit Trail

The justification is stored as `agent_reasoning` on the approval request, so it is visible wherever the request is: notifications, the request detail page, and webhook payloads.

!!! cloud "Cloud and Enterprise"
    The **Audit > All events** view, which lists every tool call with its arguments, reasoning and action, comes from the audit plugin. On OSS, the same calls appear in each runtime session's timeline.

---

## Best Practices

- **Use `required` for high-risk tools.** Tools like `bash`, `delete_issue`, or deployment commands benefit from mandatory explanations.
- **Use `optional` for audit-only.** When you want to encourage reasoning without blocking the agent.
- **Combine with async approvals.** Justification + [async approval](async-approvals.md) gives reviewers full context without blocking the agent.
- **Review justifications regularly.** They show you how agents reason about the tools you gate.

!!! tip "Native tool calls too"
    Justification-style reasoning also flows through [native tool approvals](ai-approvals.md#native-tool-approvals): agents like Claude Code and OpenClaw pass their reasoning along when a shell command or file edit is escalated for approval.
