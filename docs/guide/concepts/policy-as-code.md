# Policy-as-Code

Define and manage your AI agent governance policies declaratively using YAML files. Version-control your policies alongside your code.

---

## Overview

Policy-as-code lets you:

- **Define policies in YAML**: MCP servers, approval workflows, tool configurations, and defaults
- **Version and diff**: Track changes, compare against live config, rollback to previous versions
- **Validate before applying**: Catch errors without affecting your running system
- **Import/export**: Move policies between environments (dev → staging → production)

---

## Policy Schema

Every policy document follows this structure:

```yaml
version: "1.0"
metadata:
  name: my-policy
  description: Policy for production safety
  author: team@company.com
  tags: [production, safety]

mcp_servers:
  - name: github
    url: https://api.github.com/mcp
    transport: streamable-http
    auth_type: bearer
    auth_config:
      token: ${GITHUB_TOKEN}

approval_workflows:
  - name: sre-review
    timeout_seconds: 600
    approvals_required: 1
    approver_teams: [sre-team]

tools:
  - name: bash
    source: builtin
    enabled: true
    approval_workflow: sre-review
    conditions:
      - expression: "args.command.contains('rm')"
        action: deny
        condition_type: cel
        description: Block destructive commands

defaults:
  unknown_tools: require_approval
  require_approval_for_new_tools: true
  default_approval_workflow: sre-review
```

---

## Schema Reference

### `version` (required)

Schema version. Currently only `"1.0"` is supported.

### `metadata` (required)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | ✅ | Unique policy name |
| `description` | string | | Policy description |
| `author` | string | | Author name or email |
| `tags` | list | | Tags for categorization |

### `mcp_servers`

Define external MCP servers that tools can reference.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | n/a | Unique server name (referenced by tools) |
| `url` | string | n/a | Server endpoint URL |
| `transport` | string | `streamable-http` | `streamable-http`, `http-streaming`, `stdio`, `sse` |
| `auth_type` | string | `none` | `none`, `bearer`, `api_key`, `oauth` |
| `auth_config` | object | n/a | Auth details. Use `${ENV_VAR}` for secrets |

### `approval_workflows`

Define how approvals are handled. See [AI-Driven Approvals](../approvals/ai-approvals.md) for AI-specific options.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | n/a | Unique workflow name |
| `timeout_seconds` | int | `300` | Approval timeout in seconds (30–86400) |
| `approvals_required` | int | `1` | Number of approvals needed (quorum, 1–10) |
| `approver_users` | list | n/a | Usernames who can approve |
| `approver_teams` | list | n/a | Team names whose members can approve |
| `escalation_users` | list | n/a | Usernames to escalate to on timeout |
| `escalation_teams` | list | n/a | Team names to escalate to on timeout |
| `channel_configs` | object | n/a | Per-channel notification configuration |
| `approval_type` | string | `standard` | `standard` (human) or `ai_driven` |
| `ai_model` | string | n/a | AI model for evaluation (required if `ai_driven`) |
| `ai_guidelines` | string | n/a | Guidelines for the AI decision (optional) |
| `ai_confidence_threshold` | float | `0.8` | Minimum confidence for the AI to auto-decide (0.0–1.0) |
| `ai_fallback_behavior` | string | `escalate` | When AI is uncertain: `escalate`, `approve`, or `deny` |
| `async_approval` | bool | `false` | Tool calls return immediately; agents poll `get_approval_status` |

### `tools`

Configure individual tool behaviors.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | n/a | Tool name (must match actual tool) |
| `source` | string | `builtin` | `builtin`, `mcp`, `http`, or a server name |
| `enabled` | bool | `true` | Whether the tool is active |
| `approval_workflow` | string | n/a | Name of approval workflow to use |
| `conditions` | list | n/a | List of conditional rules |
| `description` | string | n/a | Override tool description |

#### Tool Conditions

Each condition evaluates against tool arguments:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `expression` | string | n/a | Expression to evaluate |
| `action` | string | `require_approval` | `allow`, `deny`, or `require_approval` |
| `condition_type` | string | `simple` | `simple` (OSS) or `cel` (Enterprise) |
| `description` | string | n/a | Human-readable description |

### `defaults`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `unknown_tools` | string | `allow` | `allow`, `deny`, or `require_approval` |
| `require_approval_for_new_tools` | bool | `false` | Require approval for newly discovered tools |
| `default_approval_workflow` | string | n/a | Fallback approval workflow |
| `inherit_from_parent` | bool | `true` | Inherit from parent policy |

---

## CLI Commands

```bash
# Validate a policy file (no changes applied)
preloop policy validate my-policy.yaml

# Preview what would change
preloop policy diff my-policy.yaml

# Apply a policy
preloop policy apply my-policy.yaml

# Dry-run (preview without applying)
preloop policy apply my-policy.yaml --dry-run

# Recursively apply all policies in a directory
preloop policy apply policies/ -r

# Export current config as YAML (stdout, or -o file)
preloop policy export -o my-policy.yaml

# List all policies
preloop policy list

# Generate a policy with AI (see Policy Generation)
preloop policy generate "require approval for payments over $500"
```

See [Policy Generation](../tools/policy-generation.md) for `preloop policy generate` and `preloop agents starter-policy`.

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/api/v1/policies/validate` | Validate policy syntax |
| `POST` | `/api/v1/policies/diff` | Compare against current state |
| `POST` | `/api/v1/policies/upload` | Upload and apply a policy file |
| `GET` | `/api/v1/policies/export` | Export current config as YAML |
| `GET` | `/api/v1/policies/schema` | JSON schema for policy files, with documentation |

---

## Versioning & Rollback

Preloop maintains a history of policy changes, allowing you to rollback to any previous version.

```bash
# List policy versions
GET /api/v1/policies/versions

# Create a snapshot
POST /api/v1/policies/versions

# Tag a version
PUT /api/v1/policies/versions/{id}/tag
# Body: { "tag": "production-v1" }

# Rollback to a version (with diff preview)
POST /api/v1/policies/versions/{id}/rollback

# Delete old versions
DELETE /api/v1/policies/versions/{id}
```

!!! warning "Credential Safety"
    Rollbacks preserve MCP server credentials, only configuration changes are reverted.

---

## Validation Rules

The schema validates:

1. **No duplicate names**: MCP server and approval workflow names must be unique
2. **Valid references**: Tools referencing approval workflows or MCP servers must point to defined names
3. **AI policy completeness**: AI-driven workflows require `ai_model` (`ai_guidelines` is optional)
4. **Expression syntax**: Tool condition expressions cannot be empty
5. **Default references**: `default_approval_workflow` must reference a defined policy

If validation fails, you get detailed error messages pointing to the exact issue:

```json
{
  "is_valid": false,
  "errors": [
    {
      "path": "tools[0].approval_workflow",
      "message": "Tool 'bash' references unknown approval workflow 'nonexistent'"
    }
  ]
}
```
