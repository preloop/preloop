# Creating Flows

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Create a flow that runs an AI agent when an event fires (a GitHub PR, a GitLab MR, a Jira ticket, or any HTTP webhook) with approval workflows enforced on every prelooped tool call.

---

## Overview

**Flows** are event-driven automation workflows in Preloop. Each flow:

- **Triggers** on events (GitHub PR, GitLab MR, Jira ticket, webhook)
- **Executes** AI agent tasks using configured tools
- **Respects** approval workflows on prelooped tools
- **Logs** complete audit trail of all actions

**Key Concepts:**

- **Flow** - The automated workflow definition
- **Trigger** - Event that starts the flow (webhook or tracker event)
- **AI Agent** - The agent type executing the flow (Codex CLI, Gemini CLI, or OpenCode)
- **AI Model** - The LLM powering the agent (for example GPT-5.4 or Claude Sonnet 4.7)
- **Prompt Template** - Instructions for the agent (supports variable interpolation)
- **Tools** - MCP tools the agent can use (respects approval workflows)
- **Execution** - Single run of a flow (triggered by an event)

---

## Creating Your First Flow

### Step 1: Navigate to Flows

1. Click **Flows** in the left sidebar
2. Click **Create flow**

### Step 2: Choose Creation Method

You have two options:

**Option A: Use a Preset**

Start from a template for common automation scenarios (auto-triage issues, deploy on PR merge, create a ticket from a webhook). Click a preset card to select it.

Flows cloned from a preset track their source: if the preset is later updated, the flow shows a **preset update available** badge and you can apply or dismiss the update. Applying overwrites the prompt/tools unless you customized them (customizations are flagged as `prompt_customized` / `tools_customized`).

Four presets (SBOM Verify, SBOM Exploit Check, Release Security Audit, Component Due Diligence Record) turn a CI-emitted SBOM into a versioned `result.json` plus a human-readable evidence pack for a human assessor: see [Security audit presets (CRA evidence packs)](security-audit-presets.md).

**Option B: Blank flow**

Build a custom flow from the ground up. Pick **Blank flow** at the top of the form.

For this guide, we'll start from a blank flow.

---

## Flow Configuration

### Basic Information

**Name** (required)
- Clear, descriptive name
- Shows in flow list and execution logs

**Description** (optional)
- What the flow does
- When it should trigger
- Any special conditions

**Examples:**

```yaml
Name: Auto-triage Critical Issues
Description: Automatically labels GitHub issues as "critical" when created with "urgent" in title

Name: Deploy to Staging on PR Merge
Description: Triggers deployment to staging environment when PR is merged to main branch
```

---

### Trigger Configuration

Flows can be triggered by:

1. **Webhooks**: HTTP POST from any external service
2. **Tracker events**: GitHub, GitLab, Jira events
3. **Schedules**: an interval, daily or weekly time, or cron expression, see [Schedule Triggers](flow-triggers.md#schedule-triggers)

#### Option 1: Webhook Trigger

**When to use:**

- Custom integrations
- Services without native tracker support
- Manual triggers via API
- Testing and development

**Configuration:**

1. Select **Trigger type:** Webhook
2. Save the flow (webhook URL generated after creation)
3. Use the webhook URL to trigger the flow

**Webhook URL format:**

```
https://<your-preloop-host>/api/v1/webhooks/flows/{flow_id}/{webhook_secret}
```

**Example payload:**

```json
{
  "event": "payment_received",
  "amount": 1500,
  "customer_id": "cust_123",
  "contract_id": "contract_456"
}
```

**Access in prompt template:**

```
Customer: {{trigger_event.payload.customer_id}}
Amount: ${{trigger_event.payload.amount}}
```

#### Option 2: Tracker Event Trigger

**When to use:**

- GitHub PR/issue events
- GitLab MR/issue events
- Jira ticket events
- Automated workflows based on development activity

**Configuration:**


1. **Trigger type:** Tracker event

2. **Tracker**: choose from your configured trackers
   - If no trackers exist, add one under **Trackers > Add tracker**

3. **Organization**: the GitHub org, GitLab group, or Jira project

4. **Projects (optional)**: specific repository or project
   - Can leave empty to trigger on all projects in organization

5. **Events**: what triggers the flow
   - GitHub: Issue opened, PR opened, PR merged, push, etc.
   - GitLab: MR opened, MR merged, issue opened, pipeline, etc.
   - Jira: Issue opened, issue updated, comment created, etc.
   - A flow can subscribe to multiple event types at once (`trigger_event_types` is an array), for example, trigger on both `issue_opened` and `issue_updated`

**GitHub Event Types:**

- `issue_opened` - New issue created
- `issue_updated` - Issue edited (title, description, labels, etc.)
- `issue_closed` - Issue closed
- `issue_reopened` - Issue reopened
- `pull_request_opened` - New PR created
- `pull_request_updated` - PR edited or commits pushed
- `pull_request_closed` - PR closed without merge
- `pull_request_merged` - PR merged
- `pull_request_reopened` - PR reopened
- `comment_created` - New comment on issue/PR
- `comment_updated` - Comment edited
- `push` - Code pushed to repository
- `release` - New release published

**GitLab Event Types:**

- `issue_opened`, `issue_updated`, `issue_closed`, `issue_reopened`
- `merge_request_opened`, `merge_request_updated`, `merge_request_closed`
- `merge_request_merged`, `merge_request_approved`, `merge_request_reopened`
- `comment_created`, `comment_updated`
- `push`, `tag_push`
- `pipeline` - Pipeline event (success, failure, etc.)
- `release` - Release published

**Jira Event Types:**

- `issue_opened` - New issue created
- `issue_updated` - Issue field changed
- `issue_deleted` - Issue deleted
- `comment_created`, `comment_updated`, `comment_deleted`

**Event Filters (Optional)**


Narrow when the flow triggers with filters:

- **Author/Creator** - Username who created the issue/PR
- **Assignee** - Who it's assigned to
- **Reviewer** - Requested reviewer (PR/MR only)
- **Labels** - Must have ALL specified labels
- **Milestone** - Milestone name (GitHub/GitLab only)
- **Priority** - Jira priority (Highest, High, Medium, Low, Lowest)
- **Issue Type** - Jira issue type (Task, Bug, Story, Epic, etc.)
- **State** - PR/MR state (open, closed, merged)
- **Draft** - Filter draft PR/MRs
- **Merged** - Only trigger if PR/MR is merged
- **Mergeable State** - GitHub: clean, unstable, dirty, blocked

**Filter Logic:**

- All filters are AND (all must match)
- Labels are OR (any label matches)
- Empty filters match everything

**Example: Only Critical Bugs**

```yaml
Event: issue_opened
Filters:
  labels: [bug, critical]
  assignee: sre-team
```

**Example: Only Merged to Main**

```yaml
Event: pull_request_merged
Filters:
  merged: true
  target_branch: main  # In trigger_config
```

**Access Trigger Event Data in Prompt:**


```
Issue Title: {{trigger_event.payload.object_attributes.title}}
Issue URL: {{trigger_event.payload.object_attributes.url}}
Author: {{trigger_event.payload.user.username}}
Labels: {{trigger_event.payload.labels}}
```

See [Flow Triggers](flow-triggers.md) for complete event payload reference.

---

### AI Agent Configuration

Flows need an AI agent to execute the automation tasks.

#### Select Agent Type

**Available agents:**

1. **Codex CLI** (default)
   - Fast, non-interactive agentic coding (`codex exec`)
   - Best for: Code generation, file manipulation, git operations

2. **Gemini CLI**
   - Google's terminal agent
   - Best for: Google ecosystem integrations

3. **OpenCode**
   - Lightweight multi-provider agentic coding CLI
   - Best for: Quick code tasks, scripting, automation

#### Configure AI Model

Each flow needs an AI model (LLM) to power the agent.

**If you have no models configured:**

1. Under **AI model**, click **Add AI model**
2. Fill in:
   - **Name:** Descriptive name (e.g., "GPT-5.4")
   - **Provider:** OpenAI, Anthropic, Google, Qwen, DeepSeek, or OpenAI-compatible
   - **Model:** Select from dropdown (filtered by agent compatibility)
   - **Credentials:** Configure the upstream provider credential or secret reference when the model needs direct provider access
   - **Gateway Routing:** Enable Preloop gateway routing when you want model traffic to flow through Preloop's OpenAI-compatible or Anthropic-compatible gateway
3. Click **Save**

**If you have existing models:**

- Select from the dropdown (filtered by agent type compatibility)
- Or click **Add AI model** to add another

**How model routing works:**

- **Gateway-enabled models** send model traffic through Preloop so budget checks, attribution, runtime-session telemetry, and captured gateway events stay centralized
- **Direct-provider models** can still be used when a gateway path is unavailable or not yet supported for that agent/provider combination
- **Secret-backed credentials** let Preloop keep provider secrets behind the model record instead of embedding them directly into flow definitions

**What the agent receives at runtime:**

- The resolved prompt and tool allowlist
- The selected model identity and agent-specific configuration
- Either direct provider access details or Preloop gateway settings such as a managed base URL, model alias, and short-lived bearer token

The model dropdown filters to providers the selected agent supports.

After the flow runs, you can inspect model traffic in execution-scoped gateway events, account usage summaries, runtime session views, and per-model observability screens.

#### Write the Prompt Template

The prompt tells the agent what to do. It supports template variables from the trigger event.

**Template Variables:**


Access trigger event data using `{{trigger_event.*}}` syntax:

```
{{trigger_event.payload.object_attributes.title}}
{{trigger_event.payload.user.username}}
{{trigger_event.payload.labels}}
{{trigger_event.payload.any_field.nested_field}}
```

**Example Prompts:**


**Auto-triage GitHub Issues:**
```
A new GitHub issue was created:

Title: {{trigger_event.payload.issue.title}}
Body: {{trigger_event.payload.issue.body}}
Author: {{trigger_event.payload.issue.user.login}}
Repository: {{trigger_event.payload.repository.full_name}}

Your task:
1. Analyze the issue content
2. Determine if it's a bug, feature request, or question
3. Add appropriate label using the update_issue tool
4. If it's a bug and mentions "crash" or "error", also add the "urgent" label
5. Use the add_comment tool to acknowledge receipt and estimated triage time
```

**Deploy on PR Merge:**
```
A pull request was merged to main branch:

PR Title: {{trigger_event.payload.pull_request.title}}
PR Number: #{{trigger_event.payload.pull_request.number}}
Author: {{trigger_event.payload.pull_request.user.login}}
Repository: {{trigger_event.payload.repository.full_name}}

Your task:
1. Use the deploy_to_staging tool to deploy this branch to staging environment
2. Run the smoke_tests tool to verify the deployment
3. If tests pass, post a success comment on the PR with the staging URL
4. If tests fail, post a failure comment with logs and rollback the deployment
```

**Process Payment (Webhook):**
```
A payment was received:

Customer ID: {{trigger_event.payload.customer_id}}
Amount: ${{trigger_event.payload.amount}}
Contract ID: {{trigger_event.payload.contract_id}}
Payment Method: {{trigger_event.payload.payment_method}}

Your task:
1. Use the pay tool to process the payment to the contractor
2. The pay tool has approval workflow: amounts > $1000 require CFO approval
3. After payment completes, update the contract status using update_contract tool
4. Send confirmation email to the customer
```

**Prompt Best Practices:**
- Be specific about what tools to use
- Include all relevant context from trigger event
- Specify success/failure handling
- Mention approval workflows if relevant
- Keep it concise but complete

---

### Tool Selection

Select which MCP tools the agent can use during execution.

**Tool Categories:**

1. **Built-in Tools** (Preloop MCP server)
   - `get_issue`, `create_issue`, `update_issue`, `search` (available when a tracker is connected)
   - `add_comment`, `update_comment`
   - `get_pull_request`, `create_pull_request`, `update_pull_request`
   - `estimate_compliance`, `improve_compliance`
   - `request_approval`, `get_approval_status` (manual approval requests)
   - `ask_user` (ask a human a question mid-execution)

2. **External MCP Tools**
   - Tools from your added MCP servers
   - Payment, deployment, database, cloud provider tools
   - Custom tools from your own MCP servers

**Selection:**
- **By default:** No tools are selected
- **Customize:** Check/uncheck specific tools
- **Disabled tools:** Cannot be selected (shown grayed out)

**Security Note:**
- Only selected tools are available to the agent
- Agent cannot use tools outside this list
- Approval policies still apply to prelooped tools

**Best Practice:**
- Select only tools needed for this flow
- Reduces attack surface
- Prevents accidental misuse
- Clearer agent context

---

### Git Workspace Cloning (Optional)

Automatically clone repositories before the agent starts.

**When to use:**

- Flow needs to read/modify code
- Agent will create commits
- Multi-repository operations

**Configuration:**


**1. Enable Git Workspace Cloning**
- Toggle **Enable Git Workspace Cloning** to ON

**2. Git User Info**
```yaml
Git User Name: Preloop
Git User Email: git@preloop.ai
```
Used for git commits made by the agent.

**3. Branch Configuration**
```yaml
Source Branch: main  # Branch to checkout
Target Branch:       # Branch to create for commits (auto-generated if empty)
```

**4. Pull Request / Merge Request**
- Toggle **Create Pull Request** to ON
- **PR/MR Title:** (optional, defaults to flow name)
- **PR/MR Description:** (optional)

**5. Repositories**

Add one or more repositories to clone:

**Per Repository:**
- **Tracker:** Select GitHub/GitLab tracker
- **Repository URL:** (optional - leave empty to use trigger project)
- **Clone Path:** Where to clone (e.g., `/workspace`)
- **Branch:** (optional - leave empty for default branch)

**Example 1: Single Repo (from trigger)**
```yaml
Repository 1:
  Tracker: GitHub Production
  Repository URL: [empty - auto-detect from trigger]
  Clone Path: /workspace
  Branch: [empty - use default]
```

**Example 2: Multi-Repo**
```yaml
Repository 1:
  Tracker: GitHub Production
  Repository URL: https://github.com/acme/backend
  Clone Path: /workspace/backend
  Branch: main

Repository 2:
  Tracker: GitHub Production
  Repository URL: https://github.com/acme/frontend
  Clone Path: /workspace/frontend
  Branch: main
```

**How It Works:**

1. Flow triggers
2. Preloop clones repositories
3. Agent starts with repos available
4. Agent can read/modify files
5. If Create PR enabled, commits are pushed and PR created automatically

Use repository cloning and PR creation when the flow needs to inspect or modify source code during execution.

---

### Custom Commands (Admin Only)

Custom initialization commands run inside the container before the agent starts (for example, `pip install -r requirements.txt` or a setup script). They execute sequentially, and any failure stops the execution.

Commands run with full container privileges, so they are restricted: they are configured by administrators, and in the flow detail view they render read-only. There is no custom-commands editor in the create form.

---

## Saving and Testing

### Save the Flow

1. Review all configuration
2. Click **Create flow** (or **Save changes** if editing)

Flows you create or clone start **Enabled**, they trigger on matching events immediately. Only built-in presets start disabled. Use **Pause** on the flow details page if you want to stop it from triggering, and **Resume** to turn it back on.

### Run now

Start a run by hand before real events trigger it. The run is a real one and spends like any other:

1. Click **Run now**
2. If the prompt has `{{trigger_event.*}}` variables, the dialog **Values for the trigger event** opens
3. Fill in values for each variable:
   ```
   trigger_event.payload.recipient: test@example.com
   trigger_event.payload.amount: 150
   trigger_event.payload.contract_id: TEST-001
   ```
4. Click **Run now**
5. You'll be redirected to the execution page to watch it run

---

## Flow Execution

### Monitoring Executions

Each time a flow triggers, an **execution** is created.

**View Executions:**
1. Go to flow details page
2. Scroll to **Recent executions**
3. Or: on **Flows**, click **View all** next to **In flight** for all executions

**Execution States:**
- `PENDING` - Queued, not started yet
- `INITIALIZING` - Preparing the execution environment
- `STARTING` - Container starting up
- `RUNNING` - Currently executing (stays RUNNING while a tool call waits on approval)
- `SUCCEEDED` - Completed successfully
- `FAILED` - Execution failed (see logs for details)
- `STOPPED` - Stopped by a user

### Viewing Execution Details

Click an execution to see:

1. **Overview**
   - Status, start time, duration
   - Trigger event details
   - Agent output

2. **Agent Logs**
   - Real-time streaming logs
   - Tool calls made by agent
   - Approval requests (if any)
   - Errors and warnings

3. **Tool Calls**
   - List of all tools called
   - Arguments passed
   - Results returned
   - Approval status

4. **Approval Requests**
   - If agent called prelooped tools
   - Shows approval status
   - Links to approval details

Execution pages update in real-time via WebSocket. See [Flow Execution](flow-execution.md) for retrying executions, gateway-event inspection, and debugging.

---

## Common Flow Patterns

### Pattern 1: Auto-Triage Issues

**Trigger:** GitHub issue opened

**Prompt:**
```
Analyze this issue and apply appropriate labels:

Title: {{trigger_event.payload.issue.title}}
Body: {{trigger_event.payload.issue.body}}

Rules:
- Contains "bug", "error", "crash" → add "bug" label
- Contains "feature", "enhancement", "request" → add "feature" label
- Contains "question", "help", "how to" → add "question" label
- Contains "urgent", "critical", "blocker" → add "priority:high" label

Use the update_issue tool to add labels.
```

**Tools:** `update_issue`, `add_comment`

**Result:** Issues automatically labeled within seconds

---

### Pattern 2: Deploy on PR Merge

**Trigger:** GitHub pull request merged

**Filters:**
```yaml
merged: true
target_branch: main
```

**Prompt:**
```
Deploy the merged PR to staging:

PR: {{trigger_event.payload.pull_request.title}}
Branch: {{trigger_event.payload.pull_request.head.ref}}

Steps:
1. Use deploy_to_staging tool
2. Run smoke_tests tool
3. If tests pass, post success comment on PR
4. If tests fail, rollback and post failure comment
```

**Tools:** `deploy_to_staging`, `smoke_tests`, `rollback` (from your MCP servers), `add_comment`

**Git Workspace Cloning:** Enabled (to access code)

**Result:** Automatic deployment on every merge

---

### Pattern 3: Payment Processing with Approval

**Trigger:** Webhook

**Prompt:**
```
Process contract payment:

Amount: ${{trigger_event.payload.amount}}
Recipient: {{trigger_event.payload.recipient}}
Contract: {{trigger_event.payload.contract_id}}

Use the pay tool (requires CFO approval for amounts > $1000).
After payment, update contract status.
```

**Tools:** `pay` (prelooped with amount > 1000 condition), `update_contract`

**Result:** Automated payments with approval for large amounts

---
