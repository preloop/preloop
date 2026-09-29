# Async Approvals

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

After reading this page you can switch an approval workflow to non-blocking mode, so agents get a `request_id` back immediately and poll for the decision instead of holding an MCP connection open.

---

## Overview

By default, when a tool call requires approval, the MCP connection **blocks**: the agent waits until a human approves or declines. This works well for interactive sessions but has two drawbacks:

1. **Client timeouts.** Most MCP clients default to 30–60 s; approval can take minutes.
2. **Blocked agents.** The agent cannot do other work while waiting.

**Async approval mode** solves both problems. Instead of blocking, the tool call returns immediately with a `pending_approval` status and a `request_id`. The agent then polls `get_approval_status(request_id)` until the human decides, at which point the tool executes and the result is returned in the poll response.

```
Agent ──▶ call tool ──▶ Preloop returns {status: "pending_approval", request_id: "..."}
Agent ──▶ get_approval_status(request_id) ──▶ {status: "pending", remaining_seconds: 280}
Agent ──▶ get_approval_status(request_id) ──▶ {status: "approved", tool_result: {...}}
```

---

## Configuration

### Via Policy YAML

Enable async approval on an approval workflow:

```yaml
version: "1.0"
metadata:
  name: async-deploy-review

approval_workflows:
  - name: "deploy-review"
    timeout_seconds: 600
    approvals_required: 1
    async_approval: true          # ← enables async mode

tools:
  - name: "bash"
    source: mcp
    approval_workflow: "deploy-review"
    conditions:
      - expression: "args.command.contains('deploy')"
        action: require_approval
```

### Via Web UI

1. On **Tools**, open **Workflows** and edit a workflow or choose **New workflow**
2. Turn on **Enable Async Approvals**
3. Click **Create Policy** or **Save Changes**

---

## Polling Flow

### 1. Tool Call Returns Immediately

When async approval is enabled, the tool call does **not** block. The agent receives:

```json
{
  "status": "pending_approval",
  "request_id": "8f14e45f-…",
  "message": "Approval request sent via email, mobile_push. Poll the approval status by calling get_approval_status(request_id='8f14e45f-…')",
  "poll_interval_seconds": 15,
  "timeout_seconds": 600,
  "channels": ["email", "mobile_push"]
}
```

Note the agent-facing status string is `pending_approval`; on subsequent polls the stored request status is `pending`.

### 2. Agent Polls for Decision

The agent calls `get_approval_status(request_id)` periodically. The response carries the request status plus an event log of everything that has happened so far (notifications sent, votes cast, escalations):

```json
{
  "request_id": "8f14e45f-…",
  "status": "pending",
  "tool_name": "bash",
  "events": [
    {"timestamp": "2026-07-17T20:00:01Z", "type": "notification_sent", "detail": "…"}
  ],
  "remaining_seconds": 280
}
```

`remaining_seconds` is present only while the request is pending. Once any approver has voted, `approvals_received` is included.

### 3. Human Decides

The human approves or declines via mobile app, email, Slack, or web UI.

### 4. Agent Receives Result

On the next poll after approval, the tool executes and the response includes its result:

```json
{
  "request_id": "8f14e45f-…",
  "status": "approved",
  "tool_name": "bash",
  "events": [
    {"timestamp": "2026-07-17T20:02:15Z", "type": "vote", "detail": "…", "comment": "Looks good, ship it."}
  ],
  "approvals_received": 1,
  "tool_result": {
    "text": "Deployment completed successfully."
  }
}
```

Approver comments appear as `comment` entries in the event log. If declined, `status` is `"declined"` and no `tool_result` is present. If a concurrent poll is already executing the tool, the response reports `status: "executing"`: poll again for the cached result. If execution failed, the poll returns `tool_execution_error` instead of re-running the tool.

---

## State Transitions

```
┌─────────┐
│ pending  │──── human approves ───▶ approved ──▶ tool executes ──▶ result cached
│         │──── human declines ───▶ declined
│         │──── request cancelled ─▶ cancelled
│         │──── timeout reached ──▶ expired
└─────────┘
```

| State | Description |
|-------|-------------|
| `pending` | Waiting for human decision (reported as `pending_approval` in the initial tool response) |
| `approved` | Human approved; tool result available |
| `executing` | Computed status while another poll is running the approved tool: poll again |
| `declined` | Human declined; tool not executed |
| `cancelled` | Request was cancelled; tool not executed |
| `expired` | Timeout reached without a decision |

**Idempotency:** Once a tool result is cached on an approved request, subsequent polls return the same cached result without re-executing the tool.

---

## Client Configuration

Because the tool call returns immediately in async mode, **no client timeout increase is needed**. The agent polls at its own pace.

If you are using **synchronous** (blocking) approval mode instead, you may need to increase your MCP client timeout. See the timeout guidance in your MCP client reference page, such as [Claude Code](../clients/claude-code.md).

---

## API Reference

### `get_approval_status` (MCP tool)

Called by the agent to poll approval status.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `request_id` | string | Yes | The approval request ID returned by the original tool call |

### REST Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/approval-requests/{id}` | Get approval request details (authenticated) |
| `POST /api/v1/approval-requests/{id}/approve` | Approve a request |
| `POST /api/v1/approval-requests/{id}/decline` | Decline a request |
| `POST /api/v1/approval-requests/{id}/decide` | Approve or decline |
| `GET /approval/{id}/data?token={token}` | Public token-based access |
| `POST /approval/{id}/decide?token={token}` | Public token-based decision |

---

## Best Practices

- **Set reasonable timeouts.** 10 minutes (`timeout_seconds: 600`) works for most workflows.
- **Use with agentic flows.** Async approval is ideal for automated flow executions where the agent runs unattended.
- **Combine with justification.** Require agents to explain *why* they need the tool: reviewers see the justification in the approval notification. See [Per-Tool Justification](justification.md).
- **Monitor via WebSocket.** Subscribe to the `approvals` topic for real-time approval status updates in the UI.

!!! tip "Native tool calls too"
    Approvals are not limited to MCP tools. Onboarded agents can route their native tool calls (shell commands, file edits) through Preloop approvals, see [Native Tool Approvals](ai-approvals.md#native-tool-approvals).
