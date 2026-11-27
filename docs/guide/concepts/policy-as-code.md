# Policy-as-Code

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

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
  author: team@example.com
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
```

---

`preloop policy apply` resolves `${NAME}` references in string values from
the operator's local environment before upload, including with `--dry-run`.
Unset variables stop the command and are named in the error. Empty variables
are allowed. Use `--no-resolve-env` to upload references literally. The API
preserves references and returns a warning; its `resolve_env` form field is
accepted for compatibility and ignored. `policy diff` uses the same local
resolution and supports `--no-resolve-env`; `validate` inspects the file as
written. `$NAME` without braces is always literal. Dry runs use the validation
endpoint and require only the permission to view policies.

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
| `auth_config` | object | n/a | Auth details. The CLI resolves `${ENV_VAR}` locally |

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

A tool entry replaces that tool's whole configuration, so an existing
`approval_workflow` or condition that the entry does not repeat is removed.
To add a `deny` rule to a tool that already requires approval through a
workflow, repeat the workflow and the existing condition:

```yaml
tools:
  - name: Bash
    source: builtin
    approval_workflow: high-risk          # existing: keep it
    conditions:
      - expression: "args.command == 'deploy'"
        action: require_approval          # existing: keep it
      - expression: "args.command == 'rm'"
        action: deny                      # new
```

`preloop policy export` prints the current entries to start from.
`preloop policy diff` and `preloop policy apply` list every workflow or
condition a file would remove (`! REMOVES ...`). On a terminal `apply` then
asks for confirmation; without a terminal it stops without applying unless
you pass `--yes` (or set `PRELOOP_CONFIRM=1`).

#### Tool Conditions

Each condition evaluates against tool arguments:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `expression` | string | n/a | Expression to evaluate |
| `action` | string | `require_approval` | `allow`, `deny`, or `require_approval` |
| `condition_type` | string | `simple` | `simple` or `cel`, both evaluated in every edition |
| `description` | string | n/a | Human-readable description |

Tool conditions do not accept `redact`. A `sensitive_data` rule does:
`notify`, `deny`, `require_approval` or `redact`, with optional
`redact_upstream`. See [Redaction](../../security/redaction.md).

### `defaults`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `unknown_tools` | string | `allow` | Unmatched MCP calls: `allow`, `deny`, or `require_approval` |
| `require_approval_for_new_tools` | bool | `false` | Require approval once per account and MCP tool route |
| `default_approval_workflow` | string | n/a | Workflow name, required for approval defaults or approve-once |
| `inherit_from_parent` | bool | `true` | Not used; `apply` accepts any value and ignores it |

`validate` and `apply` resolve workflow names against the file and your account.
A missing workflow is an error, including with `--dry-run`. Scoped defaults
are rejected: only the account policy supplies defaults.

**Native tool rules in unattended flow runs.** Rules on native agent tools
(`Bash`, `Write`, ...) also govern flow executions. A flow run on a private
runner host profile pauses on a command that needs approval and resumes or
fails closed (`approval_denied`, `approval_timeout`) on the decision; see the
[runner guide](../runners/quickstart-linux.md#codex-opencode-and-deepseek-native-profiles).
Hosted Codex installs the same Preloop `PreToolUse` hook in its container,
so its native commands are checked against these rules too: deny rules block
and approval-required rules pause the run. A hosted Codex run that has such a
rule but cannot install the hook fails before Codex starts with
`approval_unavailable`. Other hosted harnesses have no native command hook, so
such a run is refused before launch (`approval_unavailable`) rather than run
without the rule when
any enabled native tool configuration (including agent-pinned ones) has a rule
requiring approval, or when such a rule is scoped to the flow or to an employee
flow's target agent.

### Unlisted tools

An MCP tool is unlisted when no explicit rule matches the call, including a
configured tool with no matching condition. Its account policy controls it:

```yaml
defaults:
  unknown_tools: deny
```

| Precedence | Outcome |
|------------|---------|
| Explicit tool or subject rule matches | Rule action |
| Approve-once enabled and no rule matches | Registry pending: approval; approved: allow; denied: deny |
| No rule matches and approve-once is disabled | `defaults.unknown_tools` |
| Native agent tool (Bash, Edit, Read) | Existing rule and client-decision fallback |

To ask a human for every unmatched MCP call, use `unknown_tools: require_approval`
and `default_approval_workflow: Review`, where `Review` is a workflow defined in
the file or account. Decisions are audited with `source: unknown_tools_default`.
Workflow lookup or evaluation errors block execution. An async approved call is
checked again before replay; a new deny blocks it.

Omitting the block preserves stored defaults. An account with no stored block
loads `unknown_tools: allow` and `require_approval_for_new_tools: false`, keeping
historical behavior. Exports and snapshots include defaults; rollback restores
them. `inherit_from_parent` is accepted and unread. Defaults are account-wide;
there are no scoped defaults in this version.

Tools are identified by the selected MCP server and exposed name. A prefix is
part of that name; a same-name tool on another server cannot lend its rules.
Claude Code hook names `mcp__<server>__<tool>` also count as MCP calls. Their
server label is a distinct route from a registered Preloop proxy server.

A server-scoped `sensitive_data` CEL allowlist remains useful when only one
server should be restricted, or a tool delegates to other tools by name. See the
[read-only production access example](../examples/production-access/README.md#why-the-policy-is-an-allowlist).

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

# Apply without the confirmation for removed workflows or conditions
preloop policy apply my-policy.yaml --yes

# Keep variable references literal
preloop policy apply my-policy.yaml --no-resolve-env

# Recursively apply all policies in a directory
preloop policy apply policies/ -r

# Export current config as YAML (stdout, or -o file)
preloop policy export -o my-policy.yaml

# List policy versions, newest first
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
Every successful `preloop policy apply` (and `POST /api/v1/policies/upload`
without `dry_run`) records the resulting configuration as a new, active
version; `apply` prints its number and `preloop policy list` shows it.
A dry run records nothing.

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
2. **Valid references**: Tools, `model_io` rules and escalation settings that reference approval workflows or MCP servers must point to a name defined in the file or already configured in your account
3. **AI policy completeness**: AI-driven workflows require `ai_model` (`ai_guidelines` is optional)
4. **Expression syntax**: Tool condition expressions cannot be empty
5. **Default references**: `default_approval_workflow` must reference a workflow defined in the file or in your account (see [`defaults`](#defaults))

If validation fails, you get detailed error messages pointing to the exact issue:

```json
{
  "is_valid": false,
  "errors": [
    {
      "path": "$.tools[0].approval_workflow",
      "message": "Tool 'bash' references approval workflow 'nonexistent' which is not defined. Either add the workflow to your policy file under 'approval_workflows', or configure it in the console first.",
      "value": "nonexistent"
    }
  ],
  "warnings": ["Available approval workflows: [my-workflow]"]
}
```

### Approve a tool once

```yaml
defaults:
  unknown_tools: deny
  require_approval_for_new_tools: true
  default_approval_workflow: Review
```

Define `Review` in `approval_workflows` or configure it in the account. The first
unmatched MCP call records a pending entry and one approval request in the same
transaction. Concurrent calls reuse that request. Approving it authorizes this
route for future unmatched calls; denying it refuses them. An expired or
cancelled request leaves the entry pending, and the next call raises a fresh
request. Policy changes do not erase decisions; disabling approve-once skips the
registry. Explicit matching rules take precedence over registry decisions.

The first observer sets the shared request's approval window using its flow,
workflow and account cap. Later observers reuse that request and its expiry.
After approval, each parked run retries its own original call under current
policy. A refusal or expiry ends every run parked on that request; arguments
from the first caller are not copied into another run's resume instructions.

In **Policies → Tools seen**, filter pending/approved/denied tools and Approve or
Deny a route. Deny also revokes a prior approval. A manual decision settles the
route's open approval with the same outcome. Viewing requires `view_policies`;
deciding requires `manage_policies`. The API is `GET /api/v1/tools/registry`
(with `state`, `page`, `page_size`) and
`POST /api/v1/tools/registry/{id}/decision` with `state: approved` or `denied`.

Entries are isolated by account, selected MCP server and exposed tool name. A
hook-observed server label with no Preloop server ID is a separate route.
Changing a server prefix changes the exposed name and requires a new approval.
The registry records first/last seen, count, first observed agent when known,
and who decided. It authorizes a tool route, regardless of later argument
values; use explicit conditional rules for argument-specific restrictions.
