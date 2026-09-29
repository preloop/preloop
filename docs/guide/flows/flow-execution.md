# Flow Execution

Monitor, debug, and manage flow executions.

---

## Overview

Every time a flow triggers, an **execution** is created. An execution represents a single run of a flow, from trigger to completion.

**Execution Lifecycle:**

```
PENDING → INITIALIZING → STARTING → RUNNING → SUCCEEDED | FAILED | STOPPED
```

If a tool call requires approval, the execution stays `RUNNING` while it waits: there is no separate waiting-for-approval state.

**Key Features:**

- Real-time log streaming
- Complete audit trail
- Tool call tracking
- Approval integration
- Retry of failed/stopped executions
- Error debugging

---

## Viewing Executions

### All Executions

View all flow executions across all flows:

1. Navigate to **Flows** → **Executions** in left sidebar
2. See table of all executions

**Table Columns:**

- **Status** - Current state (pending, running, succeeded, failed)
- **Flow** - Which flow executed
- **Trigger** - What triggered it (webhook, GitHub issue, etc.)
- **Started** - Start timestamp
- **Duration** - How long it took/is taking
- **Actions** - View details button

### Flow-Specific Executions

View executions for a single flow:

1. Navigate to **Flows** → Select a flow
2. Scroll to **Recent Executions** section
3. Shows last 10 executions for this flow

### Real-Time Updates

Execution pages update in real-time via WebSocket:
- Status changes immediately
- Logs stream as they happen
- New executions appear automatically
- No page refresh needed

---

## Execution States

### PENDING

**Meaning:** Execution created, waiting to start

**What's Happening:**

- Waiting for available agent slot
- Loading flow configuration

**Next State:** INITIALIZING

---

### INITIALIZING

**Meaning:** Preparing the execution environment

**What's Happening:**

- Provisioning the isolated container
- Resolving model and MCP configuration

**Next State:** STARTING

---

### STARTING

**Meaning:** Container is starting up

**What's Happening:**

- Installing/configuring the agent CLI
- Running git clone and custom init commands

**Next State:** RUNNING

---

### RUNNING

**Meaning:** Agent is actively executing

**What's Happening:**

- Agent reading prompt
- Calling tools
- Processing results
- May pause for approvals: the execution stays RUNNING while a prelooped tool call waits on human approval

**Next States:** SUCCEEDED, FAILED, or STOPPED

---

### SUCCEEDED

**Meaning:** Execution completed successfully

**What's Happening:**

- Agent finished all tasks
- All tool calls succeeded
- Prompt goals achieved

**Final State:** Yes

---

### FAILED

**Meaning:** Execution failed

**Common Causes:**

1. Tool call failed (e.g., invalid arguments, server error)
2. Approval declined or approval request timed out
3. Agent error (e.g., unable to parse response)
4. MCP tool call timed out (default 600 seconds)
5. Out of API credits

**Next Steps:**

- Check execution logs for error details
- Review tool call history
- Fix issue and click **Retry**

**Final State:** Yes (retryable)

---

### STOPPED

**Meaning:** Stopped by a user

**How to Stop:**

1. Go to execution details page
2. Click **Cancel**
3. Confirm

**Effect:**

- Agent stops
- Any in-progress tool calls may complete
- Execution marked as STOPPED

**Final State:** Yes (retryable)

---

## Execution Details Page

### Overview Section

**Information Displayed:**


**Status Badge**

- Color-coded by state
- Real-time updates

**Timing:**

- Started: Timestamp when execution began
- Duration: How long it took (or is taking)
- Ended: Timestamp when completed (if finished)

**Trigger Info:**

- Trigger type (webhook, GitHub issue, etc.)
- Trigger event data (issue #, PR #, webhook payload)
- Link to source (GitHub issue, GitLab MR, etc.)

**Flow Details:**

- Flow name
- Agent type and model used
- Tools available to agent

---

### Agent Logs

Real-time streaming logs from the AI agent.

**What You'll See:**


**Agent Reasoning:**

```
[2025-01-25 10:30:15] Starting task: Process payment for contractor
[2025-01-25 10:30:16] Reading prompt template...
[2025-01-25 10:30:18] Recipient: contractor@example.com
[2025-01-25 10:30:18] Amount: $1500
[2025-01-25 10:30:19] Contract: CONTRACT-2025-001
[2025-01-25 10:30:20] Calling pay tool with arguments...
```

**Tool Calls:**

```
[2025-01-25 10:30:21] Tool: pay
[2025-01-25 10:30:21] Arguments: {"recipient": "contractor@example.com", "amount": 1500, "contract_id": "CONTRACT-2025-001"}
[2025-01-25 10:30:22] Status: Waiting for approval (tool is prelooped)
```

**Approval Waiting:**

```
[2025-01-25 10:30:23] Approval request sent to: alice@example.com
[2025-01-25 10:30:23] Waiting for approval... (timeout: 10 minutes)
```

**Approval Granted:**

```
[2025-01-25 10:35:45] Approval received from: alice@example.com
[2025-01-25 10:35:46] Executing tool...
[2025-01-25 10:35:48] Tool result: {"transaction_id": "tx_abc123", "status": "completed"}
```

**Completion:**

```
[2025-01-25 10:35:50] Payment processed successfully
[2025-01-25 10:35:51] Transaction ID: tx_abc123
[2025-01-25 10:35:52] Task completed
```

**Features:**

- Auto-scroll to latest log
- Pause auto-scroll to review earlier logs
- Timestamps on every line
- Color-coded by log level (info, warning, error)

---

### Tool Calls

List of all tools called during execution.

**Table Columns:**

- **Tool Name** - Which tool was called
- **Status** - Succeeded, failed, waiting for approval
- **Arguments** - JSON of arguments passed
- **Result** - JSON of result returned
- **Duration** - How long the call took
- **Approval** - Link to approval request (if prelooped)

**Example:**


| Tool | Status | Arguments | Result | Duration |
|------|--------|-----------|--------|----------|
| pay | Succeeded | `{"recipient": "test@example.com", "amount": 1500}` | `{"transaction_id": "tx_123", "status": "completed"}` | 5.2s |
| update_contract | Succeeded | `{"contract_id": "CONTRACT-001", "status": "paid"}` | `{"updated": true}` | 0.8s |

**Click a tool call to see:**

- Full arguments (formatted JSON)
- Full result (formatted JSON)
- Error details (if failed)
- Approval details (if prelooped)

---

### Approval Requests

If the agent called prelooped tools, approval requests are shown here.

**Information:**
- **Tool** - Which tool required approval
- **Status** - Pending, approved, declined, timeout
- **Requester** - Who/what triggered the approval
- **Approver** - Who approved/declined
- **Requested At** - Timestamp
- **Responded At** - When approval was granted/declined
- **Reason** - Approval/decline reason (if provided)

**Link to Approval:**
Click to view full approval request details in the Approvals section.

---

### Gateway Events and Metrics

For flows using a gateway-enabled model, you can inspect the model traffic and resource usage of an execution:

- **Gateway events** - Every model request/response routed through the Preloop gateway for this execution:
  ```
  GET /api/v1/flows/executions/{execution_id}/gateway-events
  ```
- **Metrics** - Aggregated execution metrics:
  ```
  GET /api/v1/flows/executions/{execution_id}/metrics
  ```

Both are also surfaced on the execution details page.

---

### Error Details

If execution failed, error details are shown prominently.

**Information:**
- **Error Type** - Tool failure, agent error, tool-call timeout, approval declined
- **Error Message** - Detailed error message
- **Stack Trace** - If applicable
- **Failed Tool** - Which tool call failed (if any)
- **Suggestions** - Potential fixes

**Example Error Messages (illustrative):**

**Tool Execution Error:**
```
Tool 'pay' failed with error: Invalid recipient email address
```

**Approval Declined:**
```
Approval request declined by alice@example.com
Reason: Amount exceeds budget for this month
```

**Tool Call Timeout:**
```
Tool call timed out after 600 seconds
```

**API Error:**
```
OpenAI API error: Rate limit exceeded
```

---

## Agent Runtimes

Each flow execution runs inside an isolated container. The agent type determines the CLI tool, configuration, and execution lifecycle.

### Supported Agents

| Agent | CLI | MCP Integration | Key Feature |
|-------|-----|-----------------|-------------|
| **Codex** | `codex exec` | Preloop MCP server | Fast non-interactive coding |
| **Gemini CLI** | `gemini` | MCP server via `settings.json` | Google ecosystem, stream-json output |
| **OpenCode** | `opencode run` | MCP server via `opencode.json` | Lightweight multi-provider agent |

### Execution Lifecycle

Every agent script follows the same lifecycle inside the container:

1. **Environment setup**: install CLI, configure model access, and set up MCP server connection
2. **Initialization commands**: git clone, custom init commands from the flow
3. **`PRELOOP_AGENT_EXEC_START` sentinel**: printed to stdout to signal the orchestrator that the agent is about to start. Success/failure detection is suppressed until this marker is seen, preventing false positives from setup output.
4. **Agent execution**: the CLI runs with the flow's prompt
5. **Exit code capture**: the container exits with the agent's exit code
6. **Post-execution sleep**: optional debug window (`AGENT_POST_EXEC_SLEEP` env var)

### MCP Server Connection

All agents connect to the Preloop MCP server for tool access. The connection is configured via environment variables injected into the container:

| Variable | Description |
|----------|-------------|
| `PRELOOP_MCP_URL` | Preloop MCP server URL (set automatically) |
| `PRELOOP_API_TOKEN` | Short-lived runtime token for authenticated MCP and gateway access |

Tools available to the agent are restricted to the flow's configured tool list. Approval policies and justification requirements are enforced server-side by the MCP server.

### Model Gateway Access

When a flow uses a gateway-enabled AI model, the agent does not need to talk directly to the upstream model provider. Instead, Preloop can inject managed gateway settings so model traffic flows through the Preloop control plane.

- **Gateway-enabled path**: the agent receives a managed base URL, model alias, and short-lived bearer token
- **Direct-provider path**: the agent uses provider-specific access when a gateway route is unavailable or intentionally disabled
- **Shared observability**: gateway requests are attributed to the account, flow, flow execution, runtime session, and runtime principal in one usage ledger

---

## Monitoring Executions

### Dashboard View

The main dashboard combines execution and runtime-control-plane stats:

1. Navigate to **Dashboard** (home page)
2. See metrics:
   - **Active Runtime Sessions** - Managed sessions currently active across flows and enrolled agents
   - **Recent Tool Calls** - Tool-call volume across managed runtimes
   - **Daily Model Spend** - Current day gateway cost aggregated from model traffic
   - **Gateway Success Rate** - Recent success rate for model-gateway traffic
3. Use recent executions, runtime sessions, and model views together when debugging automation behavior

### Flow Health

On each flow's details page:

**Recent Executions Chart:**
- Bar chart of last 10 executions
- Green = succeeded, Red = failed
- Click bar to view that execution

**Success Rate:**
- Percentage of successful executions (last 30 days)
- Color-coded: Green (>90%), Yellow (70-90%), Red (<70%)

**Average Duration:**
- Mean execution time for this flow
- Helps identify performance issues

---

## Debugging Failed Executions

### Step 1: Review Error Message

1. Go to execution details page
2. Look at the error section (shown prominently if failed)
3. Read error message and suggestions

### Step 2: Check Agent Logs

1. Scroll to Agent Logs section
2. Look for ERROR or WARNING messages
3. Find where execution stopped
4. Identify which tool call failed

If the flow uses a gateway-enabled model, also review the execution's gateway events and linked runtime session activity to confirm whether the failure happened during model routing, budget enforcement, upstream provider communication, or tool execution after model output.

**Common Log Patterns:**

**Tool Not Found:**
```
[ERROR] Tool 'deploy_production' not found in allowed tools
```
**Fix:** Edit flow → Tools → Check "deploy_production"

**Invalid Arguments:**
```
[ERROR] Tool 'pay' failed: required argument 'amount' missing
```
**Fix:** Update prompt template to provide 'amount' argument

**Approval Declined:**
```
[WARNING] Approval declined by approver: Reason: Budget exceeded
```
**Fix:** Adjust approval workflow or amount

### Step 3: Check Tool Calls

1. Go to Tool Calls section
2. Find the failed tool call
3. Click to see full details
4. Review arguments sent vs. expected

### Step 4: Review Trigger Data

1. Check trigger event payload
2. Verify template variables resolved correctly
3. Ensure all required data was present

**Example Issue:**
```
Prompt Template: Amount: ${{trigger_event.payload.amount}}
Trigger Payload: {"payment": 1500}  # Wrong key!
Result: Amount: $undefined
```
**Fix:** Update prompt to use `{{trigger_event.payload.payment}}`

### Step 5: Test with Simpler Case

1. Click **Test Run** on the flow
2. Provide minimal test data
3. See if it succeeds with simple input
4. Gradually add complexity

---

## Performance Optimization

### Reduce Execution Time

**1. Limit Tool Access**
- Only enable tools this flow needs
- Agent spends less time choosing tools
- Faster decision making

**2. Optimize Prompt**
- Be specific about which tool to use
- Provide clear success criteria
- Avoid ambiguous instructions

**Before (slow):**
```
Process the payment somehow and update the system.
```

**After (fast):**
```
1. Use the pay tool to send payment
2. Use the update_contract tool to mark contract as paid
3. Report the transaction ID
```

**3. Use Faster Models**
- GPT-5.4-mini is faster than GPT-5.4
- Claude Sonnet is faster than Claude Opus
- Test different models for your use case

**4. Reduce Prompt Length**
- Remove unnecessary context
- Keep it concise but complete
- Agent processes shorter prompts faster

**5. Parallel Tool Calls**
- If agent supports it, allow parallel execution
- Multiple independent tools can run simultaneously

---

## Retrying Executions

Failed or stopped executions can be retried directly.

**From the UI:**

1. Open the execution details page
2. Click **Retry**

**From the API:**

```bash
curl -X POST 'https://<your-preloop-host>/api/v1/flows/executions/{execution_id}/retry' \
  -H 'Authorization: Bearer YOUR_API_KEY'
```

**How it works:**

- Retry is available for executions in `FAILED` or `STOPPED` status (plus the legacy `TIMEOUT` and `CANCELLED` statuses from older versions)
- The retry creates a **new** execution reusing the original trigger event data: the original execution is untouched
- The new execution is linked to the original via `retry_of_execution_id`, so you can trace retry chains

There is no automatic retry, you decide when a failure is worth re-running.

---
