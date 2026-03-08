# <img alt="Preloop Logo" src="frontend/public/assets/preloop-badge.png" style="height: 21px;" height="18px" /> Preloop: The Policy Engine for AI Agents

[![License](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

Preloop is a comprehensive MCP firewall that gives you complete control over what AI agents can do. Define access policies, approval workflows, and audit trails. Allow, deny, or require approval based on conditions.


  <a href="https://youtu.be/yTtXn8WibTY" target="_blank">
    <img alt="Preloop Logo" src="frontend/public/assets/mcp-firewall.svg" alt="Watch the video" style="width: 100%; max-width: 1135px;" />
  </a>

**Works with OpenClaw, Claude Code, Cursor, Codex, and any MCP-compatible agent.**

## Why Preloop?

AI agents like Claude Code, Cursor, and OpenClaw are transforming how we work. But with great power comes great risk:

- **Accidental deletions.** One wrong command and your production database is gone.
- **Leaked secrets.** API keys pushed to public repos before anyone notices.
- **Runaway costs.** Agents spinning up expensive resources without limits.
- **Breaking changes.** Untested deployments to production at 3am.

Most teams face an impossible choice: give AI full access and move fast (but dangerously), or lock everything down and lose the productivity gains.

**Preloop solves this.** Define policies that allow safe operations, deny dangerous ones, and require human approval for everything in between. You stay in control. AI handles the routine work.

## Core Capabilities

### Access Policies

Define fine-grained access controls for any AI tool or operation:

- Tools support multiple ordered **access rules** (not just simple approval/deny)
- Rules are evaluated in priority order; first matching rule wins
- Each rule has an action (allow/deny/require_approval), optional CEL condition, and optional denial message
- Rules can be reordered via drag-and-drop in the UI

### Approval Workflows

When AI attempts a protected operation, Preloop pauses and notifies you:

- **Instant notifications** via mobile app, email, Slack, or Mattermost
- **One-tap approvals** from your phone, watch, or desktop
- **Async approval mode** — tool returns immediately with a polling handle; the agent polls `get_approval_status` until approved, then receives the tool result (Enterprise)
- **Per-tool justification** — require or optionally request agents to explain *why* a tool is being called before approval (Enterprise)
- **Team-based approvals** with quorum requirements (Enterprise)
- **Escalation policies** for time-sensitive operations (Enterprise)

### Policy-as-Code

Define policies in YAML, manage via CLI or API:

```yaml
# Example: Require approval for production deployments
version: "1.0"
metadata:
  name: "Production Safeguards"
  description: "Require approval before deploying to production"
  tags: [security, production]

approval_workflows:
  - name: "deploy-approval"
    timeout_seconds: 600
    required_approvals: 1
    async_approval: true          # Agent polls instead of blocking

tools:
  - name: "bash"
    source: mcp
    approval_workflow: "deploy-approval"
    justification: required        # Agent must explain the call
    conditions:
      - expression: "args.command.contains('deploy') && args.command.contains('production')"
        action: require_approval
        description: "Production deployments require approval"
```

- **Version control** your policies alongside your code
- **GitOps workflows** for policy changes
- **CLI management** for automation and scripting
- **API access** for programmatic policy management

### Complete Audit Trail

Every AI action is logged with full context:

- What was attempted (tool, parameters, context)
- Which policy matched and why
- Who approved or rejected (and when)
- Execution result and duration

Essential for security reviews, compliance, and debugging.

## Comparison with AWS Agent Core

| Feature | Preloop | AWS Agent Core |
|---------|:-------:|:--------------:|
| Open source | ✅ | ❌ |
| Self-hosted option | ✅ | ❌ |
| Policy-as-code (YAML) | ✅ | Limited |
| MCP native | ✅ | ❌ |
| Works with any agent | ✅ | AWS-focused |
| Human approval workflows | ✅ | ✅ |
| Audit trail | ✅ | ✅ |
| CLI management | ✅ | AWS CLI |
| GitOps-friendly | ✅ | Limited |
| Mobile app approvals | ✅ | ❌ |
| Team-based approvals | ✅ (Enterprise) | ✅ |

**Preloop is the open-source alternative to AWS Agent Core** for teams who want vendor-neutral, self-hosted AI governance.

```
AI Agent -> Preloop -> [Policy check] -> Allow / Deny / Require Approval -> Execute
```

**How it works:**
1. Define policies for each tool: allow, deny, or require approval
2. Policies can be fine-grained, checking parameter values and context
3. AI agents call tools through Preloop's MCP proxy
4. Actions are allowed, denied, or paused for approval based on your policies
5. Full audit trail of every action and decision

## Key Features

### Safety & Control

- **Policy Engine.** Define allow, deny, and approval workflows for any tool or action.
- **Access Rules.** Multiple ordered rules per tool with allow/deny/require approval actions.
- **Drag-and-Drop Priority.** Reorder rule evaluation priority visually.
- **Fine-Grained Rules.** Policies can check tool names, parameter values, and context.
- **Instant Notifications.** Get alerts on mobile, email, Slack, or Mattermost.
- **One-Tap Approvals.** Approve or reject from your phone, watch, or desktop.
- **Full Audit Trail.** Complete log of every AI action and policy decision.
- **Async Approval Mode (Enterprise).** Non-blocking approval: tool returns immediately, agent polls `get_approval_status` until the human decides.
- **Per-Tool Justification (Enterprise).** Require agents to provide a reason for each tool call. Mode: `required` (blocks without it) or `optional` (agent may provide one).
- **Flexible Conditions.** Use CEL expressions for context-aware rules (Enterprise).
- **AI Approval (Enterprise).** AI-driven approval with configurable model, prompt, confidence threshold, and fallback behavior.
- **Team Approvals.** Require quorum from multiple team members for critical ops (Enterprise).

### Integration & Compatibility

- **MCP Proxy.** Works with any Model Context Protocol-compatible AI agent.
- **Zero Infrastructure Changes.** Drop-in solution, no code modifications needed.
- **Built-in Tools.** 11 tools for issue and PR/MR management included.
- **External MCP Servers.** Proxy any external MCP server through Preloop's safety layer.
- **Issue Tracker Sync.** Connect Jira, GitHub, GitLab for full context.

### Automation Platform

- **Agentic Flows.** Build event-driven workflows triggered by webhooks, schedules, or tracker events.
- **Vector Search.** Intelligent similarity search using embeddings.
- **Duplicate Detection.** Automatically identify overlapping issues.
- **Compliance Metrics.** Evaluate and improve issue quality.
- **Web UI.** Modern interface built with Lit, Vite, and Shoelace.

> **Looking for Enterprise features?** Preloop Enterprise Edition adds RBAC, team-based approvals, advanced audit logging, and more. See [Enterprise Features](#enterprise-features) below.

### Open Source vs Enterprise (important)

- **Open Source**: single-user approvals with **email, mobile app, Slack, and Mattermost notifications**.
- **Enterprise**: adds **advanced conditions (CEL)**, **team-based approvals (quorum)**, and **escalation**.
- **Mobile & Watch apps**: the iOS/Watch and Android apps can be used with **self-hosted / open-source** Preloop deployments.

## Supported Issue Trackers

- Jira Cloud and Server
- GitHub Issues
- GitLab Issues
- (More to be added in future releases, including Azure DevOps and Linear)

## Architecture

Preloop is designed with a modular architecture:

1.  **Preloop** (`./backend/preloop`): The main RESTful HTTP API server that provides access to issue tracking systems and vector search capabilities.
2.  **Preloop Models** (`./backend/preloop/models`): Contains the database models (using SQLAlchemy and Pydantic) and CRUD operations for interacting with the PostgreSQL database, including vector embeddings via PGVector.
3.  **Preloop Sync** (`./backend/preloop/sync`): A service responsible for polling configured issue trackers, indexing issues, projects, and organizations in the database, and updating issue embeddings.
4.  **Preloop Console** (`./frontend`): A web application built using Lit, Vite, TypeScript, and Shoelace Web Components.

This structure allows:
- Clear separation of concerns between the API layer, data models, and synchronization logic.
- Independent development and versioning of the core components.

## Preloop Console

The Preloop Console is in the `frontend` directory. It is built using modern web technologies to provide a fast, responsive, and feature-rich user experience.

- **Technology Stack**: Lit, Vite, TypeScript, and Shoelace Web Components.

## Installation

### Prerequisites

- Python 3.11+
- PostgreSQL 14+
- PGVector extension for PostgreSQL (for vector search capabilities)

### Local Setup

```bash
# Clone the repository
git clone https://github.com/preloop/preloop.git
cd preloop

# Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -e ".[dev]"

# Set up the database

# Configure your environment
cp .env.example .env
# Edit .env with your settings
```

## Configuration

### Environment Variables

Preloop is configured via environment variables. Copy `.env.example` to `.env` and customize as needed.

#### Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+psycopg://postgres:postgres@localhost/preloop` | PostgreSQL connection string |
| `SECRET_KEY` | (required) | Secret key for JWT tokens |
| `ENVIRONMENT` | `development` | Environment (development, production) |
| `LOG_LEVEL` | `INFO` | Log level (DEBUG, INFO, WARNING, ERROR) |

#### Feature Flags

| Variable | Default | Description |
|----------|---------|-------------|
| `REGISTRATION_ENABLED` | `true` | Enable self-registration. Set to `false` to disable public signups and require admin invitation. |

#### Disabling Self-Registration

For private deployments where you want to control who can access the system:

```bash
# In your .env file or Docker environment
REGISTRATION_ENABLED=false
```

When registration is disabled:
- The "Sign Up" button is hidden from the UI
- The `/register` page redirects to `/login`
- **The `/api/v1/auth/register` API endpoint returns 403 Forbidden** - preventing direct API registration attempts
- New users must be invited by an administrator

**Security Note**: With `REGISTRATION_ENABLED=false`, the backend API enforces the restriction at the endpoint level. Any attempt to register via the API (including scripts or direct HTTP requests) will be rejected with a 403 status code.

To invite users when registration is disabled, use the admin API or CLI (Enterprise Edition includes a full admin dashboard for user management).

#### GitHub App (Optional)

For enhanced GitHub integration including PR status checks and bot reactions:

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_APP_ID` | | GitHub App ID (from app settings page) |
| `GITHUB_APP_SLUG` | | GitHub App slug (the URL-friendly name) |
| `GITHUB_APP_PRIVATE_KEY` | | Base64-encoded private key from GitHub App |
| `GITHUB_APP_CLIENT_ID` | | OAuth client ID for user authentication |
| `GITHUB_APP_CLIENT_SECRET` | | OAuth client secret |
| `GITHUB_APP_WEBHOOK_SECRET` | | Secret for verifying webhook payloads |

These are optional and only needed if you're using a GitHub App for authentication or advanced features like reaction management on PRs.

#### OAuth Sign-In (Enterprise)

Enable OAuth sign-in/sign-up via GitHub, Google, and/or GitLab. Users can authenticate with their existing provider accounts instead of creating a Preloop-specific password.

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_OAUTH_CLIENT_ID` | | Google OAuth 2.0 client ID |
| `GOOGLE_OAUTH_CLIENT_SECRET` | | Google OAuth 2.0 client secret |
| `GITLAB_OAUTH_CLIENT_ID` | | GitLab OAuth client ID |
| `GITLAB_OAUTH_CLIENT_SECRET` | | GitLab OAuth client secret |
| `GITLAB_OAUTH_BASE_URL` | `https://gitlab.com` | GitLab instance URL (for self-hosted) |

GitHub OAuth sign-in reuses the GitHub App credentials above. Enable via Helm values:

```yaml
mcpOauth:
  enabled: true
googleOauth:
  enabled: true
  clientId: "your-google-client-id"
  clientSecret: "your-google-client-secret"
gitlabOauth:
  enabled: true
  clientId: "your-gitlab-client-id"
  clientSecret: "your-gitlab-client-secret"
```

**Supported flows:**
- **GitHub**: Sign-in + automatic tracker setup prompt
- **Google**: Sign-in only (no tracker created)
- **GitLab**: Sign-in + automatic tracker setup prompt

#### MCP OAuth 2.1 Server

Preloop includes a built-in OAuth 2.1 Authorization Server for MCP client authentication (e.g., Claude Desktop). This is enabled automatically when `mcpOauth.enabled=true`.

| Variable | Default | Description |
|----------|---------|-------------|
| `PRELOOP_URL` | `http://localhost:8000` | Public URL of your Preloop instance (used for OAuth discovery endpoints) |

**Discovery endpoints:**
- `GET /.well-known/oauth-authorization-server` — RFC 8414 metadata
- `GET /.well-known/oauth-protected-resource` — RFC 9728 metadata

**OAuth endpoints:**
- `POST /oauth/register` — Dynamic Client Registration (RFC 7591)
- `GET /oauth/authorize` — Authorization endpoint (redirects to consent page)
- `POST /oauth/token` — Token exchange (Authorization Code + PKCE for MCP, JWT for CLI)
- `POST /oauth/revoke` — Token revocation

### Docker Setup

```bash
# Clone the repository
git clone https://github.com/preloop/preloop.git
cd preloop

# Run the full development stack (backend + workers + frontend with HMR)
docker compose up

# Run with tagged release images (production)
PRELOOP_VERSION=0.8.0-beta.1 SECRET_KEY=$(openssl rand -hex 32) \
  docker compose -f docker-compose.release.yaml up -d
```

Quick installers are also available:

```bash
# Install the standalone CLI
curl -fsSL https://preloop.ai/install/cli | sh

# Install the OSS stack
curl -fsSL https://preloop.ai/install/oss | sh
```

Set `PRELOOP_VERSION=0.8.0-beta.1` before either command to pin a specific release, or use `https://preloop.ai/install/<script>?version=0.8.0-beta.1`.

The default `docker compose up` command uses `docker-compose.override.yml` for local development, so source changes in `backend/` and `frontend/` are mounted directly into the containers. The frontend runs via Vite on `http://localhost:5173`, while the backend API stays on `http://localhost:8000`.

See [`docker-compose.release.yaml`](docker-compose.release.yaml) for full configuration and required environment variables.

#### Release Management

Use the release script to prepare a new version across the main release surfaces:

```bash
./scripts/release.sh 0.8.0-beta.1
```

The script can also optionally commit the release prep, create and push `v<version>`, and watch the GitHub `Release` workflow with `gh`.

See [`RELEASING.md`](RELEASING.md) for the full checklist and [`scripts/release.sh`](scripts/release.sh) for the release prep helper.

### Kubernetes Setup

Preloop can be deployed to Kubernetes using the provided Helm chart:

```bash
# Add the Spacecode Helm repository (if available)
# helm repo add spacecode https://charts.spacecode.ai
# helm repo update

# Install from the local chart
helm install preloop ./helm/preloop

# Or install the packaged chart from a GitHub release
# helm install preloop https://github.com/preloop/preloop/releases/download/v0.8.0/preloop-0.8.0.tgz

# Or install with custom values
helm install preloop ./helm/preloop --values custom-values.yaml
```

For more details about the Helm chart, see the [chart README](./helm/preloop/README.md).

## Usage

### Starting the Server

1.  **Set Environment Variables:**
    Ensure you have a `.env` file configured with the necessary environment variables (see `.env.example`). Key variables include database connection details, API keys, etc.

2.  **Start Preloop API:**
    Use the provided script to start the main API server:
    ```bash
    ./start.sh
    ```
    This script typically handles activating the virtual environment and running the server (e.g., `python -m preloop.server`).

3.  **Start Preloop Sync Service:**
    In a separate terminal, start the synchronization service to begin indexing data from your configured trackers:
    ```bash
    # Activate the virtual environment if not already active
    # source .venv/bin/activate
    preloop-sync scan all
    ```
    This command tells Preloop Sync to scan all configured trackers and update the database.

### API Documentation

When running, the API documentation is available at:

```
http://localhost:8000/docs
```

The OpenAPI specification is also available at:

```
http://localhost:8000/openapi.json
```

### Using the REST API

Preloop provides a RESTful HTTP API:

```python
import requests
import json

# Base URL for the Preloop API
base_url = "http://localhost:8000/api/v1"

# Authenticate and get a token
auth_response = requests.post(
    f"{base_url}/auth/token",
    json={"username": "your-username", "password": "your-password"}
)
token = auth_response.json()["access_token"]
headers = {"Authorization": f"Bearer {token}"}

# Test a tracker connection
connection = requests.post(
    f"{base_url}/projects/test-connection",
    headers=headers,
    json={
        "organization": "spacecode",
        "project": "astrobot"
    }
)
print(json.dumps(connection.json(), indent=2))

# Search for issues related to authentication
results = requests.get(
    f"{base_url}/issues/search",
    headers=headers,
    params={
        "organization": "spacecode",
        "project": "astrobot",
        "query": "authentication problems",
        "limit": 5
    }
)
print(json.dumps(results.json(), indent=2))

# Create a new issue
issue = requests.post(
    f"{base_url}/issues",
    headers=headers,
    json={
        "organization": "spacecode",
        "project": "astrobot",
        "title": "Improve login error messages",
        "description": "Current error messages are not clear enough...",
        "labels": ["enhancement", "authentication"],
        "priority": "High"
    }
)
print(json.dumps(issue.json(), indent=2))
```

## API Endpoints

Preloop provides a RESTful API with the following key endpoints:

### Authentication
- `POST /api/v1/auth/token` - Get authentication token
- `POST /api/v1/auth/refresh` - Refresh authentication token

### MCP Server Management
- `GET /api/v1/mcp-servers` - List configured MCP servers
- `POST /api/v1/mcp-servers` - Add new MCP server
- `PUT /api/v1/mcp-servers/{id}` - Update MCP server configuration
- `DELETE /api/v1/mcp-servers/{id}` - Remove MCP server
- `POST /api/v1/mcp-servers/{id}/scan` - Trigger tool discovery scan
- `GET /api/v1/mcp-servers/{id}/tools` - List tools available on server

### Tool Configuration
- `GET /api/v1/tool-configurations` - List tool configurations
- `POST /api/v1/tool-configurations` - Create tool configuration
- `PUT /api/v1/tool-configurations/{id}` - Update tool configuration
- `DELETE /api/v1/tool-configurations/{id}` - Delete tool configuration

### Access Rules
- `POST /api/v1/tool-configurations/{config_id}/access-rules` - Create access rule
- `PUT /api/v1/access-rules/{rule_id}` - Update access rule
- `DELETE /api/v1/access-rules/{rule_id}` - Delete access rule

### Approval Management
- `GET /api/v1/approval-workflows` - List approval workflows
- `POST /api/v1/approval-workflows` - Create approval workflow
- `PUT /api/v1/approval-workflows/{id}` - Update approval workflow
- `DELETE /api/v1/approval-workflows/{id}` - Delete approval workflow
- `GET /api/v1/approval-requests` - List approval requests (authenticated)
- `GET /api/v1/approval-requests/{request_id}` - Get approval request details (authenticated)
- `POST /api/v1/approval-requests/{request_id}/approve` - Approve request (authenticated)
- `POST /api/v1/approval-requests/{request_id}/decline` - Decline request (authenticated)
- `POST /api/v1/approval-requests/{request_id}/decide` - Approve or decline request (authenticated)
- `GET /approval/{request_id}/data?token={token}` - Get approval request details (public, token-based)
- `POST /approval/{request_id}/decide?token={token}` - Approve or decline approval request (public, token-based)

### Flows
- `GET /api/v1/flows` - List flows
- `POST /api/v1/flows` - Create flow
- `GET /api/v1/flows/{id}` - Get flow details
- `PUT /api/v1/flows/{id}` - Update flow
- `DELETE /api/v1/flows/{id}` - Delete flow
- `POST /api/v1/flows/{id}/trigger` - Trigger a test execution for a flow
- `GET /api/v1/flows/{id}/executions` - List flow executions
- `GET /api/v1/flows/executions/{id}` - Get execution details
- `GET /api/v1/flows/executions/{id}/logs` - Get execution logs (from container or database)
- `GET /api/v1/flows/executions/{id}/metrics` - Get execution metrics (tool calls, tokens, cost)
- `POST /api/v1/flows/executions/{id}/command` - Send command to execution (e.g., stop)
- `POST /api/v1/flows/executions/{id}/retry` - Retry a failed/stopped/cancelled execution

### Policy Generation
- `POST /api/v1/policies/generate` - Generate policy YAML from a natural-language prompt
- `POST /api/v1/policies/generate-from-audit` - Generate policy YAML from audit-log tool-call patterns

**Prerequisites:** At least one AI model must be configured in Settings → AI Models.

**Generate from Prompt:**

```bash
curl -X POST "https://YOUR_PRELOOP_URL/api/v1/policies/generate" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
  "prompt": "require approval for any bash command that modifies production",
  "include_current_config": true
}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | Yes | Natural-language description of the desired policy |
| `include_current_config` | boolean | No | Include current account config as LLM context (default: `true`) |

**Generate from Audit Logs:**

```bash
curl -X POST "https://YOUR_PRELOOP_URL/api/v1/policies/generate-from-audit" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
  "start_date": "2026-01-01",
  "end_date": "2026-02-01"
}'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `start_date` | string | No | ISO date to filter audit logs from (e.g. `2026-01-01`) |
| `end_date` | string | No | ISO date to filter audit logs until |
| `audit_logs_json` | string | No | Raw JSON array of external audit logs (bypasses DB lookup) |

**Response (both endpoints):**

```json
{
  "yaml": "version: \"1.0\"\nmetadata:\n  name: ...",
  "warnings": ["Optional validation warnings"]
}
```

## Trackers
- `GET /api/v1/trackers` - List trackers
- `GET /api/v1/trackers/{tracker_id}` - Get tracker details
- `POST /api/v1/trackers` - Create tracker
- `PUT /api/v1/trackers/{tracker_id}` - Update tracker
- `DELETE /api/v1/trackers/{tracker_id}` - Delete tracker

### Organizations
- `GET /api/v1/organizations` - List organizations
- `GET /api/v1/organizations/{org_id}` - Get organization details
- `POST /api/v1/organizations` - Create organization
- `PUT /api/v1/organizations/{org_id}` - Update organization
- `DELETE /api/v1/organizations/{org_id}` - Delete organization

### Projects
- `GET /api/v1/organizations/{org_id}/projects` - List projects
- `GET /api/v1/projects/{project_id}` - Get project details
- `POST /api/v1/projects` - Create project
- `PUT /api/v1/projects/{project_id}` - Update project
- `DELETE /api/v1/projects/{project_id}` - Delete project
- `POST /api/v1/projects/test-connection` - Test project connection

### Issues
- `GET /api/v1/issues/search` - Search issues
- `POST /api/v1/issues` - Create issue
- `GET /api/v1/issues/{issue_id}` - Get issue details
- `PUT /api/v1/issues/{issue_id}` - Update issue
- `DELETE /api/v1/issues/{issue_id}` - Delete issue
- `POST /api/v1/issues/{issue_id}/comments` - Add comment to issue

### Unified WebSocket

Preloop uses a unified WebSocket connection for real-time updates across the application:

**Connection:** `ws://localhost:8000/api/v1/ws/unified`

**Message Routing:**
- Flow execution updates (`flow_executions` topic)
- Approval request notifications (`approvals` topic)
- System activity updates (`activity` topic)
- Session events (`system` topic)

**Features:**
- Automatic reconnection with exponential backoff
- Pub/sub message routing to subscribers
- Topic-based filtering for efficient message delivery
- Session management with activity tracking
- Heartbeat monitoring

**Usage in Frontend:**
```typescript
import { unifiedWebSocketManager } from './services/unified-websocket-manager';

// Subscribe to flow execution updates
const unsubscribe = unifiedWebSocketManager.subscribe(
  'flow_executions',
  (message) => console.log('Flow update:', message),
  (message) => message.execution_id === myExecutionId  // Optional filter
);

// Clean up when done
unsubscribe();
```

### Using MCP Tools via API

The Preloop API now includes integrated MCP tool endpoints with dynamic tool filtering, allowing any HTTP-based MCP client to connect directly. This is the recommended way to automate issue management workflows.

**Authentication:** All MCP endpoints use the same Bearer Token authentication as the rest of the API.

**Dynamic Tool Visibility:** MCP tools are only visible when your account has one or more trackers configured. This ensures tools have the necessary context to operate effectively. If you connect with an account that has no trackers, you will see an empty tool list.

**Connecting with Claude Code:**

You can connect Claude Code directly to your Preloop instance using the `claude mcp add` command.

1.  **Get your Preloop API Key:** You can find or create an API key in your Preloop user settings.
2.  **Add the MCP Server:** Run the following command, replacing `YOUR_PRELOOP_URL` and `YOUR_API_KEY` with your details.

    ```bash
    claude mcp add \
      --transport http \
      --header "Authorization: Bearer YOUR_API_KEY" \
      preloop \
      https://YOUR_PRELOOP_URL/mcp/v1
    ```

    - `--transport http`: Specifies that the server uses the HTTP transport.
    - `--header "Authorization: Bearer YOUR_API_KEY"`: Provides the necessary authentication header for all requests.
    - `preloop`: This is the name you will use to refer to the server (e.g., `@preloop get_issue ...`).
    - `https://YOUR_PRELOOP_URL/mcp/v1`: This is the base URL for the Preloop MCP endpoints.

**Example Workflow (using `curl`):**

If you are not using an MCP client and want to interact with the tool endpoints directly, you can use any HTTP client like `curl`.

1.  **Create an Issue:**
    ```bash
    curl -X POST "https://YOUR_PRELOOP_URL/api/v1/mcp/create_issue" \
    -H "Authorization: Bearer YOUR_API_KEY" \
    -H "Content-Type: application/json" \
    -d '{
      "project": "your-org/your-project",
      "title": "New Feature Request",
      "description": "Add a dark mode to the dashboard."
    }'
    ```

### Tool Approval Workflows

Preloop provides approval workflows for tool execution. Control which operations require approval before execution.

**Key Concepts:**
- **Tool Configuration**: Enable/disable tools and assign approval workflows
- **Approval Workflows**: Define approval requirements, approvers, timeouts, and notification channels
- **Email Notifications**: Receive approval requests via email with one-click approve/decline

**Example: Create an Approval Workflow**

```bash
curl -X POST "https://YOUR_PRELOOP_URL/api/v1/approval-workflows" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
  "name": "Critical Operations",
  "description": "Require approval for critical issue operations",
  "is_default": false,
  "approver_user_ids": ["user-id-1", "user-id-2"],
  "approvals_required": 1,
  "timeout_seconds": 600,
  "notification_channels": ["email"]
}'
```

**Configure a tool to require approval:**

```bash
curl -X POST "https://YOUR_PRELOOP_URL/api/v1/tool-configurations" \
-H "Authorization: Bearer YOUR_API_KEY" \
-H "Content-Type: application/json" \
-d '{
  "tool_name": "update_issue",
  "tool_source": "preloop_builtin",
  "is_enabled": true,
  "approval_workflow_id": "<workflow_id_from_above>"
}'
```

> **Enterprise Features**: Preloop Enterprise Edition adds CEL-based conditional approvals, team-based approvals with quorum, and escalation policies. Contact sales@preloop.ai for more information.

### Configuring Timeouts for Approval Workflows

When using approval workflows, tool calls may take several minutes while waiting for human approval. Most MCP clients have default timeouts that are too short for approval workflows. Configure your client's timeout accordingly:

**Claude Code** (`~/.claude.json` or project `.mcp.json`):
```json
{
  "mcpServers": {
    "preloop": {
      "url": "https://YOUR_PRELOOP_URL/mcp/v1",
      "timeout": 600000
    }
  }
}
```

**Cursor / VS Code** (`.cursor/mcp.json` or VS Code settings):
```json
{
  "mcpServers": {
    "preloop": {
      "url": "https://YOUR_PRELOOP_URL/mcp/v1",
      "timeout": 600000
    }
  }
}
```

**Claude Desktop** (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "preloop": {
      "url": "https://YOUR_PRELOOP_URL/mcp/v1",
      "timeout": 600000
    }
  }
}
```

The timeout value is in milliseconds. 600000ms = 10 minutes, which should be sufficient for most approval workflows. Adjust based on your approval workflow's `timeout_seconds` setting.

> **Tip**: If your approval workflow has **async mode** enabled (`async_approval: true`), the tool returns immediately with a `pending_approval` status and a `request_id`. The agent automatically polls `get_approval_status(request_id)` until the human approves, at which point the tool executes and the result is returned in the poll response. No client timeout increase is needed in this mode.

### Mobile Push Notifications (iOS/Android)

Open-source users can enable mobile push notifications by proxying requests through the production Preloop server at https://preloop.ai.

**Setup Steps:**

1. **Create an account** at https://preloop.ai
2. **Generate an API key** with `push_proxy` scope from the Settings page
3. **Configure your instance** with these environment variables:

```bash
# Push notification proxy configuration
PUSH_PROXY_URL=https://preloop.ai/api/v1/push/proxy
PUSH_PROXY_API_KEY=your-api-key-here
```

4. **Enable push notifications** in the Notification Preferences page in your Preloop Console
5. **Register your mobile device** by scanning the QR code shown in Notification Preferences

Once configured, approval requests will trigger push notifications on your registered iOS or Android devices.

> **Note**: The mobile apps (iOS/Watch and Android) are designed to work with self-hosted Preloop instances. They connect to your server URL extracted from the QR code.

### Version Checking & Updates

By default, Preloop checks for version updates by contacting https://preloop.ai on startup and once daily. This helps you stay informed about new releases and security updates.

**Privacy**: Only instance UUID, version number, and IP address are sent. No user data is transmitted.

**Opt-out**: Set `PRELOOP_DISABLE_TELEMETRY=true` or `DISABLE_VERSION_CHECK=true` to disable version checking and telemetry entirely.

For detailed architecture, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Testing

Preloop uses pytest for unit and integration testing. The test suite covers API endpoints, database models, and tracker integrations.

### Running Tests

To run all tests:

```bash
# Run all tests
pytest

# Run with verbose output
pytest -v

# Run a specific test file
pytest tests/endpoints/test_webhooks.py

# Run a specific test case
pytest tests/endpoints/test_webhooks.py::TestWebhooksEndpoint::test_github_webhook_valid_signature
```

### Test Structure

- **Unit Tests**: Located in `tests/` directory, testing individual components in isolation
- **Integration Tests**: Test the interaction between components
- **Endpoint Tests**: Test API endpoints with mocked database sessions

### Testing Webhooks

The webhook endpoint tests (`tests/endpoints/test_webhooks.py`) validate:

1. Authentication via signatures/tokens for GitHub and GitLab webhooks
2. Error handling for invalid signatures, missing tokens, etc.
3. Organization identifier resolution
4. Database updates (last_webhook_update timestamp)
5. Error handling for database failures

These tests use mocking to isolate the webhook handling logic from external dependencies.

## Roadmap

Preloop is evolving into a comprehensive control plane for AI agents. Here's what's coming:

- 🔜 **Agent Registry** — Register, credential, and manage AI agents as first-class entities
- 🔜 **AI Model Gateway** — Unified model proxy with cost tracking, rate limits, and usage analytics
- 🔜 **Agent Monitoring** — Real-time visibility into agent activity, spending, and health
- 🔜 **Budget Controls** — Per-agent spending caps with alerts and enforcement

Star the repo and watch for updates!

## Enterprise Features

Preloop Enterprise Edition extends the open-source core with additional features for teams and organizations:

| Feature | Open Source | Enterprise |
|---------|:-----------:|:----------:|
| MCP Server with 11 built-in tools | ✅ | ✅ |
| Basic approval workflows | ✅ | ✅ |
| Email notifications | ✅ | ✅ |
| Mobile app notifications (iOS/Watch; Android) | ✅ | ✅ |
| Issue tracker integration | ✅ | ✅ |
| Vector search & duplicate detection | ✅ | ✅ |
| Agentic flows | ✅ | ✅ |
| Web UI | ✅ | ✅ |
| **Role-Based Access Control (RBAC)** | ❌ | ✅ |
| **Team management** | ❌ | ✅ |
| **CEL conditional approval workflows** | ❌ | ✅ |
| **Access rules with CEL conditions** | Basic (single condition) | Advanced (multiple conditions, AND/OR, CEL editor) |
| **AI-driven approval workflows** | ❌ | ✅ |
| **Team-based approvals with quorum** | ❌ | ✅ |
| **Async approval mode** | ❌ | ✅ |
| **Per-tool justification settings** | ❌ | ✅ |
| **Approval escalation** | ❌ | ✅ |
| Slack notifications | ✅ | ✅ |
| Mattermost notifications | ✅ | ✅ |
| **Admin dashboard** | ❌ | ✅ |
| **Audit logging & impersonation tracking** | ❌ | ✅ |
| **Billing & subscription management** | ❌ | ✅ |
| **Priority support** | ❌ | ✅ |

Contact sales@preloop.ai for Enterprise Edition licensing.

## Contributing

Contributions are welcome! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details on how to get started.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

Preloop is open source software licensed under the [Apache License 2.0](LICENSE).

Copyright (c) 2026 Spacecode AI Inc.
