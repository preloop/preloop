# Prelooping External MCP Tools

Add external MCP servers to Preloop and wrap their tools with approval workflows.

---

## Overview

**External MCP tools** are tools provided by MCP servers you connect to Preloop. Unlike built-in tools, these come from:

- Example MCP servers (like `example-mcp.preloop.ai`)
- Third-party MCP servers (Anthropic, community, vendors)
- Your own custom MCP servers

**Key benefit:** Preloop ANY tool from ANY MCP server - deployment, database, filesystem, payment, etc.

!!! note "Tools Page Layout"
    External MCP servers are listed **above** built-in tools in the Tools page. The same **access rules** system (allow/deny/require_approval, CEL conditions, priority ordering) applies to all tool types: built-in, MCP-proxied, and HTTP.

---

## Why External MCP Tools?

### Unlimited Tool Integration

Built-in tools (create_issue, update_issue) are limited to what Preloop provides. External tools let you:

- **Deploy to production** - Wrap deployment tools with approval
- **Modify databases** - Require approval before DROP/ALTER
- **Process payments** - Approve large transactions
- **Modify infrastructure** - Approve firewall/scaling changes
- **ANY operation** - If it's exposed via MCP, you can preloop it

### Real-World Examples

Preloop uses **access rules** — ordered rules that evaluate to **deny**, **allow**, or **require_approval**. Rules use [CEL expressions](../approvals/cel-expressions.md) to match on tool arguments. The first matching rule wins (priority-based evaluation).

**Deployment Tools:**

```yaml
tools:
  - name: deploy_to_production
    source: my-deploy-server
    approval_workflow: "deploy-approval"
    conditions:
      - expression: 'args.environment == "production"'
        action: require_approval
        description: "Production deploys need human approval"
      - expression: 'args.environment == "staging"'
        action: allow
        description: "Staging deploys are safe"
```

**Database Tools:**

```yaml
tools:
  - name: drop_table
    source: db-mcp-server
    approval_workflow: "db-approval"
    conditions:
      - expression: 'args.database.startsWith("prod_")'
        action: require_approval
        description: "Production DB changes need approval"
      - expression: 'args.database.startsWith("test_")'
        action: allow
        description: "Test databases are safe"
      - expression: "true"
        action: deny
        description: "Block all other databases by default"
```

**Payment Tools:**

```yaml
tools:
  - name: process_refund
    source: payment-mcp-server
    approval_workflow: "finance-approval"
    conditions:
      - expression: "args.amount > 1000"
        action: require_approval
        description: "High-value refunds need approval"
      - expression: "args.amount <= 1000"
        action: allow
        description: "Small refunds are auto-approved"
```

---

## Adding External MCP Servers

### Step 1: Navigate to Tools & MCP

1. Go to **Tools & MCP** in left sidebar
2. Click **+ Add Source** → **MCP Server**

### Step 2: Configure Server

**Required Fields:**

- **Name** - Descriptive name (e.g., "Deployment Server")
- **URL** - MCP server endpoint
- **Transport** - `streamable-http` (default), `http-streaming`, `stdio`, or `sse`
- **Status** - Set to `active`

**Optional Fields:**

- **Auth Type** - `none`, `bearer`, `api_key`, or `oauth`
- **Headers** - Custom headers if needed
- **Description** - What tools this server provides

<!-- TODO screenshot: `external-mcp-add.png` -->

### Step 3: Scan for Tools

After adding the server:

1. Click **Scan Tools** button
2. Preloop discovers available tools
3. Tools appear in your tools list
4. Each tool shows its source server

<!-- TODO screenshot: `external-mcp-scan.png`, `external-mcp-tools-list.png` -->

---

## Example: Adding the Example MCP Server

Preloop hosts an example MCP server for testing:

### Configuration

```yaml
Name: Example MCP Server
URL: https://example-mcp.preloop.ai/mcp
Transport: streamable-http
Auth Type: none
Status: active
```

### Available Tools

After scanning, you'll see:

- **`pay(recipient, amount)`** - Send payment (simulated)
- **`rollback_deployment(environment)`** - Rollback deployment (simulated)
- **`get_random_number(min, max)`** - Generate random number
- **`calculate_fibonacci(n)`** - Calculate fibonacci number
- **`reverse_text(text)`** - Reverse text
- **`count_words(text)`** - Count words
- **`slow_fibonacci(n)`** - Fibonacci with progress updates

### Testing

Perfect for testing the approval flow:

```python
# In Claude Code or other MCP client
result = pay(recipient="alice@example.com", amount=1000)
# Approval request sent!
```

---

## Running Your Own MCP Server

### Custom MCP Server

Build your own MCP server to expose custom tools. You'll need to deploy it to a publicly accessible URL (e.g., on AWS, GCP, Heroku, or your own infrastructure) so Preloop can connect to it.

**Example server.py:**


```python
from mcp.server import MCPServer
from mcp.types import Tool

server = MCPServer()

@server.tool()
def deploy_to_staging(version: str, dry_run: bool = False):
    """Deploy application to staging environment"""
    if dry_run:
        return {"status": "dry_run", "version": version}

    # Your deployment logic here
    result = run_deployment(version, "staging")

    return {
        "status": "success",
        "version": version,
        "environment": "staging",
        "url": f"https://staging.acme.com"
    }

@server.tool()
def rollback_deployment(environment: str):
    """Rollback to previous deployment"""
    # Your rollback logic
    return {"status": "rolled_back", "environment": environment}

if __name__ == "__main__":
    server.run(host="0.0.0.0", port=8001)
```

**Deploy your server to a public URL:**

- AWS Lambda + API Gateway
- Google Cloud Run
- Heroku
- Your own infrastructure with HTTPS

**Then add to Preloop:**

```yaml
Name: Custom Deployment Server
URL: https://your-mcp-server.example.com
Transport: streamable-http
Auth Type: bearer
Token: your_secret_token_here
Status: active
```

See [MCP Server Development](https://spec.modelcontextprotocol.io/) for full guide.

---

## Authentication

### No Authentication

Public servers or local development:

```yaml
Auth Type: none
```

### Bearer Token

Most common for production:

```yaml
Auth Type: bearer
Token: your_secret_token_here
```

Server receives:
```
Authorization: Bearer your_secret_token_here
```

### API Key

Key sent as a custom header (defaults to `X-API-Key`):

```yaml
Auth Type: api_key
API Key: your_api_key
Key Name: X-API-Key  # optional, header name override
```

Server receives:

```
X-API-Key: your_api_key
```

### OAuth

OAuth access token, sent as a bearer token:

```yaml
Auth Type: oauth
Access Token: your_oauth_access_token
```

Server receives:

```
Authorization: Bearer your_oauth_access_token
```

---

## Prelooping External Tools

Once tools are discovered, you control access using **access rules**. Each rule has:

- **Action** — `deny`, `allow`, or `require_approval`
- **Condition** — A [CEL expression](../approvals/cel-expressions.md) that matches on the tool's arguments (e.g., `args.amount > 1000`)
- **Priority** — Rules with a lower priority value are evaluated first
- **Approval workflow** — For `require_approval` rules, which workflow to use (human or AI-driven)
- **Description** — Human-readable explanation of the rule

!!! tip "Rule Evaluation"
    Rules are evaluated in **priority order** (lowest priority value first). The first matching rule determines the action. If no rules match, the tool's default behavior applies.

### Option 1: Via Web UI

1. Find tool in **Tools & MCP** list
2. Click the tool to expand it
3. Click **+ Add Rule** to create an access rule
4. Configure the rule:
   - **Action**: Deny, Allow, or Require Approval
   - **Condition**: CEL expression (e.g., `args.amount > 500`)
   - **Approval Workflow** (if Require Approval): Select an existing workflow or create a new one
     - **Human approval**: Designated approvers review and decide
     - **AI-driven approval**: An AI model evaluates the request against guidelines
   - **Description**: Explain what this rule does
5. Drag rules to reorder priority
6. Click **Save**

<!-- TODO screenshot: `tool-config-external.png` -->

### Option 2: Via Policy-as-Code (YAML)

Define rules declaratively in a YAML policy file:

```yaml
version: "1.0"
metadata:
  name: "Payment Protection"
  description: "Access rules for payment tools"

approval_workflows:
  - name: "finance-human"
    timeout_seconds: 600
    approvals_required: 1

  - name: "finance-ai"
    approval_type: ai_driven
    ai_model: gpt-5.4
    ai_guidelines: "Approve refunds under $5000 with valid reason"
    ai_confidence_threshold: 0.85
    ai_fallback_behavior: escalate

tools:
  - name: "pay"
    source: example-mcp.preloop.ai/mcp
    approval_workflow: "finance-human"
    conditions:
      # Rule 1 (highest priority): Block payments to internal accounts
      - expression: 'args.recipient.endsWith("@internal.acme.com")'
        action: deny
        description: "Block payments to internal accounts"

      # Rule 2: Large payments need human approval
      - expression: "args.amount > 5000"
        action: require_approval
        description: "High-value payments need human review"

      # Rule 3: Medium payments can use AI approval
      - expression: "args.amount > 500"
        action: require_approval
        description: "Medium payments reviewed by AI"

      # Rule 4: Small payments auto-approved
      - expression: "args.amount <= 500"
        action: allow
        description: "Small payments are safe"

defaults:
  unknown_tools: require_approval
  default_approval_workflow: "finance-human"
```

Apply with the CLI:

```bash
preloop policy apply payment-protection.yaml
```

### Option 3: Via API

```bash
# Create an access rule for a tool
curl -X POST https://preloop.ai/api/v1/tool-configurations/{config_id}/access-rules \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "action": "require_approval",
    "condition_type": "cel",
    "condition_expression": "args.amount > 1000",
    "approval_workflow_id": "your_workflow_id",
    "priority": 100,
    "description": "Large payments need approval"
  }'
```

---

## Tool Discovery

### Automatic Discovery

When you click **Scan Tools**, Preloop:

1. Connects to MCP server
2. Calls `tools/list` method
3. Receives tool schemas
4. Parses tool names, descriptions, arguments
5. Displays in Tools & MCP list

### Manual Refresh

Tools don't update automatically. To refresh:

1. Go to **Tools & MCP**
2. Find your MCP server
3. Click **Scan Tools** again
4. New tools appear, removed tools disappear

**Recommended:** Scan after updating your MCP server.

### Tool Filtering

Preloop shows different tools to different users based on:

- **RBAC** - User role and permissions
- **MCP Server Status** - Active vs inactive
- **Tool Configuration** - Enabled vs disabled

---

## Tool Output Filters

Some MCP tools return verbose JSON results full of fields your agent never uses — raw HTML, embedded metadata, duplicate URLs. Every one of those fields is paid for again on each subsequent model call as session context. **Tool output filters** strip named top-level fields from a tool's JSON results on the proxy hot path, *before* the result reaches the calling agent, without modifying the upstream tool.

How a filter works:

- Each filter names a **tool** and a list of **dropped fields** (top-level keys to remove from each result object).
- Filters are **account-scoped**. Optionally narrow one to a specific MCP **server** and/or a specific **managed agent**; left unset, it matches any server and applies account-wide.
- When several filters match the same call, the union of their dropped fields is stripped.
- Filtering only applies to results that parse as a JSON object (or a list of objects). Non-JSON output passes through unchanged, and any filtering error leaves the original result intact — filters can trim a result, never break it.

!!! note "Managing filters"
    Filter enforcement runs in the open-source core. Creating and managing filters is part of the cost-optimization tooling in **Preloop Cloud** and **Preloop Enterprise**: the Console's session-optimization suggestions open a filter dialog prefilled with a tool's droppable fields, and the billing API exposes CRUD at `/api/v1/billing/cost/output-filters` (`GET` list, `POST` create, `DELETE /{filter_id}`).

---

## Common External Tool Types

### 1. Deployment Tools

**Examples:** `deploy_to_production`, `rollback_deployment`, `scale_service`

**Access rules strategy:**

```yaml
tools:
  - name: deploy_to_production
    source: deploy-server
    approval_workflow: "deploy-approval"
    conditions:
      - expression: 'args.environment == "production"'
        action: require_approval
        description: "Production deploys always need approval"
      - expression: 'args.environment == "staging"'
        action: allow
        description: "Staging deploys are safe"
      - expression: "true"
        action: deny
        description: "Block unknown environments"
```

### 2. Database Tools

**Examples:** `drop_table`, `alter_schema`, `backup_database`

**Access rules strategy:**

```yaml
tools:
  - name: drop_table
    source: db-server
    approval_workflow: "db-critical"
    conditions:
      - expression: 'args.database.startsWith("prod_")'
        action: require_approval
        description: "Production tables need approval"
      - expression: 'args.database.startsWith("test_")'
        action: allow
        description: "Test tables are safe"
      - expression: "true"
        action: deny
        description: "Block everything else"

  - name: backup_database
    source: db-server
    conditions:
      - expression: "true"
        action: allow
        description: "Backups are always safe"
```

### 3. Infrastructure Tools

**Examples:** `modify_firewall`, `scale_cluster`, `rotate_credentials`

**Access rules strategy:**

```yaml
tools:
  - name: modify_firewall
    source: infra-server
    approval_workflow: "security-review"
    conditions:
      - expression: "true"
        action: require_approval
        description: "All firewall changes need review"
```

### 4. Payment Tools

**Examples:** `process_payment`, `issue_refund`, `void_transaction`

**Access rules strategy — tiered by amount with AI + human approval:**

```yaml
approval_workflows:
  - name: "finance-ai"
    approval_type: ai_driven
    ai_model: gpt-5.4
    ai_guidelines: "Approve refunds with valid justification"
    ai_confidence_threshold: 0.9
    ai_fallback_behavior: escalate

  - name: "finance-human"
    timeout_seconds: 600
    approvals_required: 2

tools:
  - name: process_payment
    source: payment-server
    approval_workflow: "finance-human"
    conditions:
      # High-value: 2 human approvers
      - expression: "args.amount > 10000"
        action: require_approval
        description: "High-value payments — human review"
      # Medium-value: AI review
      - expression: "args.amount > 500"
        action: require_approval
        description: "Medium payments — AI review"
      # Small: auto-approve
      - expression: "args.amount <= 500"
        action: allow
        description: "Small payments are safe"
```

### 5. Filesystem Tools

**Examples:** `read_file`, `write_file`, `delete_file`

**Access rules strategy:**

```yaml
tools:
  - name: write_file
    source: fs-server
    approval_workflow: "file-approval"
    conditions:
      # Block credentials
      - expression: 'args.path.endsWith(".env") || args.path.endsWith(".pem")'
        action: deny
        description: "Never modify credential files"
      # System files need approval
      - expression: 'args.path.startsWith("/etc/") || args.path.startsWith("/var/")'
        action: require_approval
        description: "System files need human review"
      # Everything else is fine
      - expression: "true"
        action: allow
        description: "Other files are safe"

  - name: delete_file
    source: fs-server
    approval_workflow: "file-approval"
    conditions:
      - expression: "true"
        action: require_approval
        description: "All deletes need approval"
```

---

## Security Considerations

### 1. Verify Server Trust

Only connect to trusted MCP servers:

- Your own servers
- Official vendor servers
- Community servers with good reputation

**Never connect to:**

- Unknown/suspicious servers
- Servers over HTTP (not HTTPS)
- Servers without authentication

### 2. Use Strong Authentication

```yaml
# Bad - no auth for sensitive tools
Auth Type: none

# Good - require authentication
Auth Type: bearer
Token: <use_secure_secret_manager>
```

### 3. Principle of Least Privilege

Only expose tools that are needed:

```python
# On MCP server side - don't expose dangerous tools
@server.tool()
def read_config():
    """Safe - read-only"""
    return load_config()

# Don't expose unless absolutely needed
# @server.tool()
# def delete_all_data():
#     """Dangerous - don't expose"""
```

### 4. Audit External Tool Usage

Monitor all external tool calls:

- Who called what tool
- With what arguments
- Approved or declined
- Result or error

Enable audit logging in **Settings** → **Audit Logs**.

### 5. Rotate Credentials

```bash
# Rotate MCP server tokens quarterly
1. Generate new token on MCP server
2. Update in Preloop: Tools & MCP → Edit Server → Update Token
3. Test connection
4. Revoke old token
```

---

## Advanced Patterns

### Pattern 1: Multi-Environment Setup

```yaml
# Development — no rules, everything allowed
Name: Deploy (Dev)
URL: https://deploy-dev.acme.com
Status: active

# Staging — AI approval for low friction
Name: Deploy (Staging)
URL: https://deploy-staging.acme.com
Status: active
# Tools use AI-driven approval workflow

# Production — human approval, 2 reviewers
Name: Deploy (Prod)
URL: https://deploy-prod.acme.com
Status: active
# Tools use human approval workflow with approvals_required: 2
```

### Pattern 2: Layered Rules

Combine deny, allow, and require_approval rules on a single tool. CEL string matching is case-sensitive, so match the forms your tools actually emit:

```yaml
tools:
  - name: query_database
    source: db-server
    approval_workflow: "db-review"
    conditions:
      # Always block DROP/DELETE
      - expression: 'args.query.contains("DROP") || args.query.contains("DELETE")'
        action: deny
        description: "Block destructive queries"
      # Approve writes to production
      - expression: 'args.query.startsWith("INSERT") || args.query.startsWith("UPDATE")'
        action: require_approval
        description: "Write queries need approval"
      # Allow reads
      - expression: 'args.query.startsWith("SELECT")'
        action: allow
        description: "Read queries are safe"
```

---
