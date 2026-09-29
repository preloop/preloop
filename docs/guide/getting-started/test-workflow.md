# Testing Your Approval Workflow

Verify your Preloop setup end-to-end: call a prelooped tool from your MCP client, receive the approval request, approve it, and confirm the tool executes and everything lands in the audit trail.

These tests use the hosted demo MCP server at `https://example-mcp.preloop.ai/mcp`, which provides harmless sample tools like `pay` and `get_random_number`.

**Time Required:** 10-15 minutes

**Prerequisites:**

- Preloop account created ([Creating Your Account](account.md))
- API key generated
- MCP client connected ([Connect Your MCP Client](connect-mcp-client.md))
- At least one tool prelooped

---

## Test 1: Basic Approval Workflow

### Goal

Verify the complete approval flow works: call tool → receive notification → approve → tool executes.

### Setup

**Step 1: Add the Example MCP Server**

If you haven't already:

1. Go to **Tools & MCP** → **+ Add Source** → **MCP Server**
2. Configure:
   ```
   Name: Example MCP Server
   URL: https://example-mcp.preloop.ai/mcp
   Transport: http-streaming
   Auth Type: none
   Status: active
   ```
3. Click **Save**
4. Click **Scan Tools**

**Step 2: Preloop the `pay` Tool**

1. Find `pay` tool in Tools & MCP list
2. Click **Configure** (gear icon)
3. Toggle **Require Approval** to ON
4. Set approvers: Add yourself
5. Notification: Select "Email"
6. Click **Save**

### Execute Test

**Step 1: Trigger Tool Call**

In your MCP client (e.g., Claude Code):

```
Using @preloop tools, pay alice@example.com $500
```

Expected: the client sends the request and waits — the tool does NOT execute immediately.

**Step 2: Check for Notification**

Within 30 seconds:

- **Email:** message from `hello@preloop.ai`, subject "Approval Required: pay", with the tool name, arguments, request ID, and Approve/Decline buttons
- **Web Dashboard:** notification bell (top-right) shows a badge; click it to see the pending request
- **Mobile (if configured):** push notification with tool name and key arguments

**Step 3: Approve Request**

Approve from any one channel:

- **Email:** click **Approve**, confirmation page shows "Approval recorded successfully"
- **Web Dashboard:** click the notification → review → **Approve** (optionally add a reason)
- **Mobile App:** tap the notification → **Approve** → authenticate

**Step 4: Verify Execution**

In your MCP client the tool now executes and returns a result (illustrative):

```
Approval received
Payment of $500 sent to alice@example.com
Transaction ID: tx_abc123
```

In the dashboard, the request moves to "Approved" in **Approvals** → **History**.

### Verify Audit Trail

1. Go to **Approvals** → **History**
2. Find your test request and click it
3. Verify it shows: requester, tool (`pay`), arguments, status (Approved), approver, timestamps, and the execution result

### Result

**Pass if:** notification arrives within ~30 seconds, approval works, tool executes only after approval, and the audit log shows the complete record.

**Fail if:** no notification, tool executes immediately without approval, or no audit log entry.

---

## Test 2: Decline Workflow

### Goal

Verify declining an approval request prevents execution.

### Execute Test

**Step 1: Trigger Another Tool Call**

```
Using @preloop tools, pay bob@example.com $1000
```

**Step 2: Decline Request**

When the notification arrives, click **Decline** (email or dashboard) and add a reason, e.g. "Test decline".

**Step 3: Verify No Execution**

Your MCP client receives a decline message (illustrative):

```
Approval declined
Reason: Test decline
The tool was not executed.
```

The dashboard shows the request as "Declined" with your reason, and no execution result.

### Result

**Pass if:** the tool does NOT execute, the client receives the decline message, and the audit log shows "Declined".

**Fail if:** the tool executes despite decline.

---

## Test 3: Conditional Approval

### Goal

Verify CEL conditions work: approval only required when the condition matches.

### Setup

1. Go to Tools & MCP → Find `pay` tool → Configure
2. In **Approval Condition**, enter:
   ```cel
   args.amount > 1000
   ```
3. Click **Save**

This means: only require approval if amount > $1000.

### Execute Test

**Test A: Below Threshold**

```
Using @preloop tools, pay charlie@example.com $500
```

Expected: no approval request — the tool executes immediately.

**Test B: Above Threshold**

```
Using @preloop tools, pay charlie@example.com $1500
```

Expected: approval request sent, client waits, tool executes after you approve.

### Result

**Pass if:** small amounts execute immediately, large amounts require approval.

**Fail if:** all amounts require approval (condition ignored) or none do (condition broken).

---

## Remaining Checks

Once tests 1-3 pass, the core Safety Layer works. Run through these quicker checks as needed:

- **Team-based approval with quorum** (needs 2+ users): create a team under **Settings** → **Teams**, set it as the `pay` approver with quorum 2, and trigger a call. The tool must wait for the second approval before executing, and the audit log should show both approvers. See [Team-Based Approvals](../approvals/teams.md).
- **Timeout and escalation:** set a short timeout (e.g. 120s) and escalation delay (e.g. 60s) on the `pay` policy, trigger a call, and don't respond. The escalation contact should be notified after the delay, and after the timeout the client should receive a timeout error with the tool NOT executed.
- **Multiple notification channels:** enable email, web, Slack, and mobile for the `pay` policy and trigger a call. All configured channels should notify within ~30 seconds, and approving from one channel should update the others.
- **Non-prelooped tool:** call `get_random_number` from the example server with **Require Approval** OFF. It should execute immediately with no notification, but still appear in the audit trail.
- **Error handling:** trigger a call with invalid arguments (e.g. `pay invalid-email $-100`) and approve it — the execution failure should be reported clearly. Then point the example server at an invalid URL and confirm you get a clean "server unavailable" error instead of a silent failure; restore the URL to `https://example-mcp.preloop.ai/mcp` afterwards.

## Next Steps

Your setup is working. **Next:** [Add your own MCP tools](../tools/external-mcp.md) or [create automated flows](../flows/creating-flows.md).
